"""Behavioural Store cases shared by the in-memory and Postgres test modules.

Each test module imports these functions and provides the ``store`` (seeded) and
``empty_store`` fixtures for its own Store implementation.
"""

from datetime import date
from decimal import Decimal

from pdl_bank.models import Account, Transaction
from pdl_bank.recurring import recurring_key
from pdl_bank.store import ChangedCharge, RecurringCharge, Store, SummaryRow


def tx(
    id_: str,
    on: date,
    amount: str,
    desc: str,
    category: str | None = None,
    account: str = "a1",
) -> Transaction:
    return Transaction(id_, account, on, Decimal(amount), desc, category, "f.csv")


def seed_accounts(s: Store) -> Store:
    s.upsert_account(Account("a1", "Checking", "Bank"))
    s.upsert_account(Account("a2", "Card", None))
    return s


def seed(s: Store) -> Store:
    seed_accounts(s)
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


def _search(store: Store, **kw: object) -> list[str]:
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


def test_upsert_idempotent(empty_store: Store) -> None:
    s = empty_store
    batch = [tx("1", date(2026, 1, 1), "-1", "x"), tx("2", date(2026, 1, 2), "-2", "y")]
    assert s.upsert_transactions(batch) == 2
    assert s.upsert_transactions(batch) == 0
    assert s.upsert_transactions([*batch, tx("3", date(2026, 1, 3), "-3", "z")]) == 1


def test_accounts_ordered_by_name_and_upserted(store: Store) -> None:
    store.upsert_account(Account("a1", "Renamed", "Bank"))
    assert [(a.id, a.name) for a in store.accounts()] == [("a2", "Card"), ("a1", "Renamed")]


def test_search_order_and_ties(store: Store) -> None:
    # 3, 4, 5 share a date; ties break by id (ascending), newest date first
    assert _search(store) == ["3", "4", "5", "2", "1"]


def test_search_query_matches_description_or_category(store: Store) -> None:
    assert _search(store, query="GROCER") == ["5", "1"]
    assert _search(store, query="dining") == ["2"]
    assert _search(store, query="nomatch") == []


def test_search_dates_inclusive(store: Store) -> None:
    assert _search(store, since=date(2026, 1, 20), until=date(2026, 2, 1)) == [
        "3",
        "4",
        "5",
        "2",
    ]
    assert _search(store, until=date(2026, 1, 5)) == ["1"]


def test_search_amount_bounds_signed(store: Store) -> None:
    assert _search(store, min_amount=Decimal(-30)) == ["3", "4", "5", "2"]
    assert _search(store, max_amount=Decimal(-30)) == ["4", "1"]
    assert _search(store, min_amount=Decimal(0)) == ["3"]


def test_search_limit(store: Store) -> None:
    assert _search(store, limit=2) == ["3", "4"]


def test_search_limit_zero_clamps_to_one(store: Store) -> None:
    assert len(_search(store, limit=0)) == 1


def test_reupsert_does_not_overwrite(store: Store) -> None:
    changed = tx("1", date(2026, 1, 5), "-999.00", "Changed")
    assert store.upsert_transactions([changed]) == 0
    assert store.search("Green", None, None, None, None, 100)[-1].amount == Decimal("-50.00")


def test_duplicate_id_in_batch_counts_once(empty_store: Store) -> None:
    s = empty_store
    dup = [tx("1", date(2026, 1, 1), "-1", "x"), tx("1", date(2026, 1, 1), "-1", "x")]
    assert s.upsert_transactions(dup) == 1


def test_summary_by_category(store: Store) -> None:
    rows = store.summary(date(2026, 1, 1), date(2026, 2, 28), "category")
    assert rows == [
        SummaryRow("groceries", Decimal("60.00"), 2),
        SummaryRow("uncategorized", Decimal("30.00"), 1),
        SummaryRow("dining", Decimal("20.50"), 1),
    ]


def test_summary_by_month_and_account(store: Store) -> None:
    by_month = store.summary(date(2026, 1, 1), date(2026, 2, 28), "month")
    assert by_month == [
        SummaryRow("2026-01", Decimal("70.50"), 2),
        SummaryRow("2026-02", Decimal("40.00"), 2),
    ]
    by_acc = store.summary(date(2026, 1, 1), date(2026, 2, 28), "account")
    assert by_acc == [SummaryRow("a1", Decimal("90.00"), 3), SummaryRow("a2", Decimal("20.50"), 1)]


def test_summary_range_inclusive_and_excludes_income(store: Store) -> None:
    rows = store.summary(date(2026, 2, 1), date(2026, 2, 1), "category")
    assert sum((r.total for r in rows), Decimal(0)) == Decimal("40.00")
    assert store.summary(date(2025, 1, 1), date(2025, 1, 31), "month") == []


def test_summary_tie_sorted_by_key(empty_store: Store) -> None:
    s = empty_store
    s.upsert_transactions(
        [
            tx("1", date(2026, 1, 1), "-5", "a", "zeta"),
            tx("2", date(2026, 1, 1), "-5", "b", "alpha"),
        ]
    )
    keys = [r.key for r in s.summary(date(2026, 1, 1), date(2026, 1, 31), "category")]
    assert keys == ["alpha", "zeta"]


def test_recurring_detects_monthly_netflix(empty_store: Store) -> None:
    s = empty_store
    s.upsert_transactions(
        [
            tx("n1", date(2026, 1, 3), "-15.49", "NETFLIX 1234"),
            tx("n2", date(2026, 2, 3), "-15.49", "Netflix"),
            tx("n3", date(2026, 3, 5), "-15.49", "NETFLIX #99"),
            tx("i1", date(2026, 1, 1), "15.49", "Netflix refund"),
        ]
    )
    assert s.recurring() == [RecurringCharge("netflix", Decimal("15.49"), 3, date(2026, 3, 5), 30)]


def test_recurring_ignores_irregular_and_short_groups(empty_store: Store) -> None:
    s = empty_store
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


def test_recurring_sorted_by_last_on_desc(empty_store: Store) -> None:
    s = empty_store
    for i, d in enumerate([date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]):
        s.upsert_transactions([tx(f"a{i}", d, "-10", "alpha")])
    for i, d in enumerate([date(2026, 1, 9), date(2026, 2, 9), date(2026, 3, 9)]):
        s.upsert_transactions([tx(f"b{i}", d, "-20", "beta")])
    assert [r.description_key for r in s.recurring()] == ["beta", "alpha"]


def test_import_ledger(empty_store: Store) -> None:
    s = empty_store
    assert not s.was_imported("h1")
    s.record_import("h1", "a.csv", date(2026, 4, 1), 5)
    assert s.was_imported("h1")
    assert not s.was_imported("h2")


def test_recurring_rounds_half_up(empty_store: Store) -> None:
    s = empty_store
    s.upsert_transactions(
        [
            tx("a", date(2026, 1, 1), "-12.50", "Svc"),
            tx("b", date(2026, 2, 1), "-13.49", "Svc"),
            tx("c", date(2026, 3, 1), "-12.49", "Svc"),
        ]
    )
    assert s.recurring(min_occurrences=2) == [
        RecurringCharge(
            "svc",
            Decimal("13.49"),
            2,
            date(2026, 2, 1),
            31,
            (ChangedCharge("c", date(2026, 3, 1), Decimal("12.49"), "Svc"),),
        )
    ]


def test_recurring_key_falls_back_when_empty(empty_store: Store) -> None:
    assert recurring_key("  12345  ") == "12345"
    s = empty_store
    s.upsert_transactions([tx(f"n{i}", date(2026, i, 1), "-10", "9999") for i in (1, 2, 3)])
    assert [r.description_key for r in s.recurring()] == ["9999"]


def test_cadence_single_gap(empty_store: Store) -> None:
    empty_store.upsert_transactions(
        [tx("a", date(2026, 1, 1), "-5", "A"), tx("b", date(2026, 1, 8), "-5", "A")]
    )
    assert empty_store.recurring(min_occurrences=2)[0].cadence_days == 7


def test_cadence_median_of_three_gaps(empty_store: Store) -> None:
    days = [date(2026, 1, 1), date(2026, 1, 11), date(2026, 1, 14), date(2026, 2, 13)]
    empty_store.upsert_transactions([tx(f"x{i}", d, "-5", "B") for i, d in enumerate(days)])
    assert empty_store.recurring()[0].cadence_days == 10  # gaps 10, 3, 30 -> median 10


def test_search_query_treats_wildcards_literally(empty_store: Store) -> None:
    empty_store.upsert_transactions(
        [
            tx("1", date(2026, 1, 1), "-1", "100% Juice"),
            tx("2", date(2026, 1, 2), "-2", "Pay_Pal"),
            tx("3", date(2026, 1, 3), "-3", "PayXPal"),
            tx("4", date(2026, 1, 4), "-4", r"back\slash"),
        ]
    )
    assert _search(empty_store, query="%") == ["1"]
    assert _search(empty_store, query="y_p") == ["2"]
    assert _search(empty_store, query="\\") == ["4"]


def test_amounts_keep_exact_decimal_precision(empty_store: Store) -> None:
    empty_store.upsert_transactions(
        [tx("1", date(2026, 1, 1), "-0.10", "a"), tx("2", date(2026, 1, 2), "-0.20", "b")]
    )
    found = empty_store.search(None, None, None, None, None, 10)
    assert all(isinstance(t.amount, Decimal) for t in found)
    rows = empty_store.summary(date(2026, 1, 1), date(2026, 1, 31), "month")
    assert rows == [SummaryRow("2026-01", Decimal("0.30"), 2)]


def test_upsert_returns_exact_new_count_for_mixed_batch(store: Store) -> None:
    batch = [
        tx("1", date(2026, 1, 5), "-50.00", "Green Grocer"),
        tx("n1", date(2026, 3, 1), "-1", "new one"),
        tx("n2", date(2026, 3, 2), "-2", "new two"),
    ]
    assert store.upsert_transactions(batch) == 2
    assert store.upsert_transactions([]) == 0


def test_transaction_fields_round_trip(store: Store) -> None:
    (found,) = store.search("Cafe", None, None, None, None, 5)
    assert found == tx("2", date(2026, 1, 20), "-20.50", "Cafe", "dining", "a2")
