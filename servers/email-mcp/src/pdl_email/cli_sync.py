"""Command line entry point: ``pdl-email-sync``."""

from __future__ import annotations

import argparse
import imaplib
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from pdl_email.imap_source import ImapSource
from pdl_email.memory_store import MemoryStore
from pdl_email.store import Store, StoreConfigError
from pdl_email.sync import sync_once

log = logging.getLogger("pdl_email.sync")

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

# Failures of one interval run that are retried on the next tick: network and IMAP protocol
# errors, malformed IMAP responses (RuntimeError from ImapSource) and database errors.
_RETRYABLE = (OSError, imaplib.IMAP4.error, RuntimeError, psycopg.Error)

# Tests substitute this to avoid real network access.
source_factory: Callable[..., Any] = ImapSource


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    password: str
    mailbox: str
    port: int
    initial_days: int


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
    from pdl_email.store_factory import store_from_env

    return store_from_env


def _open_store() -> Store | None:
    if os.environ.get("EMAIL_STORE") == "memory":
        return MemoryStore()
    try:
        factory = _load_store_factory()
    except ModuleNotFoundError as exc:
        if exc.name != "pdl_email.store_factory":
            raise
        log.error("Postgres store not available yet; set EMAIL_STORE=memory")
        return None
    try:
        return factory(os.environ)
    except StoreConfigError as exc:
        log.error("store configuration error: %s", exc)
    except psycopg.Error as exc:
        log.error("cannot open the store: %s", exc)
    return None


def _read_password(path: str) -> str | None:
    try:
        password = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        log.error("cannot read EMAIL_IMAP_PASSWORD_FILE: %s", exc)
        return None
    if not password:
        log.error("EMAIL_IMAP_PASSWORD_FILE is empty")
        return None
    return password


def _int_env(name: str, default: int) -> int | None:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        log.error("%s must be a positive integer, got %r", name, raw)
        return None
    return value


def _load_config() -> Config | None:
    env = os.environ
    host, user, pw_file = (
        env.get("EMAIL_IMAP_HOST"),
        env.get("EMAIL_IMAP_USER"),
        env.get("EMAIL_IMAP_PASSWORD_FILE"),
    )
    if not (host and user and pw_file):
        log.error("EMAIL_IMAP_HOST, EMAIL_IMAP_USER and EMAIL_IMAP_PASSWORD_FILE are required")
        return None
    port, days = _int_env("EMAIL_IMAP_PORT", 993), _int_env("EMAIL_INITIAL_DAYS", 90)
    password = _read_password(pw_file)
    if port is None or days is None or password is None:
        return None
    return Config(host, user, password, env.get("EMAIL_MAILBOX", "INBOX"), port, days)


def _run_once(cfg: Config, store: Store) -> None:
    source = source_factory(cfg.host, cfg.user, cfg.password, mailbox=cfg.mailbox, port=cfg.port)
    result = sync_once(source, store, mailbox=cfg.mailbox, initial_days=cfg.initial_days)
    log.info("sync result: %s", result)


def _loop(cfg: Config, store: Store, interval: int) -> int:
    try:
        while True:
            try:
                _run_once(cfg, store)
            except _RETRYABLE:
                log.warning("sync run failed; will retry", exc_info=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted; stopping")
        return 0


def _once(cfg: Config, store: Store) -> int:
    try:
        _run_once(cfg, store)
    except KeyboardInterrupt:
        log.info("interrupted")
        return EXIT_INTERRUPTED
    except Exception:
        log.exception("sync failed")
        return EXIT_FAILED
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = _load_config()
    if cfg is None:
        return EXIT_USAGE
    store = _open_store()
    if store is None:
        return EXIT_USAGE
    if args.interval is not None:
        return _loop(cfg, store, args.interval)
    return _once(cfg, store)


if __name__ == "__main__":
    sys.exit(main())
