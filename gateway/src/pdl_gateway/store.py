"""SQLite persistence for OAuth clients, approvals, authorization codes and tokens."""

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthClientInformationFull

TokenKind = Literal["access", "refresh"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    request_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    client_name TEXT,
    params_json TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY,
    code_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    client_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    expires_at INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_client ON tokens (client_id);
"""


@dataclass(frozen=True)
class StoredToken:
    kind: TokenKind
    client_id: str
    scopes: tuple[str, ...]
    expires_at: int | None


@dataclass(frozen=True)
class PendingApproval:
    request_id: str
    client_id: str
    client_name: str | None
    params_json: str
    expires_at: float


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Thread-safe SQLite store; tokens are persisted only as SHA-256 digests."""

    def __init__(self, path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _fetch_one(self, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # --- clients ---------------------------------------------------------

    def save_client(self, info: OAuthClientInformationFull) -> None:
        self._execute(
            "INSERT OR REPLACE INTO clients (client_id, info_json) VALUES (?, ?)",
            (info.client_id, info.model_dump_json()),
        )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._fetch_one("SELECT info_json FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(str(row[0])) if row else None

    # --- pending approvals -----------------------------------------------

    def save_pending(self, p: PendingApproval) -> None:
        self._execute(
            "INSERT OR REPLACE INTO pending"
            " (request_id, client_id, client_name, params_json, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (p.request_id, p.client_id, p.client_name, p.params_json, p.expires_at),
        )

    def get_pending(self, request_id: str, now: float) -> PendingApproval | None:
        """Return the approval without consuming it; None if missing or expired."""
        row = self._fetch_one(
            "SELECT request_id, client_id, client_name, params_json, expires_at"
            " FROM pending WHERE request_id = ?",
            (request_id,),
        )
        if row is None or now > row[4]:
            return None
        return PendingApproval(*row)

    def pop_pending(self, request_id: str, now: float) -> PendingApproval | None:
        """Delete and return the approval; None if missing or expired."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT request_id, client_id, client_name, params_json, expires_at"
                " FROM pending WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            self._conn.execute("DELETE FROM pending WHERE request_id = ?", (request_id,))
        if row is None or now > row[4]:
            return None
        return PendingApproval(*row)

    # --- authorization codes ---------------------------------------------

    def save_code(self, code: AuthorizationCode) -> None:
        self._execute(
            "INSERT OR REPLACE INTO codes (code, code_json) VALUES (?, ?)",
            (code.code, code.model_dump_json()),
        )

    def get_code(self, code: str) -> AuthorizationCode | None:
        row = self._fetch_one("SELECT code_json FROM codes WHERE code = ?", (code,))
        return AuthorizationCode.model_validate_json(str(row[0])) if row else None

    def delete_code(self, code: str) -> int:
        """Delete the code; return the number of rows removed (0 if already gone)."""
        return self._execute("DELETE FROM codes WHERE code = ?", (code,)).rowcount

    # --- tokens ----------------------------------------------------------

    def save_token(
        self,
        kind: TokenKind,
        token: str,
        client_id: str,
        scopes: Sequence[str],
        expires_at: int | None,
    ) -> None:
        """Insert a token row; a hash collision raises ``sqlite3.IntegrityError``."""
        self._execute(
            "INSERT INTO tokens"
            " (hash, kind, client_id, scopes_json, expires_at, revoked, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?)",
            (_digest(token), kind, client_id, json.dumps(list(scopes)), expires_at, time.time()),
        )

    def load_token(self, kind: TokenKind, token: str, now: int) -> StoredToken | None:
        """Return the live token of ``kind``; None if unknown, revoked or expired."""
        row = self._fetch_one(
            "SELECT client_id, scopes_json, expires_at FROM tokens"
            " WHERE hash = ? AND kind = ? AND revoked = 0",
            (_digest(token), kind),
        )
        if row is None:
            return None
        client_id, scopes_json, expires_at = row
        if expires_at is not None and expires_at <= now:
            return None
        return StoredToken(
            kind=kind,
            client_id=str(client_id),
            scopes=tuple(json.loads(str(scopes_json))),
            expires_at=None if expires_at is None else int(str(expires_at)),
        )

    def revoke(self, token: str) -> int:
        """Revoke a token of any kind; return the number of rows revoked."""
        cursor = self._execute(
            "UPDATE tokens SET revoked = 1 WHERE hash = ? AND revoked = 0", (_digest(token),)
        )
        return cursor.rowcount

    def revoke_client_tokens(self, client_id: str) -> int:
        """Revoke every live token issued to ``client_id``."""
        cursor = self._execute(
            "UPDATE tokens SET revoked = 1 WHERE client_id = ? AND revoked = 0", (client_id,)
        )
        return cursor.rowcount
