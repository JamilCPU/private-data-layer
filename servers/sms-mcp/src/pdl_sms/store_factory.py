"""Build the Store from environment variables. Never applies migrations (sms-store owns them)."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from pdl_sms.memory_store import MemoryStore
from pdl_sms.postgres_store import PostgresStore
from pdl_sms.store import Store, StoreConfigError

DEFAULT_HOST = "postgres.lab.svc.cluster.local"
DEFAULT_PORT = "5432"
DEFAULT_DATABASE = "lab"


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
    return PostgresStore(dsn_from_env(env))
