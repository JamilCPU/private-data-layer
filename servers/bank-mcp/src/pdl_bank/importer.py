"""Statement importer: hash, parse, categorise and store files, scanning an inbox."""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal

from pdl_bank.categorize import Rule, RulesError, categorize
from pdl_bank.parsers import MAX_FILE_BYTES, CsvMapping, ParseError, detect_and_parse
from pdl_bank.store import Store

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".csv", ".ofx", ".qfx"})
_CHUNK = 1024 * 1024

Status = Literal["imported", "skipped", "duplicate", "error"]


@dataclass(frozen=True)
class ImportResult:
    file: str
    status: Status
    new_transactions: int
    reason: str | None = None
    file_hash: str | None = None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _is_candidate(path: Path) -> bool:
    return not path.name.startswith(".") and path.suffix.lower() in SUPPORTED_SUFFIXES


def _import_new(
    path: Path,
    file_hash: str,
    store: Store,
    mappings: tuple[CsvMapping, ...],
    rules: tuple[Rule, ...],
) -> ImportResult:
    statement = detect_and_parse(path, mappings)
    txs = [replace(t, category=categorize(t.description, rules)) for t in statement.transactions]
    store.upsert_account(statement.account)
    new = store.upsert_transactions(txs)
    store.record_import(file_hash, path.name, date.today(), new)
    return ImportResult(path.name, "imported", new, None, file_hash)


def import_file(
    path: Path,
    store: Store,
    mappings: tuple[CsvMapping, ...],
    rules: tuple[Rule, ...],
) -> ImportResult:
    path = Path(path)
    if not _is_candidate(path):
        log.info("skipping %s: not a statement file", path.name)
        return ImportResult(path.name, "skipped", 0, "not a statement file")
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ParseError(f"{path.name}: file too large ({size} > {MAX_FILE_BYTES} bytes)")
        file_hash = file_sha256(path)
        if store.was_imported(file_hash):
            log.info("skipping %s: already imported", path.name)
            return ImportResult(path.name, "duplicate", 0, None, file_hash)
        return _import_new(path, file_hash, store, mappings, rules)
    except (ParseError, OSError, RulesError) as exc:
        log.warning("failed to import %s: %s", path.name, exc)
        return ImportResult(path.name, "error", 0, str(exc))
    except Exception as exc:  # one bad file must not stop the run
        log.warning("failed to import %s: %s", path.name, exc, exc_info=True)
        return ImportResult(path.name, "error", 0, f"{type(exc).__name__}: {exc}")


def _destination(processed: Path, path: Path, file_hash: str | None) -> Path:
    dest = processed / path.name
    if dest.exists():
        suffix = (file_hash or file_sha256(path))[:8]
        dest = processed / f"{path.stem}.{suffix}{path.suffix}"
    return dest


def _move_processed(path: Path, processed: Path, file_hash: str | None) -> None:
    try:
        shutil.move(str(path), str(_destination(processed, path, file_hash)))
    except OSError as exc:
        log.warning("could not move %s to %s: %s", path.name, processed, exc)


def import_inbox(
    inbox: Path,
    store: Store,
    mappings: tuple[CsvMapping, ...],
    rules: tuple[Rule, ...],
    *,
    processed: Path | None,
) -> list[ImportResult]:
    if processed is not None:
        processed.mkdir(parents=True, exist_ok=True)
    results: list[ImportResult] = []
    for path in sorted(Path(inbox).iterdir()):
        if not path.is_file():
            continue
        result = import_file(path, store, mappings, rules)
        results.append(result)
        if processed is not None and result.status in ("imported", "duplicate"):
            _move_processed(path, processed, result.file_hash)
    return results
