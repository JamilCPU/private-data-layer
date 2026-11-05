"""Build the Store from environment variables."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

import psycopg

from pdl_sms_store.memory_store import MemoryStore
from pdl_sms_store.migrations import apply_migrations
from pdl_sms_store.postgres_store import PostgresStore
from pdl_sms_store.store import Store, StoreConfigError

DEFAULT_HOST = "postgres.lab.svc.cluster.local"
DEFAULT_PORT = "5432"
DEFAULT_DATABASE = "lab"
_TRUE = {"1", "true"}


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise StoreConfigError(f"{name} is required for the Postgres store")
    return value


def dsn_from_env(env: Mapping[str, str]) -> str:
    user, password = _required(env, "PGUSER"), _required(env, "PGPASSWORD")
    host = env.get("PGHOST") or DEFAULT_HOST
    port = env.get("PGPORT") or DEFAULT_PORT
    database = env.get("PGDATABASE") or DEFAULT_DATABASE
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def store_from_env(env: Mapping[str, str]) -> Store:
    kind = (env.get("SMS_STORE") or "postgres").lower()
    if kind == "memory":
        return MemoryStore()
    if kind != "postgres":
        raise StoreConfigError(f"SMS_STORE must be 'memory' or 'postgres', got {kind!r}")
    dsn = dsn_from_env(env)
    if env.get("SMS_APPLY_MIGRATIONS", "").lower() in _TRUE:
        with psycopg.connect(dsn) as conn:
            apply_migrations(conn)
    return PostgresStore(dsn)
