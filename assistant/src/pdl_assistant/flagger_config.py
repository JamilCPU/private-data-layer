"""Threshold checks for the hourly flagger, loaded from the owner's YAML file."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml

_NAME = re.compile(r"[a-z0-9-]{1,40}")
_MAX_CHECKS = 20
_MAX_SENDERS = 50
_MAX_PERCENT = Decimal(1000)
_DIRECTIONS = ("spend", "any")


class FlaggerConfigError(ValueError):
    """Raised when the flagger checks file is invalid."""


@dataclass(frozen=True)
class TransactionOver:
    name: str
    amount: Decimal
    direction: Literal["spend", "any"] = "spend"


@dataclass(frozen=True)
class RecurringChange:
    name: str
    percent: Decimal


@dataclass(frozen=True)
class EmailFrom:
    name: str
    senders: tuple[str, ...]


Check = TransactionOver | RecurringChange | EmailFrom

_ALLOWED_KEYS = {
    "transaction_over": {"name", "type", "amount", "direction"},
    "recurring_change": {"name", "type", "percent"},
    "email_from": {"name", "type", "senders"},
}


def _read(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FlaggerConfigError(f"{path}: cannot read flagger checks ({exc})") from exc


def _positive(label: str, value: Any, maximum: Decimal | None = None) -> Decimal:
    """Parse a finite number > 0 (and <= ``maximum``); ``label`` prefixes any error."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a number, got {value!r}")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a number, got {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} must be a finite number > 0, got {value!r}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be <= {maximum}, got {value!r}")
    return number


def _senders(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_SENDERS:
        raise ValueError(f"senders must be a list of 1-{_MAX_SENDERS} addresses")
    if not all(isinstance(s, str) and s.strip() for s in value):
        raise ValueError("senders must be non-blank strings")
    return tuple(s.strip().lower() for s in value)


def _build(entry: dict[str, Any]) -> Check:
    name = entry["name"]
    kind = entry["type"]
    if kind == "transaction_over":
        direction = entry.get("direction", "spend")
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(_DIRECTIONS)}")
        return TransactionOver(name, _positive("amount", entry.get("amount")), direction)
    if kind == "recurring_change":
        return RecurringChange(name, _positive("percent", entry.get("percent"), _MAX_PERCENT))
    return EmailFrom(name, _senders(entry.get("senders")))


def _validate_entry(entry: Any, index: int, seen: set[str]) -> Check:
    """Validate one list entry; raises ValueError whose message names the check."""
    if not isinstance(entry, dict):
        raise TypeError(f"check #{index + 1}: expected a mapping")
    name = entry.get("name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError(f"check #{index + 1}: name must match [a-z0-9-]{{1,40}}, got {name!r}")
    if name in seen:
        raise ValueError(f"check {name!r}: duplicate name")
    seen.add(name)
    kind = entry.get("type")
    if kind not in _ALLOWED_KEYS:
        raise ValueError(f"check {name!r}: unknown type {kind!r}")
    unknown = sorted(set(entry) - _ALLOWED_KEYS[kind])
    if unknown:
        raise ValueError(f"check {name!r}: unknown key(s) {', '.join(map(str, unknown))}")
    try:
        return _build(entry)
    except ValueError as exc:
        raise ValueError(f"check {name!r}: {exc}") from exc


def load_checks(path: Path) -> tuple[Check, ...]:
    """Load and validate the checks file; every problem is a FlaggerConfigError naming ``path``."""
    raw = _read(path)
    if not isinstance(raw, dict) or set(raw) - {"checks"}:
        raise FlaggerConfigError(f"{path}: expected a mapping with only a 'checks' key")
    entries = raw.get("checks")
    if not isinstance(entries, list) or not entries:
        raise FlaggerConfigError(f"{path}: 'checks' must be a non-empty list")
    if len(entries) > _MAX_CHECKS:
        raise FlaggerConfigError(f"{path}: at most {_MAX_CHECKS} checks allowed")
    seen: set[str] = set()
    try:
        return tuple(_validate_entry(e, i, seen) for i, e in enumerate(entries))
    except (TypeError, ValueError) as exc:
        raise FlaggerConfigError(f"{path}: {exc}") from exc
