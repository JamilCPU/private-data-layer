"""In-memory Store implementation backed by plain dicts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pdl_email.models import Message
from pdl_email.store import SyncState


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _matches(msg: Message, needle: str | None, since: datetime | None) -> bool:
    if since is not None and msg.sent_at < since:
        return False
    if not needle:
        return True
    haystack = (msg.subject, msg.from_addr, msg.from_name or "", msg.body_text)
    return any(needle in part.lower() for part in haystack)


class MemoryStore:
    def __init__(self) -> None:
        self._messages: dict[str, Message] = {}
        self._states: dict[str, SyncState] = {}

    def upsert_messages(self, msgs: Sequence[Message]) -> int:
        new = 0
        for msg in msgs:
            if msg.id not in self._messages:
                self._messages[msg.id] = msg
                new += 1
        return new

    def get(self, id: str) -> Message | None:
        return self._messages.get(id)

    def search(self, query: str | None, since: datetime | None, limit: int) -> list[Message]:
        needle = query.strip().lower() if query else None
        floor = _aware(since) if since is not None else None
        hits = [m for m in self._messages.values() if _matches(m, needle, floor)]
        hits.sort(key=lambda m: m.id)
        hits.sort(key=lambda m: m.sent_at, reverse=True)
        return hits[: max(1, limit)]

    def recent(self, since: datetime | None, limit: int) -> list[Message]:
        return self.search(None, since, limit)

    def sync_state(self, mailbox: str) -> SyncState | None:
        return self._states.get(mailbox)

    def set_sync_state(self, mailbox: str, state: SyncState) -> None:
        self._states[mailbox] = state
