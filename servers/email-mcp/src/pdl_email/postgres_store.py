"""Postgres-backed Store (schema ``email``). One short-lived connection per call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg

from pdl_email.models import Message
from pdl_email.store import SyncState

SCHEMA = "email"
_COLUMNS = (
    "id, uid, uidvalidity, mailbox, from_addr, from_name, to_addrs, subject, sent_at, "
    "snippet, body_text, has_attachments, message_id_header"
)
_INSERT = (
    f"INSERT INTO {SCHEMA}.messages ({_COLUMNS}) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (id) DO NOTHING"
)
_FULLTEXT_MIN_CHARS = 3
_ILIKE_FIELDS = ("subject", "from_addr", "from_name", "body_text")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _params(m: Message) -> tuple[Any, ...]:
    return (
        m.id,
        m.uid,
        m.uidvalidity,
        m.mailbox,
        m.from_addr,
        m.from_name,
        list(m.to_addrs),
        m.subject,
        m.sent_at,
        m.snippet,
        m.body_text,
        m.has_attachments,
        m.message_id_header,
    )


def _message(row: Sequence[Any]) -> Message:
    return Message(
        id=row[0],
        uid=row[1],
        uidvalidity=row[2],
        mailbox=row[3],
        from_addr=row[4],
        from_name=row[5],
        to_addrs=tuple(row[6]),
        subject=row[7],
        sent_at=row[8].astimezone(UTC),
        snippet=row[9],
        body_text=row[10],
        has_attachments=row[11],
        message_id_header=row[12],
    )


def _search_clause(query: str) -> tuple[str, list[Any]]:
    """WHERE fragment for a non-blank query: full text OR ILIKE (ILIKE only when short)."""
    ilike = " OR ".join(f"{f} ILIKE %s" for f in _ILIKE_FIELDS)
    params: list[Any] = [_like_pattern(query)] * len(_ILIKE_FIELDS)
    if len(query) >= _FULLTEXT_MIN_CHARS:
        return f"(tsv @@ plainto_tsquery('simple', %s) OR {ilike})", [query, *params]
    return f"({ilike})", params


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PostgresStore:
        from pdl_email.store_factory import dsn_from_env

        return cls(dsn_from_env(env))

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def upsert_messages(self, msgs: Sequence[Message]) -> int:
        new = 0
        with self._connect() as conn, conn.cursor() as cur:
            for msg in msgs:
                cur.execute(_INSERT, _params(msg))
                new += cur.rowcount
        return new

    def get(self, id: str) -> Message | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {SCHEMA}.messages WHERE id = %s", (id,)
            ).fetchone()
        return _message(row) if row else None

    def search(self, query: str | None, since: datetime | None, limit: int) -> list[Message]:
        where: list[str] = []
        params: list[Any] = []
        needle = (query or "").strip()
        if needle:
            clause, clause_params = _search_clause(needle)
            where.append(clause)
            params += clause_params
        if since is not None:
            where.append("sent_at >= %s")
            params.append(_aware(since))
        stmt = f"SELECT {_COLUMNS} FROM {SCHEMA}.messages"
        if where:
            stmt += " WHERE " + " AND ".join(where)
        stmt += " ORDER BY sent_at DESC, id ASC LIMIT %s"
        with self._connect() as conn:
            rows = conn.execute(stmt, [*params, max(1, limit)]).fetchall()  # type: ignore[call-overload]
        return [_message(r) for r in rows]

    def recent(self, since: datetime | None, limit: int) -> list[Message]:
        return self.search(None, since, limit)

    def sync_state(self, mailbox: str) -> SyncState | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT uidvalidity, last_uid FROM {SCHEMA}.sync_state WHERE mailbox = %s",
                (mailbox,),
            ).fetchone()
        return SyncState(row[0], row[1]) if row else None

    def set_sync_state(self, mailbox: str, state: SyncState) -> None:
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {SCHEMA}.sync_state (mailbox, uidvalidity, last_uid) "
                "VALUES (%s, %s, %s) ON CONFLICT (mailbox) DO UPDATE "
                "SET uidvalidity = EXCLUDED.uidvalidity, last_uid = EXCLUDED.last_uid",
                (mailbox, state.uidvalidity, state.last_uid),
            )
