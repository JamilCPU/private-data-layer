"""Domain models for bank accounts and transactions. Money is always Decimal."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    institution: str | None
    currency: str = "USD"


@dataclass(frozen=True)
class Transaction:
    id: str
    account_id: str
    posted_on: date
    amount: Decimal
    description: str
    category: str | None
    source_file: str


def normalise_description(description: str) -> str:
    return " ".join(description.split()).lower()


def transaction_id(
    account_id: str,
    posted_on: date,
    amount: Decimal,
    description: str,
    occurrence: int = 1,
) -> str:
    """Deterministic id so re-importing the same statement is idempotent.

    Rows that are identical within one file are told apart by ``occurrence``
    (1-based count of identical rows so far). Occurrence 1 hashes exactly the
    plain key; ``occurrence >= 2`` appends ``|<occurrence>``. The result is
    deterministic and idempotent per file: the same duplicate pair re-imported
    from the same file yields the same two ids.
    """
    raw = f"{account_id}|{posted_on.isoformat()}|{amount:.2f}|{normalise_description(description)}"
    if occurrence >= 2:
        raw += f"|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
