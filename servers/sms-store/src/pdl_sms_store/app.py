"""HTTP receiver: phones POST batches of messages with a per-device bearer token."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from starlette.applications import Starlette
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


class _Receiver:
    def __init__(
        self,
        store: Store,
        devices: Mapping[str, str],
        now: Callable[[], datetime],
        rate: RateLimiter,
    ) -> None:
        self._store, self._devices, self._now, self._rate = store, devices, now, rate

    async def post_messages(self, request: Request) -> Response:
        token = _bearer_token(request)
        device = find_device(token, self._devices) if token else None
        key = f"device:{device}" if device else _client_key(request)
        if not self._rate.allow(key):
            wait = str(self._rate.retry_after(key))
            return _error(429, "rate limited", device=device, headers={"retry-after": wait})
        if device is None:
            return _error(401, "unauthorized")
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
            last_id = max(m.provider_id for m in msgs)
            self._store.record_device_seen(device, self._now(), last_id)
        except Exception:
            log.exception("store failure for device=%s", device)
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
    return Starlette(routes=[route])
