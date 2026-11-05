"""In-memory Store implementation backed by plain dicts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pdl_sms_store.models import Message


class MemoryStore:
    def __init__(self) -> None:
        self._messages: dict[str, Message] = {}
        self._devices: dict[str, tuple[datetime, str | None]] = {}

    def upsert_messages(self, msgs: Sequence[Message]) -> int:
        new = 0
        for msg in msgs:
            if msg.id not in self._messages:
                self._messages[msg.id] = msg
                new += 1
        return new

    def record_device_seen(self, device: str, at: datetime, last_provider_id: str | None) -> None:
        self._devices[device] = (at, last_provider_id)

    def device_seen(self, device: str) -> tuple[datetime, str | None] | None:
        return self._devices.get(device)

    def count(self) -> int:
        return len(self._messages)
