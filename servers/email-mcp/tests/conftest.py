"""Shared fixtures: a MemoryStore seeded with synthetic messages."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdl_email.memory_store import MemoryStore
from pdl_email.models import Message

LONG_BODY = "x" * 5000


def _msg(n: int, day: int, subject: str, **kw) -> Message:
    fields = {
        "id": f"id{n}",
        "uid": n,
        "uidvalidity": 1,
        "mailbox": "INBOX",
        "from_addr": f"sender{n}@example.test",
        "from_name": None,
        "to_addrs": ("me@example.test",),
        "subject": subject,
        "sent_at": datetime(2026, 10, day, 9, 0, tzinfo=UTC),
        "snippet": f"snippet {n}",
        "body_text": f"body {n}",
        "has_attachments": False,
        "message_id_header": None,
    }
    fields.update(kw)
    return Message(**fields)


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    s.upsert_messages(
        [
            _msg(
                1,
                1,
                "Rent reminder",
                from_addr="landlord@example.test",
                from_name="Pat Landlord",
                snippet="Rent is due on the 5th",
                body_text="Please pay the rent by the 5th.",
            ),
            _msg(2, 5, "Long newsletter", body_text=LONG_BODY),
            _msg(3, 10, "Parcel shipped", body_text="Your parcel is on its way."),
            _msg(4, 15, "Exactly at cap", body_text="y" * 4000),
            _msg(5, 20, "Meeting notes", to_addrs=("me@example.test", "you@example.test")),
        ]
    )
    return s


PG_DSN_ENV = "TEST_PG_DSN"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get(PG_DSN_ENV)
    if not dsn:
        pytest.skip(f"{PG_DSN_ENV} not set")
    import psycopg

    from pdl_email.migrations import apply_migrations

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS email CASCADE")
        apply_migrations(conn, MIGRATIONS)
    return dsn


@pytest.fixture
def pg_store(pg_dsn):
    import psycopg

    from pdl_email.postgres_store import PostgresStore

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE email.messages, email.sync_state")
    return PostgresStore(pg_dsn)


@pytest.fixture(params=["memory", "postgres"])
def empty_store(request):
    if request.param == "memory":
        return MemoryStore()
    return request.getfixturevalue("pg_store")
