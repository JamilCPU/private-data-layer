"""Command line entry point: ``pdl-bank-import``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg

from pdl_bank.categorize import Rule, RulesError, load_rules
from pdl_bank.importer import ImportResult, import_inbox
from pdl_bank.memory_store import MemoryStore
from pdl_bank.parsers import CsvMapping, load_mappings
from pdl_bank.store import Store, StoreConfigError
from pdl_bank.store_factory import store_from_env

log = logging.getLogger(__name__)

EXIT_ERRORS = 1
EXIT_USAGE = 2
_STATUSES = ("imported", "duplicate", "skipped", "error")


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdl-bank-import", description="Import bank statements.")
    p.add_argument("--inbox", type=Path, required=True)
    p.add_argument("--rules", type=Path, required=True)
    p.add_argument("--mappings", type=Path, required=True)
    p.add_argument("--processed", type=Path, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="scan once and exit (default)")
    mode.add_argument("--interval", type=_positive_int, metavar="SECONDS")
    p.add_argument("--memory", action="store_true", help="dry run against an in-memory store")
    return p


def _open_store(use_memory: bool) -> Store | None:
    if use_memory:
        return MemoryStore()
    try:
        return store_from_env(os.environ)
    except (StoreConfigError, psycopg.Error) as exc:
        print(f"cannot open store: {exc}", file=sys.stderr)
        return None


def _scan(
    args: argparse.Namespace,
    store: Store,
    mappings: tuple[CsvMapping, ...],
    rules: tuple[Rule, ...],
) -> list[ImportResult]:
    results = import_inbox(args.inbox, store, mappings, rules, processed=args.processed)
    for r in results:
        note = f" ({r.reason})" if r.reason else ""
        line = f"{r.file}: {r.status} new={r.new_transactions}{note}"
        if args.memory:
            print(line)
        else:
            log.info("%s", line)
    counts = Counter(r.status for r in results)
    log.info("import run finished: %s", ", ".join(f"{s}={counts[s]}" for s in _STATUSES))
    return results


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        rules = load_rules(args.rules)
        mappings = load_mappings(args.mappings)
    except (RulesError, ValueError, OSError) as exc:
        print(f"cannot load configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE
    store = _open_store(args.memory)
    if store is None:
        return EXIT_USAGE
    if args.interval is None:
        results = _scan(args, store, mappings, rules)
        return EXIT_ERRORS if any(r.status == "error" for r in results) else 0
    try:
        while True:
            _scan(args, store, mappings, rules)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
