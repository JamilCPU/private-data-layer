"""dsn_from_env, store_from_env and migrations directory resolution."""

from pathlib import Path
from typing import Self

import pytest

from pdl_bank import store_factory
from pdl_bank.memory_store import MemoryStore
from pdl_bank.migrations import MIGRATIONS_DIR
from pdl_bank.postgres_store import PostgresStore
from pdl_bank.store import StoreConfigError
from pdl_bank.store_factory import dsn_from_env, store_from_env

BASE = {"PGUSER": "owner", "PGPASSWORD": "pw"}


def test_dsn_defaults() -> None:
    assert dsn_from_env(BASE) == "postgresql://owner:pw@postgres.lab.svc.cluster.local:5432/lab"


def test_dsn_overrides_and_quoting() -> None:
    env = {
        "PGUSER": "bank owner",
        "PGPASSWORD": "p@ss:/w#rd%",
        "PGHOST": "db",
        "PGPORT": "6543",
        "PGDATABASE": "other",
    }
    assert dsn_from_env(env) == "postgresql://bank%20owner:p%40ss%3A%2Fw%23rd%25@db:6543/other"


@pytest.mark.parametrize("missing", ["PGUSER", "PGPASSWORD"])
def test_dsn_missing_required(missing: str) -> None:
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(StoreConfigError, match=missing):
        dsn_from_env(env)


def test_blank_required_is_missing() -> None:
    with pytest.raises(StoreConfigError, match="PGPASSWORD"):
        dsn_from_env({"PGUSER": "u", "PGPASSWORD": "  "})


def test_store_from_env_memory() -> None:
    assert isinstance(store_from_env({"BANK_STORE": "memory"}), MemoryStore)


def test_store_from_env_rejects_unknown_kind() -> None:
    with pytest.raises(StoreConfigError, match="BANK_STORE"):
        store_from_env({"BANK_STORE": "sqlite"})


def test_store_from_env_postgres_without_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(store_factory, "_migrate", calls.append)
    assert isinstance(store_from_env(BASE), PostgresStore)
    assert calls == []


@pytest.mark.parametrize("flag", ["1", "true", "TRUE"])
def test_store_from_env_postgres_applies_migrations(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(store_factory, "_migrate", calls.append)
    store = store_from_env({**BASE, "BANK_STORE": "postgres", "BANK_APPLY_MIGRATIONS": flag})
    assert isinstance(store, PostgresStore)
    assert calls == [dsn_from_env(BASE)]


def test_postgres_store_from_env() -> None:
    assert isinstance(PostgresStore.from_env(BASE), PostgresStore)


def test_migrations_dir_resolves_to_package_migrations() -> None:
    assert MIGRATIONS_DIR.is_dir()
    assert (MIGRATIONS_DIR / "001_bank.sql").is_file()
    assert MIGRATIONS_DIR == Path(__file__).resolve().parent.parent / "migrations"


def test_migrate_applies_through_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class FakeConn:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(store_factory.psycopg, "connect", lambda dsn: FakeConn())

    def fake_apply(conn: object, directory: Path) -> list[str]:
        seen["dir"] = directory
        return ["001_bank.sql"]

    monkeypatch.setattr(store_factory, "apply_migrations", fake_apply)
    store_factory._migrate("postgresql://x")
    assert seen["dir"] == MIGRATIONS_DIR
