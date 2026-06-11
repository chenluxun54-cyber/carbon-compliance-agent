"""
SQLite-backed session persistence for the carbon agent.
Persists full message history (for API context) and display history (for UI restore).
DB file: carbon_skill/chat_history.db (auto-created on first run).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).parent / "chat_history.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                last_active TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id);
        """)
        # Migration: add 'name' column if absent
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "name" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT DEFAULT NULL")

        # Memories table (new in memory system)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_type       TEXT NOT NULL,
                content           TEXT NOT NULL,
                source_session_id TEXT,
                created_at        TEXT NOT NULL,
                importance        INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_memories_type
                ON memories(memory_type, importance DESC, id DESC);
        """)
        # Migration: add 'summarized' column to sessions
        cols2 = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "summarized" not in cols2:
            conn.execute("ALTER TABLE sessions ADD COLUMN summarized INTEGER DEFAULT 0")


def session_exists(sid: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        return row is not None


def create_session(sid: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions(session_id, created_at, last_active) VALUES(?,?,?)",
            (sid, now, now),
        )


def save_messages(sid: str, messages: list, from_idx: int) -> None:
    """Persist messages[from_idx:] to DB and update last_active."""
    new_msgs = messages[from_idx:]
    if not new_msgs:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (sid, m["role"], json.dumps(m["content"], ensure_ascii=False), now)
        for m in new_msgs
    ]
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
            rows,
        )
        conn.execute(
            "UPDATE sessions SET last_active = ? WHERE session_id = ?",
            (now, sid),
        )


def load_api_messages(sid: str) -> list:
    """Return full message list for API continuation (JSON-decoded content)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
            (sid,),
        ).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def load_display_history(sid: str) -> list:
    """
    Return displayable turns only: plain user messages and plain assistant responses.
    Skips tool-call/tool-result messages (content is a list, not a string).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
            (sid,),
        ).fetchall()

    history = []
    for r in rows:
        role = r["role"]
        content = json.loads(r["content"])
        # Only include plain text turns (not tool call/result lists)
        if isinstance(content, str) and content.strip():
            text = content.strip()
            if not text.startswith("[系统上下文]") and not text.startswith("[calc_auto_continue]"):
                history.append({"role": role, "text": content})
    return history


def delete_session(sid: str) -> None:
    """Delete a session and all its messages."""
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))


def get_session_name(sid: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT name FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
    return row["name"] if row else None


def update_session_name(sid: str, name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET name = ? WHERE session_id = ?", (name, sid)
        )


def list_sessions(limit: int = 50) -> list:
    """Return sessions ordered by last_active DESC with preview from first real user message."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.name, s.created_at, s.last_active,
                   (SELECT content FROM messages m
                    WHERE m.session_id = s.session_id AND m.role = 'user'
                    ORDER BY m.id LIMIT 1) AS first_user_content
            FROM sessions s
            ORDER BY s.last_active DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    result = []
    for r in rows:
        preview = ""
        if r["first_user_content"]:
            try:
                text = json.loads(r["first_user_content"])
                if isinstance(text, str):
                    text = text.strip()
                    if text.startswith("[系统上下文]") or text.startswith("[calc_auto_continue]"):
                        text = ""
                    preview = text[:30] + ("…" if len(text) > 30 else "")
            except (json.JSONDecodeError, TypeError):
                pass
        result.append({
            "session_id":  r["session_id"],
            "name":        r["name"],
            "created_at":  r["created_at"],
            "last_active": r["last_active"],
            "preview":     preview,
        })
    return result


# ── Memory system ─────────────────────────────────────────────────────

def add_memory(
    memory_type: str,
    content: str,
    source_session_id: str | None = None,
    importance: int = 1,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories(memory_type, content, source_session_id, created_at, importance)"
            " VALUES(?,?,?,?,?)",
            (memory_type, content, source_session_id, now, importance),
        )
        return cur.lastrowid


def get_memories(limit: int = 40) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, memory_type, content, source_session_id, created_at, importance"
            " FROM memories ORDER BY importance DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_session_summarized(sid: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE sessions SET summarized=1 WHERE session_id=?", (sid,))


def is_session_summarized(sid: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT summarized FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
    return bool(row and row["summarized"])
