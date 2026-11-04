"""Shared fixtures: a seeded in-memory store and a fixed clock."""

from datetime import date
from decimal import Decimal

import pytest

from pdl_bank.memory_store import MemoryStore
from pdl_bank.models import Account, Transaction

TODAY = date(2026, 11, 15)


def _tx(n: int, account: str, day: date, amount: str, desc: str, cat: str | None) -> Transaction:
    return Transaction(f"t{n}", account, day, Decimal(amount), desc, cat, "seed.csv")


@pytest.fixture
def today() -> date:
    return TODAY


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    s.upsert_account(Account("chk", "Everyday", "Example Bank", "USD"))
    s.upsert_account(Account("sav", "Savings", None, "USD"))
    rows = [
        ("chk", date(2026, 9, 5), "-9.99", "STREAMCO", "subscriptions"),
        ("chk", date(2026, 10, 5), "-9.99", "STREAMCO", "subscriptions"),
        ("chk", date(2026, 11, 5), "-9.99", "STREAMCO", "subscriptions"),
        ("chk", date(2026, 10, 8), "-54.20", "GREEN GROCER", "groceries"),
        ("chk", date(2026, 10, 20), "-30.30", "CORNER MARKET", "groceries"),
        ("chk", date(2026, 10, 25), "-12.00", "MYSTERY SHOP", None),
        ("chk", date(2026, 10, 28), "2500.00", "EMPLOYER PAYROLL", "income"),
        ("sav", date(2026, 10, 30), "-100.00", "TRANSFER OUT", "transfers"),
    ]
    for n, row in enumerate(rows):
        s.upsert_transactions([_tx(n, *row)])
    return s
