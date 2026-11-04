"""Pure recurring-charge detection shared by every Store implementation."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from pdl_bank.models import Transaction, normalise_description
from pdl_bank.store import RecurringCharge

_NON_LETTERS = re.compile(r"[^a-z\s]", re.ASCII)


def recurring_key(description: str) -> str:
    """Lowercased description with digits and punctuation removed.

    Falls back to the plain normalised description when nothing else remains.
    """
    normalised = normalise_description(description)
    stripped = " ".join(_NON_LETTERS.sub("", normalised).split())
    return stripped or normalised


def _to_charge(key: str, group: list[Transaction]) -> RecurringCharge:
    ordered = sorted(group, key=lambda t: (t.posted_on, t.id))
    dates = [t.posted_on for t in ordered]
    gaps = [(b - a).days for a, b in pairwise(dates)]
    cadence = statistics.median_low(gaps) if gaps else 0
    return RecurringCharge(key, abs(ordered[-1].amount), len(ordered), dates[-1], cadence)


def recurring_charges(
    txs: Sequence[Transaction], min_occurrences: int = 3
) -> list[RecurringCharge]:
    """Group debits by (key, amount rounded half-up to whole units)."""
    groups: dict[tuple[str, Decimal], list[Transaction]] = defaultdict(list)
    for tx in txs:
        if tx.amount < 0:
            rounded = abs(tx.amount).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            groups[(recurring_key(tx.description), rounded)].append(tx)
    charges = [
        _to_charge(key, group)
        for (key, _), group in groups.items()
        if len(group) >= min_occurrences
    ]
    return sorted(charges, key=lambda c: (c.last_on, c.description_key), reverse=True)
