"""SQLite-backed conversation memory, preferences and login sessions."""

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Conversation:
    id: int
    created_at: str
    title: str | None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    conversation_id: int
    role: str
    content: str
    tool_name: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryStore:
    """Thread-safe SQLite store (WAL) for conversations, preferences and sessions."""

    def __init__(self, path: str | Path) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.lastrowid or 0

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def new_conversation(self, title: str | None = None) -> int:
        return self._write(
            "INSERT INTO conversations (created_at, title) VALUES (?, ?)", (_now(), title)
        )

    def add_message(
        self, conversation_id: int, role: str, content: str, tool_name: str | None = None
    ) -> int:
        return self._write(
            "INSERT INTO messages (conversation_id, role, content, tool_name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, tool_name, _now()),
        )

    def recent_messages(self, conversation_id: int, limit: int = 20) -> list[StoredMessage]:
        """Return the last ``limit`` messages of a conversation, oldest first."""
        rows = self._read(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        )
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    def recent_turns(
        self, conversation_id: int, limit: int, skip_assistant: tuple[str, ...] = ()
    ) -> list[StoredMessage]:
        """The last ``limit`` user and final assistant texts, oldest first.

        Rows with a ``tool_name`` (tool results, tool-call records) are left out, as are
        assistant rows whose content is one of ``skip_assistant``.
        """
        marks = ", ".join("?" * len(skip_assistant))
        rows = self._read(
            "SELECT * FROM messages WHERE conversation_id = ? AND tool_name IS NULL"
            f" AND (role = 'user' OR (role = 'assistant' AND content NOT IN ({marks})))"
            " ORDER BY id DESC LIMIT ?",
            (conversation_id, *skip_assistant, limit),
        )
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    def list_conversations(self, limit: int = 50) -> list[Conversation]:
        """Return conversations, newest first."""
        rows = self._read("SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,))
        return [Conversation(**dict(row)) for row in rows]

    def get_conversation(self, conversation_id: int) -> Conversation | None:
        rows = self._read("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return Conversation(**dict(rows[0])) if rows else None

    def set_preference(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, value, _now()),
        )

    def get_preference(self, key: str) -> str | None:
        rows = self._read("SELECT value FROM preferences WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(32)
        self._write("INSERT INTO sessions (id, created_at) VALUES (?, ?)", (session_id, _now()))
        return session_id

    def revoke_session(self, session_id: str) -> None:
        self._write("UPDATE sessions SET revoked = 1 WHERE id = ?", (session_id,))

    def session_valid(self, session_id: str) -> bool:
        rows = self._read("SELECT revoked FROM sessions WHERE id = ?", (session_id,))
        return bool(rows) and not rows[0]["revoked"]
