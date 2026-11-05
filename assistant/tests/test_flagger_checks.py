"""Tests for the deterministic flagger checks."""

import hashlib
import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from pdl_assistant.flagger_checks import (
    MAX_LINE_CHARS,
    MAX_RECURRING,
    CheckError,
    Flag,
    evaluate,
    evaluate_all,
)
from pdl_assistant.flagger_config import EmailFrom, RecurringChange, TransactionOver
from pdl_assistant.gateway_client import ToolResult

TODAY = date(2026, 11, 4)
SEARCH = "bank_search_transactions"
RECURRING = "bank_recurring_charges"
EMAIL = "email_search_email"


def ok(rows: list[Any]) -> ToolResult:
    return ToolResult(text="ok", is_error=False, structured={"result": rows})


def tx(**kw: Any) -> dict[str, Any]:
    row = {
        "id": "t1",
        "posted_on": "2026-11-01",
        "amount": "-600.00",
        "description": "EXAMPLE FURNITURE",
        "category": None,
        "account": "Everyday",
    }
    return row | kw


Responder = Callable[[str, dict[str, Any]], ToolResult]


class FakeGateway:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self.responder(name, arguments)


def fixed(result: ToolResult) -> FakeGateway:
    return FakeGateway(lambda _n, _a: result)


async def run(check: Any, gateway: FakeGateway, lookback: int = 31) -> list[Flag]:
    return await evaluate(check, gateway, today=TODAY, lookback_days=lookback)


# --- transaction_over -------------------------------------------------------


async def test_transaction_over_flags_with_exact_summary_and_arguments() -> None:
    gw = fixed(ok([tx()]))
    flags = await run(TransactionOver("big-spend", Decimal(500)), gw)
    assert flags == [
        Flag("big-spend", "t1", "big-spend: -600.00 EXAMPLE FURNITURE on 2026-11-01 (Everyday)")
    ]
    assert gw.calls == [(SEARCH, {"since": "2026-10-04", "max_amount": "-500", "limit": 200})]


async def test_transaction_over_flags_row_thirty_days_old() -> None:
    gw = fixed(ok([tx(posted_on="2026-10-05")]))
    flags = await run(TransactionOver("big", Decimal(500)), gw, lookback=31)
    assert [f.key for f in flags] == ["t1"]
    assert gw.calls[0][1]["since"] == "2026-10-04"


async def test_transaction_over_equal_to_threshold_does_not_trip() -> None:
    gw = fixed(ok([tx(amount="-500.00")]))
    assert await run(TransactionOver("big", Decimal(500)), gw) == []


async def test_transaction_over_spend_ignores_income() -> None:
    gw = fixed(ok([tx(id="p", amount="2500.00")]))
    assert await run(TransactionOver("big", Decimal(500)), gw) == []
    assert len(gw.calls) == 1


async def test_transaction_over_any_flags_income_and_dedupes() -> None:
    def responder(_n: str, args: dict[str, Any]) -> ToolResult:
        if "min_amount" in args:
            return ok([tx(id="p", amount="2500.00", description="EMPLOYER PAYROLL"), tx()])
        return ok([tx()])

    gw = FakeGateway(responder)
    flags = await run(TransactionOver("big", Decimal(500), "any"), gw)
    assert [f.key for f in flags] == ["t1", "p"]
    assert gw.calls[0][1] == {"since": "2026-10-04", "max_amount": "-500", "limit": 200}
    assert gw.calls[1][1] == {"since": "2026-10-04", "min_amount": "500", "limit": 200}


async def test_transaction_over_key_falls_back_to_hash() -> None:
    row = tx()
    del row["id"]
    flags = await run(TransactionOver("big", Decimal(500)), fixed(ok([row])))
    raw = "2026-11-01|-600.00|EXAMPLE FURNITURE|Everyday"
    assert flags[0].key == hashlib.sha256(raw.encode()).hexdigest()[:32]


@pytest.mark.parametrize("bad", ["lots", "NaN", "Infinity", None])
async def test_bad_amount_is_check_error(bad: Any) -> None:
    with pytest.raises(CheckError):
        await run(TransactionOver("big", Decimal(500)), fixed(ok([tx(amount=bad)])))


@pytest.mark.parametrize(
    "result",
    [
        ToolResult("boom", True, None),
        ToolResult("boom", True, {"result": []}),
        ToolResult("ok", False, None),
        ToolResult("ok", False, {}),
        ToolResult("ok", False, {"result": "nope"}),
        ToolResult("ok", False, {"result": ["x"]}),
    ],
)
async def test_bad_tool_results_are_check_errors(result: ToolResult) -> None:
    with pytest.raises(CheckError):
        await run(TransactionOver("big", Decimal(500)), fixed(result))


# --- recurring_change -------------------------------------------------------


def recurring_row(**kw: Any) -> dict[str, Any]:
    row = {
        "description": "STREAMCO",
        "amount": "10.00",
        "occurrences": 3,
        "last_on": "2026-10-05",
        "cadence_days": 30,
    }
    return row | kw


def recurring_gateway(search_rows: list[dict[str, Any]]) -> FakeGateway:
    def responder(name: str, _args: dict[str, Any]) -> ToolResult:
        return ok([recurring_row()]) if name == RECURRING else ok(search_rows)

    return FakeGateway(responder)


async def test_recurring_change_flags_with_summary_and_arguments() -> None:
    gw = recurring_gateway([tx(id="s9", posted_on="2026-11-05", amount="-12.50")])
    flags = await run(RecurringChange("sub-change", Decimal(20)), gw)
    assert flags == [
        Flag("sub-change", "s9", "sub-change: STREAMCO charged -12.50 on 2026-11-05, usually 10.00")
    ]
    assert gw.calls[0] == (RECURRING, {})
    assert gw.calls[1] == (SEARCH, {"query": "STREAMCO", "since": "2026-10-05", "limit": 20})


@pytest.mark.parametrize("amount", ["-12.00", "-8.00", "-10.00", "-11.99"])
async def test_recurring_change_at_or_under_percent_does_not_trip(amount: str) -> None:
    gw = recurring_gateway([tx(posted_on="2026-11-05", amount=amount)])
    assert await run(RecurringChange("c", Decimal(20)), gw) == []


async def test_recurring_change_flags_price_drop_beyond_percent() -> None:
    gw = recurring_gateway([tx(id="d", posted_on="2026-11-05", amount="-7.99")])
    assert [f.key for f in await run(RecurringChange("c", Decimal(20)), gw)] == ["d"]


async def test_recurring_change_ignores_old_and_income_rows() -> None:
    rows = [
        tx(id="same", posted_on="2026-10-05", amount="-50.00"),
        tx(id="old", posted_on="2026-10-01", amount="-50.00"),
        tx(id="refund", posted_on="2026-11-02", amount="50.00"),
    ]
    assert await run(RecurringChange("c", Decimal(20)), recurring_gateway(rows)) == []


async def test_recurring_change_skips_unusable_recurring_rows() -> None:
    rows = [
        recurring_row(amount="0.00"),
        recurring_row(description="  "),
        recurring_row(last_on=None),
    ]
    gw = FakeGateway(lambda _n, _a: ok(rows))
    assert await run(RecurringChange("c", Decimal(20)), gw) == []
    assert [c[0] for c in gw.calls] == [RECURRING]


async def test_recurring_change_stops_at_max_recurring() -> None:
    rows = [recurring_row(description=f"MERCHANT {n}") for n in range(MAX_RECURRING + 5)]

    def responder(name: str, _a: dict[str, Any]) -> ToolResult:
        return ok(rows) if name == RECURRING else ok([])

    gw = FakeGateway(responder)
    await run(RecurringChange("c", Decimal(20)), gw)
    assert sum(1 for c in gw.calls if c[0] == SEARCH) == MAX_RECURRING


async def test_recurring_change_dedupes_by_row_id() -> None:
    rows = [recurring_row(), recurring_row(description="STREAMCO 2")]

    def responder(name: str, _a: dict[str, Any]) -> ToolResult:
        if name == RECURRING:
            return ok(rows)
        return ok([tx(id="same", posted_on="2026-11-05", amount="-50.00")])

    flags = await run(RecurringChange("c", Decimal(20)), FakeGateway(responder))
    assert len(flags) == 1


async def test_recurring_change_search_error_is_check_error() -> None:
    def responder(name: str, _a: dict[str, Any]) -> ToolResult:
        return ok([recurring_row()]) if name == RECURRING else ToolResult("x", True, None)

    with pytest.raises(CheckError):
        await run(RecurringChange("c", Decimal(20)), FakeGateway(responder))


# --- email_from -------------------------------------------------------------


def mail(**kw: Any) -> dict[str, Any]:
    row = {
        "id": "m1",
        "from": "Pat Example <Pat@Example.test>",
        "date": "2026-11-03T09:00:00+00:00",
        "subject": "Invoice overdue",
        "snippet": "pay now",
    }
    return row | kw


async def test_email_from_flags_case_insensitively_with_arguments() -> None:
    gw = fixed(ok([mail()]))
    flags = await run(EmailFrom("watch", ("pat@example.test",)), gw)
    assert flags == [
        Flag(
            "watch",
            "m1",
            "watch: email from Pat Example <Pat@Example.test> — Invoice overdue "
            "(2026-11-03T09:00:00+00:00)",
        )
    ]
    since = "2026-10-04T00:00:00+00:00"
    assert gw.calls == [(EMAIL, {"query": "pat@example.test", "since": since, "limit": 20})]


async def test_email_from_ignores_non_matching_sender() -> None:
    gw = fixed(ok([mail(**{"from": "other@example.com"})]))
    assert await run(EmailFrom("watch", ("pat@example.test",)), gw) == []


async def test_email_from_dedupes_across_senders() -> None:
    gw = fixed(ok([mail(**{"from": "a@example.test b@example.test"})]))
    flags = await run(EmailFrom("watch", ("a@example.test", "b@example.test")), gw)
    assert len(flags) == 1
    assert len(gw.calls) == 2


@pytest.mark.parametrize("missing", ["from", "id"])
async def test_email_row_missing_field_is_check_error(missing: str) -> None:
    row = mail()
    del row[missing]
    with pytest.raises(CheckError):
        await run(EmailFrom("watch", ("pat@example.test",)), fixed(ok([row])))


# --- summaries --------------------------------------------------------------


async def test_summary_is_single_line_and_capped() -> None:
    desc = "LINE ONE\n\n  LINE\tTWO " + "X" * 400
    flags = await run(TransactionOver("big", Decimal(500)), fixed(ok([tx(description=desc)])))
    summary = flags[0].summary
    assert "\n" not in summary
    assert "\t" not in summary
    assert "  " not in summary
    assert len(summary) == MAX_LINE_CHARS
    assert summary.endswith("…")


async def test_short_summary_has_no_ellipsis() -> None:
    flags = await run(TransactionOver("big", Decimal(500)), fixed(ok([tx()])))
    assert not flags[0].summary.endswith("…")


# --- evaluate_all -----------------------------------------------------------


async def test_evaluate_all_keeps_good_checks_and_names_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def responder(name: str, _a: dict[str, Any]) -> ToolResult:
        if name == EMAIL:
            return ToolResult("secret body text", True, None)
        return ok([tx()])

    checks = (TransactionOver("big", Decimal(500)), EmailFrom("watch", ("pat@example.test",)))
    with caplog.at_level(logging.WARNING):
        flags, failed = await evaluate_all(
            checks, FakeGateway(responder), today=TODAY, lookback_days=31
        )
    assert [f.check for f in flags] == ["big"]
    assert failed == ["watch"]
    assert "watch" in caplog.text
    assert "secret body text" not in caplog.text


async def test_evaluate_all_survives_gateway_exceptions() -> None:
    def responder(name: str, _a: dict[str, Any]) -> ToolResult:
        if name == EMAIL:
            raise ConnectionError("down: private detail")
        return ok([tx()])

    checks = (EmailFrom("watch", ("pat@example.test",)), TransactionOver("big", Decimal(500)))
    flags, failed = await evaluate_all(
        checks, FakeGateway(responder), today=TODAY, lookback_days=31
    )
    assert failed == ["watch"]
    assert [f.key for f in flags] == ["t1"]


async def test_evaluate_all_dedupes_by_check_and_key() -> None:
    checks = (TransactionOver("a", Decimal(500)), TransactionOver("b", Decimal(100)))
    flags, failed = await evaluate_all(checks, fixed(ok([tx()])), today=TODAY, lookback_days=31)
    assert [(f.check, f.key) for f in flags] == [("a", "t1"), ("b", "t1")]
    assert failed == []
