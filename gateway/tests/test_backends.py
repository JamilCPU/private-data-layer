"""Tests for the remote MCP backends: allowlists, injected auth headers, failure mapping."""

import dataclasses
import json
import os
import ssl
from collections.abc import Callable
from pathlib import Path

import anyio
import certifi
import httpx
import mcp.types
import pytest
from mcp.shared._httpx_utils import McpHttpClientFactory
from starlette.types import ASGIApp

from pdl_gateway import backends
from pdl_gateway.backends import (
    BackendUnavailable,
    RemoteBackend,
    build_backends,
    client_factory_for,
)
from pdl_gateway.config import ConfigError, GatewayConfig

from .conftest import STUB_URL, HeaderRecorder

AsgiFactory = Callable[[ASGIApp], McpHttpClientFactory]
SECRET = "s3cret-value"


def _homestudio(stub: HeaderRecorder, factory: AsgiFactory) -> RemoteBackend:
    return RemoteBackend(
        "homestudio",
        "homestudio:read",
        STUB_URL,
        {"X-Gateway-Secret": SECRET},
        httpx_client_factory=factory(stub),
    )


async def test_list_tools_passthrough(
    homestudio_stub: HeaderRecorder, asgi_factory: AsgiFactory
) -> None:
    backend = _homestudio(homestudio_stub, asgi_factory)

    tools = await backend.list_tools()

    assert {t.name for t in tools} == {"get_taste_profile", "list_queue"}
    assert homestudio_stub.requests, "the stub saw no requests"
    assert all(r.get("x-gateway-secret") == SECRET for r in homestudio_stub.requests)


async def test_allowlist_filters_list_and_call(
    automations_stub: HeaderRecorder, asgi_factory: AsgiFactory
) -> None:
    backend = RemoteBackend(
        "automations",
        "automations:read",
        STUB_URL,
        {"Authorization": "Bearer tok"},
        allow_tools=frozenset({"list_devices", "recent_history"}),
        httpx_client_factory=asgi_factory(automations_stub),
    )

    tools = await backend.list_tools()
    assert {t.name for t in tools} == {"list_devices", "recent_history"}

    seen = len(automations_stub.requests)
    with pytest.raises(LookupError, match="reboot_device"):
        await backend.call_tool("reboot_device", {"device": "pi1"})
    assert len(automations_stub.requests) == seen, "a blocked call must not reach the backend"


async def test_call_tool_returns_result(
    automations_stub: HeaderRecorder, asgi_factory: AsgiFactory
) -> None:
    backend = RemoteBackend(
        "automations",
        "automations:read",
        STUB_URL,
        {"Authorization": "Bearer tok"},
        allow_tools=frozenset({"list_devices"}),
        httpx_client_factory=asgi_factory(automations_stub),
    )

    result = await backend.call_tool("list_devices", {})

    assert isinstance(result, mcp.types.CallToolResult)
    assert not result.isError
    assert result.content
    assert automations_stub.requests[-1]["authorization"] == "Bearer tok"


async def test_call_tool_passes_arguments(
    automations_stub: HeaderRecorder, asgi_factory: AsgiFactory
) -> None:
    backend = RemoteBackend(
        "automations",
        "automations:read",
        STUB_URL,
        {},
        httpx_client_factory=asgi_factory(automations_stub),
    )

    result = await backend.call_tool("reboot_device", {"device": "pi2"})

    assert isinstance(result.content[0], mcp.types.TextContent)
    assert json.loads(result.content[0].text) == {"rebooted": "pi2"}


async def test_unreachable_raises_backend_unavailable() -> None:
    backend = RemoteBackend("homestudio", "homestudio:read", "http://127.0.0.1:9/mcp", {})

    with pytest.raises(BackendUnavailable, match="^homestudio: "):
        await backend.list_tools()
    with pytest.raises(BackendUnavailable, match="^homestudio: "):
        await backend.call_tool("list_queue", {})


def _mock_factory(handler: Callable[[httpx.Request], httpx.Response]) -> McpHttpClientFactory:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

    return factory


async def test_rejected_auth_raises_backend_unavailable() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad secret"})

    backend = RemoteBackend(
        "homestudio",
        "homestudio:read",
        STUB_URL,
        {},
        httpx_client_factory=_mock_factory(unauthorized),
    )

    with pytest.raises(BackendUnavailable, match="^homestudio: .*401 Unauthorized[^\n]*$"):
        await backend.list_tools()


async def test_timeout_raises_backend_unavailable() -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    backend = RemoteBackend(
        "automations",
        "automations:read",
        STUB_URL,
        {},
        httpx_client_factory=_mock_factory(hang),
    )

    with pytest.raises(BackendUnavailable, match="^automations: "):
        await backend.call_tool("list_devices", {})


async def test_hung_backend_hits_deadline() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(10)
        return httpx.Response(200)

    backend = RemoteBackend(
        "automations",
        "automations:read",
        STUB_URL,
        {},
        timeout_s=0.2,
        httpx_client_factory=_mock_factory(hang),
    )

    with anyio.fail_after(5):
        with pytest.raises(BackendUnavailable, match="^automations: timed out$"):
            await backend.list_tools()


async def test_json_rpc_error_raises_backend_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        error = {"code": -32600, "message": "initialize refused"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})

    backend = RemoteBackend(
        "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=_mock_factory(refuse)
    )

    with pytest.raises(BackendUnavailable, match="^homestudio: initialize refused$"):
        await backend.list_tools()


async def test_programming_errors_are_not_disguised_as_unavailable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise ValueError("bug in the client stack")

    backend = RemoteBackend(
        "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=_mock_factory(broken)
    )

    with pytest.raises(BaseException) as caught:
        await backend.list_tools()
    assert not isinstance(caught.value, BackendUnavailable)


def test_ca_file_factory_verifies_with_that_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    cafiles: list[str | None] = []
    real = ssl.create_default_context

    def spy(*args: object, cafile: str | None = None, **kwargs: object) -> ssl.SSLContext:
        cafiles.append(cafile)
        return real(*args, cafile=cafile, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends.ssl, "create_default_context", spy)

    client = client_factory_for(certifi.where())(headers={"X-Test": "1"})

    assert isinstance(client, httpx.AsyncClient)
    assert cafiles == [certifi.where()]
    assert client.headers["X-Test"] == "1"
    assert client.timeout.read == 300.0


async def test_missing_ca_file_raises_backend_unavailable(tmp_path: Path) -> None:
    missing = str(tmp_path / "absent-ca.pem")
    backend = RemoteBackend(
        "automations", "automations:read", "https://127.0.0.1:9/mcp", {}, ca_file=missing
    )

    with pytest.raises(BackendUnavailable, match="^automations: "):
        await backend.list_tools()


def test_no_ca_file_uses_sdk_default_factory() -> None:
    assert client_factory_for(None) is backends.create_mcp_http_client


def _env() -> dict[str, str]:
    return {"HOMESTUDIO_MCP_SECRET": "hs-secret", "AUTOMATIONS_MCP_TOKEN": "auto-token"}


def test_build_backends_headers(config: GatewayConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _env().items():
        monkeypatch.setenv(key, value)

    built = build_backends(config, os.environ)

    assert [b.name for b in built] == ["homestudio", "automations"]
    homestudio, automations = built
    assert isinstance(homestudio, RemoteBackend)
    assert isinstance(automations, RemoteBackend)
    assert dict(homestudio.headers) == {"X-Gateway-Secret": "hs-secret"}
    assert dict(automations.headers) == {"Authorization": "Bearer auto-token"}
    assert homestudio.allow_tools is None
    assert automations.allow_tools is not None and "recent_history" in automations.allow_tools
    assert automations.ca_file == "/etc/pdl/automations-ca.pem"
    assert (homestudio.scope, automations.scope) == ("homestudio:read", "automations:read")


def test_build_backends_missing_env_is_config_error(
    config: GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMESTUDIO_MCP_SECRET", raising=False)
    env = {"AUTOMATIONS_MCP_TOKEN": "auto-token"}

    with pytest.raises(ConfigError, match="HOMESTUDIO_MCP_SECRET"):
        build_backends(config, env)


def test_build_backends_empty_env_is_config_error(config: GatewayConfig) -> None:
    env = {**_env(), "AUTOMATIONS_MCP_TOKEN": ""}

    with pytest.raises(ConfigError, match="AUTOMATIONS_MCP_TOKEN"):
        build_backends(config, env)


def test_headers_are_copied_not_shared() -> None:
    headers = {"X-Gateway-Secret": "a"}
    backend = RemoteBackend("homestudio", "homestudio:read", STUB_URL, headers)

    headers["X-Gateway-Secret"] = "b"

    assert backend.headers["X-Gateway-Secret"] == "a"


def test_build_backends_rejects_backend_without_credentials(config: GatewayConfig) -> None:
    homestudio = dataclasses.replace(config.backends[0], secret_env=None)
    no_auth = dataclasses.replace(config, backends=(homestudio,))

    with pytest.raises(ConfigError, match="homestudio"):
        build_backends(no_auth, _env())


def test_build_backends_rejects_secret_without_header(config: GatewayConfig) -> None:
    homestudio = dataclasses.replace(config.backends[0], secret_header=None)
    headerless = dataclasses.replace(config, backends=(homestudio,))

    with pytest.raises(ConfigError, match="secret_header"):
        build_backends(headerless, _env())
