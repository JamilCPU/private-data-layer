"""The bank MCP server: read-only tools over the statement store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pdl_bank.models import Account, Transaction
from pdl_bank.periods import PeriodError, resolve_period
from pdl_bank.store import GroupBy, Store

INSTRUCTIONS = (
    "Read-only view of the owner's bank statements. "
    "Spending summaries, individual transactions, accounts and recurring charges."
)
SUMMARY_DESCRIPTION = (
    "Total spending for a period, grouped by category, month or account. "
    "Use it for questions like what did I spend on food in October. "
    "period accepts 2026-10, October, last 30 days, this month, last month."
)
SEARCH_DESCRIPTION = (
    "Find individual transactions by words in the description or category, date range or "
    "amount range. Use it to list specific purchases or check whether a payment happened."
)
ACCOUNTS_DESCRIPTION = "The bank accounts that statements have been imported for."
RECURRING_DESCRIPTION = (
    "Charges that repeat with a similar amount, such as subscriptions and rent, "
    "with how often and when they last occurred."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
Limit = Annotated[int, Field(ge=1, le=200)]


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _parse_date(name: str, value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


def _parse_amount(name: str, value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} must be a decimal number, got {value!r}") from None
    if not amount.is_finite():
        raise ValueError(f"{name} must be a finite decimal number, got {value!r}")
    return amount


def _transaction_row(tx: Transaction, names: dict[str, str]) -> dict[str, Any]:
    return {
        "posted_on": tx.posted_on.isoformat(),
        "amount": _money(tx.amount),
        "description": tx.description,
        "category": tx.category,
        "account": names.get(tx.account_id, tx.account_id),
    }


def _account_row(account: Account) -> dict[str, Any]:
    return {
        "id": account.id,
        "name": account.name,
        "institution": account.institution,
        "currency": account.currency,
    }


def build_server(
    store: Store,
    *,
    today: Callable[[], date] = date.today,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> FastMCP:
    mcp = FastMCP(
        "bank",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @mcp.tool(description=SUMMARY_DESCRIPTION, annotations=READ_ONLY)
    def spend_summary(
        period: str = "this month",
        group_by: Literal["category", "month", "account"] = "category",
    ) -> dict[str, Any]:
        try:
            resolved = resolve_period(period, today())
        except PeriodError as exc:
            raise ValueError(str(exc)) from exc
        grouping: GroupBy = group_by
        rows = store.summary(resolved.since, resolved.until, grouping)
        names = {a.id: a.name for a in store.accounts()} if group_by == "account" else {}
        # Map ids to names before sorting so ties order by the key the caller actually sees.
        shown = sorted(
            ((names.get(r.key, r.key), r) for r in rows), key=lambda p: (-p[1].total, p[0])
        )
        return {
            "since": resolved.since.isoformat(),
            "until": resolved.until.isoformat(),
            "group_by": group_by,
            "rows": [{"key": key, "total": _money(r.total), "count": r.count} for key, r in shown],
            "total": _money(sum((r.total for r in rows), Decimal(0))),
        }

    @mcp.tool(description=SEARCH_DESCRIPTION, annotations=READ_ONLY)
    def search_transactions(
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        min_amount: str | None = None,
        max_amount: str | None = None,
        limit: Limit = 50,
    ) -> list[dict[str, Any]]:
        found = store.search(
            query,
            _parse_date("since", since),
            _parse_date("until", until),
            _parse_amount("min_amount", min_amount),
            _parse_amount("max_amount", max_amount),
            limit,
        )
        names = {a.id: a.name for a in store.accounts()}
        return [_transaction_row(tx, names) for tx in found]

    @mcp.tool(description=ACCOUNTS_DESCRIPTION, annotations=READ_ONLY)
    def list_accounts() -> list[dict[str, Any]]:
        return [_account_row(a) for a in store.accounts()]

    @mcp.tool(description=RECURRING_DESCRIPTION, annotations=READ_ONLY)
    def recurring_charges() -> list[dict[str, Any]]:
        return [
            {
                "description": c.description_key,
                "amount": _money(c.amount),
                "occurrences": c.occurrences,
                "last_on": c.last_on.isoformat(),
                "cadence_days": c.cadence_days,
            }
            for c in store.recurring()
        ]

    return mcp
