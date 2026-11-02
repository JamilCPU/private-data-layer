"""Tests for the SQLite token store."""

from pathlib import Path

import pytest
from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthClientInformationFull

from pdl_gateway.store import PendingApproval, StoredToken, TokenStore


@pytest.fixture
def store(tmp_path: Path):
    s = TokenStore(tmp_path / "gw.db")
    yield s
    s.close()


def _client(client_id: str = "cid") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="s",
        redirect_uris=["http://localhost:1/cb"],
        client_name="Claude Code",
    )


def _code(code: str = "c1") -> AuthorizationCode:
    return AuthorizationCode(
        code=code,
        scopes=["homestudio:read"],
        expires_at=1.0e12,
        client_id="cid",
        code_challenge="x",
        redirect_uri="http://localhost:1/cb",
        redirect_uri_provided_explicitly=True,
    )


def _pending(request_id: str = "r1", expires_at: float = 100.0) -> PendingApproval:
    return PendingApproval(
        request_id=request_id,
        client_id="cid",
        client_name="Claude Code",
        params_json='{"state": "abc"}',
        expires_at=expires_at,
    )


def test_client_round_trip(store: TokenStore) -> None:
    client = _client()
    store.save_client(client)
    assert store.get_client("cid") == client
    assert store.get_client("unknown") is None


def test_save_client_overwrites(store: TokenStore) -> None:
    store.save_client(_client())
    store.save_client(_client().model_copy(update={"client_name": "Renamed"}))
    loaded = store.get_client("cid")
    assert loaded is not None and loaded.client_name == "Renamed"


def test_pending_is_single_use_and_expires(store: TokenStore) -> None:
    store.save_pending(_pending("r1", expires_at=100.0))
    assert store.pop_pending("r1", now=50.0) == _pending("r1", expires_at=100.0)
    assert store.pop_pending("r1", now=50.0) is None

    store.save_pending(_pending("r2", expires_at=100.0))
    assert store.pop_pending("r2", now=100.5) is None
    assert store.pop_pending("r2", now=50.0) is None  # expired pop still deletes
    assert store.pop_pending("never-saved", now=0.0) is None


def test_code_round_trip_and_delete(store: TokenStore) -> None:
    code = _code()
    store.save_code(code)
    assert store.get_code("c1") == code
    store.delete_code("c1")
    assert store.get_code("c1") is None
    store.delete_code("c1")  # deleting twice is harmless


def test_token_hashing(store: TokenStore, tmp_path: Path) -> None:
    raw = "super-secret-token-value-123"
    store.save_token("access", raw, "cid", ["homestudio:read"], expires_at=None)
    assert store.load_token("access", raw, now=0) is not None
    store.close()
    data = b"".join(p.read_bytes() for p in tmp_path.glob("gw.db*"))
    assert raw.encode() not in data


def test_load_token_returns_stored_fields(store: TokenStore) -> None:
    store.save_token("refresh", "tok", "cid", ["a:read", "b:read"], expires_at=500)
    assert store.load_token("refresh", "tok", now=100) == StoredToken(
        kind="refresh", client_id="cid", scopes=("a:read", "b:read"), expires_at=500
    )
    assert store.load_token("access", "tok", now=100) is None  # wrong kind
    assert store.load_token("refresh", "other", now=100) is None


def test_load_token_expired_is_none(store: TokenStore) -> None:
    now = 1000
    store.save_token("access", "old", "cid", ["a:read"], expires_at=now - 1)
    store.save_token("access", "edge", "cid", ["a:read"], expires_at=now)
    store.save_token("access", "forever", "cid", ["a:read"], expires_at=None)
    assert store.load_token("access", "old", now) is None
    assert store.load_token("access", "edge", now) is None
    assert store.load_token("access", "forever", now) is not None


def test_revoke_any_kind(store: TokenStore) -> None:
    store.save_token("access", "a-tok", "cid", ["a:read"], expires_at=None)
    store.save_token("refresh", "r-tok", "cid", ["a:read"], expires_at=None)
    assert store.revoke("a-tok") == 1
    assert store.load_token("access", "a-tok", now=0) is None
    assert store.revoke("r-tok") == 1
    assert store.load_token("refresh", "r-tok", now=0) is None
    assert store.revoke("unknown") == 0
    assert store.revoke("a-tok") == 0  # already revoked


def test_revoke_client_tokens(store: TokenStore) -> None:
    store.save_token("access", "a1", "a", ["x:read"], expires_at=None)
    store.save_token("refresh", "a2", "a", ["x:read"], expires_at=None)
    store.save_token("access", "b1", "b", ["x:read"], expires_at=None)
    assert store.revoke_client_tokens("a") == 2
    assert store.load_token("access", "a1", now=0) is None
    assert store.load_token("access", "b1", now=0) is not None
    assert store.revoke_client_tokens("a") == 0


def test_data_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "gw.db"
    first = TokenStore(path)
    first.save_client(_client())
    first.save_token("access", "tok", "cid", ["a:read"], expires_at=None)
    first.close()
    second = TokenStore(path)
    try:
        assert second.get_client("cid") == _client()
        assert second.load_token("access", "tok", now=0) is not None
    finally:
        second.close()
