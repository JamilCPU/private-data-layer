from datetime import date
from decimal import Decimal

import pytest

from pdl_bank.memory_store import MemoryStore
from pdl_bank.models import Account, Transaction
from pdl_bank.recurring import recurring_key
from pdl_bank.store import RecurringCharge, Store, SummaryRow


def tx(
    id_: str,
    on: date,
    amount: str,
    desc: str,
    category: str | None = None,
    account: str = "a1",
) -> Transaction:
    return Transaction(id_, account, on, Decimal(amount), desc, category, "f.csv")


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    s.upsert_account(Account("a1", "Checking", "Bank"))
    s.upsert_account(Account("a2", "Card", None))
    s.upsert_transactions(
        [
            tx("1", date(2026, 1, 5), "-50.00", "Green Grocer", "groceries"),
            tx("2", date(2026, 1, 20), "-20.50", "Cafe", "dining", "a2"),
            tx("3", date(2026, 2, 1), "2000.00", "Salary", "income"),
            tx("4", date(2026, 2, 1), "-30.00", "Mystery", None),
            tx("5", date(2026, 2, 1), "-10.00", "Green Grocer", "groceries"),
        ]
    )
    return s


def _search(store: MemoryStore, **kw: object) -> list[str]:
    args: dict[str, object] = {
        "query": None,
        "since": None,
        "until": None,
        "min_amount": None,
        "max_amount": None,
        "limit": 100,
    }
    args.update(kw)
    return [t.id for t in store.search(**args)]  # type: ignore[arg-type]


def test_satisfies_protocol() -> None:
    store: Store = MemoryStore()
    assert store.accounts() == []


def test_upsert_idempotent() -> None:
    s = MemoryStore()
    batch = [tx("1", date(2026, 1, 1), "-1", "x"), tx("2", date(2026, 1, 2), "-2", "y")]
    assert s.upsert_transactions(batch) == 2
    assert s.upsert_transactions(batch) == 0
    assert s.upsert_transactions([*batch, tx("3", date(2026, 1, 3), "-3", "z")]) == 1


def test_accounts_sorted_and_upserted(store: MemoryStore) -> None:
    store.upsert_account(Account("a1", "Renamed", "Bank"))
    assert [(a.id, a.name) for a in store.accounts()] == [("a1", "Renamed"), ("a2", "Card")]


def test_search_order_and_ties(store: MemoryStore) -> None:
    # 3, 4, 5 share a date; ties break by id (ascending), newest date first
    assert _search(store) == ["3", "4", "5", "2", "1"]


def test_search_query_matches_description_or_category(store: MemoryStore) -> None:
    assert _search(store, query="GROCER") == ["5", "1"]
    assert _search(store, query="dining") == ["2"]
    assert _search(store, query="nomatch") == []


def test_search_dates_inclusive(store: MemoryStore) -> None:
    assert _search(store, since=date(2026, 1, 20), until=date(2026, 2, 1)) == [
        "3",
        "4",
        "5",
        "2",
    ]
    assert _search(store, until=date(2026, 1, 5)) == ["1"]


def test_search_amount_bounds_signed(store: MemoryStore) -> None:
    assert _search(store, min_amount=Decimal(-30)) == ["3", "4", "5", "2"]
    assert _search(store, max_amount=Decimal(-30)) == ["4", "1"]
    assert _search(store, min_amount=Decimal(0)) == ["3"]


def test_search_limit(store: MemoryStore) -> None:
    assert _search(store, limit=2) == ["3", "4"]


def test_search_limit_zero_clamps_to_one(store: MemoryStore) -> None:
    assert len(_search(store, limit=0)) == 1


def test_reupsert_does_not_overwrite(store: MemoryStore) -> None:
    changed = tx("1", date(2026, 1, 5), "-999.00", "Changed")
    assert store.upsert_transactions([changed]) == 0
    assert store.search("Green", None, None, None, None, 100)[-1].amount == Decimal("-50.00")


def test_duplicate_id_in_batch_counts_once() -> None:
    s = MemoryStore()
    dup = [tx("1", date(2026, 1, 1), "-1", "x"), tx("1", date(2026, 1, 1), "-1", "x")]
    assert s.upsert_transactions(dup) == 1


def test_summary_by_category(store: MemoryStore) -> None:
    rows = store.summary(date(2026, 1, 1), date(2026, 2, 28), "category")
    assert rows == [
        SummaryRow("groceries", Decimal("60.00"), 2),
        SummaryRow("uncategorized", Decimal("30.00"), 1),
        SummaryRow("dining", Decimal("20.50"), 1),
    ]


def test_summary_by_month_and_account(store: MemoryStore) -> None:
    by_month = store.summary(date(2026, 1, 1), date(2026, 2, 28), "month")
    assert by_month == [
        SummaryRow("2026-01", Decimal("70.50"), 2),
        SummaryRow("2026-02", Decimal("40.00"), 2),
    ]
    by_acc = store.summary(date(2026, 1, 1), date(2026, 2, 28), "account")
    assert by_acc == [SummaryRow("a1", Decimal("90.00"), 3), SummaryRow("a2", Decimal("20.50"), 1)]


def test_summary_range_inclusive_and_excludes_income(store: MemoryStore) -> None:
    rows = store.summary(date(2026, 2, 1), date(2026, 2, 1), "category")
    assert sum((r.total for r in rows), Decimal(0)) == Decimal("40.00")
    assert store.summary(date(2025, 1, 1), date(2025, 1, 31), "month") == []


def test_summary_tie_sorted_by_key() -> None:
    s = MemoryStore()
    s.upsert_transactions(
        [
            tx("1", date(2026, 1, 1), "-5", "a", "zeta"),
            tx("2", date(2026, 1, 1), "-5", "b", "alpha"),
        ]
    )
    keys = [r.key for r in s.summary(date(2026, 1, 1), date(2026, 1, 31), "category")]
    assert keys == ["alpha", "zeta"]


def test_recurring_detects_monthly_netflix() -> None:
    s = MemoryStore()
    s.upsert_transactions(
        [
            tx("n1", date(2026, 1, 3), "-15.49", "NETFLIX 1234"),
            tx("n2", date(2026, 2, 3), "-15.49", "Netflix"),
            tx("n3", date(2026, 3, 5), "-15.49", "NETFLIX #99"),
            tx("i1", date(2026, 1, 1), "15.49", "Netflix refund"),
        ]
    )
    assert s.recurring() == [RecurringCharge("netflix", Decimal("15.49"), 3, date(2026, 3, 5), 30)]


def test_recurring_ignores_irregular_and_short_groups() -> None:
    s = MemoryStore()
    s.upsert_transactions(
        [
            tx("1", date(2026, 1, 3), "-12.99", "Shop"),
            tx("2", date(2026, 2, 3), "-45.10", "Shop"),
            tx("3", date(2026, 3, 3), "-80.00", "Shop"),
            tx("4", date(2026, 1, 3), "-9.00", "Gym"),
            tx("5", date(2026, 2, 3), "-9.00", "Gym"),
        ]
    )
    assert s.recurring() == []
    assert [r.description_key for r in s.recurring(min_occurrences=2)] == ["gym"]


def test_recurring_sorted_by_last_on_desc() -> None:
    s = MemoryStore()
    for i, d in enumerate([date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]):
        s.upsert_transactions([tx(f"a{i}", d, "-10", "alpha")])
    for i, d in enumerate([date(2026, 1, 9), date(2026, 2, 9), date(2026, 3, 9)]):
        s.upsert_transactions([tx(f"b{i}", d, "-20", "beta")])
    assert [r.description_key for r in s.recurring()] == ["beta", "alpha"]


def test_import_ledger() -> None:
    s = MemoryStore()
    assert not s.was_imported("h1")
    s.record_import("h1", "a.csv", date(2026, 4, 1), 5)
    assert s.was_imported("h1")
    assert not s.was_imported("h2")


def test_recurring_rounds_half_up() -> None:
    s = MemoryStore()
    s.upsert_transactions(
        [
            tx("a", date(2026, 1, 1), "-12.50", "Svc"),
            tx("b", date(2026, 2, 1), "-13.49", "Svc"),
            tx("c", date(2026, 3, 1), "-12.49", "Svc"),
        ]
    )
    assert s.recurring(min_occurrences=2) == [
        RecurringCharge("svc", Decimal("13.49"), 2, date(2026, 2, 1), 31)
    ]


def test_recurring_key_falls_back_when_empty() -> None:
    assert recurring_key("  12345  ") == "12345"
    s = MemoryStore()
    s.upsert_transactions([tx(f"n{i}", date(2026, i, 1), "-10", "9999") for i in (1, 2, 3)])
    assert [r.description_key for r in s.recurring()] == ["9999"]


def test_cadence_single_gap_and_median_of_three() -> None:
    s = MemoryStore()
    s.upsert_transactions(
        [tx("a", date(2026, 1, 1), "-5", "A"), tx("b", date(2026, 1, 8), "-5", "A")]
    )
    assert s.recurring(min_occurrences=2)[0].cadence_days == 7
    s2 = MemoryStore()
    days = [date(2026, 1, 1), date(2026, 1, 11), date(2026, 1, 14), date(2026, 2, 13)]
    s2.upsert_transactions([tx(f"x{i}", d, "-5", "B") for i, d in enumerate(days)])
    assert s2.recurring()[0].cadence_days == 10  # gaps 10, 3, 30 -> median 10
