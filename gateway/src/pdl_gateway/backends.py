"""Remote MCP backends: the gateway acts as an MCP client towards each backend server."""

import logging
import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Protocol

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import (
    MCP_DEFAULT_SSE_READ_TIMEOUT,
    MCP_DEFAULT_TIMEOUT,
    McpHttpClientFactory,
    create_mcp_http_client,
)
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, PaginatedRequestParams, Tool

from pdl_gateway.config import BackendConfig, ConfigError, GatewayConfig

logger = logging.getLogger(__name__)

# Failures that mean "the backend could not be reached or did not speak MCP properly".
_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    McpError,
    OSError,
    TimeoutError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
)


class Backend(Protocol):
    """Anything the proxy can list and call tools on (remote or in-process)."""

    name: str
    scope: str

    async def list_tools(self) -> list[Tool]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult: ...


class BackendUnavailable(Exception):
    """A backend could not be reached, timed out, or failed at the protocol level."""


def client_factory_for(ca_file: str | None) -> McpHttpClientFactory:
    """The SDK's default client factory, verifying TLS against ``ca_file`` when one is set."""
    if ca_file is None:
        return create_mcp_http_client

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout
            or httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            auth=auth,
            verify=ssl.create_default_context(cafile=ca_file),
        )

    return factory


def _reason(exc: BaseException) -> str:
    """A short human reason for a transport failure, unwrapping exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, McpError):
        text = exc.error.message
    elif isinstance(exc, TimeoutError):
        text = "timed out"
    else:
        text = str(exc) or type(exc).__name__
    return (text.strip().splitlines() or [type(exc).__name__])[0]


def _is_transport_failure(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return all(_is_transport_failure(inner) for inner in exc.exceptions)
    return isinstance(exc, _TRANSPORT_ERRORS)


class RemoteBackend:
    """An MCP server reached over streamable HTTP with an injected auth header."""

    def __init__(
        self,
        name: str,
        scope: str,
        url: str,
        headers: Mapping[str, str],
        *,
        allow_tools: frozenset[str] | None = None,
        ca_file: str | None = None,
        timeout_s: float = 30.0,
        httpx_client_factory: McpHttpClientFactory = create_mcp_http_client,
    ) -> None:
        self.name = name
        self.scope = scope
        self.url = url
        self.headers: Mapping[str, str] = MappingProxyType(dict(headers))
        self.allow_tools = allow_tools
        self.ca_file = ca_file
        self.timeout_s = timeout_s
        if httpx_client_factory is create_mcp_http_client:
            httpx_client_factory = client_factory_for(ca_file)
        self._client_factory = httpx_client_factory

    def _allowed(self, tool: str) -> bool:
        return self.allow_tools is None or tool in self.allow_tools

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        """Open a fresh, initialized session; translate transport failures."""
        timeout = httpx.Timeout(self.timeout_s, read=self.timeout_s)
        try:
            client = self._client_factory(headers=dict(self.headers), timeout=timeout)
            with anyio.fail_after(self.timeout_s):
                async with (
                    client,
                    streamable_http_client(self.url, http_client=client) as (read, write, _),
                    ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=self.timeout_s)
                    ) as session,
                ):
                    await session.initialize()
                    yield session
        except BaseException as exc:
            if not _is_transport_failure(exc):
                raise
            reason = _reason(exc)
            logger.warning("backend %s unavailable: %s", self.name, reason)
            raise BackendUnavailable(f"{self.name}: {reason}") from exc

    async def list_tools(self) -> list[Tool]:
        """Every tool the backend offers that the allowlist permits."""
        tools: list[Tool] = []
        async with self._session() as session:
            cursor: str | None = None
            while True:
                params = PaginatedRequestParams(cursor=cursor) if cursor else None
                page = await session.list_tools(params=params)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
        return [tool for tool in tools if self._allowed(tool.name)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Call ``name`` on the backend; LookupError if the allowlist forbids it."""
        if not self._allowed(name):
            raise LookupError(f"{self.name}: tool {name!r} is not allowed")
        async with self._session() as session:
            return await session.call_tool(name, arguments)


def _auth_headers(backend: BackendConfig, env: Mapping[str, str]) -> dict[str, str]:
    var = backend.secret_env or backend.bearer_env
    if var is None:
        raise ConfigError(f"backend {backend.name!r} has no secret_env or bearer_env")
    value = env.get(var, "")
    if not value:
        raise ConfigError(f"backend {backend.name!r}: environment variable {var} is not set")
    if backend.secret_env is not None:
        if backend.secret_header is None:
            raise ConfigError(f"backend {backend.name!r}: secret_env needs a secret_header")
        return {backend.secret_header: value}
    return {"Authorization": f"Bearer {value}"}


def build_backends(config: GatewayConfig, env: Mapping[str, str]) -> tuple[Backend, ...]:
    """One RemoteBackend per configured backend, in config order, with auth headers from env."""
    return tuple(
        RemoteBackend(
            b.name,
            b.scope,
            b.url,
            _auth_headers(b, env),
            allow_tools=frozenset(b.allow_tools) if b.allow_tools is not None else None,
            ca_file=b.ca_file,
        )
        for b in config.backends
    )
