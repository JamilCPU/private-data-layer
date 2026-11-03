"""Append-only JSONL audit log: one file per UTC day, hashes only, never payloads."""

import hashlib
import json
import logging
import threading
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500
_FILE_PREFIX = "audit-"
_FILE_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class AuditEntry:
    ts: str
    client_id: str
    client_name: str | None
    tool: str
    args_sha256: str
    result_bytes: int
    latency_ms: int
    error: str | None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "AuditEntry":
        return cls(**json.loads(line))


def hash_args(arguments: Mapping[str, Any]) -> str:
    """SHA-256 hex of the canonical JSON form of ``arguments``."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def truncate_error(text: str | None) -> str | None:
    """Cap an error message at ``MAX_ERROR_CHARS`` so audit lines stay small."""
    if text is None or len(text) <= MAX_ERROR_CHARS:
        return text
    return text[:MAX_ERROR_CHARS]


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` in UTC (naive means UTC); ValueError if it is out of range."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError(f"timestamp out of range: {moment!r}") from exc


def _lacks_trailing_newline(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            if fh.tell() == 0:
                return False
            fh.seek(-1, 2)
            return fh.read(1) != b"\n"
    except FileNotFoundError:
        return False


class AuditLog:
    """Writes entries to ``audit-YYYY-MM-DD.jsonl`` (by the entry's UTC date) in a directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    def _path_for(self, day: date) -> Path:
        return self._directory / f"{_FILE_PREFIX}{day.isoformat()}{_FILE_SUFFIX}"

    def record(self, entry: AuditEntry) -> None:
        day = _as_utc(datetime.fromisoformat(entry.ts)).date()
        line = entry.to_json() + "\n"
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            path = self._path_for(day)
            if _lacks_trailing_newline(path):
                line = "\n" + line  # repair a partial last line left by a crash
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()

    def search(
        self,
        since: datetime,
        *,
        until: datetime | None = None,
        client_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Entries with ``since <= ts <= until`` (default now), newest first."""
        start = _as_utc(since)
        end = _as_utc(until) if until is not None else datetime.now(UTC)
        matches: list[tuple[datetime, int, AuditEntry]] = []
        sequence = 0
        for path in self._day_files(start.date(), end.date()):
            for moment, entry in self._read_file(path):
                sequence += 1
                if start <= moment <= end and client_id in (None, entry.client_id):
                    matches.append((moment, sequence, entry))
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in matches[: max(limit, 0)]]

    def _day_files(self, first: date, last: date) -> list[Path]:
        """Existing day files dated within [first, last], oldest first."""
        dated: list[tuple[date, Path]] = []
        for path in self._directory.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}"):
            stem = path.name[len(_FILE_PREFIX) : -len(_FILE_SUFFIX)]
            try:
                day = date.fromisoformat(stem)
            except ValueError:
                continue
            if first <= day <= last:
                dated.append((day, path))
        return [path for _, path in sorted(dated)]

    def _read_file(self, path: Path) -> Iterator[tuple[datetime, AuditEntry]]:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = AuditEntry.from_json(line)
                    yield _as_utc(datetime.fromisoformat(entry.ts)), entry
                except (ValueError, TypeError, KeyError):
                    logger.warning("skipping malformed audit line in %s", path.name)
