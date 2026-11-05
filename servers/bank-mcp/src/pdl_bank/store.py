"""Storage protocol and result types. Business logic depends only on this."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

from pdl_bank.models import Account, Transaction


class StoreConfigError(Exception):
    """Raised when the store cannot be configured from the environment."""


GroupBy = Literal["category", "month", "account"]


@dataclass(frozen=True)
class SummaryRow:
    key: str
    total: Decimal
    count: int


@dataclass(frozen=True)
class ChangedCharge:
    """A newer charge from a recurring merchant at a different amount (positive)."""

    id: str
    posted_on: date
    amount: Decimal
    description: str


@dataclass(frozen=True)
class RecurringCharge:
    description_key: str
    amount: Decimal
    occurrences: int
    last_on: date
    cadence_days: int
    changed: tuple[ChangedCharge, ...] = ()


@dataclass(frozen=True)
class Period:
    since: date
    until: date
    label: str


class Store(Protocol):
    def upsert_account(self, a: Account) -> None: ...

    def upsert_transactions(self, txs: Sequence[Transaction]) -> int:
        """Insert unseen transactions; return the number newly inserted."""
        ...

    def accounts(self) -> list[Account]: ...

    def search(
        self,
        query: str | None,
        since: date | None,
        until: date | None,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
        limit: int,
    ) -> list[Transaction]:
        """Newest first; ``limit`` is clamped to a minimum of 1 (0 returns one row)."""
        ...

    def summary(self, since: date, until: date, group_by: GroupBy) -> list[SummaryRow]: ...

    def recurring(self, min_occurrences: int = 3) -> list[RecurringCharge]: ...

    def record_import(
        self, file_hash: str, filename: str, imported_on: date, count: int
    ) -> None: ...

    def was_imported(self, file_hash: str) -> bool: ...
