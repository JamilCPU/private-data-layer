"""Postgres-backed Store (schema ``sms``). One short-lived connection per call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import psycopg

from pdl_sms_store.models import Message

SCHEMA = "sms"
_INSERT = (
    f"INSERT INTO {SCHEMA}.messages (id, device, provider_id, thread_id, address, contact, "
    "direction, body, sent_at, received_at) "
    "SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], "
    "%s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::timestamptz[]) "
    "ON CONFLICT (id) DO NOTHING"
)
_DEVICE_UPSERT = (
    f"INSERT INTO {SCHEMA}.devices (name, last_seen_at, last_provider_id) VALUES (%s, %s, %s) "
    "ON CONFLICT (name) DO UPDATE SET "
    f"last_seen_at = GREATEST({SCHEMA}.devices.last_seen_at, EXCLUDED.last_seen_at), "
    "last_provider_id = EXCLUDED.last_provider_id"
)


def _columns(msgs: Sequence[Message]) -> list[list[object]]:
    return [
        [m.id for m in msgs],
        [m.device for m in msgs],
        [m.provider_id for m in msgs],
        [m.thread_id for m in msgs],
        [m.address for m in msgs],
        [m.contact for m in msgs],
        [m.direction for m in msgs],
        [m.body for m in msgs],
        [m.sent_at for m in msgs],
        [m.received_at for m in msgs],
    ]


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PostgresStore:
        from pdl_sms_store.store_factory import dsn_from_env

        return cls(dsn_from_env(env))

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def upsert_messages(self, msgs: Sequence[Message]) -> int:
        if not msgs:
            return 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_INSERT, _columns(msgs))  # type: ignore[arg-type]
            return cur.rowcount

    def record_device_seen(self, device: str, at: datetime, last_provider_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(_DEVICE_UPSERT, (device, at, last_provider_id))

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(f"SELECT count(*) FROM {SCHEMA}.messages").fetchone()
        return int(row[0]) if row else 0

    def device_seen(self, device: str) -> tuple[datetime, str | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT last_seen_at, last_provider_id FROM {SCHEMA}.devices WHERE name = %s",
                (device,),
            ).fetchone()
        return (row[0], row[1]) if row else None
