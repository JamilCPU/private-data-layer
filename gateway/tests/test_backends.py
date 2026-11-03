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

from .backend_helpers import fake_server, mock_factory
from .conftest import STUB_URL, RequestRecorder, serve_stub

AsgiFactory = Callable[[ASGIApp], McpHttpClientFactory]
SECRET = "s3cret-value"
HANDSHAKE = ["initialize", "notifications/initialized"]


def _homestudio(stub: RequestRecorder, factory: AsgiFactory) -> RemoteBackend:
    return RemoteBackend(
        "homestudio",
        "homestudio:read",
        STUB_URL,
        {"X-Gateway-Secret": SECRET},
        httpx_client_factory=factory(stub),
    )


async def test_list_tools_passthrough(
    homestudio_stub: RequestRecorder, asgi_factory: AsgiFactory
) -> None:
    backend = _homestudio(homestudio_stub, asgi_factory)

    tools = await backend.list_tools()

    assert {t.name for t in tools} == {"get_taste_profile", "list_queue"}
    assert homestudio_stub.requests, "the stub saw no requests"
    assert all(r.get("x-gateway-secret") == SECRET for r in homestudio_stub.requests)
    assert homestudio_stub.methods == [*HANDSHAKE, "tools/list"]


async def test_allowlist_filters_list_and_call(
    automations_stub: RequestRecorder, asgi_factory: AsgiFactory
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
    automations_stub: RequestRecorder, asgi_factory: AsgiFactory
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
    assert automations_stub.methods == [*HANDSHAKE, "tools/call"], "no hidden tools/list"


def explode(device: str) -> dict:
    raise RuntimeError(f"cannot reach {device}")


async def test_tool_error_result_is_passed_through(asgi_factory: AsgiFactory) -> None:
    async for stub in serve_stub({"explode": explode}, "automations"):
        backend = RemoteBackend(
            "automations",
            "automations:read",
            STUB_URL,
            {},
            httpx_client_factory=asgi_factory(stub),
        )

        result = await backend.call_tool("explode", {"device": "pi-host"})

    assert result.isError
    assert isinstance(result.content[0], mcp.types.TextContent)
    assert "cannot reach pi-host" in result.content[0].text


async def test_call_tool_passes_arguments(
    automations_stub: RequestRecorder, asgi_factory: AsgiFactory
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


async def test_rejected_auth_raises_backend_unavailable() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad secret"})

    backend = RemoteBackend(
        "homestudio",
        "homestudio:read",
        STUB_URL,
        {},
        httpx_client_factory=mock_factory(unauthorized),
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
        httpx_client_factory=mock_factory(hang),
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
        httpx_client_factory=mock_factory(hang),
    )

    with anyio.fail_after(5):
        with pytest.raises(BackendUnavailable, match="^automations: timed out$"):
            await backend.list_tools()


async def test_wrong_shaped_result_raises_backend_unavailable() -> None:
    backend = RemoteBackend(
        "homestudio",
        "homestudio:read",
        STUB_URL,
        {},
        httpx_client_factory=fake_server({"tools/list": {"tools": "not-a-list"}}),
    )

    with pytest.raises(BackendUnavailable, match="^homestudio: .*validation error"):
        await backend.list_tools()


async def test_json_rpc_error_raises_backend_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        error = {"code": -32600, "message": "initialize refused"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})

    backend = RemoteBackend(
        "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=mock_factory(refuse)
    )

    with pytest.raises(BackendUnavailable, match="^homestudio: initialize refused$"):
        await backend.list_tools()


async def test_programming_errors_are_not_disguised_as_unavailable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise ValueError("bug in the client stack")

    backend = RemoteBackend(
        "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=mock_factory(broken)
    )

    with pytest.raises(ValueError, match="bug in the client stack"):
        await backend.list_tools()


async def test_factory_errors_propagate_unchanged() -> None:
    def bad_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        raise TypeError("factory misconfigured")

    backend = RemoteBackend(
        "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=bad_factory
    )

    with pytest.raises(TypeError, match="factory misconfigured"):
        await backend.list_tools()


def test_ca_file_factory_verifies_with_that_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    cafiles: list[str | None] = []
    real = ssl.create_default_context

    def spy(*args: object, cafile: str | None = None, **kwargs: object) -> ssl.SSLContext:
        cafiles.append(cafile)
        return real(*args, cafile=cafile, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backends.ssl, "create_default_context", spy)

    factory = client_factory_for(certifi.where())
    client = factory(headers={"X-Test": "1"})
    factory()

    assert isinstance(client, httpx.AsyncClient)
    assert cafiles == [certifi.where()], "the TLS context is built once, not per client"
    assert client.headers["X-Test"] == "1"
    assert client.timeout.read == 300.0


def test_no_ca_file_uses_sdk_default_factory() -> None:
    assert client_factory_for(None) is backends.create_mcp_http_client


def _env() -> dict[str, str]:
    return {"HOMESTUDIO_MCP_SECRET": "hs-secret", "AUTOMATIONS_MCP_TOKEN": "auto-token"}


def _with_automations(config: GatewayConfig, **changes: object) -> GatewayConfig:
    homestudio, automations = config.backends
    return dataclasses.replace(
        config, backends=(homestudio, dataclasses.replace(automations, **changes))
    )


def test_build_backends_headers(config: GatewayConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _env().items():
        monkeypatch.setenv(key, value)

    built = build_backends(_with_automations(config, ca_file=certifi.where()), os.environ)

    assert [b.name for b in built] == ["homestudio", "automations"]
    homestudio, automations = built
    assert isinstance(homestudio, RemoteBackend)
    assert isinstance(automations, RemoteBackend)
    assert dict(homestudio.headers) == {"X-Gateway-Secret": "hs-secret"}
    assert dict(automations.headers) == {"Authorization": "Bearer auto-token"}
    assert homestudio.allow_tools is None
    assert automations.allow_tools is not None and "recent_history" in automations.allow_tools
    assert automations.ca_file == certifi.where()
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


def test_build_backends_missing_ca_file_is_config_error(
    config: GatewayConfig, tmp_path: Path
) -> None:
    missing = str(tmp_path / "absent-ca.pem")

    with pytest.raises(ConfigError, match="absent-ca.pem"):
        build_backends(_with_automations(config, ca_file=missing), _env())


def test_build_backends_unreadable_ca_file_is_config_error(
    config: GatewayConfig, tmp_path: Path
) -> None:
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate", encoding="utf-8")

    with pytest.raises(ConfigError, match="garbage.pem"):
        build_backends(_with_automations(config, ca_file=str(garbage)), _env())


def test_build_backends_ca_file_needs_https(config: GatewayConfig) -> None:
    changed = _with_automations(
        config, url="http://automations-mcp:7702/mcp", ca_file=certifi.where()
    )

    with pytest.raises(ConfigError, match="ca_file needs an https url"):
        build_backends(changed, _env())


@pytest.mark.parametrize(
    "url",
    ["ftp://automations-mcp/mcp", "https:///mcp", "automations-mcp/mcp", "https://[::1/mcp"],
)
def test_build_backends_rejects_bad_url(config: GatewayConfig, url: str) -> None:
    changed = _with_automations(config, url=url, ca_file=None)

    with pytest.raises(ConfigError, match="url must be"):
        build_backends(changed, _env())
