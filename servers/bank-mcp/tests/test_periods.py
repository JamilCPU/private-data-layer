from datetime import date

import pytest

from pdl_bank.periods import Period, PeriodError, resolve_period

TODAY = date(2026, 4, 15)


@pytest.mark.parametrize(
    ("text", "since", "until", "label"),
    [
        ("2026-03", date(2026, 3, 1), date(2026, 3, 31), "2026-03"),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29), "2024-02"),
        ("  March ", date(2026, 3, 1), date(2026, 3, 31), "2026-03"),
        ("march 2025", date(2025, 3, 1), date(2025, 3, 31), "2025-03"),
        ("MAR", date(2026, 3, 1), date(2026, 3, 31), "2026-03"),
        ("april", date(2026, 4, 1), date(2026, 4, 30), "2026-04"),
        ("may", date(2025, 5, 1), date(2025, 5, 31), "2025-05"),
        ("december", date(2025, 12, 1), date(2025, 12, 31), "2025-12"),
        ("last 30 days", date(2026, 3, 17), TODAY, "last 30 days"),
        ("Last 1 days", TODAY, TODAY, "last 1 days"),
        ("this month", date(2026, 4, 1), date(2026, 4, 30), "2026-04"),
        ("last month", date(2026, 3, 1), date(2026, 3, 31), "2026-03"),
        ("this year", date(2026, 1, 1), date(2026, 12, 31), "2026"),
        ("2025", date(2025, 1, 1), date(2025, 12, 31), "2025"),
    ],
)
def test_resolve_period(text: str, since: date, until: date, label: str) -> None:
    assert resolve_period(text, TODAY) == Period(since, until, label)


def test_last_month_wraps_year() -> None:
    p = resolve_period("last month", date(2026, 1, 10))
    assert (p.since, p.until) == (date(2025, 12, 1), date(2025, 12, 31))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "soon",
        "2026-13",
        "last 0 days",
        "last x days",
        "smarch",
        "march abc",
        "12345",
        "0000",
        "0000-05",
        "march 0000",
        "last 800000 days",
        "٢٠٢٦",
    ],
)
def test_resolve_period_errors(text: str) -> None:
    with pytest.raises(PeriodError, match="unrecognised period"):
        resolve_period(text, TODAY)
