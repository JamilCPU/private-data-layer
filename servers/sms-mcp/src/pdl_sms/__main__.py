"""Entry point: serve the SMS MCP server over streamable HTTP behind the shared secret."""

from __future__ import annotations

import logging
import os
import sys

import psycopg
import uvicorn

from pdl_sms.memory_store import MemoryStore
from pdl_sms.middleware import SharedSecretMiddleware
from pdl_sms.server import build_server
from pdl_sms.store import Store, StoreConfigError
from pdl_sms.store_factory import store_from_env

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

logger = logging.getLogger("sms-mcp")


def _error(message: str) -> int:
    sys.stderr.write(f"sms-mcp: {message}\n")
    return 1


def _open_store() -> Store | None:
    kind = os.environ.get("SMS_STORE", "").strip().lower() or "postgres"
    if kind == "memory":
        return MemoryStore()
    if kind != "postgres":
        _error("SMS_STORE must be memory or postgres")
        return None
    try:
        return store_from_env(os.environ)
    except (StoreConfigError, psycopg.Error) as exc:
        _error(str(exc))
        return None


def main(argv: list[str] | None = None) -> int:
    del argv  # configuration comes from the environment only
    secret = os.environ.get("SMS_MCP_SHARED_SECRET", "").strip()
    if not secret:
        return _error("environment variable SMS_MCP_SHARED_SECRET is required")
    host = os.environ.get("SMS_MCP_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.environ.get("SMS_MCP_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        return _error(f"SMS_MCP_PORT must be an integer, got {raw_port!r}")
    store = _open_store()
    if store is None:
        return 1
    server = build_server(store, host=host, port=port)
    logger.info("serving on %s:%d", host, port)
    uvicorn.run(SharedSecretMiddleware(server.streamable_http_app(), secret), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
