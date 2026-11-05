"""Shared fixtures: stores for the contract cases; Postgres only when TEST_PG_DSN is set."""

import os
from pathlib import Path

import pytest

from pdl_sms_store.memory_store import MemoryStore

PG_DSN_ENV = "TEST_PG_DSN"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get(PG_DSN_ENV)
    if not dsn:
        pytest.skip(f"{PG_DSN_ENV} not set")
    import psycopg

    from pdl_sms_store.migrations import apply_migrations

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS sms CASCADE")
        apply_migrations(conn, MIGRATIONS)
    return dsn


@pytest.fixture
def pg_store(pg_dsn):
    import psycopg

    from pdl_sms_store.postgres_store import PostgresStore

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE sms.messages, sms.devices")
    return PostgresStore(pg_dsn)


@pytest.fixture(params=["memory", "postgres"])
def empty_store(request):
    if request.param == "memory":
        return MemoryStore()
    return request.getfixturevalue("pg_store")
