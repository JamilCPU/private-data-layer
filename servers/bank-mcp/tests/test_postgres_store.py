"""Shared Store behaviour cases run against a real Postgres (needs TEST_PG_DSN)."""

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from pdl_bank.migrations import MIGRATIONS_DIR, apply_migrations
from pdl_bank.postgres_store import PostgresStore
from pdl_bank.store import Store
from tests.store_cases import *
from tests.store_cases import seed, seed_accounts

DSN = os.environ.get("TEST_PG_DSN", "")

pytestmark = pytest.mark.skipif(not DSN, reason="TEST_PG_DSN not set")


@pytest.fixture(scope="session")
def migrated() -> Iterator[None]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS bank CASCADE")
        apply_migrations(conn, MIGRATIONS_DIR)
    yield


@pytest.fixture
def pg(migrated: None) -> Iterator[PostgresStore]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE bank.transactions, bank.imports, bank.accounts CASCADE")
    yield PostgresStore(DSN)


@pytest.fixture
def empty_store(pg: PostgresStore) -> Store:
    return seed_accounts(pg)


@pytest.fixture
def store(pg: PostgresStore) -> Store:
    return seed(pg)


def test_satisfies_protocol(pg: PostgresStore) -> None:
    store: Store = pg
    assert store.accounts() == []


def test_migrations_idempotent(migrated: None) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        assert apply_migrations(conn, MIGRATIONS_DIR) == []
        rows = conn.execute("SELECT name FROM bank.schema_migrations").fetchall()
    assert rows == [("001_bank.sql",)]


def test_migration_failure_rolls_back_and_is_not_recorded(migrated: None, tmp_path: Path) -> None:
    (tmp_path / "001_bad.sql").write_text(
        "CREATE TABLE bank.tmp_ok (id int);\nSELECT * FROM bank.does_not_exist;\n"
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        try:
            with pytest.raises(psycopg.Error):
                apply_migrations(conn, tmp_path)
            assert conn.execute("SELECT to_regclass('bank.tmp_ok')").fetchone() == (None,)
            names = [r[0] for r in conn.execute("SELECT name FROM bank.schema_migrations")]
            assert "001_bad.sql" not in names
        finally:
            conn.execute("DROP TABLE IF EXISTS bank.tmp_ok")


def test_apply_migrations_creates_missing_schema(migrated: None, tmp_path: Path) -> None:
    (tmp_path / "001_x.sql").write_text("CREATE TABLE bank_scratch.t (id int);")
    with psycopg.connect(DSN, autocommit=True) as conn:
        try:
            assert apply_migrations(conn, tmp_path, schema="bank_scratch") == ["001_x.sql"]
            assert apply_migrations(conn, tmp_path, schema="bank_scratch") == []
        finally:
            conn.execute("DROP SCHEMA IF EXISTS bank_scratch CASCADE")


def test_import_ledger_upsert(pg: PostgresStore) -> None:
    from datetime import date

    pg.record_import("h", "a.csv", date(2026, 1, 1), 1)
    pg.record_import("h", "b.csv", date(2026, 1, 2), 2)
    assert pg.was_imported("h")


def test_unknown_account_is_rejected(pg: PostgresStore) -> None:
    from datetime import date

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg.upsert_transactions([tx("x", date(2026, 1, 1), "-1", "d", None, "nope")])
