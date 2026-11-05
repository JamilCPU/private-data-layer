"""Pure tests for recurring-charge price-change detection."""

from datetime import date
from decimal import Decimal

from pdl_bank.recurring import recurring_charges
from pdl_bank.store import ChangedCharge
from tests.store_cases import tx


def monthly(prefix: str, amount: str, desc: str, months: range) -> list:
    return [tx(f"{prefix}{m}", date(2026, m, 5), amount, desc) for m in months]


def test_price_rise_reports_first_newer_charge() -> None:
    txs = monthly("a", "-10.00", "STREAMCO", range(1, 5)) + [
        tx("b1", date(2026, 5, 5), "-12.50", "STREAMCO")
    ]
    [charge] = recurring_charges(txs)
    assert charge.changed == (ChangedCharge("b1", date(2026, 5, 5), Decimal("12.50"), "STREAMCO"),)


def test_later_charges_at_new_price_are_not_repeated() -> None:
    txs = monthly("a", "-10.00", "STREAMCO", range(1, 5)) + [
        tx("b1", date(2026, 5, 5), "-12.50", "STREAMCO"),
        tx("b2", date(2026, 6, 5), "-12.50", "STREAMCO"),
    ]
    [charge] = recurring_charges(txs)
    assert [c.id for c in charge.changed] == ["b1"]


def test_new_price_with_three_occurrences_is_its_own_group() -> None:
    txs = monthly("a", "-10.00", "STREAMCO", range(1, 5)) + [
        tx(f"b{m}", date(2026, m, 5), "-12.50", "STREAMCO") for m in (5, 6, 7)
    ]
    by_amount = {c.amount: c for c in recurring_charges(txs)}
    assert by_amount[Decimal("10.00")].changed == ()
    assert by_amount[Decimal("12.50")].changed == ()


def test_two_subscriptions_one_merchant_report_nothing() -> None:
    txs = monthly("a", "-10.00", "STREAMCO", range(1, 5)) + monthly(
        "b", "-15.00", "STREAMCO", range(1, 5)
    )
    assert all(c.changed == () for c in recurring_charges(txs))


def test_raw_description_with_stripped_characters_matches() -> None:
    txs = monthly("a", "-10.00", "NETFLIX.COM", range(1, 4)) + [
        tx("b1", date(2026, 4, 5), "-20.00", "Netflix.com 123")
    ]
    [charge] = recurring_charges(txs)
    assert charge.description_key == "netflixcom"
    assert [(c.id, c.description) for c in charge.changed] == [("b1", "Netflix.com 123")]


def test_income_other_merchants_and_old_charges_ignored() -> None:
    txs = monthly("a", "-10.00", "STREAMCO", range(2, 5)) + [
        tx("old", date(2026, 1, 1), "-99.00", "STREAMCO"),
        tx("on", date(2026, 4, 5), "-77.00", "STREAMCO"),
        tx("inc", date(2026, 5, 1), "50.00", "STREAMCO"),
        tx("other", date(2026, 5, 1), "-50.00", "OTHERCO"),
    ]
    [charge] = recurring_charges(txs)
    assert charge.changed == ()


def test_each_new_amount_reported_once_in_date_order() -> None:
    txs = monthly("a", "-10.00", "STREAMCO", range(1, 4)) + [
        tx("c2", date(2026, 6, 5), "-14.00", "STREAMCO"),
        tx("b1", date(2026, 4, 5), "-12.00", "STREAMCO"),
        tx("b2", date(2026, 5, 5), "-12.00", "STREAMCO"),
        tx("c3", date(2026, 7, 5), "-14.00", "STREAMCO"),
    ]
    [charge] = recurring_charges(txs)
    assert [c.id for c in charge.changed] == ["b1", "c2"]
