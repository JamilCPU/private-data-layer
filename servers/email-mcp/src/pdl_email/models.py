"""Domain models for synced email messages."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Message:
    id: str
    uid: int
    uidvalidity: int
    mailbox: str
    from_addr: str
    from_name: str | None
    to_addrs: tuple[str, ...]
    subject: str
    sent_at: datetime
    snippet: str
    body_text: str
    has_attachments: bool
    message_id_header: str | None


def message_id(mailbox: str, uidvalidity: int, uid: int) -> str:
    """Deterministic id so re-syncing the same message is idempotent."""
    raw = f"{mailbox}|{uidvalidity}|{uid}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
