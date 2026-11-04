"""Build a Store from environment variables."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import quote

import psycopg

from pdl_bank.memory_store import MemoryStore
from pdl_bank.migrations import MIGRATIONS_DIR, apply_migrations
from pdl_bank.postgres_store import PostgresStore
from pdl_bank.store import Store, StoreConfigError

__all__ = ["StoreConfigError", "dsn_from_env", "store_from_env"]

log = logging.getLogger(__name__)

DEFAULT_HOST = "postgres.lab.svc.cluster.local"
DEFAULT_PORT = "5432"
DEFAULT_DATABASE = "lab"
_TRUTHY = frozenset({"1", "true"})


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise StoreConfigError(f"environment variable {name} is required")
    return value


def _optional(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(name, "").strip() or default


def dsn_from_env(env: Mapping[str, str]) -> str:
    user = quote(_required(env, "PGUSER"), safe="")
    password = quote(_required(env, "PGPASSWORD"), safe="")
    host = _optional(env, "PGHOST", DEFAULT_HOST)
    port = _optional(env, "PGPORT", DEFAULT_PORT)
    database = quote(_optional(env, "PGDATABASE", DEFAULT_DATABASE), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _migrate(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        applied = apply_migrations(conn, MIGRATIONS_DIR)
    log.info("migrations applied: %s", ", ".join(applied) or "none (up to date)")


def store_from_env(env: Mapping[str, str]) -> Store:
    kind = env.get("BANK_STORE", "").strip().lower() or "postgres"
    if kind == "memory":
        return MemoryStore()
    if kind != "postgres":
        raise StoreConfigError(f"BANK_STORE must be 'memory' or 'postgres', got {kind!r}")
    dsn = dsn_from_env(env)
    if env.get("BANK_APPLY_MIGRATIONS", "").strip().lower() in _TRUTHY:
        _migrate(dsn)
    return PostgresStore(dsn)
