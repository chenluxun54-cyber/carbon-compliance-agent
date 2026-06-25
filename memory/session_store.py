"""
Tier 2 — SessionStore: Redis-backed session persistence.

Redis key layout:
  session:{sid}:messages  → List of JSON-encoded message dicts (RPUSH/LRANGE)
  session:{sid}:meta      → Hash  { name, created_at, summarized }
  sessions:index          → Sorted Set  { member=sid, score=unix_ts }

Falls back to SQLite (memory/sessions.db) if Redis is unavailable so sessions
survive server restarts without Redis.
"""

import json
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Redis backend ─────────────────────────────────────────────────

class _RedisBackend:
    def __init__(self, url: str):
        import redis
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._r.ping()  # raises if unreachable

    # ── session lifecycle ──────────────────────────────────────────

    def create(self, sid: str) -> None:
        self._r.hset(f"session:{sid}:meta", mapping={
            "name": "",
            "created_at": _now_iso(),
            "summarized": "0",
        })
        self._r.zadd("sessions:index", {sid: _now_ts()})

    def exists(self, sid: str) -> bool:
        return bool(self._r.exists(f"session:{sid}:meta"))

    def delete(self, sid: str) -> None:
        self._r.delete(f"session:{sid}:messages", f"session:{sid}:meta")
        self._r.zrem("sessions:index", sid)

    def list(self, limit: int = 50) -> list[dict]:
        sids = self._r.zrevrangebyscore("sessions:index", "+inf", "-inf", start=0, num=limit)
        result = []
        for sid in sids:
            meta = self._r.hgetall(f"session:{sid}:meta")
            if meta:
                result.append({
                    "session_id":  sid,
                    "name":        meta.get("name", ""),
                    "created_at":  meta.get("created_at", ""),
                    "summarized":  meta.get("summarized", "0") == "1",
                })
        return result

    # ── messages ──────────────────────────────────────────────────

    def save_messages(self, sid: str, messages: list) -> None:
        """Replace the full message list for this session."""
        key = f"session:{sid}:messages"
        pipe = self._r.pipeline()
        pipe.delete(key)
        for msg in messages:
            pipe.rpush(key, json.dumps(msg, ensure_ascii=False))
        pipe.execute()
        self._r.zadd("sessions:index", {sid: _now_ts()})

    def load_messages(self, sid: str) -> list:
        raw = self._r.lrange(f"session:{sid}:messages", 0, -1)
        return [json.loads(r) for r in raw]

    def load_display(self, sid: str) -> list:
        """Return only plain user/assistant text turns (no tool calls/results)."""
        msgs = self.load_messages(sid)
        out = []
        for m in msgs:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                out.append({"role": role, "text": content})
        return out

    # ── metadata ──────────────────────────────────────────────────

    def get_name(self, sid: str) -> str:
        return self._r.hget(f"session:{sid}:meta", "name") or ""

    def set_name(self, sid: str, name: str) -> None:
        self._r.hset(f"session:{sid}:meta", "name", name)

    def mark_summarized(self, sid: str) -> None:
        self._r.hset(f"session:{sid}:meta", "summarized", "1")

    def is_summarized(self, sid: str) -> bool:
        val = self._r.hget(f"session:{sid}:meta", "summarized")
        return val == "1"


# ── SQLite fallback ───────────────────────────────────────────────

class _SQLiteBackend:
    """Persists sessions to SQLite so they survive server restarts without Redis."""

    def __init__(self):
        import sqlite3 as _sqlite3
        from pathlib import Path
        self._db_path = str(Path(__file__).parent / "sessions.db")
        self._sqlite3 = _sqlite3
        self._init_db()

    def _connect(self):
        conn = self._sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = self._sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id  TEXT PRIMARY KEY,
                    name        TEXT DEFAULT '',
                    created_at  TEXT NOT NULL,
                    last_active TEXT NOT NULL,
                    summarized  INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
            """)

    def create(self, sid: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(session_id, name, created_at, last_active) VALUES(?,?,?,?)",
                (sid, '', now, now),
            )

    def exists(self, sid: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        return row is not None

    def delete(self, sid: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))

    def list(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT s.session_id, s.name, s.created_at, s.last_active,
                       (SELECT content FROM messages m
                        WHERE m.session_id = s.session_id AND m.role = 'user'
                        ORDER BY m.id LIMIT 1) AS first_user_content
                FROM sessions s
                ORDER BY s.last_active DESC
                LIMIT ?
            """, (limit,)).fetchall()
        result = []
        for r in rows:
            preview = ''
            if r['first_user_content']:
                try:
                    text = json.loads(r['first_user_content'])
                    if isinstance(text, str):
                        preview = text[:50]
                except Exception:
                    pass
            result.append({
                'session_id': r['session_id'],
                'name':       r['name'] or '',
                'created_at': r['created_at'],
                'last_active': r['last_active'],
                'preview':    preview,
            })
        return result

    def save_messages(self, sid: str, messages: list) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            for m in messages:
                conn.execute(
                    "INSERT INTO messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
                    (sid, m['role'], json.dumps(m['content'], ensure_ascii=False), now),
                )
            conn.execute("UPDATE sessions SET last_active = ? WHERE session_id = ?", (now, sid))

    def load_messages(self, sid: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
                (sid,),
            ).fetchall()
        return [{'role': r['role'], 'content': json.loads(r['content'])} for r in rows]

    def load_display(self, sid: str) -> list:
        msgs = self.load_messages(sid)
        out = []
        for m in msgs:
            role    = m.get('role')
            content = m.get('content')
            if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
                text = content.strip()
                if not text.startswith('[系统上下文]') and not text.startswith('[calc_auto_continue]'):
                    out.append({'role': role, 'text': content})
        return out

    def get_name(self, sid: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT name FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        return (row['name'] or '') if row else ''

    def set_name(self, sid: str, name: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET name = ? WHERE session_id = ?", (name, sid))

    def mark_summarized(self, sid: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET summarized = 1 WHERE session_id = ?", (sid,))

    def is_summarized(self, sid: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT summarized FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        return bool(row['summarized']) if row else False


# ── In-memory fallback (kept for tests/CI only) ───────────────────

class _DictBackend:
    """Used when Redis is unavailable. Sessions lost on restart."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}  # sid → {messages, name, created_at, summarized}

    def _get(self, sid: str) -> dict:
        return self._sessions.setdefault(sid, {
            "messages": [], "name": "", "created_at": _now_iso(), "summarized": False
        })

    def create(self, sid: str) -> None:
        self._sessions[sid] = {"messages": [], "name": "", "created_at": _now_iso(), "summarized": False}

    def exists(self, sid: str) -> bool:
        return sid in self._sessions

    def delete(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def list(self, limit: int = 50) -> list[dict]:
        return [
            {"session_id": sid, "name": v["name"], "created_at": v["created_at"], "summarized": v["summarized"]}
            for sid, v in list(self._sessions.items())[:limit]
        ]

    def save_messages(self, sid: str, messages: list) -> None:
        self._get(sid)["messages"] = list(messages)

    def load_messages(self, sid: str) -> list:
        return list(self._get(sid)["messages"])

    def load_display(self, sid: str) -> list:
        out = []
        for m in self._get(sid)["messages"]:
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                out.append({"role": m["role"], "text": m["content"]})
        return out

    def get_name(self, sid: str) -> str:
        return self._get(sid)["name"]

    def set_name(self, sid: str, name: str) -> None:
        self._get(sid)["name"] = name

    def mark_summarized(self, sid: str) -> None:
        self._get(sid)["summarized"] = True

    def is_summarized(self, sid: str) -> bool:
        return self._get(sid)["summarized"]


# ── Public facade ─────────────────────────────────────────────────

class SessionStore:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        try:
            self._backend = _RedisBackend(redis_url)
            log.info("SessionStore: connected to Redis at %s", redis_url)
        except Exception as exc:
            log.warning("SessionStore: Redis unavailable (%s) — falling back to SQLite", exc)
            self._backend = _SQLiteBackend()

    # Delegate everything to backend
    def create(self, sid: str)                          -> None:   self._backend.create(sid)
    def exists(self, sid: str)                          -> bool:   return self._backend.exists(sid)
    def delete(self, sid: str)                          -> None:   self._backend.delete(sid)
    def list(self, limit: int = 50)                     -> list:   return self._backend.list(limit)
    def save_messages(self, sid: str, messages: list)   -> None:   self._backend.save_messages(sid, messages)
    def load_messages(self, sid: str)                   -> list:   return self._backend.load_messages(sid)
    def load_display(self, sid: str)                    -> list:   return self._backend.load_display(sid)
    def get_name(self, sid: str)                        -> str:    return self._backend.get_name(sid)
    def set_name(self, sid: str, name: str)             -> None:   self._backend.set_name(sid, name)
    def mark_summarized(self, sid: str)                 -> None:   self._backend.mark_summarized(sid)
    def is_summarized(self, sid: str)                   -> bool:   return self._backend.is_summarized(sid)
