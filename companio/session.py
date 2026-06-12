"""SQLite-based session manager for persistent conversation storage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from companio.helpers import ensure_dir


@dataclass
class Session:
    """A conversation session."""

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_consolidated: int = 0
    claude_session_id: str | None = None
    claude_session_updated_at: str | None = None


class SessionManager:
    """SQLite-based session manager with atomic writes and indexed lookup."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        last_consolidated INTEGER DEFAULT 0,
        total_cost_usd REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        role TEXT NOT NULL,
        content TEXT,
        tool_calls TEXT,
        tool_call_id TEXT,
        name TEXT,
        turn_cost_usd REAL,
        total_cost_usd REAL,
        duration_ms INTEGER,
        num_turns INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_input_tokens INTEGER,
        cache_creation_input_tokens INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_messages_session
        ON messages(session_id);
    CREATE TABLE IF NOT EXISTS telegram_users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        chat_id INTEGER,
        chat_title TEXT,
        chat_type TEXT,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    _MIGRATIONS = [
        # Migration 1: Add cost columns to messages table
        "ALTER TABLE messages ADD COLUMN turn_cost_usd REAL",
        "ALTER TABLE messages ADD COLUMN total_cost_usd REAL",
        "ALTER TABLE messages ADD COLUMN duration_ms INTEGER",
        # Migration 2: Add total_cost_usd to sessions table
        "ALTER TABLE sessions ADD COLUMN total_cost_usd REAL DEFAULT 0.0",
        # Migration 3: Add num_turns to messages table
        "ALTER TABLE messages ADD COLUMN num_turns INTEGER",
        # Migration 4: Add token usage columns to messages table
        "ALTER TABLE messages ADD COLUMN input_tokens INTEGER",
        "ALTER TABLE messages ADD COLUMN output_tokens INTEGER",
        "ALTER TABLE messages ADD COLUMN cache_read_input_tokens INTEGER",
        "ALTER TABLE messages ADD COLUMN cache_creation_input_tokens INTEGER",
        # Migration 5: Add claude_session_id to sessions table
        "ALTER TABLE sessions ADD COLUMN claude_session_id TEXT",
        "ALTER TABLE sessions ADD COLUMN claude_session_updated_at TIMESTAMP",
    ]

    def __init__(self, sessions_dir: Path):
        self._db_path = ensure_dir(sessions_dir) / "companio.db"
        self._db: aiosqlite.Connection | None = None
        self._cache: dict[str, Session] = {}

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.executescript(self._SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._apply_migrations()
        await self._db.commit()

    async def _apply_migrations(self) -> None:
        """Apply schema migrations safely (ignores already-applied ALTERs)."""
        assert self._db is not None
        for sql in self._MIGRATIONS:
            try:
                await self._db.execute(sql)
            except Exception:
                pass  # Column already exists

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def get_or_create(self, session_id: str) -> Session:
        if session_id in self._cache:
            return self._cache[session_id]

        assert self._db is not None

        cursor = await self._db.execute(
            "SELECT last_consolidated, claude_session_id, claude_session_updated_at "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            await self._db.execute("INSERT INTO sessions (session_id) VALUES (?)", (session_id,))
            await self._db.commit()
            session = Session(session_id=session_id)
        else:
            last_consolidated = row[0]
            cursor = await self._db.execute(
                "SELECT role, content, tool_calls, tool_call_id, name "
                "FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
            rows = await cursor.fetchall()
            messages = []
            for r in rows:
                msg: dict[str, Any] = {"role": r[0]}
                if r[1] is not None:
                    msg["content"] = r[1]
                if r[2] is not None:
                    msg["tool_calls"] = json.loads(r[2])
                if r[3] is not None:
                    msg["tool_call_id"] = r[3]
                if r[4] is not None:
                    msg["name"] = r[4]
                messages.append(msg)
            session = Session(
                session_id=session_id,
                messages=messages,
                last_consolidated=last_consolidated,
                claude_session_id=row[1],
                claude_session_updated_at=row[2],
            )

        self._cache[session_id] = session
        return session

    async def save(self, session: Session) -> None:
        assert self._db is not None

        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session.session_id,),
        )
        (db_count,) = await cursor.fetchone()

        new_messages = session.messages[db_count:]
        for msg in new_messages:
            await self._db.execute(
                "INSERT INTO messages "
                "(session_id, role, content, tool_calls, tool_call_id, name, "
                " turn_cost_usd, total_cost_usd, duration_ms, num_turns, "
                " input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    msg.get("role"),
                    msg.get("content"),
                    json.dumps(msg["tool_calls"]) if "tool_calls" in msg else None,
                    msg.get("tool_call_id"),
                    msg.get("name"),
                    msg.get("turn_cost_usd"),
                    msg.get("total_cost_usd"),
                    msg.get("duration_ms"),
                    msg.get("num_turns"),
                    msg.get("input_tokens"),
                    msg.get("output_tokens"),
                    msg.get("cache_read_input_tokens"),
                    msg.get("cache_creation_input_tokens"),
                ),
            )

        # Update session metadata including cumulative cost
        total_cost = session.messages[-1].get("total_cost_usd") if session.messages else None
        await self._db.execute(
            "UPDATE sessions SET last_consolidated = ?, "
            "total_cost_usd = COALESCE(?, total_cost_usd), "
            "claude_session_id = ?, "
            "claude_session_updated_at = CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE claude_session_updated_at END, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ?",
            (session.last_consolidated, total_cost, session.claude_session_id, session.claude_session_id, session.session_id),
        )
        await self._db.commit()

    async def clear(self, session_id: str) -> None:
        assert self._db is not None
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute(
            "UPDATE sessions SET last_consolidated = 0, claude_session_id = NULL, "
            "claude_session_updated_at = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ?",
            (session_id,),
        )
        await self._db.commit()
        self._cache.pop(session_id, None)
