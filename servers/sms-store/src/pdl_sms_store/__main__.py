"""Entry point: serve the SMS receiver over HTTP."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg
import uvicorn

from pdl_sms_store.app import build_app
from pdl_sms_store.store import StoreConfigError
from pdl_sms_store.store_factory import store_from_env
from pdl_sms_store.tokens import TokensError, load_device_tokens

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def _error(message: str) -> int:
    print(f"sms-store: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    del argv  # configuration comes from the environment only
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    tokens_path = os.environ.get("SMS_DEVICE_TOKENS_FILE", "").strip()
    if not tokens_path:
        return _error("environment variable SMS_DEVICE_TOKENS_FILE is required")
    host = os.environ.get("SMS_STORE_LISTEN_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.environ.get("SMS_STORE_LISTEN_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        return _error(f"SMS_STORE_LISTEN_PORT must be an integer, got {raw_port!r}")
    try:
        devices = load_device_tokens(Path(tokens_path))
        store = store_from_env(os.environ)
    except (TokensError, StoreConfigError, psycopg.Error) as exc:
        return _error(str(exc))
    uvicorn.run(build_app(store, devices), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
