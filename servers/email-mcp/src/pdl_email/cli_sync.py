"""Command line entry point: ``pdl-email-sync``."""

from __future__ import annotations

import argparse
import imaplib
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pdl_email.imap_source import ImapSource
from pdl_email.memory_store import MemoryStore
from pdl_email.store import Store
from pdl_email.sync import sync_once

log = logging.getLogger("pdl_email.sync")

EXIT_FAILED = 1
EXIT_USAGE = 2

# Tests substitute this to avoid real network access.
source_factory: Callable[..., Any] = ImapSource


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdl-email-sync", description="Sync IMAP mail to the store.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="sync once and exit (default)")
    mode.add_argument("--interval", type=_positive_int, metavar="SECONDS")
    return p


def _load_store_factory() -> Callable[[Mapping[str, str]], Store]:
    from pdl_email.store_factory import store_from_env  # type: ignore[import-not-found]

    return store_from_env  # type: ignore[no-any-return]


def _open_store() -> Store | None:
    if os.environ.get("EMAIL_STORE") == "memory":
        return MemoryStore()
    try:
        factory = _load_store_factory()
    except ModuleNotFoundError as exc:
        if exc.name != "pdl_email.store_factory":
            raise
        print("Postgres store not available yet; set EMAIL_STORE=memory", file=sys.stderr)
        return None
    return factory(os.environ)


def _read_password(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        print(f"cannot read EMAIL_IMAP_PASSWORD_FILE: {exc}", file=sys.stderr)
        return None


def _run_once(host: str, user: str, password: str, store: Store) -> None:
    mailbox = os.environ.get("EMAIL_MAILBOX", "INBOX")
    source = source_factory(
        host, user, password, mailbox=mailbox, port=int(os.environ.get("EMAIL_IMAP_PORT", "993"))
    )
    result = sync_once(
        source,
        store,
        mailbox=mailbox,
        initial_days=int(os.environ.get("EMAIL_INITIAL_DAYS", "90")),
    )
    log.info("sync result: %s", result)


def _loop(host: str, user: str, password: str, store: Store, interval: int) -> int:
    try:
        while True:
            try:
                _run_once(host, user, password, store)
            except (OSError, imaplib.IMAP4.error):
                log.exception("sync run failed; will retry")
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted; stopping")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    env = os.environ
    host, user, pw_file = (
        env.get("EMAIL_IMAP_HOST"),
        env.get("EMAIL_IMAP_USER"),
        env.get("EMAIL_IMAP_PASSWORD_FILE"),
    )
    if not (host and user and pw_file):
        print(
            "EMAIL_IMAP_HOST, EMAIL_IMAP_USER and EMAIL_IMAP_PASSWORD_FILE are required",
            file=sys.stderr,
        )
        return EXIT_USAGE
    password = _read_password(pw_file)
    if password is None:
        return EXIT_USAGE
    store = _open_store()
    if store is None:
        return EXIT_USAGE
    if args.interval is not None:
        return _loop(host, user, password, store, args.interval)
    try:
        _run_once(host, user, password, store)
    except Exception:
        log.exception("sync failed")
        return EXIT_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())
