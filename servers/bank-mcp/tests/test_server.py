"""MCP tools over a seeded MemoryStore, run through an in-memory session."""

from datetime import date

from mcp.shared.memory import create_connected_server_and_client_session

from pdl_bank.server import build_server

EXPECTED_TOOLS = {"spend_summary", "search_transactions", "list_accounts", "recurring_charges"}


async def _call(store, today, name: str, args: dict | None = None):
    server = build_server(store, today=lambda: today)
    async with create_connected_server_and_client_session(server) as session:
        return await session.call_tool(name, args or {})


def _value(result):
    assert not result.isError, result.content
    data = result.structuredContent
    assert data is not None
    return data.get("result", data)


async def test_tools_registered_read_only(store, today) -> None:
    server = build_server(store, today=lambda: today)
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False


async def test_spend_summary_by_category(store, today) -> None:
    out = _value(await _call(store, today, "spend_summary", {"period": "October"}))
    assert out["since"] == "2026-10-01"
    assert out["until"] == "2026-10-31"
    assert out["group_by"] == "category"
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["groceries"] == {"key": "groceries", "total": "84.50", "count": 2}
    assert out["total"] == "206.49"


async def test_spend_summary_defaults_and_grouping(store, today) -> None:
    default = _value(await _call(store, today, "spend_summary"))
    assert default["since"] == "2026-11-01"
    assert default["total"] == "9.99"
    by_month = _value(
        await _call(store, today, "spend_summary", {"period": "last 90 days", "group_by": "month"})
    )
    assert {r["key"] for r in by_month["rows"]} == {"2026-09", "2026-10", "2026-11"}
    by_account = _value(
        await _call(store, today, "spend_summary", {"period": "2026-10", "group_by": "account"})
    )
    assert {r["key"] for r in by_account["rows"]} == {"Everyday", "Savings"}


async def test_spend_summary_bad_period_is_tool_error(store, today) -> None:
    result = await _call(store, today, "spend_summary", {"period": "whenever"})
    assert result.isError


async def test_spend_summary_bad_group_by_is_error(store, today) -> None:
    assert (await _call(store, today, "spend_summary", {"group_by": "colour"})).isError


async def test_search_shape_and_order(store, today) -> None:
    rows = _value(await _call(store, today, "search_transactions", {"limit": 3}))
    assert [r["posted_on"] for r in rows] == ["2026-11-05", "2026-10-30", "2026-10-28"]
    assert rows[1] == {
        "id": "t7",
        "posted_on": "2026-10-30",
        "amount": "-100.00",
        "description": "TRANSFER OUT",
        "category": "transfers",
        "account": "Savings",
    }


async def test_search_filters(store, today) -> None:
    args = {"query": "groc", "since": "2026-10-01", "until": "2026-10-31", "max_amount": "-50"}
    rows = _value(await _call(store, today, "search_transactions", args))
    assert [r["description"] for r in rows] == ["GREEN GROCER"]
    none_category = _value(await _call(store, today, "search_transactions", {"query": "mystery"}))
    assert none_category[0]["category"] is None
    big = _value(await _call(store, today, "search_transactions", {"min_amount": "1000"}))
    assert [r["amount"] for r in big] == ["2500.00"]


async def test_search_account_falls_back_to_id(store, today) -> None:
    from decimal import Decimal

    from pdl_bank.models import Transaction

    store.upsert_transactions(
        [Transaction("x", "ghost", date(2026, 11, 9), Decimal(-1), "X", None, "f")]
    )
    rows = _value(await _call(store, today, "search_transactions", {"limit": 1}))
    assert rows[0]["account"] == "ghost"


async def test_search_bad_inputs_are_tool_errors(store, today) -> None:
    for args in (
        {"since": "yesterday"},
        {"until": "2026-13-01"},
        {"min_amount": "lots"},
        {"max_amount": "NaN"},
        {"limit": 0},
        {"limit": 201},
    ):
        assert (await _call(store, today, "search_transactions", args)).isError, args


async def test_list_accounts(store, today) -> None:
    rows = _value(await _call(store, today, "list_accounts"))
    assert rows == [
        {"id": "chk", "name": "Everyday", "institution": "Example Bank", "currency": "USD"},
        {"id": "sav", "name": "Savings", "institution": None, "currency": "USD"},
    ]


async def test_recurring_charges(store, today) -> None:
    rows = _value(await _call(store, today, "recurring_charges"))
    assert len(rows) == 1
    assert rows[0]["description"] == "streamco"
    assert rows[0]["amount"] == "9.99"
    assert rows[0]["occurrences"] == 3
    assert rows[0]["last_on"] == "2026-11-05"
    assert rows[0]["cadence_days"] in (30, 31)


async def test_healthz_open(store, today) -> None:
    import httpx

    app = build_server(store, today=lambda: today).streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/healthz")
    assert resp.json() == {"ok": True}


async def test_spend_summary_account_grouping_uses_names(store, today) -> None:
    out = _value(
        await _call(store, today, "spend_summary", {"period": "October", "group_by": "account"})
    )
    assert [r["key"] for r in out["rows"]] == ["Everyday", "Savings"]


async def test_spend_summary_account_grouping_falls_back_to_id(store, today) -> None:
    from decimal import Decimal

    from pdl_bank.models import Transaction

    store.upsert_transactions(
        [Transaction("g", "ghost", date(2026, 10, 9), Decimal(-1), "G", None, "f")]
    )
    out = _value(
        await _call(store, today, "spend_summary", {"period": "October", "group_by": "account"})
    )
    assert "ghost" in [r["key"] for r in out["rows"]]


async def test_tool_descriptions_are_pinned(store, today) -> None:
    server = build_server(store, today=lambda: today)
    async with create_connected_server_and_client_session(server) as session:
        tools = {t.name: t.description for t in (await session.list_tools()).tools}
    assert tools == {
        "spend_summary": (
            "Total spending for a period, grouped by category, month or account. "
            "Use it for questions like what did I spend on food in October. "
            "period accepts 2026-10, October, last 30 days, this month, last month."
        ),
        "search_transactions": (
            "Find individual transactions by words in the description or category, date range "
            "or amount range. Use it to list specific purchases or check whether a payment "
            "happened."
        ),
        "list_accounts": "The bank accounts that statements have been imported for.",
        "recurring_charges": (
            "Charges that repeat with a similar amount, such as subscriptions and rent, "
            "with how often and when they last occurred."
        ),
    }


async def test_healthz_through_middleware(store, today) -> None:
    import httpx

    from pdl_bank.middleware import SharedSecretMiddleware

    inner = build_server(store, today=lambda: today).streamable_http_app()
    app = SharedSecretMiddleware(inner, "s3cret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).json() == {"ok": True}
        assert (await c.post("/mcp", json={})).status_code == 401


async def test_spend_summary_account_grouping_ties_sort_by_shown_name(store, today) -> None:
    from decimal import Decimal

    from pdl_bank.models import Account, Transaction

    store.upsert_account(Account("zzz", "Alpha", None, "USD"))
    store.upsert_account(Account("aaa", "Zulu", None, "USD"))
    store.upsert_transactions(
        [
            Transaction("e1", "zzz", date(2026, 10, 9), Decimal(-500), "E", None, "f"),
            Transaction("e2", "aaa", date(2026, 10, 9), Decimal(-500), "E", None, "f"),
        ]
    )
    out = _value(
        await _call(store, today, "spend_summary", {"period": "October", "group_by": "account"})
    )
    assert [r["key"] for r in out["rows"]][:2] == ["Alpha", "Zulu"]
