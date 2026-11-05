"""Domain model and input validation for SMS messages."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

MAX_TEXT_CHARS = 2000
TRUNCATED_MARK = " [truncated]"
MAX_ID_CHARS = 64
MAX_ADDRESS_CHARS = 64
MAX_CONTACT_CHARS = 128
MAX_DATE_MS = 4_102_444_800_000  # 2100-01-01 UTC


class ValidationError(Exception):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class Message:
    id: str
    device: str
    provider_id: str
    thread_id: str | None
    address: str
    contact: str | None
    direction: Literal["in", "out"]
    body: str
    sent_at: datetime
    received_at: datetime


def message_id(device: str, provider_id: str, date_ms: int) -> str:
    """Deterministic id so re-sending the same message is idempotent."""
    raw = f"{device}|{provider_id}|{date_ms}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _text(value: str) -> str:
    """Postgres ``text`` cannot hold NUL, so a phone-supplied one is dropped, not rejected."""
    return value.replace("\x00", "")


def _optional_text(raw: dict[str, Any], key: str, *, max_len: int) -> str | None:
    """Stripped string, or None when absent, null or blank."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(key, "must be a string")
    value = _text(value).strip()
    if len(value) > max_len:
        raise ValidationError(key, f"must be at most {max_len} characters")
    return value or None


def _required_text(raw: dict[str, Any], key: str, *, max_len: int, strip: bool) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValidationError(key, "must be a string")
    value = _text(value)
    if strip:
        value = value.strip()
    if not value:
        raise ValidationError(key, "must not be empty")
    if len(value) > max_len:
        raise ValidationError(key, f"must be at most {max_len} characters")
    return value


def _date_ms(raw: dict[str, Any]) -> int:
    value = raw.get("date_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("date_ms", "must be an integer")
    if value < 0:
        raise ValidationError("date_ms", "must not be negative")
    if value > MAX_DATE_MS:
        raise ValidationError("date_ms", "out of range")
    return value


def _sent_at(date_ms: int) -> datetime:
    try:
        return datetime.fromtimestamp(date_ms / 1000, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValidationError("date_ms", "out of range") from exc


def _body(raw: dict[str, Any]) -> str:
    body = raw.get("body")
    if not isinstance(body, str):
        raise ValidationError("body", "must be a string")
    body = _text(body)
    if len(body) > MAX_TEXT_CHARS:
        return body[:MAX_TEXT_CHARS] + TRUNCATED_MARK
    return body


def parse_incoming(device: str, raw: dict[str, Any], received_at: datetime) -> Message:
    """Validate one device-supplied message dict; raise ``ValidationError`` on bad input."""
    provider_id = _required_text(raw, "provider_id", max_len=MAX_ID_CHARS, strip=False)
    date_ms = _date_ms(raw)
    address = _required_text(raw, "address", max_len=MAX_ADDRESS_CHARS, strip=True)
    contact = _optional_text(raw, "contact", max_len=MAX_CONTACT_CHARS)
    thread_id = _optional_text(raw, "thread_id", max_len=MAX_ID_CHARS)
    direction = raw.get("direction")
    if direction not in ("in", "out"):
        raise ValidationError("direction", "must be 'in' or 'out'")
    return Message(
        id=message_id(device, provider_id, date_ms),
        device=device,
        provider_id=provider_id,
        thread_id=thread_id,
        address=address,
        contact=contact,
        direction=direction,
        body=_body(raw),
        sent_at=_sent_at(date_ms),
        received_at=received_at,
    )
