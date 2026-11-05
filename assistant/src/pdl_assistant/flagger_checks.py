"""Deterministic checks for the hourly flagger: tool results in, ``Flag`` values out.

No model is involved. Every check calls gateway tools with exact arguments and
compares the returned rows against the owner's thresholds.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pdl_assistant.flagger_config import Check, EmailFrom, RecurringChange, TransactionOver
from pdl_assistant.gateway_client import ToolResult

log = logging.getLogger(__name__)

MAX_LINE_CHARS = 160
MAX_RECURRING = 25
_TX_LIMIT = 200
_LOOKUP_LIMIT = 20
_ELLIPSIS = "…"


class CheckError(Exception):
    """A check could not run: tool error or malformed payload."""


@dataclass(frozen=True)
class Flag:
    check: str
    key: str
    summary: str


class _Gateway(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


def _rows(result: ToolResult, tool: str) -> list[dict[str, Any]]:
    if result.is_error:
        raise CheckError(f"{tool} returned an error")
    if result.structured is None:
        raise CheckError(f"{tool} returned no structured result")
    rows = result.structured.get("result")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise CheckError(f"{tool} returned an unexpected result shape")
    return rows


def _decimal(row: dict[str, Any], field: str, tool: str) -> Decimal:
    try:
        value = Decimal(str(row.get(field)))
    except InvalidOperation:
        raise CheckError(f"{tool} row has a non-numeric {field}") from None
    if not value.is_finite():
        raise CheckError(f"{tool} row has a non-finite {field}")
    return value


def _amount(row: dict[str, Any]) -> Decimal:
    return _decimal(row, "amount", "row")


def _line(text: str) -> str:
    one = " ".join(text.split())
    if len(one) <= MAX_LINE_CHARS:
        return one
    return one[: MAX_LINE_CHARS - 1] + _ELLIPSIS


def _tx_key(row: dict[str, Any]) -> str:
    if row.get("id"):
        return str(row["id"])
    parts = (row.get(k) for k in ("posted_on", "amount", "description", "account"))
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _tx_flag(name: str, row: dict[str, Any]) -> Flag:
    summary = (
        f"{name}: {row.get('amount')} {row.get('description')} "
        f"on {row.get('posted_on')} ({row.get('account')})"
    )
    return Flag(name, _tx_key(row), _line(summary))


async def _transaction_over(
    check: TransactionOver, gateway: _Gateway, today: date, lookback_days: int
) -> list[Flag]:
    since = (today - timedelta(days=lookback_days)).isoformat()
    calls = [{"since": since, "max_amount": str(-check.amount), "limit": _TX_LIMIT}]
    if check.direction == "any":
        calls.append({"since": since, "min_amount": str(check.amount), "limit": _TX_LIMIT})
    flags: dict[str, Flag] = {}
    for arguments in calls:
        tool = "bank_search_transactions"
        for row in _rows(await gateway.call_tool(tool, arguments), tool):
            amount = _amount(row)
            if abs(amount) <= check.amount:
                continue
            if check.direction == "spend" and amount >= 0:
                continue
            flag = _tx_flag(check.name, row)
            flags.setdefault(flag.key, flag)
    return list(flags.values())


def _recurring_targets(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, Decimal]]:
    """(description, last_on, baseline text, baseline) for usable recurring rows."""
    targets = []
    for row in rows[:MAX_RECURRING]:
        description = str(row.get("description") or "").strip()
        last_on = row.get("last_on")
        if not description or not last_on:
            continue
        baseline = _decimal(row, "amount", "bank_recurring_charges")
        if baseline <= 0:
            continue
        targets.append((description, str(last_on), str(row["amount"]), baseline))
    return targets


def _changed(row_amount: Decimal, baseline: Decimal, percent: Decimal) -> bool:
    return abs(abs(row_amount) - baseline) / baseline * 100 > percent


async def _recurring_change(check: RecurringChange, gateway: _Gateway) -> list[Flag]:
    tool = "bank_recurring_charges"
    recurring = _rows(await gateway.call_tool(tool, {}), tool)
    flags: dict[str, Flag] = {}
    search = "bank_search_transactions"
    for description, last_on, baseline_text, baseline in _recurring_targets(recurring):
        arguments = {"query": description, "since": last_on, "limit": _LOOKUP_LIMIT}
        for row in _rows(await gateway.call_tool(search, arguments), search):
            if str(row.get("posted_on")) <= last_on:
                continue
            amount = _amount(row)
            if amount >= 0 or not _changed(amount, baseline, check.percent):
                continue
            summary = (
                f"{check.name}: {description} charged {row.get('amount')} "
                f"on {row.get('posted_on')}, usually {baseline_text}"
            )
            key = _tx_key(row)
            flags.setdefault(key, Flag(check.name, key, _line(summary)))
    return list(flags.values())


def _email_flag(name: str, row: dict[str, Any]) -> Flag:
    summary = f"{name}: email from {row['from']} — {row.get('subject')} ({row.get('date')})"
    return Flag(name, str(row["id"]), _line(summary))


async def _email_from(
    check: EmailFrom, gateway: _Gateway, today: date, lookback_days: int
) -> list[Flag]:
    start = today - timedelta(days=lookback_days)
    since = datetime(start.year, start.month, start.day, tzinfo=UTC).isoformat()
    tool = "email_search_email"
    flags: dict[str, Flag] = {}
    for sender in check.senders:
        arguments = {"query": sender, "since": since, "limit": _LOOKUP_LIMIT}
        for row in _rows(await gateway.call_tool(tool, arguments), tool):
            if not row.get("id") or row.get("from") is None:
                raise CheckError(f"{tool} row is missing id or from")
            if sender in str(row["from"]).lower():
                flag = _email_flag(check.name, row)
                flags.setdefault(flag.key, flag)
    return list(flags.values())


async def evaluate(
    check: Check, gateway: _Gateway, *, today: date, lookback_days: int
) -> list[Flag]:
    """Run one check; raises ``CheckError`` for a tool error or malformed payload."""
    if isinstance(check, TransactionOver):
        return await _transaction_over(check, gateway, today, lookback_days)
    if isinstance(check, RecurringChange):
        return await _recurring_change(check, gateway)
    return await _email_from(check, gateway, today, lookback_days)


async def evaluate_all(
    checks: Sequence[Check], gateway: _Gateway, *, today: date, lookback_days: int
) -> tuple[list[Flag], list[str]]:
    """Run every check in order; a failing check is logged and named, never fatal."""
    flags: dict[tuple[str, str], Flag] = {}
    failed: list[str] = []
    for check in checks:
        try:
            found = await evaluate(check, gateway, today=today, lookback_days=lookback_days)
        except Exception as exc:  # noqa: BLE001 - one broken check must not stop the others
            log.warning("check %s failed: %s", check.name, type(exc).__name__)
            failed.append(check.name)
            continue
        for flag in found:
            flags.setdefault((flag.check, flag.key), flag)
    return list(flags.values()), failed
