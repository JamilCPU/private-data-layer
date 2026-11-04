"""Tests for the web app: chat page, gateway login, chat API, OpenAI-compatible endpoint."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from pdl_assistant.agent import Reply
from pdl_assistant.auth import GatewayLogin
from pdl_assistant.config import AssistantConfig, AssistantSecrets
from pdl_assistant.memory import MemoryStore
from pdl_assistant.secrets import hash_secret
from pdl_assistant.web import LOGIN_LIMIT, build_app

from .conftest import GATEWAY_URL, serve_gateway

PUBLIC_URL = "https://assistant.test"
API_TOKEN = "api-token-for-tests"
COOKIE_SECRET = "c" * 40
MODEL = "qwen-test"
DEFAULT_REPLY = Reply("Hi there.", ("homestudio_get_taste_profile",), False, False)


def _config(tmp_path: Path, public_url: str = PUBLIC_URL) -> AssistantConfig:
    return AssistantConfig(
        gateway_url=GATEWAY_URL,
        ollama_url="http://ollama.test:11434",
        model=MODEL,
        client_secret_file=str(tmp_path / "client.secret"),
        secrets_file=str(tmp_path / "secrets.yaml"),
        db_path=str(tmp_path / "assistant.db"),
        public_url=public_url,
    )


@dataclass
class FakeAgent:
    """Stores the turn like the real agent and returns a scripted reply."""

    memory: MemoryStore
    script: Reply = DEFAULT_REPLY
    calls: list[tuple[int, str]] = field(default_factory=list)

    async def reply(self, conversation_id: int, text: str) -> Reply:
        self.calls.append((conversation_id, text))
        self.memory.add_message(conversation_id, "user", text)
        self.memory.add_message(conversation_id, "assistant", self.script.text)
        return self.script


@dataclass
class Rig:
    client: httpx.AsyncClient
    gateway: httpx.AsyncClient
    agent: FakeAgent
    memory: MemoryStore


@pytest.fixture
def make_rig(tmp_path: Path):
    async def make(public_url: str = PUBLIC_URL) -> AsyncIterator[Rig]:
        memory = MemoryStore(tmp_path / "assistant.db")
        agent = FakeAgent(memory)
        secrets = AssistantSecrets(
            {"tester": hash_secret(API_TOKEN, iterations=1000)}, COOKIE_SECRET
        )
        try:
            async with serve_gateway() as (_gw_app, gw_http):
                login = GatewayLogin(GATEWAY_URL, public_url, memory, http=gw_http)
                app = build_app(_config(tmp_path, public_url), secrets, agent, login, memory)
                transport = httpx.ASGITransport(app=app)
                async with (
                    LifespanManager(app),
                    httpx.AsyncClient(transport=transport, base_url=public_url) as client,
                ):
                    yield Rig(client, gw_http, agent, memory)
        finally:
            memory.close()

    return make


@pytest.fixture
async def rig(make_rig) -> AsyncIterator[Rig]:
    async for r in make_rig():
        yield r


async def _log_in(rig: Rig) -> httpx.Response:
    """Walk the whole login: /login → gateway /authorize (auto-approves) → /oauth/callback."""
    start = await rig.client.get("/login")
    assert start.status_code == 302
    approved = await rig.gateway.get(start.headers["location"])
    assert approved.status_code == 302
    return await rig.client.get(approved.headers["location"])


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {h.split("=", 1)[0]: h for h in resp.headers.get_list("set-cookie")}


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


# --- pages and login -------------------------------------------------------------------


async def test_healthz_open(rig: Rig) -> None:
    resp = await rig.client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_root_redirects_to_login_without_cookie(rig: Rig) -> None:
    resp = await rig.client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    rig.client.cookies.set("pdl_session", "forged.value.sig", domain="assistant.test")
    assert (await rig.client.get("/")).status_code == 302


async def test_login_sets_state_cookie_and_redirects_to_gateway(rig: Rig) -> None:
    resp = await rig.client.get("/login")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{GATEWAY_URL}/authorize?")
    cookie = _set_cookies(resp)["pdl_login"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
    assert "Max-Age=600" in cookie


async def test_callback_without_state_cookie_is_400(rig: Rig) -> None:
    start = await rig.client.get("/login")
    approved = await rig.gateway.get(start.headers["location"])
    rig.client.cookies.clear()
    resp = await rig.client.get(approved.headers["location"])
    assert resp.status_code == 400
    assert "login did not start here" in resp.text
    assert "pdl_session" not in _set_cookies(resp)


async def test_callback_with_other_state_is_400(rig: Rig) -> None:
    await rig.client.get("/login")
    resp = await rig.client.get("/oauth/callback", params={"code": "x", "state": "other"})
    assert resp.status_code == 400


async def test_callback_sets_session_cookie_flags(rig: Rig) -> None:
    resp = await _log_in(rig)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    cookies = _set_cookies(resp)
    session = cookies["pdl_session"]
    assert "HttpOnly" in session and "SameSite=lax" in session and "Secure" in session
    assert "Max-Age=43200" in session and "Path=/" in session
    assert "Max-Age=0" in cookies["pdl_login"]
    page = await rig.client.get("/")
    assert page.status_code == 200
    assert "<textarea" in page.text


async def test_session_cookie_not_secure_on_http(make_rig) -> None:
    async for rig in make_rig("http://assistant.test"):
        cookies = _set_cookies(await _log_in(rig))
        assert "Secure" not in cookies["pdl_session"]


async def test_callback_refused_is_403(rig: Rig) -> None:
    start = await rig.client.get("/login")
    state = dict(httpx.URL(start.headers["location"]).params)["state"]
    resp = await rig.client.get("/oauth/callback", params={"code": "bogus", "state": state})
    assert resp.status_code == 403
    assert "login refused" in resp.text
    assert "pdl_session" not in _set_cookies(resp)


async def test_callback_gateway_error_is_403_and_escaped(rig: Rig) -> None:
    resp = await rig.client.get("/oauth/callback", params={"error": "<b>access_denied</b>"})
    assert resp.status_code == 403
    assert "&lt;b&gt;access_denied" in resp.text
    assert "<b>" not in resp.text


async def test_login_rate_limited(rig: Rig) -> None:
    for _ in range(LOGIN_LIMIT):
        assert (await rig.client.get("/login")).status_code == 302
    resp = await rig.client.get("/login")
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("text/plain")


async def test_security_headers_on_html(rig: Rig) -> None:
    await _log_in(rig)
    resp = await rig.client.get("/")
    assert resp.headers["content-type"].startswith("text/html")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


async def test_logout_kills_cookie(rig: Rig) -> None:
    await _log_in(rig)
    stolen = rig.client.cookies.get("pdl_session")
    resp = await rig.client.post("/logout")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    assert "Max-Age=0" in _set_cookies(resp)["pdl_session"]
    rig.client.cookies.set("pdl_session", stolen, domain="assistant.test")
    assert (await rig.client.get("/")).status_code == 302  # revoked server-side too


# --- chat API ------------------------------------------------------------------------------


async def test_chat_requires_cookie(rig: Rig) -> None:
    for resp in (
        await rig.client.post("/api/chat", json={"conversation_id": None, "message": "hi"}),
        await rig.client.get("/api/conversations"),
        await rig.client.get("/api/conversations/1/messages"),
    ):
        assert resp.status_code == 401
        assert resp.json() == {"error": "login required"}
    assert rig.agent.calls == []


async def test_chat_round_trip_creates_conversation(rig: Rig) -> None:
    await _log_in(rig)
    text = "What music do I like? " * 5
    resp = await rig.client.post("/api/chat", json={"conversation_id": None, "message": text})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "conversation_id": body["conversation_id"],
        "reply": "Hi there.",
        "offline": False,
        "tool_calls": ["homestudio_get_taste_profile"],
        "step_capped": False,
    }
    cid = body["conversation_id"]
    conversation = rig.memory.get_conversation(cid)
    assert conversation is not None and conversation.title == " ".join(text.split())[:60]
    again = await rig.client.post("/api/chat", json={"conversation_id": cid, "message": "more"})
    assert again.json()["conversation_id"] == cid
    assert rig.agent.calls == [(cid, text), (cid, "more")]


async def test_chat_offline_reply(rig: Rig) -> None:
    await _log_in(rig)
    rig.agent.script = Reply("Brain is offline.", (), offline=True, step_capped=False)
    resp = await rig.client.post("/api/chat", json={"conversation_id": None, "message": "hi"})
    assert resp.json()["offline"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"conversation_id": None, "message": ""},
        {"conversation_id": None, "message": "   "},
        {"conversation_id": None, "message": "x" * 4001},
        {"conversation_id": None, "message": 5},
        {"conversation_id": "1", "message": "hi"},
        {"conversation_id": True, "message": "hi"},
        {"message": "hi", "conversation_id": None, "extra": 1},
        ["not", "an", "object"],
    ],
)
async def test_chat_message_length_422(rig: Rig, body: object) -> None:
    await _log_in(rig)
    resp = await rig.client.post("/api/chat", json=body)
    assert resp.status_code == 422
    assert "error" in resp.json()
    assert rig.agent.calls == []


async def test_chat_invalid_json_and_unknown_conversation(rig: Rig) -> None:
    await _log_in(rig)
    bad = await rig.client.post("/api/chat", content=b"{nope", headers={"content-type": "x"})
    assert bad.status_code == 422
    missing = await rig.client.post("/api/chat", json={"conversation_id": 999, "message": "hi"})
    assert missing.status_code == 404
    ok = await rig.client.post("/api/chat", json={"conversation_id": None, "message": "x" * 4000})
    assert ok.status_code == 200


async def test_conversations_and_messages_endpoints(rig: Rig) -> None:
    await _log_in(rig)
    first = await rig.client.post("/api/chat", json={"conversation_id": None, "message": "one"})
    second = await rig.client.post("/api/chat", json={"conversation_id": None, "message": "two"})
    listed = (await rig.client.get("/api/conversations")).json()["conversations"]
    assert [c["id"] for c in listed] == [
        second.json()["conversation_id"],
        first.json()["conversation_id"],
    ]
    assert set(listed[0]) == {"id", "created_at", "title"}
    cid = first.json()["conversation_id"]
    messages = (await rig.client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "one"),
        ("assistant", "Hi there."),
    ]
    assert set(messages[0]) == {"role", "content", "tool_name", "created_at"}
    assert (await rig.client.get("/api/conversations/999/messages")).status_code == 404


# --- OpenAI-compatible endpoint -------------------------------------------------------------


async def test_openai_requires_bearer(rig: Rig) -> None:
    await _log_in(rig)  # a browser session does not open /v1
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}):
        models = await rig.client.get("/v1/models", headers=headers)
        chat = await rig.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
        for resp in (models, chat):
            assert resp.status_code == 401
            assert resp.headers["www-authenticate"] == "Bearer"
            assert "error" in resp.json()
    assert rig.agent.calls == []


async def test_openai_models_and_completion_shape(rig: Rig) -> None:
    models = await rig.client.get("/v1/models", headers=_bearer())
    assert models.json() == {
        "object": "list",
        "data": [{"id": MODEL, "object": "model", "owned_by": "local"}],
    }
    payload = {
        "model": "anything",
        "messages": [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": "last"}]},
        ],
    }
    resp = await rig.client.post("/v1/chat/completions", json=payload, headers=_bearer())
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == MODEL
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi there."},
            "finish_reason": "stop",
        }
    ]
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert [text for _cid, text in rig.agent.calls] == ["last"]


async def test_openai_user_field_selects_conversation(rig: Rig) -> None:
    cid = rig.memory.new_conversation("existing")
    msgs = [{"role": "user", "content": "hi"}]
    resp = await rig.client.post(
        "/v1/chat/completions", json={"messages": msgs, "user": str(cid)}, headers=_bearer()
    )
    assert resp.status_code == 200
    await rig.client.post(
        "/v1/chat/completions", json={"messages": msgs, "user": "alice"}, headers=_bearer()
    )
    unknown = await rig.client.post(
        "/v1/chat/completions", json={"messages": msgs, "user": "999"}, headers=_bearer()
    )
    assert unknown.status_code == 404
    assert rig.agent.calls[0][0] == cid
    assert rig.agent.calls[1][0] != cid


async def test_openai_stream_true_is_400(rig: Rig) -> None:
    resp = await rig.client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(),
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "streaming is not supported"}
    assert rig.agent.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [{"role": "system", "content": "only system"}]},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "user", "content": "x" * 4001}]},
        {"messages": "nope"},
        ["not an object"],
    ],
)
async def test_openai_bad_request_is_400(rig: Rig, body: object) -> None:
    resp = await rig.client.post("/v1/chat/completions", json=body, headers=_bearer())
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert rig.agent.calls == []


async def test_login_gateway_unreachable_is_502(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path / "a.db")
    agent = FakeAgent(memory)
    secrets = AssistantSecrets({}, COOKIE_SECRET)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as gw_http:
            login = GatewayLogin(GATEWAY_URL, PUBLIC_URL, memory, http=gw_http)
            app = build_app(_config(tmp_path), secrets, agent, login, memory)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as client:
                resp = await client.get("/login")
        assert resp.status_code == 502
    finally:
        memory.close()
