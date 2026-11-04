"""In-memory Store implementation backed by plain dicts."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from itertools import pairwise

from pdl_bank.categorize import UNCATEGORIZED
from pdl_bank.models import Account, Transaction, normalise_description
from pdl_bank.store import GroupBy, RecurringCharge, SummaryRow

_NON_LETTERS = re.compile(r"[^a-z\s]")


def recurring_key(description: str) -> str:
    """Lowercased description with digits and punctuation removed."""
    stripped = _NON_LETTERS.sub("", normalise_description(description))
    return " ".join(stripped.split())


def _group_key(tx: Transaction, group_by: GroupBy) -> str:
    if group_by == "category":
        return tx.category or UNCATEGORIZED
    if group_by == "month":
        return tx.posted_on.strftime("%Y-%m")
    return tx.account_id


def _matches(
    tx: Transaction,
    query: str | None,
    since: date | None,
    until: date | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
) -> bool:
    if since is not None and tx.posted_on < since:
        return False
    if until is not None and tx.posted_on > until:
        return False
    if min_amount is not None and tx.amount < min_amount:
        return False
    if max_amount is not None and tx.amount > max_amount:
        return False
    if query:
        needle = query.lower()
        return needle in tx.description.lower() or needle in (tx.category or "").lower()
    return True


def _to_charge(key: str, group: list[Transaction]) -> RecurringCharge:
    ordered = sorted(group, key=lambda t: (t.posted_on, t.id))
    dates = [t.posted_on for t in ordered]
    gaps = [(b - a).days for a, b in pairwise(dates)]
    cadence = statistics.median_low(gaps) if gaps else 0
    return RecurringCharge(key, abs(ordered[-1].amount), len(ordered), dates[-1], cadence)


class MemoryStore:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._txs: dict[str, Transaction] = {}
        self._imports: dict[str, tuple[str, date, int]] = {}

    def upsert_account(self, a: Account) -> None:
        self._accounts[a.id] = a

    def upsert_transactions(self, txs: Sequence[Transaction]) -> int:
        new = 0
        for tx in txs:
            if tx.id not in self._txs:
                self._txs[tx.id] = tx
                new += 1
        return new

    def accounts(self) -> list[Account]:
        return sorted(self._accounts.values(), key=lambda a: a.id)

    def search(
        self,
        query: str | None,
        since: date | None,
        until: date | None,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
        limit: int,
    ) -> list[Transaction]:
        found = [
            t
            for t in self._txs.values()
            if _matches(t, query, since, until, min_amount, max_amount)
        ]
        found.sort(key=lambda t: t.id)
        found.sort(key=lambda t: t.posted_on, reverse=True)
        return found[: max(limit, 1)]

    def summary(self, since: date, until: date, group_by: GroupBy) -> list[SummaryRow]:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        counts: dict[str, int] = defaultdict(int)
        for tx in self._txs.values():
            if tx.amount < 0 and since <= tx.posted_on <= until:
                key = _group_key(tx, group_by)
                totals[key] += -tx.amount
                counts[key] += 1
        rows = [SummaryRow(k, totals[k], counts[k]) for k in totals]
        return sorted(rows, key=lambda r: (-r.total, r.key))

    def recurring(self, min_occurrences: int = 3) -> list[RecurringCharge]:
        groups: dict[tuple[str, Decimal], list[Transaction]] = defaultdict(list)
        for tx in self._txs.values():
            if tx.amount < 0:
                rounded = abs(tx.amount).quantize(Decimal(1))
                groups[(recurring_key(tx.description), rounded)].append(tx)
        charges = [
            _to_charge(key, group)
            for (key, _), group in groups.items()
            if len(group) >= min_occurrences
        ]
        return sorted(charges, key=lambda c: (c.last_on, c.description_key), reverse=True)

    def record_import(self, file_hash: str, filename: str, imported_on: date, count: int) -> None:
        self._imports[file_hash] = (filename, imported_on, count)

    def was_imported(self, file_hash: str) -> bool:
        return file_hash in self._imports
