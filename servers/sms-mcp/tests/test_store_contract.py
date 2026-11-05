"""Run the shared store cases against every available Store implementation."""

import pytest

from .store_cases import CASES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__.removeprefix("case_"))
def test_store_contract(empty_store, case):
    case(empty_store)
