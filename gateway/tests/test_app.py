"""End-to-end tests of the assembled gateway app, in-process over ASGI."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import mcp.types
import pytest
import yaml
from asgi_lifespan import LifespanManager
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette

from pdl_gateway import __main__ as entry
from pdl_gateway.app import build_app
from pdl_gateway.audit import AuditEntry
from pdl_gateway.backends import RemoteBackend
from pdl_gateway.config import ConfigError, GatewayConfig, GatewaySecrets
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.secrets import hash_secret
from pdl_gateway.store import TokenStore

from .conftest import EXAMPLE_CONFIG, ISSUER, PA_SECRET, STUB_URL, FakeClock
from .oauth_helpers import client_credentials, session

MCP_URL = f"{ISSUER}/mcp"
PRM_URL = f"{ISSUER}/.well-known/oauth-protected-resource/mcp"
INTERACTIVE_TOOLS = {
    "homestudio_get_taste_profile",
    "homestudio_list_queue",
    "automations_list_devices",
    "automations_recent_history",
}
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": mcp.types.LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
async def gateway_app(
    config: GatewayConfig,
    secrets: GatewaySecrets,
    store: TokenStore,
    homestudio_stub,
    automations_stub,
    asgi_factory,
) -> AsyncIterator[Starlette]:
    backends = [
        RemoteBackend(
            "homestudio",
            "homestudio:read",
            STUB_URL,
            {},
            httpx_client_factory=asgi_factory(homestudio_stub),
        ),
        RemoteBackend(
            "automations",
            "automations:read",
            STUB_URL,
            {},
            allow_tools=frozenset({"list_devices", "recent_history"}),
            httpx_client_factory=asgi_factory(automations_stub),
        ),
    ]
    app = build_app(config, secrets, backends, store=store)
    async with LifespanManager(app):
        yield app


@pytest.fixture
async def gw(gateway_app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app), base_url=ISSUER
    ) as client:
        yield client


@asynccontextmanager
async def mcp_client(app: Starlette, token: str) -> AsyncIterator[ClientSession]:
    """An initialized MCP client session on the gateway's ``/mcp``, as ``token``'s holder."""
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER,
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
        streamable_http_client(MCP_URL, http_client=http) as (read, write, _),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


async def _probe(gw: httpx.AsyncClient, token: str | None) -> httpx.Response:
    headers = dict(_MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await gw.post("/mcp", json=_INITIALIZE, headers=headers)


def _assert_401(resp: httpx.Response) -> None:
    assert resp.status_code == 401, resp.text
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PRM_URL}"' in challenge
    assert resp.json()["error"] == "invalid_token"


def _text(result: mcp.types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, mcp.types.TextContent)
    return block.text


def _audit_entries(config: GatewayConfig) -> list[AuditEntry]:
    lines: list[str] = []
    for path in sorted(Path(config.audit_dir).glob("audit-*.jsonl")):
        lines.extend(line for line in path.read_text(encoding="utf-8").splitlines() if line)
    return [AuditEntry.from_json(line) for line in lines]


async def _claude_code_calls(app: Starlette, gw: httpx.AsyncClient) -> set[str]:
    """DCR + approve + token, then one allowed and three unknown calls; return listed names."""
    _, tokens = await session(gw)
    async with mcp_client(app, tokens["access_token"]) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        ok = await client.call_tool("homestudio_get_taste_profile", {})
        assert ok.isError is False
        assert "jazz" in _text(ok)
        for name in ("bank_spend_summary", "automations_reboot_device", "gateway_audit_search"):
            result = await client.call_tool(name, {"since": "2026-01-01"})
            assert result.isError is True
            assert _text(result) == f"unknown tool: {name}"
    return names


async def test_healthz_is_open(gw: httpx.AsyncClient) -> None:
    resp = await gw.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_mcp_without_token_is_401_with_resource_metadata(gw: httpx.AsyncClient) -> None:
    _assert_401(await _probe(gw, None))
    _assert_401(await _probe(gw, "not-a-real-token"))
    _assert_401(await gw.get("/mcp", headers=_MCP_HEADERS))
    _assert_401(await gw.delete("/mcp"))
    prm = await gw.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200
    assert prm.json()["resource"] == MCP_URL


async def test_expired_token_is_401(
    gw: httpx.AsyncClient,
    provider: GatewayAuthProvider,
    clock: FakeClock,
    config: GatewayConfig,
) -> None:
    fresh = provider.issue_client_credentials("personal-assistant", PA_SECRET).access_token
    assert (await _probe(gw, fresh)).status_code == 200
    # Mint a token whose lifetime ended before now: the app's provider runs on real time.
    clock.advance(-(config.access_token_ttl_s + 60))
    stale = provider.issue_client_credentials("personal-assistant", PA_SECRET).access_token
    _assert_401(await _probe(gw, stale))


async def test_claude_code_flow_sees_interactive_tools(
    gateway_app: Starlette, gw: httpx.AsyncClient, config: GatewayConfig
) -> None:
    assert await _claude_code_calls(gateway_app, gw) == INTERACTIVE_TOOLS

    entries = _audit_entries(config)
    assert [e.tool for e in entries] == [
        "homestudio_get_taste_profile",
        "bank_spend_summary",
        "automations_reboot_device",
        "gateway_audit_search",
    ]
    assert {e.client_name for e in entries} == {"Claude Code"}
    assert [e.error for e in entries] == [None, "unknown tool", "unknown tool", "unknown tool"]


async def test_assistant_flow_sees_audit_tool(
    gateway_app: Starlette, gw: httpx.AsyncClient
) -> None:
    await _claude_code_calls(gateway_app, gw)
    resp = await client_credentials(gw, "personal-assistant", PA_SECRET)
    assert resp.status_code == 200, resp.text

    async with mcp_client(gateway_app, resp.json()["access_token"]) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == INTERACTIVE_TOOLS | {"gateway_audit_search"}
        today = datetime.now(UTC).date().isoformat()
        result = await client.call_tool("gateway_audit_search", {"since": today})

    assert result.isError is False
    entries: list[dict[str, Any]] = json.loads(_text(result))["entries"]
    tools = {e["tool"] for e in entries}
    assert {"homestudio_get_taste_profile", "bank_spend_summary"} <= tools
    assert {e["client_name"] for e in entries} == {"Claude Code"}


async def test_revoked_token_is_401(gw: httpx.AsyncClient) -> None:
    client, tokens = await session(gw)
    assert (await _probe(gw, tokens["access_token"])).status_code == 200
    form = {"token": tokens["access_token"], "client_id": client["client_id"], "client_secret": ""}
    resp = await gw.post("/revoke", data=form)
    assert resp.status_code == 200, resp.text
    _assert_401(await _probe(gw, tokens["access_token"]))


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name
        self.scope = f"{name}:read"

    async def list_tools(self) -> list[mcp.types.Tool]:  # pragma: no cover - never called
        return []

    async def call_tool(  # pragma: no cover - never called
        self, name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        raise NotImplementedError


@pytest.mark.parametrize(
    ("names", "clash"),
    [
        (["home", "home_studio", "home_x"], "'home' and 'home_studio'; 'home' and 'home_x'"),
        (["gateway_extra"], "'gateway' and 'gateway_extra'"),
        (["gateway"], r"duplicate backend name\(s\): 'gateway'"),
    ],
    ids=["prefix", "prefix-of-audit-backend", "duplicate"],
)
def test_build_app_rejects_ambiguous_backend_names(
    names: list[str], clash: str, config: GatewayConfig, secrets: GatewaySecrets, store: TokenStore
) -> None:
    with pytest.raises(ConfigError, match=clash):
        build_app(config, secrets, [_Named(n) for n in names], store=store)


async def test_build_app_owns_store_and_audit_when_not_given(
    config: GatewayConfig, secrets: GatewaySecrets
) -> None:
    app = build_app(config, secrets, [])
    transport = httpx.ASGITransport(app=app)
    async with LifespanManager(app), httpx.AsyncClient(transport=transport, base_url=ISSUER) as gw:
        resp = await client_credentials(gw, "personal-assistant", PA_SECRET)
        assert resp.status_code == 200, resp.text
    assert Path(config.db_path).exists()


@pytest.mark.parametrize(
    "resource", ["http://evil.example/mcp", None], ids=["foreign-resource", "no-resource"]
)
async def test_token_for_another_resource_is_401(
    gw: httpx.AsyncClient,
    provider: GatewayAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
    resource: str | None,
) -> None:
    token = provider.issue_client_credentials("personal-assistant", PA_SECRET).access_token
    assert (await _probe(gw, token)).status_code == 200
    original = GatewayAuthProvider.load_access_token

    async def foreign(self: GatewayAuthProvider, value: str) -> AccessToken | None:
        loaded = await original(self, value)
        return loaded.model_copy(update={"resource": resource}) if loaded else None

    # The app builds its own provider, so the class is patched: its tokens now name `resource`.
    monkeypatch.setattr(GatewayAuthProvider, "load_access_token", foreign)
    _assert_401(await _probe(gw, token))


def test_build_app_closes_its_store_when_assembly_fails(
    config: GatewayConfig, secrets: GatewaySecrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("assembly failed")

    closed: list[bool] = []

    class RecordingStore(TokenStore):
        def close(self) -> None:
            closed.append(True)
            super().close()

    monkeypatch.setattr("pdl_gateway.app.TokenStore", RecordingStore)
    monkeypatch.setattr("pdl_gateway.app.build_proxy_server", broken)
    with pytest.raises(RuntimeError, match="assembly failed"):
        build_app(config, secrets, [])
    assert closed == [True]


# --- entry point -------------------------------------------------------------


def _write_files(tmp_path: Path) -> tuple[Path, Path]:
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    raw["db_path"] = str(tmp_path / "gateway.db")
    raw["audit_dir"] = str(tmp_path / "audit")
    for backend in raw["backends"]:
        backend.pop("ca_file", None)
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    secrets_raw = {
        "approval_passphrase": hash_secret("pass", iterations=1000),
        "client_secrets": {"personal-assistant": hash_secret(PA_SECRET, iterations=1000)},
    }
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(yaml.safe_dump(secrets_raw), encoding="utf-8")
    return config_path, secrets_path


def test_main_bad_config_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.yaml"
    assert entry.main(["--config", str(missing), "--secrets", str(missing)]) == 1
    assert "nope.yaml" in capsys.readouterr().err


def test_main_missing_backend_env_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, secrets_path = _write_files(tmp_path)
    monkeypatch.delenv("HOMESTUDIO_MCP_SECRET", raising=False)
    monkeypatch.setenv("GATEWAY_CONFIG", str(config_path))
    monkeypatch.setenv("GATEWAY_SECRETS", str(secrets_path))
    assert entry.main([]) == 1
    assert "HOMESTUDIO_MCP_SECRET" in capsys.readouterr().err


def test_main_runs_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, secrets_path = _write_files(tmp_path)
    monkeypatch.setenv("HOMESTUDIO_MCP_SECRET", "s1")
    monkeypatch.setenv("AUTOMATIONS_MCP_TOKEN", "t1")
    calls: list[tuple[Any, str, int]] = []

    def fake_run(app: Any, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    assert entry.main(["--config", str(config_path), "--secrets", str(secrets_path)]) == 0
    [(app, host, port)] = calls
    assert isinstance(app, Starlette)
    assert (host, port) == ("0.0.0.0", 8787)
