"""HTTP receiver: phones POST batches of messages with a per-device bearer token."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pdl_sms_store.limits import MAX_BATCH, MAX_BODY_BYTES, RateLimiter, utcnow
from pdl_sms_store.models import Message, ValidationError, parse_incoming
from pdl_sms_store.store import Store
from pdl_sms_store.tokens import find_device

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
}
_BEARER = "bearer "
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{32,128}")
CACHE_PER_DEVICE = 4


class _BodyTooLarge(Exception):
    pass


class _BadRequest(Exception):
    pass


def _reply(
    status: int,
    payload: dict[str, Any],
    *,
    device: str | None = None,
    count: int = 0,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    log.info("device=%s status=%d count=%d", device or "-", status, count)
    return JSONResponse(
        payload, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})}
    )


def _error(status: int, message: str, **kwargs: Any) -> JSONResponse:
    return _reply(status, {"error": message}, **kwargs)


def _bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    if value[: len(_BEARER)].lower() != _BEARER:
        return None
    return value[len(_BEARER) :].strip() or None


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _BodyTooLarge
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_batch(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise _BadRequest("body must be valid JSON") from exc
    if not isinstance(data, list):
        raise _BadRequest("body must be a JSON list")
    if not 1 <= len(data) <= MAX_BATCH:
        raise _BadRequest(f"list must hold 1 to {MAX_BATCH} items")
    if not all(isinstance(item, dict) for item in data):
        raise _BadRequest("every item must be an object")
    return data


def _parse_all(device: str, items: list[dict[str, Any]], at: datetime) -> list[Message]:
    msgs: list[Message] = []
    for index, item in enumerate(items):
        try:
            msgs.append(parse_incoming(device, item, at))
        except ValidationError as exc:
            raise _BadRequest(f"item {index}: {exc.field}: {exc.reason}") from exc
    return msgs


def _client_key(request: Request) -> str:
    return f"ip:{request.client.host}" if request.client else "ip:unknown"


def _last_provider_id(msgs: list[Message]) -> str:
    ids = [m.provider_id for m in msgs]
    if all(i.isascii() and i.isdigit() for i in ids):
        return max(ids, key=int)
    return max(ids)


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _Receiver:
    def __init__(
        self,
        store: Store,
        devices: Mapping[str, str],
        now: Callable[[], datetime],
        rate: RateLimiter,
    ) -> None:
        self._store, self._devices, self._now, self._rate = store, devices, now, rate
        self._verified: dict[str, str] = {}
        self._cache_max = max(1, len(devices) * CACHE_PER_DEVICE)

    def _remember(self, token: str, device: str) -> None:
        if len(self._verified) >= self._cache_max:
            self._verified.pop(next(iter(self._verified)))
        self._verified[_token_key(token)] = device

    def _limited(self, key: str, device: str | None) -> Response | None:
        if self._rate.allow(key):
            return None
        wait = str(self._rate.retry_after(key))
        return _error(429, "rate limited", device=device, headers={"retry-after": wait})

    async def _authenticate(self, request: Request) -> tuple[str | None, Response | None]:
        """Return (device, None) or (None, error response); hash only after the IP limiter."""
        token = _bearer_token(request)
        if token is None or _TOKEN_SHAPE.fullmatch(token) is None:
            return None, _error(401, "unauthorized")
        device = self._verified.get(_token_key(token))
        if device is None:
            limited = self._limited(_client_key(request), None)
            if limited is not None:
                return None, limited
            device = await run_in_threadpool(find_device, token, self._devices)
            if device is None:
                return None, _error(401, "unauthorized")
            self._remember(token, device)
        limited = self._limited(f"device:{device}", device)
        return (None, limited) if limited is not None else (device, None)

    async def post_messages(self, request: Request) -> Response:
        device, failure = await self._authenticate(request)
        if device is None:
            assert failure is not None
            return failure
        try:
            msgs = _parse_all(device, _decode_batch(await _read_body(request)), self._now())
        except _BodyTooLarge:
            return _error(413, "body too large", device=device)
        except _BadRequest as exc:
            return _error(422, str(exc), device=device)
        return self._store_messages(device, msgs)

    def _store_messages(self, device: str, msgs: list[Message]) -> Response:
        try:
            new = self._store.upsert_messages(msgs)
            last_id = _last_provider_id(msgs)
            self._store.record_device_seen(device, self._now(), last_id)
        except Exception as exc:  # noqa: BLE001 - any store failure maps to a 500
            log.error("store failure device=%s error=%s", device, type(exc).__name__)
            return _error(500, "store error", device=device, count=len(msgs))
        payload = {"accepted": new, "duplicates": len(msgs) - new}
        return _reply(200, payload, device=device, count=len(msgs))

    async def dispatch(self, request: Request) -> Response:
        path, method = request.url.path, request.method
        if path == "/healthz" and method == "GET":
            return _reply(200, {"ok": True})
        if path == "/messages":
            if method == "POST":
                return await self.post_messages(request)
            return _error(405, "method not allowed", headers={"allow": "POST"})
        return _error(404, "not found")


async def _unexpected(request: Request, exc: Exception) -> Response:
    log.error("unexpected error=%s", type(exc).__name__)
    return _error(500, "store error")


def build_app(
    store: Store,
    devices: Mapping[str, str],
    *,
    now: Callable[[], datetime] = utcnow,
    rate: RateLimiter | None = None,
) -> Starlette:
    receiver = _Receiver(store, devices, now, rate or RateLimiter())
    route = Route(
        "/{path:path}",
        receiver.dispatch,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    return Starlette(routes=[route], exception_handlers={Exception: _unexpected})
