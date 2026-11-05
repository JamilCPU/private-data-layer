"""Tests for the flagger runner: no-repeat notifications, caps, offline and dry-run."""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pdl_assistant.flagger import MAX_FLAG_LINES, FlaggerOutcome, run_flagger
from pdl_assistant.flagger_config import EmailFrom, TransactionOver
from pdl_assistant.gateway_client import ToolResult
from pdl_assistant.memory import MemoryStore
from pdl_assistant.notify import NotifyError

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
BIG = TransactionOver("big", Decimal(500))
CHECKS = (BIG,)


def tx(tx_id: str, amount: str = "-600.00") -> dict[str, Any]:
    return {
        "id": tx_id,
        "posted_on": "2026-11-01",
        "amount": amount,
        "description": f"SHOP {tx_id}",
        "category": None,
        "account": "Everyday",
    }


class FakeGateway:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(name)
        if name != "bank_search_transactions":
            return ToolResult("boom", True, None)
        return ToolResult("ok", False, {"result": list(self.rows)})


class Notifier:
    def __init__(self, failures: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failures = failures

    async def send(self, title: str, body: str) -> None:
        if self.failures:
            self.failures -= 1
            raise NotifyError("down")
        self.sent.append((title, body))


@pytest.fixture
def memory(tmp_path: Path):
    store = MemoryStore(tmp_path / "f.db")
    yield store
    store.close()


async def go(gateway, memory, notifier, checks=CHECKS, **kw) -> FlaggerOutcome:
    return await run_flagger(
        checks, gateway, memory, notifier, now=lambda: NOW, lookback_days=31, **kw
    )


def line(tx_id: str) -> str:
    return f"big: -600.00 SHOP {tx_id} on 2026-11-01 (Everyday)"


async def test_each_new_flag_is_notified_once(memory):
    gateway, notifier = FakeGateway([tx("t1")]), Notifier()
    first = await go(gateway, memory, notifier)
    assert first == FlaggerOutcome(1, 1, (), "notified")
    assert notifier.sent == [("Flagged: big", line("t1"))]

    second = await go(gateway, memory, notifier)
    assert second == FlaggerOutcome(0, 0, (), "quiet")
    assert len(notifier.sent) == 1

    gateway.rows.append(tx("t2"))
    third = await go(gateway, memory, notifier)
    assert third.status == "notified"
    assert notifier.sent[1] == ("Flagged: big", line("t2"))
    assert len(notifier.sent) == 2


async def test_failed_send_keeps_flags_pending_then_sends_old_and_new(memory):
    gateway, notifier = FakeGateway([tx("t1")]), Notifier(failures=1)
    first = await go(gateway, memory, notifier)
    assert (first.status, first.new_flags, first.notified) == ("notify_failed", 1, 0)
    assert notifier.sent == []
    assert len(memory.unnotified_flags()) == 1

    gateway.rows.append(tx("t2"))
    second = await go(gateway, memory, notifier)
    assert second.status == "notified"
    assert second.notified == 2
    assert notifier.sent == [("Flagged: 2 items", f"{line('t1')}\n{line('t2')}")]
    assert memory.unnotified_flags() == []


async def test_line_cap_and_all_marked_notified(memory):
    gateway, notifier = FakeGateway([tx(f"t{i:02d}") for i in range(13)]), Notifier()
    outcome = await go(gateway, memory, notifier)
    assert outcome == FlaggerOutcome(13, 13, (), "notified")
    title, body = notifier.sent[0]
    assert title == "Flagged: 13 items"
    lines = body.split("\n")
    assert len(lines) == MAX_FLAG_LINES + 1
    assert lines[-1] == "and 3 more"
    assert lines[0] == line("t00")
    assert memory.unnotified_flags() == []


async def test_offline_probe_skips_before_any_tool_call(memory):
    gateway, notifier = FakeGateway([tx("t1")]), Notifier()

    async def probe() -> bool:
        return False

    outcome = await go(gateway, memory, notifier, probe=probe)
    assert outcome == FlaggerOutcome(0, 0, (), "skipped_offline")
    assert gateway.calls == []
    assert notifier.sent == []


async def test_online_probe_runs(memory):
    async def probe() -> bool:
        return True

    outcome = await go(FakeGateway([tx("t1")]), memory, Notifier(), probe=probe)
    assert outcome.status == "notified"


async def test_dry_run_writes_and_sends_nothing(memory, caplog):
    gateway, notifier = FakeGateway([tx("t1")]), Notifier()
    with caplog.at_level(logging.INFO):
        outcome = await go(gateway, memory, notifier, dry_run=True)
    assert outcome == FlaggerOutcome(1, 0, (), "quiet")
    assert notifier.sent == []
    assert memory.unnotified_flags() == []
    assert not memory.has_flag("big", "t1")
    assert line("t1") in caplog.text


async def test_dry_run_counts_only_unstored_flags(memory):
    memory.add_flag("big", "t1", line("t1"), NOW.isoformat())
    gateway = FakeGateway([tx("t1"), tx("t2")])
    outcome = await go(gateway, memory, Notifier(), dry_run=True)
    assert outcome.new_flags == 1


async def test_failed_check_does_not_stop_others(memory):
    checks = (BIG, EmailFrom("mail", ("a@example.com",)))
    gateway, notifier = FakeGateway([tx("t1")]), Notifier()
    outcome = await go(gateway, memory, notifier, checks=checks)
    assert outcome == FlaggerOutcome(1, 1, ("mail",), "notified")
    assert notifier.sent == [("Flagged: big", line("t1"))]


async def test_no_summaries_at_info_when_not_dry_run(memory, caplog):
    with caplog.at_level(logging.INFO):
        await go(FakeGateway([tx("t1")]), memory, Notifier())
    assert "SHOP" not in caplog.text


async def test_dry_run_also_lists_pending_flags_from_an_earlier_run(memory, caplog):
    memory.add_flag("big", "old", line("old"), NOW.isoformat())
    notifier = Notifier()
    with caplog.at_level(logging.INFO):
        outcome = await go(FakeGateway([tx("new")]), memory, notifier, dry_run=True)
    assert outcome.new_flags == 1
    assert "dry run: still pending from an earlier run: " + line("old") in caplog.text
    assert line("new") in caplog.text
    assert notifier.sent == []
    assert [f.key for f in memory.unnotified_flags()] == ["old"]
    assert not memory.has_flag("big", "new")
