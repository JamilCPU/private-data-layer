"""Storage interface for synced messages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pdl_email.models import Message


@dataclass(frozen=True)
class SyncState:
    uidvalidity: int
    last_uid: int


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    new: int
    uidvalidity_reset: bool


class Store(Protocol):
    def upsert_messages(self, msgs: Sequence[Message]) -> int:
        """Insert messages; existing ids are left unchanged. Returns the count of NEW ids."""
        ...

    def get(self, id: str) -> Message | None: ...

    def search(self, query: str | None, since: datetime | None, limit: int) -> list[Message]:
        """Case-insensitive substring over subject, from, body; newest first, ties by id.

        ``limit`` is clamped to at least 1. A naive ``since`` is treated as UTC.
        """
        ...

    def recent(self, since: datetime | None, limit: int) -> list[Message]:
        """Same ordering and limit rules as ``search`` without a query."""
        ...

    def sync_state(self, mailbox: str) -> SyncState | None: ...

    def set_sync_state(self, mailbox: str, state: SyncState) -> None: ...
