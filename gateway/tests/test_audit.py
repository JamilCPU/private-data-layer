"""Tests for the append-only audit log and the gateway audit_search backend."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import mcp.types

from pdl_gateway.audit import AuditEntry, AuditLog, hash_args
from pdl_gateway.audit_backend import AuditBackend

DAY1 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
DAY2 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def _entry(
    ts: datetime,
    *,
    client_id: str = "cid-a",
    tool: str = "notes_search",
    args: dict | None = None,
) -> AuditEntry:
    return AuditEntry(
        ts=ts.isoformat(),
        client_id=client_id,
        client_name="Claude Code",
        tool=tool,
        args_sha256=hash_args(args or {}),
        result_bytes=42,
        latency_ms=7,
        error=None,
    )


def test_entry_holds_hash_not_arguments(tmp_path: Path) -> None:
    args = {"query": "landlord rent"}
    log = AuditLog(tmp_path / "audit")
    log.record(_entry(DAY1, args=args))

    files = list((tmp_path / "audit").glob("*.jsonl"))
    assert len(files) == 1
    assert b"landlord" not in files[0].read_bytes()
    assert AuditEntry.from_json(files[0].read_text(encoding="utf-8").strip()).args_sha256 == (
        hash_args(args)
    )


def test_entry_json_round_trip() -> None:
    entry = _entry(DAY1)
    assert AuditEntry.from_json(entry.to_json()) == entry
    assert "\n" not in entry.to_json()


def test_one_file_per_utc_day(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(_entry(DAY1))
    log.record(_entry(DAY2))
    log.record(_entry(DAY2 + timedelta(minutes=1)))

    names = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert names == ["audit-2026-11-02.jsonl", "audit-2026-11-03.jsonl"]
    assert len((tmp_path / "audit-2026-11-03.jsonl").read_text().splitlines()) == 2


def test_search_filters_and_orders(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(_entry(DAY1, tool="t1"))
    log.record(_entry(DAY1 + timedelta(hours=1), client_id="cid-b", tool="t2"))
    log.record(_entry(DAY2, tool="t3"))
    log.record(_entry(DAY2 + timedelta(hours=1), tool="t4"))
    until = DAY2 + timedelta(hours=2)

    everything = log.search(DAY1 - timedelta(days=1), until=until)
    assert [e.tool for e in everything] == ["t4", "t3", "t2", "t1"]

    only_a = log.search(DAY1, until=until, client_id="cid-a")
    assert [e.tool for e in only_a] == ["t4", "t3", "t1"]

    limited = log.search(DAY1, until=until, limit=2)
    assert [e.tool for e in limited] == ["t4", "t3"]

    later = log.search(DAY1 + timedelta(hours=2), until=until)
    assert [e.tool for e in later] == ["t4", "t3"]

    day_one_only = log.search(DAY1, until=DAY1 + timedelta(hours=2))
    assert [e.tool for e in day_one_only] == ["t2", "t1"]


def test_search_treats_naive_datetimes_as_utc(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    log.record(_entry(DAY1))
    found = log.search(datetime(2026, 11, 2, 8, 0), until=datetime(2026, 11, 2, 10, 0))
    assert len(found) == 1


def test_search_without_files_or_with_bad_lines(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "missing")
    assert log.search(DAY1, until=DAY2) == []

    log.record(_entry(DAY1))
    path = tmp_path / "missing" / "audit-2026-11-02.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n\n")
    assert len(log.search(DAY1, until=DAY2)) == 1


def test_hash_args_is_canonical() -> None:
    assert hash_args({"a": 1, "b": 2}) == hash_args({"b": 2, "a": 1})
    assert hash_args({"a": 1}) != hash_args({"a": 2})


async def test_audit_backend_lists_one_tool(tmp_path: Path) -> None:
    backend = AuditBackend(AuditLog(tmp_path))
    tools = await backend.list_tools()
    assert backend.name == "gateway"
    assert backend.scope == "gateway:audit"
    assert [t.name for t in tools] == ["audit_search"]
    assert tools[0].inputSchema["required"] == ["since"]


async def test_audit_backend_search(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    when = datetime.now(UTC) - timedelta(minutes=5)
    log.record(_entry(when, tool="vault_read"))
    backend = AuditBackend(log)

    since = (when - timedelta(hours=1)).isoformat()
    result = await backend.call_tool("audit_search", {"since": since})

    assert isinstance(result, mcp.types.CallToolResult)
    assert not result.isError
    assert result.structuredContent["entries"][0]["tool"] == "vault_read"
    content = result.content[0]
    assert isinstance(content, mcp.types.TextContent)
    assert json.loads(content.text) == result.structuredContent


async def test_audit_backend_filters_by_client_and_limit(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    now = datetime.now(UTC)
    log.record(_entry(now - timedelta(minutes=3), client_id="a", tool="x"))
    log.record(_entry(now - timedelta(minutes=2), client_id="b", tool="y"))
    log.record(_entry(now - timedelta(minutes=1), client_id="b", tool="z"))
    backend = AuditBackend(log)
    since = (now - timedelta(hours=1)).isoformat()

    result = await backend.call_tool("audit_search", {"since": since, "client_id": "b", "limit": 1})
    assert [e["tool"] for e in result.structuredContent["entries"]] == ["z"]


async def test_audit_backend_bad_since_is_error(tmp_path: Path) -> None:
    backend = AuditBackend(AuditLog(tmp_path))
    for arguments in ({"since": "yesterday-ish"}, {}):
        result = await backend.call_tool("audit_search", arguments)
        assert result.isError
        assert result.content[0].text == "since must be an ISO 8601 timestamp"


async def test_audit_backend_bad_limit_and_unknown_tool(tmp_path: Path) -> None:
    backend = AuditBackend(AuditLog(tmp_path))
    bad_limit = await backend.call_tool(
        "audit_search", {"since": DAY1.isoformat(), "limit": 0}
    )
    assert bad_limit.isError
    unknown = await backend.call_tool("nope", {})
    assert unknown.isError
