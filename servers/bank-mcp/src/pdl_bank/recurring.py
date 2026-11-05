"""Pure recurring-charge detection shared by every Store implementation."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from pdl_bank.models import Transaction, normalise_description
from pdl_bank.store import ChangedCharge, RecurringCharge

_NON_LETTERS = re.compile(r"[^a-z\s]", re.ASCII)


def recurring_key(description: str) -> str:
    """Lowercased description with digits and punctuation removed.

    Falls back to the plain normalised description when nothing else remains.
    """
    normalised = normalise_description(description)
    stripped = " ".join(_NON_LETTERS.sub("", normalised).split())
    return stripped or normalised


def _round(amount: Decimal) -> Decimal:
    return abs(amount).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def _to_charge(
    key: str, group: list[Transaction], changed: tuple[ChangedCharge, ...]
) -> RecurringCharge:
    ordered = sorted(group, key=lambda t: (t.posted_on, t.id))
    dates = [t.posted_on for t in ordered]
    gaps = [(b - a).days for a, b in pairwise(dates)]
    cadence = statistics.median_low(gaps) if gaps else 0
    return RecurringCharge(key, abs(ordered[-1].amount), len(ordered), dates[-1], cadence, changed)


def _changed_after(
    last_on: date, candidates: list[Transaction], known: set[Decimal]
) -> tuple[ChangedCharge, ...]:
    """First charge per new rounded amount, dated after ``last_on``, ordered by date."""
    first: dict[Decimal, Transaction] = {}
    for t in sorted(candidates, key=lambda t: (t.posted_on, t.id)):
        rounded = _round(t.amount)
        if t.posted_on > last_on and rounded not in known:
            first.setdefault(rounded, t)
    found = sorted(first.values(), key=lambda t: (t.posted_on, t.id))
    return tuple(ChangedCharge(t.id, t.posted_on, abs(t.amount), t.description) for t in found)


def recurring_charges(
    txs: Sequence[Transaction], min_occurrences: int = 3
) -> list[RecurringCharge]:
    """Group debits by (key, amount rounded half-up to whole units).

    Each group also lists the first newer charge per amount that no recurring
    group of the same merchant key accounts for (a likely price change).
    """
    groups: dict[tuple[str, Decimal], list[Transaction]] = defaultdict(list)
    by_key: dict[str, list[Transaction]] = defaultdict(list)
    for tx in txs:
        if tx.amount < 0:
            key = recurring_key(tx.description)
            groups[(key, _round(tx.amount))].append(tx)
            by_key[key].append(tx)
    recurring = {k: g for k, g in groups.items() if len(g) >= min_occurrences}
    known: dict[str, set[Decimal]] = defaultdict(set)
    for key, rounded in recurring:
        known[key].add(rounded)
    charges = []
    for (key, _), group in recurring.items():
        last_on = max(t.posted_on for t in group)
        changed = _changed_after(last_on, by_key[key], known[key])
        charges.append(_to_charge(key, group, changed))
    return sorted(charges, key=lambda c: (c.last_on, c.description_key), reverse=True)
