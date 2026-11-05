"""Shared fixtures: a seeded MemoryStore and an optional Postgres store."""

import os
from datetime import UTC, datetime

import pytest

from pdl_sms.memory_store import MemoryStore
from pdl_sms.models import Message

PG_DSN_ENV = "TEST_PG_DSN"

# Mirrors servers/sms-store/migrations/001_sms.sql (columns, tsv expression); this package only reads it.
PG_DDL = """
DROP SCHEMA IF EXISTS sms CASCADE;
CREATE SCHEMA sms;
CREATE TABLE sms.messages (
    id text PRIMARY KEY,
    device text NOT NULL,
    provider_id text NOT NULL,
    thread_id text,
    address text NOT NULL,
    contact text,
    direction text NOT NULL CHECK (direction IN ('in', 'out')),
    body text NOT NULL,
    sent_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(contact, '') || ' '
            || regexp_replace(coalesce(address, ''), '[^[:alnum:]]+', ' ', 'g') || ' '
            || coalesce(body, '')
        )
    ) STORED
);
CREATE INDEX messages_tsv_idx ON sms.messages USING gin (tsv);
CREATE INDEX messages_sent_at_idx ON sms.messages (sent_at);
"""
PG_INSERT = (
    "INSERT INTO sms.messages (id, device, provider_id, thread_id, address, contact, direction, "
    "body, sent_at, received_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def make_message(n: int, day: int = 1, **kw) -> Message:
    fields = {
        "id": f"id{n}",
        "device": "phone1",
        "provider_id": f"p{n}",
        "thread_id": "t1",
        "address": f"+1555010{n}",
        "contact": None,
        "direction": "in",
        "body": f"body {n}",
        "sent_at": datetime(2026, 10, day, 9, 0, tzinfo=UTC),
        "received_at": datetime(2026, 10, day, 9, 0, 5, tzinfo=UTC),
    }
    fields.update(kw)
    return Message(**fields)


def seed_messages() -> list[Message]:
    return [
        make_message(
            1, 1, contact="Pat Neighbour", body="Is the car still for sale?", address="+15550100"
        ),
        make_message(2, 5, contact=None, body="y" * 800, address="+15550200"),
        make_message(3, 10, contact="Sam Plumber", body="Boiler fixed", direction="out"),
        make_message(4, 15, contact="Pat Neighbour", body="See you at 6", address="+15550100"),
        make_message(5, 20, contact="Mum", body="Call me", address="+15550500"),
    ]


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(seed_messages())


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get(PG_DSN_ENV)
    if not dsn:
        pytest.skip(f"{PG_DSN_ENV} not set")
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(PG_DDL)  # type: ignore[arg-type]
    return dsn


@pytest.fixture
def pg_store(pg_dsn):
    import psycopg

    from pdl_sms.postgres_store import PostgresStore

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE sms.messages")
    return _SeedingPg(PostgresStore(pg_dsn), pg_dsn)


class _SeedingPg:
    """PostgresStore plus ``add`` (inserts as the sms-store writer would)."""

    def __init__(self, inner, dsn: str) -> None:
        self._inner, self._dsn = inner, dsn

    def add(self, msgs) -> None:
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            for m in msgs:
                conn.execute(
                    PG_INSERT,
                    (m.id, m.device, m.provider_id, m.thread_id, m.address, m.contact,
                     m.direction, m.body, m.sent_at, m.received_at),
                )  # fmt: skip

    def search(self, *args, **kwargs):
        return self._inner.search(*args, **kwargs)

    def recent(self, *args, **kwargs):
        return self._inner.recent(*args, **kwargs)


@pytest.fixture(params=["memory", "postgres"])
def empty_store(request):
    if request.param == "memory":
        return MemoryStore()
    return request.getfixturevalue("pg_store")
