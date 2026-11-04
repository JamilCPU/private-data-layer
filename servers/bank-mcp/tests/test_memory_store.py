"""Shared Store behaviour cases run against MemoryStore."""

import pytest

from pdl_bank.memory_store import MemoryStore
from pdl_bank.store import Store
from tests.store_cases import *
from tests.store_cases import seed, seed_accounts


@pytest.fixture
def empty_store() -> MemoryStore:
    return seed_accounts(MemoryStore())  # type: ignore[return-value]


@pytest.fixture
def store() -> MemoryStore:
    return seed(MemoryStore())  # type: ignore[return-value]


def test_satisfies_protocol() -> None:
    store: Store = MemoryStore()
    assert store.accounts() == []
