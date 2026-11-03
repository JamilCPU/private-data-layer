"""The MCP server agents talk to: scope-filtered, prefixed backend tools with an audit trail."""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from pdl_gateway.audit import MAX_ERROR_CHARS, AuditEntry, AuditLog, hash_args, truncate_error
from pdl_gateway.backends import Backend, BackendUnavailable

logger = logging.getLogger(__name__)

SERVER_NAME = "private-data-layer"
ANONYMOUS = "anonymous"
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


@dataclass(frozen=True)
class Caller:
    client_id: str
    client_name: str | None
    scopes: frozenset[str]


def caller_from_request(request: Any) -> Caller | None:
    """The authenticated OAuth client behind ``request``, or None when there is none."""
    try:
        user = request.user
    except (AttributeError, AssertionError):  # Starlette asserts when no auth middleware ran
        return None
    if not isinstance(user, AuthenticatedUser):
        return None
    token = user.access_token
    client_name = token.claims.get("client_name") if token.claims else None
    return Caller(token.client_id, client_name, frozenset(token.scopes))


def prefixed(backend: str, tool: str) -> str:
    return f"{backend}_{tool}"


def split_tool_name(name: str, backend_names: Sequence[str]) -> tuple[str, str] | None:
    """``(backend, tool)`` for the longest matching ``<backend>_`` prefix, else None."""
    for backend in sorted(backend_names, key=len, reverse=True):
        prefix = f"{backend}_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return backend, name[len(prefix) :]
    return None


@dataclass(frozen=True)
class _CachedList:
    tools: tuple[Tool, ...]
    fetched_at: float


class _ToolCache:
    """Per-backend tool lists, refreshed lazily after ``ttl_s``; stale lists outlive failures."""

    def __init__(self, ttl_s: float, clock: Callable[[], float]) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: Mapping[str, _CachedList] = {}

    async def tools_for(self, backend: Backend) -> tuple[Tool, ...]:
        """The backend's tools; BackendUnavailable only when it fails and nothing is cached."""
        cached = self._entries.get(backend.name)
        now = self._clock()
        if cached is not None and now - cached.fetched_at < self._ttl_s:
            return cached.tools
        try:
            tools = tuple(await backend.list_tools())
        except BackendUnavailable as exc:
            if cached is None:
                raise
            logger.warning("serving cached tool list for %s: %s", backend.name, exc)
            return cached.tools
        self._entries = {**self._entries, backend.name: _CachedList(tools, now)}
        return tools


def _expose(backend: str, tool: Tool) -> Tool:
    return Tool(
        name=prefixed(backend, tool.name),
        description=tool.description,
        inputSchema=tool.inputSchema,
        outputSchema=tool.outputSchema,
        annotations=_READ_ONLY,
    )


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(isError=True, content=[TextContent(type="text", text=text)])


def _result_bytes(result: CallToolResult) -> int:
    return len(json.dumps([block.model_dump(mode="json") for block in result.content]))


@dataclass(frozen=True)
class _Outcome:
    result: CallToolResult
    error: str | None
    result_bytes: int = 0


def _unknown(name: str) -> _Outcome:
    return _Outcome(_error_result(f"unknown tool: {name}"), "unknown tool")


def _safe_hash(arguments: dict[str, Any]) -> str:
    """The arguments' hash, or "" when even hashing them fails."""
    try:
        return hash_args(arguments)
    except Exception:
        logger.exception("cannot hash tool arguments for the audit log")
        return ""


def _entry(
    caller: Caller | None, name: str, args_sha256: str, outcome: _Outcome, started: float
) -> AuditEntry:
    return AuditEntry(
        ts=datetime.now(UTC).isoformat(),
        client_id=caller.client_id if caller else ANONYMOUS,
        client_name=caller.client_name if caller else None,
        tool=name[:MAX_ERROR_CHARS],
        args_sha256=args_sha256,
        result_bytes=outcome.result_bytes,
        latency_ms=int((time.perf_counter() - started) * 1000),
        error=truncate_error(outcome.error),
    )


class _Proxy:
    def __init__(self, backends: Sequence[Backend], audit: AuditLog, cache: _ToolCache) -> None:
        self._backends: Mapping[str, Backend] = {b.name: b for b in backends}
        self._audit = audit
        self._cache = cache

    async def list_tools(self, caller: Caller | None) -> list[Tool]:
        if caller is None:
            return []
        exposed: list[Tool] = []
        for backend in self._backends.values():
            if backend.scope not in caller.scopes:
                continue
            try:
                tools = await self._cache.tools_for(backend)
            except BackendUnavailable as exc:
                logger.warning("backend %s contributes no tools: %s", backend.name, exc)
                continue
            except Exception:
                logger.exception("listing tools of backend %s failed", backend.name)
                continue
            exposed.extend(_expose(backend.name, tool) for tool in tools)
        return sorted(exposed, key=lambda tool: tool.name)

    async def call_tool(
        self, current_caller: Callable[[], Caller | None], name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        """Dispatch and audit one call; any failure, even building the audit entry, is audited."""
        started = time.perf_counter()
        caller: Caller | None = None
        try:
            caller = current_caller()
            outcome = await self._dispatch(caller, name, arguments)
            entry = _entry(caller, name, hash_args(arguments), outcome, started)
        except Exception:
            logger.exception("tool call %s failed", name[:MAX_ERROR_CHARS])
            outcome = _Outcome(_error_result("internal error"), "internal error")
            entry = _entry(caller, name, _safe_hash(arguments), outcome, started)
        try:
            await anyio.to_thread.run_sync(self._audit.record, entry)
        except Exception:
            logger.exception("audit write failed; withholding the result of %s", entry.tool)
            return _error_result("internal error")
        return outcome.result

    def _resolve(self, caller: Caller | None, name: str) -> tuple[Backend, str] | None:
        """The in-scope backend and backend-side tool name, checked before any backend contact."""
        if caller is None:
            return None
        split = split_tool_name(name, list(self._backends))
        if split is None:
            return None
        backend = self._backends[split[0]]
        if backend.scope not in caller.scopes:
            return None
        return backend, split[1]

    async def _dispatch(
        self, caller: Caller | None, name: str, arguments: dict[str, Any]
    ) -> _Outcome:
        resolved = self._resolve(caller, name)
        if resolved is None:
            return _unknown(name)
        backend, tool = resolved
        try:
            if tool not in {t.name for t in await self._cache.tools_for(backend)}:
                return _unknown(name)
            result = await backend.call_tool(tool, arguments)
        except LookupError:
            return _unknown(name)
        except BackendUnavailable as exc:
            reason = str(exc).removeprefix(f"{backend.name}: ")
            text = f"backend unavailable: {backend.name} ({reason})"
            return _Outcome(_error_result(text), "backend unavailable")
        error = "tool error" if result.isError else None
        return _Outcome(result, error, _result_bytes(result))


def build_proxy_server(
    backends: Sequence[Backend],
    audit: AuditLog,
    *,
    tools_cache_ttl_s: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> Server:
    """A lowlevel MCP server exposing each backend's tools to callers holding its scope."""
    server: Server = Server(SERVER_NAME)
    proxy = _Proxy(backends, audit, _ToolCache(tools_cache_ttl_s, clock))

    def current_caller() -> Caller | None:
        return caller_from_request(server.request_context.request)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return await proxy.list_tools(current_caller())

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return await proxy.call_tool(current_caller, name, arguments)

    return server
