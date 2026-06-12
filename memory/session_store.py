"""
Tier 2 — SessionStore: Redis-backed session persistence.

Redis key layout:
  session:{sid}:messages  → List of JSON-encoded message dicts (RPUSH/LRANGE)
  session:{sid}:meta      → Hash  { name, created_at, summarized }
  sessions:index          → Sorted Set  { member=sid, score=unix_ts }

Falls back to an in-memory dict if Redis is unavailable (dev/test mode).
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


# ── In-memory fallback ────────────────────────────────────────────

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
            log.warning("SessionStore: Redis unavailable (%s) — falling back to in-memory dict", exc)
            self._backend = _DictBackend()

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
