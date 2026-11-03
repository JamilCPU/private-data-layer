"""Tests for the proxy server: scope filtering, prefixing, unknown-tool semantics, audit."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import mcp.types
import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import Server
from mcp.shared._httpx_utils import McpHttpClientFactory
from starlette.types import ASGIApp

from pdl_gateway.audit import AuditEntry, AuditLog, hash_args
from pdl_gateway.backends import Backend, BackendUnavailable, RemoteBackend
from pdl_gateway.proxy import (
    Caller,
    build_proxy_server,
    caller_from_request,
    prefixed,
    split_tool_name,
)

from .conftest import STUB_URL, FakeClock, RequestRecorder, gateway_session

AsgiFactory = Callable[[ASGIApp], McpHttpClientFactory]
HOME = Caller("agent-1", "Agent One", frozenset({"homestudio:read"}))
BOTH = Caller("agent-2", None, frozenset({"homestudio:read", "automations:read"}))
COUNT = Caller("agent-4", None, frozenset({"count:read"}))
HOME_TOOLS = {"homestudio_get_taste_profile", "homestudio_list_queue"}


def _homestudio(stub: RequestRecorder, factory: AsgiFactory) -> RemoteBackend:
    return RemoteBackend(
        "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=factory(stub)
    )


def _automations(stub: RequestRecorder, factory: AsgiFactory) -> RemoteBackend:
    return RemoteBackend(
        "automations",
        "automations:read",
        STUB_URL,
        {},
        allow_tools=frozenset({"list_devices", "recent_history"}),
        httpx_client_factory=factory(stub),
    )


def _dead() -> RemoteBackend:
    return RemoteBackend("dead", "dead:read", "http://127.0.0.1:9/mcp", {}, timeout_s=5.0)


def _text_result(text: str, *, is_error: bool = False) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)], isError=is_error
    )


class CountingBackend:
    """An in-process backend counting list_tools calls, with a scripted call_tool."""

    def __init__(self, call: Callable[[str], mcp.types.CallToolResult] | None = None) -> None:
        self.name = "count"
        self.scope = "count:read"
        self.list_calls = 0
        self.fail_listing = False
        self._call = call or (lambda tool: _text_result("ok"))

    async def list_tools(self) -> list[mcp.types.Tool]:
        self.list_calls += 1
        if self.fail_listing:
            raise BackendUnavailable(f"{self.name}: connection refused")
        return [mcp.types.Tool(name="ping", inputSchema={"type": "object"})]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> mcp.types.CallToolResult:
        return self._call(name)


def _audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit")


def _entries(log: AuditLog) -> list[AuditEntry]:
    return log.search(datetime.now(UTC) - timedelta(minutes=5))


def _text(result: mcp.types.CallToolResult) -> str:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, mcp.types.TextContent)
    return block.text


async def _names(
    server: Server, caller: Caller | None, monkeypatch: pytest.MonkeyPatch
) -> set[str]:
    async with gateway_session(server, caller, monkeypatch) as session:
        return {tool.name for tool in (await session.list_tools()).tools}


async def _assert_unknown(
    backends: list[Backend],
    caller: Caller | None,
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AuditEntry:
    log = _audit(tmp_path)
    server = build_proxy_server(backends, log)
    async with gateway_session(server, caller, monkeypatch) as session:
        result = await session.call_tool(tool, {})
    assert result.isError is True
    assert _text(result) == f"unknown tool: {tool}"
    [entry] = _entries(log)
    assert (entry.tool, entry.error, entry.result_bytes) == (tool, "unknown tool", 0)
    return entry


async def test_list_tools_filtered_by_scope(
    homestudio_stub, automations_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backends = [
        _homestudio(homestudio_stub, asgi_factory),
        _automations(automations_stub, asgi_factory),
    ]
    server = build_proxy_server(backends, _audit(tmp_path))

    assert await _names(server, HOME, monkeypatch) == HOME_TOOLS
    assert automations_stub.requests == [], "an out-of-scope backend must not be contacted"


async def test_list_tools_two_scopes_and_allowlist(
    homestudio_stub, automations_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backends = [
        _homestudio(homestudio_stub, asgi_factory),
        _automations(automations_stub, asgi_factory),
    ]
    server = build_proxy_server(backends, _audit(tmp_path))

    names = await _names(server, BOTH, monkeypatch)

    assert names == HOME_TOOLS | {"automations_list_devices", "automations_recent_history"}
    assert "automations_reboot_device" not in names


async def test_tools_are_marked_read_only(
    homestudio_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backend = _homestudio(homestudio_stub, asgi_factory)
    upstream = {tool.name: tool for tool in await backend.list_tools()}
    server = build_proxy_server([backend], _audit(tmp_path))

    async with gateway_session(server, HOME, monkeypatch) as session:
        tools = (await session.list_tools()).tools

    assert [t.name for t in tools] == sorted(HOME_TOOLS)
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        original = upstream[tool.name.removeprefix("homestudio_")]
        assert tool.description == original.description
        assert tool.inputSchema == original.inputSchema
        assert tool.outputSchema == original.outputSchema


async def test_call_allowed_tool_audited(
    homestudio_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    log = _audit(tmp_path)
    server = build_proxy_server([_homestudio(homestudio_stub, asgi_factory)], log)

    async with gateway_session(server, HOME, monkeypatch) as session:
        result = await session.call_tool("homestudio_get_taste_profile", {})

    assert result.isError is False
    assert "jazz" in _text(result)
    [entry] = _entries(log)
    assert entry.tool == "homestudio_get_taste_profile"
    assert entry.error is None
    assert entry.args_sha256 == hash_args({})
    assert (entry.client_id, entry.client_name) == ("agent-1", "Agent One")
    expected = len(json.dumps([c.model_dump(mode="json") for c in result.content]))
    assert entry.result_bytes == expected
    assert entry.latency_ms >= 0


async def test_call_out_of_scope_is_unknown_tool(
    homestudio_stub, automations_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backends = [
        _homestudio(homestudio_stub, asgi_factory),
        _automations(automations_stub, asgi_factory),
    ]
    await _assert_unknown(backends, HOME, "automations_list_devices", tmp_path, monkeypatch)
    assert automations_stub.requests == [], "an out-of-scope backend must not be contacted"


async def test_call_nonexistent_is_unknown_tool(
    homestudio_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backends = [_homestudio(homestudio_stub, asgi_factory)]
    await _assert_unknown(backends, HOME, "bank_spend_summary", tmp_path, monkeypatch)


async def test_call_filtered_write_tool_is_unknown_tool(
    automations_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backends = [_automations(automations_stub, asgi_factory)]
    await _assert_unknown(backends, BOTH, "automations_reboot_device", tmp_path, monkeypatch)
    assert "tools/call" not in automations_stub.methods


async def test_no_caller_sees_nothing(homestudio_stub, asgi_factory, tmp_path, monkeypatch) -> None:
    backends = [_homestudio(homestudio_stub, asgi_factory)]
    assert await _names(build_proxy_server(backends, _audit(tmp_path)), None, monkeypatch) == set()

    entry = await _assert_unknown(backends, None, "homestudio_list_queue", tmp_path, monkeypatch)

    assert (entry.client_id, entry.client_name) == ("anonymous", None)
    assert homestudio_stub.requests == [], "no backend may be contacted without a caller"


async def test_list_tools_skips_unreachable_backend(
    homestudio_stub, asgi_factory, tmp_path, monkeypatch
) -> None:
    backends = [_dead(), _homestudio(homestudio_stub, asgi_factory)]
    server = build_proxy_server(backends, _audit(tmp_path))
    caller = Caller("agent-3", None, frozenset({"homestudio:read", "dead:read"}))

    assert await _names(server, caller, monkeypatch) == HOME_TOOLS


async def test_call_on_unreachable_backend_is_error_result(tmp_path, monkeypatch) -> None:
    log = _audit(tmp_path)
    server = build_proxy_server([_dead()], log)
    caller = Caller("agent-3", None, frozenset({"dead:read"}))

    async with gateway_session(server, caller, monkeypatch) as session:
        result = await session.call_tool("dead_anything", {})

    assert result.isError is True
    assert _text(result).startswith("backend unavailable: dead (")
    [entry] = _entries(log)
    assert (entry.tool, entry.error) == ("dead_anything", "backend unavailable")


async def test_tool_cache_refreshes_after_ttl(tmp_path, monkeypatch) -> None:
    backend = CountingBackend()
    clock = FakeClock(1000.0)
    server = build_proxy_server([backend], _audit(tmp_path), tools_cache_ttl_s=60.0, clock=clock)

    async with gateway_session(server, COUNT, monkeypatch) as session:
        await session.list_tools()
        await session.list_tools()
        assert backend.list_calls == 1
        clock.advance(61.0)
        assert [t.name for t in (await session.list_tools()).tools] == ["count_ping"]
        assert backend.list_calls == 2


async def test_stale_cache_served_when_refresh_fails(tmp_path, monkeypatch) -> None:
    backend = CountingBackend()
    clock = FakeClock(1000.0)
    server = build_proxy_server([backend], _audit(tmp_path), clock=clock)

    async with gateway_session(server, COUNT, monkeypatch) as session:
        await session.list_tools()
        backend.fail_listing = True
        clock.advance(120.0)
        assert [t.name for t in (await session.list_tools()).tools] == ["count_ping"]
        result = await session.call_tool("count_ping", {})
        assert result.isError is False
    assert backend.list_calls == 3


def _raises(exc: Exception) -> Callable[[str], mcp.types.CallToolResult]:
    def call(tool: str) -> mcp.types.CallToolResult:
        raise exc

    return call


@pytest.mark.parametrize(
    ("call", "text", "error"),
    [
        (_raises(LookupError("count: no")), "unknown tool: count_ping", "unknown tool"),
        (_raises(RuntimeError("secret detail")), "internal error", "internal error"),
        (lambda tool: _text_result("boom", is_error=True), "boom", "tool error"),
    ],
    ids=["lookup-error", "unexpected-exception", "backend-error-result"],
)
async def test_call_failure_modes(call, text, error, tmp_path, monkeypatch) -> None:
    log = _audit(tmp_path)
    server = build_proxy_server([CountingBackend(call=call)], log)

    async with gateway_session(server, COUNT, monkeypatch) as session:
        result = await session.call_tool("count_ping", {"x": 1})
        assert (await session.list_tools()).tools, "the session must survive the failure"

    assert result.isError is True
    assert _text(result) == text
    [entry] = _entries(log)
    assert (entry.error, entry.args_sha256) == (error, hash_args({"x": 1}))


def test_split_tool_name_longest_prefix() -> None:
    names = ["home", "home_studio"]
    assert split_tool_name("home_studio_x", names) == ("home_studio", "x")
    assert split_tool_name("home_x", names) == ("home", "x")
    assert split_tool_name("home_", names) is None
    assert split_tool_name("bank_spend_summary", names) is None
    assert prefixed("home_studio", "x") == "home_studio_x"


class _Request:
    def __init__(self, user: Any) -> None:
        self.user = user


def test_caller_from_request() -> None:
    token = AccessToken(
        token="t", client_id="c1", scopes=["a:read", "b:read"], claims={"client_name": "Cee"}
    )
    assert caller_from_request(_Request(AuthenticatedUser(token))) == Caller(
        "c1", "Cee", frozenset({"a:read", "b:read"})
    )
    bare = AccessToken(token="t", client_id="c2", scopes=[])
    assert caller_from_request(_Request(AuthenticatedUser(bare))) == Caller("c2", None, frozenset())
    assert caller_from_request(_Request(object())) is None
    assert caller_from_request(object()) is None
    assert caller_from_request(None) is None


class _BrokenListing(CountingBackend):
    async def list_tools(self) -> list[mcp.types.Tool]:
        raise RuntimeError("bug in backend")


async def test_list_tools_skips_backend_raising_unexpectedly(tmp_path, monkeypatch) -> None:
    server = build_proxy_server([_BrokenListing()], _audit(tmp_path))

    assert await _names(server, COUNT, monkeypatch) == set()


class _FailingAudit(AuditLog):
    def record(self, entry: AuditEntry) -> None:
        raise OSError("disk full")


async def test_result_withheld_when_audit_write_fails(tmp_path, monkeypatch) -> None:
    server = build_proxy_server([CountingBackend()], _FailingAudit(tmp_path / "audit"))

    async with gateway_session(server, COUNT, monkeypatch) as session:
        result = await session.call_tool("count_ping", {})

    assert result.isError is True
    assert _text(result) == "internal error"
