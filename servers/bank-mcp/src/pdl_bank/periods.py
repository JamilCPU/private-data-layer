"""Natural-language period parsing.

``until`` is the period's natural end (end of month/year), even if that lies in
the future relative to ``today``; callers may clamp if they need to.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from pdl_bank.store import Period

__all__ = ["Period", "PeriodError", "resolve_period"]

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})

_YEAR_MONTH = re.compile(r"(\d{4})-(\d{2})")
_YEAR = re.compile(r"\d{4}")
_LAST_DAYS = re.compile(r"last (\d+) days?")
_MONTH_NAME = re.compile(r"([a-z]+)(?: (\d{4}))?")


class PeriodError(ValueError):
    """Raised when a period expression cannot be understood."""


def _month(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    return Period(date(year, month, 1), date(year, month, last), f"{year:04d}-{month:02d}")


def _year(year: int) -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31), f"{year:04d}")


def _named_month(name: str, year: int | None, today: date) -> Period | None:
    month = _MONTHS.get(name)
    if month is None:
        return None
    if year is None:
        year = today.year if month <= today.month else today.year - 1
    return _month(year, month)


def _previous_month(today: date) -> Period:
    first = today.replace(day=1)
    prev = first - timedelta(days=1)
    return _month(prev.year, prev.month)


def resolve_period(text: str, today: date) -> Period:
    """Resolve ``text`` to a Period relative to ``today``; raise PeriodError otherwise."""
    raw = " ".join(text.lower().split())
    result = _resolve(raw, today)
    if result is None:
        raise PeriodError(f"unrecognised period: {text!r}")
    return result


def _resolve(raw: str, today: date) -> Period | None:
    if raw == "this month":
        return _month(today.year, today.month)
    if raw == "last month":
        return _previous_month(today)
    if raw == "this year":
        return _year(today.year)
    if _YEAR.fullmatch(raw):
        return _year(int(raw))
    if m := _YEAR_MONTH.fullmatch(raw):
        month = int(m.group(2))
        return _month(int(m.group(1)), month) if 1 <= month <= 12 else None
    if m := _LAST_DAYS.fullmatch(raw):
        days = int(m.group(1))
        if days < 1:
            return None
        return Period(today - timedelta(days=days - 1), today, f"last {days} days")
    if m := _MONTH_NAME.fullmatch(raw):
        year = int(m.group(2)) if m.group(2) else None
        return _named_month(m.group(1), year, today)
    return None
