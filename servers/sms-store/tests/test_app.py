import json
from datetime import UTC, datetime

import httpx
import pytest

import pdl_sms_store.tokens as tokens_mod
from pdl_sms_store.app import build_app
from pdl_sms_store.limits import MAX_BATCH, MAX_BODY_BYTES, RateLimiter
from pdl_sms_store.memory_store import MemoryStore
from pdl_sms_store.tokens import hash_token

TOKEN = "G" * 43
WRONG = "W" * 43
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def devices():
    return {"pixel": hash_token(TOKEN, iterations=1000)}


class SpyStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def upsert_messages(self, msgs):
        self.calls += 1
        return super().upsert_messages(msgs)


def msg(pid="p1", **over):
    base = {
        "provider_id": pid,
        "date_ms": 1_700_000_000_000,
        "address": "+15550001",
        "direction": "in",
        "body": "hello",
    }
    return {**base, **over}


def make(devices, store=None, rate=None):
    store = store if store is not None else SpyStore()
    app = build_app(store, devices, now=lambda: NOW, rate=rate)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return store, client


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


async def test_healthz_and_headers(devices):
    _, c = make(devices)
    r = await c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in r.headers
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "headers", [{}, auth(WRONG), auth("short"), {"Authorization": "Basic abc"}]
)
async def test_unauthorized_before_body_is_parsed(devices, headers):
    store, c = make(devices)
    r = await c.post("/messages", content=b"{not json", headers=headers)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert r.headers["cache-control"] == "no-store"
    assert store.calls == 0 and store.count() == 0


async def test_revoked_token_is_rejected(devices):
    store, c = make(devices)
    ok = await c.post("/messages", json=[msg()], headers=auth())
    assert ok.status_code == 200
    app = build_app(store, {"other": hash_token("X" * 43, iterations=1000)}, now=lambda: NOW)
    c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c2.post("/messages", json=[msg("p2")], headers=auth())
    assert r.status_code == 401 and store.count() == 1


async def test_happy_path_and_duplicates(devices):
    store, c = make(devices)
    r = await c.post("/messages", json=[msg("a"), msg("b")], headers=auth())
    assert r.status_code == 200 and r.json() == {"accepted": 2, "duplicates": 0}
    r = await c.post("/messages", json=[msg("a"), msg("c")], headers=auth())
    assert r.json() == {"accepted": 1, "duplicates": 1}
    assert store.count() == 3
    assert store.device_seen("pixel") == (NOW, "c")


async def test_last_provider_id_is_max_by_string(devices):
    store, c = make(devices)
    await c.post("/messages", json=[msg("b"), msg("z"), msg("a")], headers=auth())
    assert store.device_seen("pixel") == (NOW, "z")


async def test_long_text_is_cut_and_accepted(devices):
    store, c = make(devices)
    r = await c.post("/messages", json=[msg(body="x" * 5000)], headers=auth())
    assert r.status_code == 200
    stored = next(iter(store._messages.values()))
    assert stored.body.endswith(" [truncated]") and len(stored.body) < 2100


async def test_too_many_items(devices):
    store, c = make(devices)
    items = [msg(f"p{i}") for i in range(MAX_BATCH + 1)]
    r = await c.post("/messages", json=items, headers=auth())
    assert r.status_code == 422 and "error" in r.json()
    assert store.count() == 0


async def test_oversized_body_declared_and_streamed(devices):
    store, c = make(devices)
    big = json.dumps([msg(body="x" * (MAX_BODY_BYTES + 10))]).encode()
    r = await c.post("/messages", content=big, headers=auth())
    assert r.status_code == 413 and r.json() == {"error": "body too large"}

    async def chunks():
        for _ in range(8):
            yield b"x" * 10000

    r = await c.post("/messages", content=chunks(), headers=auth())  # no Content-Length
    assert r.status_code == 413
    assert store.calls == 0


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"provider_id": "x"}',
        b"[]",
        b"[1]",
        b"\xff\xfe",
        json.dumps([{k: v for k, v in msg().items() if k != "provider_id"}]).encode(),
    ],
)
async def test_invalid_payloads_store_nothing(devices, body):
    store, c = make(devices)
    r = await c.post("/messages", content=body, headers=auth())
    assert r.status_code == 422 and set(r.json()) == {"error"}
    assert store.calls == 0 and store.count() == 0


async def test_first_bad_item_rejects_whole_batch(devices):
    store, c = make(devices)
    r = await c.post("/messages", json=[msg("a"), msg("b", direction="x")], headers=auth())
    assert r.status_code == 422
    assert r.json() == {"error": "item 1: direction: must be 'in' or 'out'"}
    assert store.count() == 0


async def test_rate_limit_61st_request(devices):
    _, c = make(devices, rate=RateLimiter())
    for i in range(60):
        r = await c.post("/messages", json=[msg(f"p{i}")], headers=auth())
        assert r.status_code == 200
    r = await c.post("/messages", json=[msg("late")], headers=auth())
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json() == {"error": "rate limited"}


async def test_unauthenticated_flood_limited_by_ip(devices):
    _, c = make(devices, rate=RateLimiter(limit=3))
    codes = []
    for _ in range(5):
        r = await c.post("/messages", json=[], headers=auth(WRONG))
        codes.append(r.status_code)
    assert codes == [401, 401, 401, 429, 429]


async def test_not_found_and_method_not_allowed(devices):
    _, c = make(devices)
    r = await c.get("/nope")
    assert r.status_code == 404 and r.json() == {"error": "not found"}
    assert (await c.post("/healthz")).status_code == 404
    r = await c.get("/messages")
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert r.json() == {"error": "method not allowed"}
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_store_error_is_500_without_leak(devices):
    class Boom(MemoryStore):
        def upsert_messages(self, msgs):
            raise RuntimeError("secret detail")

    _, c = make(devices, store=Boom())
    r = await c.post("/messages", json=[msg()], headers=auth())
    assert r.status_code == 500 and r.json() == {"error": "store error"}
    assert "secret detail" not in r.text


async def test_log_line_has_no_body_or_token(devices, caplog):
    caplog.set_level("INFO", logger="pdl_sms_store.app")
    _, c = make(devices)
    await c.post("/messages", json=[msg(body="topsecretbody")], headers=auth())
    text = caplog.text
    assert "device=pixel status=200 count=1" in text
    assert "topsecretbody" not in text and TOKEN not in text


@pytest.fixture
def verify_calls(monkeypatch):
    calls = []
    real = tokens_mod.verify_token

    def counting(token, hashed):
        calls.append(token)
        return real(token, hashed)

    monkeypatch.setattr(tokens_mod, "verify_token", counting)
    return calls


async def test_over_limit_bad_token_never_hashes(devices, verify_calls):
    _, c = make(devices, rate=RateLimiter(limit=2))
    codes = []
    for _ in range(4):
        codes.append((await c.post("/messages", json=[], headers=auth(WRONG))).status_code)
    assert codes == [401, 401, 429, 429]
    assert len(verify_calls) == 2


@pytest.mark.parametrize("bad", ["x" * 31, "x" * 129, "a" * 40 + "!!!", "a b" * 20])
async def test_malformed_token_never_hashes(devices, verify_calls, bad):
    _, c = make(devices)
    r = await c.post("/messages", json=[msg()], headers=auth(bad))
    assert r.status_code == 401 and verify_calls == []


async def test_verified_token_is_cached(devices, verify_calls):
    _, c = make(devices)
    for i in range(3):
        r = await c.post("/messages", json=[msg(f"p{i}")], headers=auth())
        assert r.status_code == 200
    assert len(verify_calls) == 1


async def test_bad_token_budget_does_not_block_valid_device(devices):
    _, c = make(devices, rate=RateLimiter(limit=10))
    for _ in range(9):
        await c.post("/messages", json=[], headers=auth(WRONG))
    r = await c.post("/messages", json=[msg()], headers=auth())
    assert r.status_code == 200


async def test_numeric_provider_ids_use_numeric_max(devices):
    store, c = make(devices)
    await c.post("/messages", json=[msg("99"), msg("100")], headers=auth())
    assert store.device_seen("pixel") == (NOW, "100")


async def test_unexpected_exception_is_500_json_with_headers(devices):
    class Boom(MemoryStore):
        def record_device_seen(self, device, at, last_provider_id):
            raise RuntimeError("secret detail")

    app = build_app(Boom(), devices, now=lambda: NOW)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    c = httpx.AsyncClient(transport=transport, base_url="http://t")
    r = await c.post("/messages", json=[msg()], headers=auth())
    assert r.status_code == 500 and r.json() == {"error": "store error"}
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_catch_all_handler_covers_non_store_errors(devices, monkeypatch):
    import pdl_sms_store.app as app_mod

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(app_mod, "_decode_batch", boom)
    app = build_app(MemoryStore(), devices, now=lambda: NOW)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    c = httpx.AsyncClient(transport=transport, base_url="http://t")
    r = await c.post("/messages", json=[msg()], headers=auth())
    assert r.status_code == 500 and r.json() == {"error": "store error"}
    assert r.headers["cache-control"] == "no-store"
    assert "secret detail" not in r.text


async def test_store_failure_log_has_only_exception_type(devices, caplog):
    class Boom(MemoryStore):
        def upsert_messages(self, msgs):
            raise RuntimeError("secret detail")

    caplog.set_level("INFO", logger="pdl_sms_store.app")
    _, c = make(devices, store=Boom())
    await c.post("/messages", json=[msg()], headers=auth())
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


async def test_cache_is_bounded_and_evicts_oldest(monkeypatch, verify_calls):
    import pdl_sms_store.app as app_mod

    monkeypatch.setattr(app_mod, "CACHE_PER_DEVICE", 0)  # bound becomes 1 entry
    other = "O" * 43
    two = {
        "pixel": hash_token(TOKEN, iterations=1000),
        "tab": hash_token(other, iterations=1000),
    }
    _, c = make(two)
    for token in (TOKEN, other, TOKEN):
        assert (await c.post("/messages", json=[msg()], headers=auth(token))).status_code == 200
    assert verify_calls.count(TOKEN) >= 2  # evicted by `other`, hashed again
