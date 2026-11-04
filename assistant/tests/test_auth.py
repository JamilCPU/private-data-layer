"""Tests for front-door auth: signed session cookies, gateway login, bearer tokens."""

import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from itsdangerous import TimestampSigner

from pdl_assistant import auth as auth_module
from pdl_assistant.auth import (
    MAX_PENDING_LOGINS,
    GatewayLogin,
    LoginError,
    SessionSigner,
    bearer_ok,
    issue_session,
    session_from_cookie,
)
from pdl_assistant.memory import MemoryStore
from pdl_assistant.secrets import hash_secret

from .conftest import GATEWAY_URL, serve_gateway

PUBLIC_URL = "https://assistant.test"
CALLBACK = f"{PUBLIC_URL}/oauth/callback"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path: Path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


# --- SessionSigner -------------------------------------------------------------------


def test_signer_round_trip() -> None:
    signer = SessionSigner("cookie-secret", ttl_s=60)
    cookie = signer.issue("sess-1")
    assert cookie != "sess-1"
    assert signer.verify(cookie) == "sess-1"


def test_tampered_cookie_is_none() -> None:
    signer = SessionSigner("cookie-secret", ttl_s=60)
    cookie = signer.issue("sess-1")
    assert signer.verify("sess-2" + cookie[len("sess-1") :]) is None
    assert signer.verify(cookie[:-1] + ("A" if cookie[-1] != "A" else "B")) is None
    assert SessionSigner("other-secret", ttl_s=60).verify(cookie) is None
    assert signer.verify("") is None
    assert signer.verify("garbage") is None


def test_expired_cookie_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = SessionSigner("cookie-secret", ttl_s=60)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 1_000_000)
    cookie = signer.issue("sess-1")
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 1_000_060)
    assert signer.verify(cookie) == "sess-1"
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 1_000_061)
    assert signer.verify(cookie) is None


# --- sessions --------------------------------------------------------------------------


def test_issue_and_read_session(store: MemoryStore) -> None:
    signer = SessionSigner("cookie-secret", ttl_s=60)
    cookie = issue_session(signer, store)
    session_id = session_from_cookie(signer, store, cookie)
    assert session_id is not None
    assert store.session_valid(session_id)
    assert session_from_cookie(signer, store, None) is None
    assert session_from_cookie(signer, store, "forged") is None
    unknown = signer.issue("not-in-store")
    assert session_from_cookie(signer, store, unknown) is None


def test_revoked_session_is_rejected(store: MemoryStore) -> None:
    signer = SessionSigner("cookie-secret", ttl_s=60)
    cookie = issue_session(signer, store)
    session_id = session_from_cookie(signer, store, cookie)
    assert session_id is not None
    store.revoke_session(session_id)
    assert session_from_cookie(signer, store, cookie) is None


# --- bearer_ok -------------------------------------------------------------------------


def test_bearer_ok_checks_every_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = auth_module.verify_secret

    def spy(secret: str, encoded: str) -> bool:
        calls.append(encoded)
        return real(secret, encoded)

    monkeypatch.setattr(auth_module, "verify_secret", spy)
    hashes = {name: hash_secret(f"tok-{name}", iterations=1) for name in ("a", "b", "c")}
    assert bearer_ok("Bearer tok-a", hashes)  # the first hash matches
    assert calls == list(hashes.values())


def test_bearer_ok() -> None:
    hashes = {
        "laptop": hash_secret("tok-a", iterations=1),
        "phone": hash_secret("tok-b", iterations=1),
    }
    assert bearer_ok("Bearer tok-a", hashes)
    assert bearer_ok("Bearer tok-b", hashes)
    assert bearer_ok("bearer tok-b", hashes)
    assert bearer_ok("BEARER   tok-a  ", hashes)
    assert not bearer_ok("Bearer tok-c", hashes)
    assert not bearer_ok("Basic tok-a", hashes)
    assert not bearer_ok("tok-a", hashes)
    assert not bearer_ok("Bearer ", hashes)
    assert not bearer_ok("", hashes)
    assert not bearer_ok(None, hashes)
    assert not bearer_ok("Bearer tok-a", {})


# --- GatewayLogin ----------------------------------------------------------------------


def _login(store: MemoryStore, http: httpx.AsyncClient, **kwargs) -> GatewayLogin:
    return GatewayLogin(GATEWAY_URL, PUBLIC_URL, store, http=http, **kwargs)


async def _approve(http: httpx.AsyncClient, authorize_url: str) -> dict[str, str]:
    """Follow the (auto-approving) stub /authorize; return the callback query."""
    resp = await http.get(authorize_url)
    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CALLBACK
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def test_ensure_registered_once_persists_client_id(store: MemoryStore) -> None:
    async with serve_gateway() as (app, http):
        client_id = await _login(store, http).ensure_registered()
        assert await _login(store, http).ensure_registered() == client_id
        assert store.get_preference("oauth_client_id") == client_id
        [registration] = app.state.stats.registrations
    assert registration == {
        "client_name": "private-data-layer assistant",
        "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


async def test_ensure_registered_rejected_raises(store: MemoryStore) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_redirect_uri"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        with pytest.raises(LoginError, match="invalid_redirect_uri"):
            await _login(store, http).ensure_registered()
    assert store.get_preference("oauth_client_id") is None


async def test_ensure_registered_unreachable_raises(store: MemoryStore) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        with pytest.raises(LoginError, match="unreachable"):
            await _login(store, http).ensure_registered()


async def test_ensure_registered_without_client_id_raises(store: MemoryStore) -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=["not", "an", "object"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(odd)) as http:
        with pytest.raises(LoginError, match="client_id"):
            await _login(store, http).ensure_registered()


async def test_start_requires_registration(store: MemoryStore) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(LoginError):
            _login(store, http).start()


async def test_start_builds_authorize_url_with_pkce(store: MemoryStore) -> None:
    async with serve_gateway() as (_, http):
        login = _login(store, http)
        client_id = await login.ensure_registered()
        url, state = login.start()
        _, other_state = login.start()
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{GATEWAY_URL}/authorize"
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == client_id
    assert params["redirect_uri"] == CALLBACK
    assert params["code_challenge_method"] == "S256"
    assert params["state"] == state
    assert params["resource"] == f"{GATEWAY_URL}/mcp"
    assert "scope" not in params
    assert "code_verifier" not in params
    assert len(params["code_challenge"]) == 43
    assert state != other_state and len(state) >= 32


async def test_finish_exchanges_code_true(store: MemoryStore) -> None:
    async with serve_gateway() as (app, http):
        login = _login(store, http)
        await login.ensure_registered()
        url, state = login.start()
        callback = await _approve(http, url)
        assert callback["state"] == state
        assert await login.finish(callback["code"], state) is True
        assert await login.finish(callback["code"], state) is False, "state is single-use"
        [params] = app.state.stats.authorize_params
    assert params["state"] == state


async def test_finish_unknown_state_false(store: MemoryStore) -> None:
    async with serve_gateway() as (app, http):
        login = _login(store, http)
        await login.ensure_registered()
        url, _ = login.start()
        callback = await _approve(http, url)
        before = app.state.stats.token_requests
        assert await login.finish(callback["code"], "not-a-state") is False
        assert app.state.stats.token_requests == before, "no exchange for an unknown state"


async def test_finish_expired_state_false(store: MemoryStore) -> None:
    clock = FakeClock()
    async with serve_gateway() as (_, http):
        login = _login(store, http, clock=clock, pending_ttl_s=600)
        await login.ensure_registered()
        url, state = login.start()
        callback = await _approve(http, url)
        clock.now += 601
        assert await login.finish(callback["code"], state) is False


async def test_start_purges_expired_pending(store: MemoryStore) -> None:
    clock = FakeClock()
    async with serve_gateway() as (_, http):
        login = _login(store, http, clock=clock, pending_ttl_s=600)
        await login.ensure_registered()
        url, old_state = login.start()
        callback = await _approve(http, url)
        clock.now += 601
        login.start()
        clock.now -= 601  # even with the clock back, the purged state is gone
        assert await login.finish(callback["code"], old_state) is False


async def test_finish_bad_verifier_false(store: MemoryStore) -> None:
    async with serve_gateway() as (app, http):
        login = _login(store, http)
        await login.ensure_registered()
        url, state = login.start()
        callback = await _approve(http, url)
        # Swap in a challenge for a different verifier so the stub rejects the exchange.
        digest = hashlib.sha256(b"someone-else").digest()
        other = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        app.state.stats.codes[callback["code"]]["code_challenge"] = other
        assert await login.finish(callback["code"], state) is False


async def test_finish_without_access_token_or_unreachable_false(store: MemoryStore) -> None:
    store.set_preference("oauth_client_id", "client-x")

    replies = [
        httpx.Response(200, json={"token_type": "Bearer"}),
        httpx.Response(200, text="not json"),
    ]
    for reply in replies:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r, r=reply: r)) as http:
            login = _login(store, http)
            _, state = login.start()
            assert await login.finish("code", state) is False

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        login = _login(store, http)
        _, state = login.start()
        assert await login.finish("code", state) is False


async def test_owned_client_is_closed(store: MemoryStore) -> None:
    login = GatewayLogin(GATEWAY_URL, PUBLIC_URL, store)
    await login.aclose()
    assert login._http.is_closed


async def test_injected_client_is_not_closed(store: MemoryStore) -> None:
    async with httpx.AsyncClient() as http:
        await _login(store, http).aclose()
        assert not http.is_closed


async def test_pending_logins_are_capped(store: MemoryStore) -> None:
    async with serve_gateway() as (_, http):
        login = _login(store, http)
        await login.ensure_registered()
        url, oldest = login.start()
        callback = await _approve(http, url)
        newer = [login.start()[1] for _ in range(299)]
        assert len(login._pending) <= MAX_PENDING_LOGINS
        assert newer[-1] in login._pending
        assert await login.finish(callback["code"], oldest) is False


async def test_finish_uses_client_id_from_start(store: MemoryStore) -> None:
    async with serve_gateway() as (_, http):
        login = _login(store, http)
        await login.ensure_registered()
        url, state = login.start()
        callback = await _approve(http, url)
        store.set_preference("oauth_client_id", "")  # cleared mid-login: finish must not raise
        assert await login.finish(callback["code"], state) is True


async def test_registration_error_body_is_truncated(store: MemoryStore) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="x" * 1000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        with pytest.raises(LoginError) as info:
            await _login(store, http).ensure_registered()
    assert str(info.value).count("x") == 200
