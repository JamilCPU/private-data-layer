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


def changed(**kw: Any) -> dict[str, Any]:
    row = {
        "id": "c1",
        "posted_on": "2026-11-01",
        "amount": "12.50",
        "description": "Streamco.com 123",
    }
    return row | kw


def recurring_row(*changes: dict[str, Any], **kw: Any) -> dict[str, Any]:
    row = {
        "description": "streamco",
        "amount": "10.00",
        "occurrences": 3,
        "last_on": "2026-10-05",
        "cadence_days": 30,
        "changed": list(changes),
    }
    return row | kw


def recurring_gateway(*rows: dict[str, Any]) -> FakeGateway:
    return fixed(ok(list(rows)))


async def test_recurring_change_flags_with_summary_and_single_call() -> None:
    gw = recurring_gateway(recurring_row(changed()))
    flags = await run(RecurringChange("sub-change", Decimal(20)), gw)
    assert flags == [
        Flag(
            "sub-change",
            "c1",
            "sub-change: Streamco.com 123 charged 12.50 on 2026-11-01, usually 10.00",
        )
    ]
    assert gw.calls == [(RECURRING, {})]


async def test_recurring_change_window_boundary() -> None:
    inside = changed(id="in", posted_on="2026-10-04")
    outside = changed(id="out", posted_on="2026-10-03")
    gw = recurring_gateway(recurring_row(outside, inside))
    flags = await run(RecurringChange("c", Decimal(20)), gw, lookback=31)
    assert [f.key for f in flags] == ["in"]


@pytest.mark.parametrize("amount", ["12.00", "8.00", "10.00", "11.99"])
async def test_recurring_change_at_or_under_percent_does_not_trip(amount: str) -> None:
    gw = recurring_gateway(recurring_row(changed(amount=amount)))
    assert await run(RecurringChange("c", Decimal(20)), gw) == []


async def test_recurring_change_flags_drop_beyond_percent() -> None:
    gw = recurring_gateway(recurring_row(changed(id="d", amount="7.99")))
    assert [f.key for f in await run(RecurringChange("c", Decimal(20)), gw)] == ["d"]


async def test_recurring_change_missing_changed_is_empty() -> None:
    row = recurring_row()
    del row["changed"]
    gw = recurring_gateway(row, recurring_row(changed=None))
    assert await run(RecurringChange("c", Decimal(20)), gw) == []


async def test_recurring_change_skips_non_positive_baseline() -> None:
    gw = recurring_gateway(recurring_row(changed(), amount="0.00"))
    assert await run(RecurringChange("c", Decimal(20)), gw) == []


async def test_recurring_change_only_first_max_recurring_rows() -> None:
    rows = [recurring_row() for _ in range(MAX_RECURRING)]
    rows.append(recurring_row(changed(id="late")))
    assert await run(RecurringChange("c", Decimal(20)), recurring_gateway(*rows)) == []


async def test_recurring_change_dedupes_same_id_under_two_groups() -> None:
    gw = recurring_gateway(recurring_row(changed()), recurring_row(changed(), amount="9.00"))
    assert len(await run(RecurringChange("c", Decimal(20)), gw)) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        ["x"],
        [changed(id=None)],
        [changed(posted_on=None)],
        [changed(amount="lots")],
    ],
)
async def test_recurring_change_malformed_changed_is_check_error(bad: Any) -> None:
    gw = recurring_gateway(recurring_row(changed=bad))
    with pytest.raises(CheckError):
        await run(RecurringChange("c", Decimal(20)), gw)


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


# --- review minors ------------------------------------------------------------


async def test_transaction_row_missing_posted_on_is_check_error() -> None:
    row = tx()
    del row["posted_on"]
    with pytest.raises(CheckError):
        await run(TransactionOver("big", Decimal(500)), fixed(ok([row])))


async def test_non_dict_structured_is_check_error() -> None:
    result = ToolResult("ok", False, ["x"])  # type: ignore[arg-type]
    with pytest.raises(CheckError):
        await run(TransactionOver("big", Decimal(500)), fixed(result))


async def test_amount_error_names_the_tool() -> None:
    with pytest.raises(CheckError, match=SEARCH):
        await run(TransactionOver("big", Decimal(500)), fixed(ok([tx(amount="lots")])))


async def test_unknown_check_type_is_check_error() -> None:
    with pytest.raises(CheckError):
        await run(object(), fixed(ok([])))


async def test_result_at_limit_logs_truncation_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [tx(id=f"t{n}", amount="-1.00", description="PRIVATE MERCHANT") for n in range(200)]
    with caplog.at_level(logging.INFO):
        await run(TransactionOver("big", Decimal(500)), fixed(ok(rows)))
    assert "may be truncated" in caplog.text
    assert "PRIVATE MERCHANT" not in caplog.text
