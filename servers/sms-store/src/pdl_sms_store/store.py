"""Storage interface for SMS messages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pdl_sms_store.models import Message


class StoreConfigError(ValueError):
    """The store cannot be built from the environment (missing or invalid setting)."""


class Store(Protocol):
    def upsert_messages(self, msgs: Sequence[Message]) -> int:
        """Insert messages; existing ids are left unchanged. Returns the count of NEW ids."""
        ...

    def record_device_seen(self, device: str, at: datetime, last_provider_id: str | None) -> None:
        """Upsert the device's last-seen time and last provider id."""
        ...

    def count(self) -> int: ...
