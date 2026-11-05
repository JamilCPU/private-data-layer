"""Domain model for a text message as stored by sms-store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Message:
    id: str
    device: str
    provider_id: str
    thread_id: str
    address: str
    contact: str | None
    direction: str  # "in" or "out"
    body: str
    sent_at: datetime
    received_at: datetime
