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


def _rows(result: ToolResult, tool: str, limit: int | None = None) -> list[dict[str, Any]]:
    if result.is_error:
        raise CheckError(f"{tool} returned an error")
    if not isinstance(result.structured, dict):
        raise CheckError(f"{tool} returned no structured result")
    rows = result.structured.get("result")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise CheckError(f"{tool} returned an unexpected result shape")
    if limit is not None and len(rows) == limit:
        log.info("%s returned %d rows, the requested limit: result may be truncated", tool, limit)
    return rows


def _decimal(row: dict[str, Any], field: str, tool: str) -> Decimal:
    try:
        value = Decimal(str(row.get(field)))
    except InvalidOperation:
        raise CheckError(f"{tool} row has a non-numeric {field}") from None
    if not value.is_finite():
        raise CheckError(f"{tool} row has a non-finite {field}")
    return value


def _amount(row: dict[str, Any], tool: str) -> Decimal:
    return _decimal(row, "amount", tool)


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
        for row in _rows(await gateway.call_tool(tool, arguments), tool, _TX_LIMIT):
            if not row.get("posted_on"):
                raise CheckError(f"{tool} row is missing posted_on")
            amount = _amount(row, tool)
            if abs(amount) <= check.amount:
                continue
            if check.direction == "spend" and amount >= 0:
                continue
            flag = _tx_flag(check.name, row)
            flags.setdefault(flag.key, flag)
    return list(flags.values())


def _changed_list(row: dict[str, Any]) -> list[dict[str, Any]]:
    changed = row.get("changed")
    if changed is None:
        return []
    if not isinstance(changed, list) or not all(isinstance(c, dict) for c in changed):
        raise CheckError("bank_recurring_charges row has a malformed changed list")
    return changed


def _changed_flag(
    check: RecurringChange, change: dict[str, Any], baseline_text: str, baseline: Decimal
) -> Flag | None:
    tool = "bank_recurring_charges"
    if not change.get("id") or not change.get("posted_on"):
        raise CheckError(f"{tool} changed entry is missing id or posted_on")
    amount = _decimal(change, "amount", tool)
    if abs(amount - baseline) / baseline * 100 <= check.percent:
        return None
    summary = (
        f"{check.name}: {change.get('description')} charged {change.get('amount')} "
        f"on {change['posted_on']}, usually {baseline_text}"
    )
    return Flag(check.name, str(change["id"]), _line(summary))


async def _recurring_change(
    check: RecurringChange, gateway: _Gateway, today: date, lookback_days: int
) -> list[Flag]:
    tool = "bank_recurring_charges"
    window_start = (today - timedelta(days=lookback_days)).isoformat()
    flags: dict[str, Flag] = {}
    for row in _rows(await gateway.call_tool(tool, {}), tool)[:MAX_RECURRING]:
        baseline = _decimal(row, "amount", tool)
        if baseline <= 0:
            continue
        for change in _changed_list(row):
            if str(change.get("posted_on")) < window_start:
                continue
            flag = _changed_flag(check, change, str(row["amount"]), baseline)
            if flag is not None:
                flags.setdefault(flag.key, flag)
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
        for row in _rows(await gateway.call_tool(tool, arguments), tool, _LOOKUP_LIMIT):
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
        return await _recurring_change(check, gateway, today, lookback_days)
    if isinstance(check, EmailFrom):
        return await _email_from(check, gateway, today, lookback_days)
    raise CheckError(f"unsupported check type {type(check).__name__}")


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
