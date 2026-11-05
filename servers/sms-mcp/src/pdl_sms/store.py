"""Read-only storage interface for text messages."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pdl_sms.models import Message


class StoreConfigError(ValueError):
    """The store cannot be built from the environment (missing or invalid setting)."""


class Store(Protocol):
    def search(
        self, query: str | None, since: datetime | None, contact: str | None, limit: int
    ) -> list[Message]:
        """Case-insensitive substring over body, contact and address; newest first, ties by id.

        ``contact`` keeps messages whose contact or address contains it (case-insensitive).
        A blank query with no contact behaves like ``recent``. ``limit`` is clamped to at
        least 1. A naive ``since`` is treated as UTC.
        """
        ...

    def recent(self, since: datetime | None, limit: int) -> list[Message]:
        """Same ordering and limit rules as ``search`` without a query."""
        ...
