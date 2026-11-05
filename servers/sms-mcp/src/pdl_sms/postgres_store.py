"""Postgres-backed Store (schema ``sms``), read-only. One short-lived connection per call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg

from pdl_sms.models import Message

SCHEMA = "sms"
_COLUMNS = (
    "id, device, provider_id, thread_id, address, contact, direction, body, sent_at, received_at"
)
_FULLTEXT_MIN_CHARS = 3
_ILIKE_FIELDS = ("body", "contact", "address")
_CONTACT_FIELDS = ("contact", "address")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _message(row: Sequence[Any]) -> Message:
    return Message(
        id=row[0],
        device=row[1],
        provider_id=row[2],
        thread_id=row[3],
        address=row[4],
        contact=row[5],
        direction=row[6],
        body=row[7],
        sent_at=row[8].astimezone(UTC),
        received_at=row[9].astimezone(UTC),
    )


def _ilike(fields: Sequence[str]) -> str:
    # ESCAPE is explicit so the helper's backslash escaping holds regardless of server settings.
    return " OR ".join(f"{f} ILIKE %s ESCAPE '\\'" for f in fields)


def _search_clause(query: str) -> tuple[str, list[Any]]:
    """WHERE fragment for a non-blank query: full text OR ILIKE (ILIKE only when short)."""
    # The OR'd ILIKE terms stop the planner using the GIN index on tsv, so every search scans the
    # table; acceptable at single-phone scale.
    params: list[Any] = [_like_pattern(query)] * len(_ILIKE_FIELDS)
    if len(query) >= _FULLTEXT_MIN_CHARS:
        return (
            f"(tsv @@ plainto_tsquery('simple', %s) OR {_ilike(_ILIKE_FIELDS)})",
            [query, *params],
        )
    return f"({_ilike(_ILIKE_FIELDS)})", params


def _filters(
    query: str | None, since: datetime | None, contact: str | None
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if query and query.strip():
        clause, clause_params = _search_clause(query.strip())
        where.append(clause)
        params += clause_params
    if contact and contact.strip():
        where.append(f"({_ilike(_CONTACT_FIELDS)})")
        params += [_like_pattern(contact.strip())] * len(_CONTACT_FIELDS)
    if since is not None:
        where.append("sent_at >= %s")
        params.append(_aware(since))
    return where, params


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PostgresStore:
        from pdl_sms.store_factory import dsn_from_env

        return cls(dsn_from_env(env))

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def search(
        self, query: str | None, since: datetime | None, contact: str | None, limit: int
    ) -> list[Message]:
        where, params = _filters(query, since, contact)
        stmt = f"SELECT {_COLUMNS} FROM {SCHEMA}.messages"
        if where:
            stmt += " WHERE " + " AND ".join(where)
        stmt += " ORDER BY sent_at DESC, id ASC LIMIT %s"
        with self._connect() as conn:
            rows = conn.execute(stmt, [*params, max(1, limit)]).fetchall()  # type: ignore[call-overload]
        return [_message(r) for r in rows]

    def recent(self, since: datetime | None, limit: int) -> list[Message]:
        return self.search(None, since, None, limit)
