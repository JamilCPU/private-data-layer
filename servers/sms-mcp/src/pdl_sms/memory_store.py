"""In-memory Store implementation backed by a dict."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from pdl_sms.models import Message


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _norm(text: str | None) -> str | None:
    cleaned = text.strip().lower() if text else ""
    return cleaned or None


def _matches(msg: Message, needle: str | None, contact: str | None, since: datetime | None) -> bool:
    if since is not None and msg.sent_at < since:
        return False
    fields = (msg.contact or "", msg.address)
    if contact is not None and not any(contact in f.lower() for f in fields):
        return False
    if needle is None:
        return True
    return any(needle in part.lower() for part in (msg.body, *fields))


class MemoryStore:
    """Holds messages for tests and trials; ``add`` stands in for sms-store's writes."""

    def __init__(self, messages: Iterable[Message] = ()) -> None:
        self._messages: dict[str, Message] = {m.id: m for m in messages}

    def add(self, msgs: Iterable[Message]) -> None:
        for msg in msgs:
            self._messages[msg.id] = msg

    def search(
        self, query: str | None, since: datetime | None, contact: str | None, limit: int
    ) -> list[Message]:
        needle, who = _norm(query), _norm(contact)
        floor = _aware(since) if since is not None else None
        hits = [m for m in self._messages.values() if _matches(m, needle, who, floor)]
        hits.sort(key=lambda m: m.id)
        hits.sort(key=lambda m: m.sent_at, reverse=True)
        return hits[: max(1, limit)]

    def recent(self, since: datetime | None, limit: int) -> list[Message]:
        return self.search(None, since, None, limit)
