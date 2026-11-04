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


def transaction_id(account_id: str, posted_on: date, amount: Decimal, description: str) -> str:
    """Deterministic id so re-importing the same statement is idempotent."""
    normalised = " ".join(description.split()).lower()
    raw = f"{account_id}|{posted_on.isoformat()}|{amount:.2f}|{normalised}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
