"""Entry point: serve the bank MCP server over streamable HTTP behind the shared secret."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

import psycopg
import uvicorn

from pdl_bank.memory_store import MemoryStore
from pdl_bank.middleware import SharedSecretMiddleware
from pdl_bank.server import build_server
from pdl_bank.store import Store, StoreConfigError

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def _error(message: str) -> int:
    print(f"bank-mcp: {message}", file=sys.stderr)
    return 1


def _load_store_factory() -> Callable[[Mapping[str, str]], Store]:
    from pdl_bank.store_factory import store_from_env  # type: ignore[import-not-found]

    return store_from_env  # type: ignore[no-any-return]


def _open_store() -> Store | None:
    kind = os.environ.get("BANK_STORE", "").strip().lower() or "postgres"
    if kind == "memory":
        return MemoryStore()
    if kind != "postgres":
        _error(f"BANK_STORE must be 'memory' or 'postgres', got {kind!r}")
        return None
    try:
        factory = _load_store_factory()
    except ModuleNotFoundError as exc:
        if exc.name != "pdl_bank.store_factory":
            raise
        _error("Postgres store not available yet; set BANK_STORE=memory")
        return None
    try:
        return factory(os.environ)
    except (StoreConfigError, psycopg.Error) as exc:
        _error(f"cannot open store: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    del argv  # configuration comes from the environment only
    secret = os.environ.get("BANK_MCP_SHARED_SECRET", "").strip()
    if not secret:
        return _error("environment variable BANK_MCP_SHARED_SECRET is required")
    host = os.environ.get("BANK_MCP_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.environ.get("BANK_MCP_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        return _error(f"BANK_MCP_PORT must be an integer, got {raw_port!r}")
    store = _open_store()
    if store is None:
        return 1
    server = build_server(store, host=host, port=port)
    uvicorn.run(SharedSecretMiddleware(server.streamable_http_app(), secret), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
