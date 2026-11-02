"""Append-only JSONL audit log: one file per UTC day, hashes only, never payloads."""

import hashlib
import json
import logging
import threading
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _days(first: date, last: date) -> Iterator[date]:
    for offset in range((last - first).days + 1):
        yield first + timedelta(days=offset)


class AuditLog:
    """Writes entries to ``audit-YYYY-MM-DD.jsonl`` (by the entry's UTC date) in a directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    def _path_for(self, day: date) -> Path:
        return self._directory / f"audit-{day.isoformat()}.jsonl"

    def record(self, entry: AuditEntry) -> None:
        day = _as_utc(datetime.fromisoformat(entry.ts)).date()
        line = entry.to_json() + "\n"
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            with self._path_for(day).open("a", encoding="utf-8", newline="\n") as fh:
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
        matches: list[tuple[datetime, AuditEntry]] = []
        for day in _days(start.date(), end.date()):
            for moment, entry in self._read_day(day):
                if start <= moment <= end and client_id in (None, entry.client_id):
                    matches.append((moment, entry))
        matches.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in matches[: max(limit, 0)]]

    def _read_day(self, day: date) -> Iterator[tuple[datetime, AuditEntry]]:
        path = self._path_for(day)
        if not path.exists():
            return
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = AuditEntry.from_json(line)
                    yield _as_utc(datetime.fromisoformat(entry.ts)), entry
                except (ValueError, TypeError, KeyError):
                    logger.warning("skipping malformed audit line in %s", path.name)
