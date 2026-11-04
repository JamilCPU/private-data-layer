"""The assistant's web front door: chat page, login through the gateway, and app assembly.

Only ``/healthz``, ``/login`` and ``/oauth/callback`` are open. The chat page and ``/api``
need the signed ``pdl_session`` cookie; ``/v1`` needs an API bearer token (``web_api``).
"""

import hmac
import html
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources

from itsdangerous import BadSignature, TimestampSigner
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .agent import Agent
from .auth import GatewayLogin, LoginError, SessionSigner, issue_session, session_from_cookie
from .config import AssistantConfig, AssistantSecrets
from .memory import MemoryStore
from .web_api import chat_routes, openai_routes

log = logging.getLogger(__name__)

SESSION_COOKIE = "pdl_session"
LOGIN_COOKIE = "pdl_login"
LOGIN_COOKIE_TTL_S = 600
LOGIN_LIMIT = 30
LOGIN_WINDOW_S = 600.0
_LOGIN_SALT = "pdl-assistant-login"
_ERROR_NAME_CHARS = 64

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    ),
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


class LoginLimiter:
    """At most ``limit`` login starts per sliding ``window_s`` for the whole process."""

    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._hits and self._hits[0] <= now - self._window_s:
            self._hits.popleft()
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True


class SecurityHeaders:
    """ASGI middleware adding ``SECURITY_HEADERS`` to every response that lacks them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra = [
                    (name.encode(), value.encode())
                    for name, value in SECURITY_HEADERS.items()
                    if name.encode() not in present
                ]
                message = {**message, "headers": headers + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass(frozen=True)
class _Front:
    config: AssistantConfig
    login: GatewayLogin
    memory: MemoryStore
    signer: SessionSigner
    login_signer: TimestampSigner
    limiter: LoginLimiter
    page: str

    @property
    def secure(self) -> bool:
        return self.config.public_url.startswith("https")

    def session(self, request: Request) -> str | None:
        return session_from_cookie(self.signer, self.memory, request.cookies.get(SESSION_COOKIE))

    def login_state(self, request: Request) -> str | None:
        """The ``state`` held in this browser's signed, unexpired ``pdl_login`` cookie."""
        cookie = request.cookies.get(LOGIN_COOKIE)
        if not cookie:
            return None
        try:
            return self.login_signer.unsign(cookie, max_age=LOGIN_COOKIE_TTL_S).decode("utf-8")
        except (BadSignature, UnicodeError):
            return None

    def set_cookie(self, resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(
            name, value, max_age=max_age, path="/", httponly=True, secure=self.secure,
            samesite="lax",
        )  # fmt: skip

    def clear_cookie(self, resp: Response, name: str) -> None:
        resp.delete_cookie(name, path="/", httponly=True, secure=self.secure, samesite="lax")


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _page_routes(front: _Front) -> list[Route]:
    async def index(request: Request) -> Response:
        if front.session(request) is None:
            return RedirectResponse("/login", status_code=302)
        return HTMLResponse(front.page)

    async def login(request: Request) -> Response:
        if not front.limiter.allow():
            return PlainTextResponse("too many login attempts; try again later", 429)
        try:
            await front.login.ensure_registered()
            url, state = front.login.start()
        except LoginError as exc:
            log.warning("login could not start: %s", exc)
            return PlainTextResponse("the gateway is unavailable; try again later", 502)
        resp = RedirectResponse(url, status_code=302)
        signed = front.login_signer.sign(state).decode("ascii")
        front.set_cookie(resp, LOGIN_COOKIE, signed, LOGIN_COOKIE_TTL_S)
        return resp

    async def logout(request: Request) -> Response:
        session_id = front.session(request)
        if session_id is not None:
            front.memory.revoke_session(session_id)
        resp = RedirectResponse("/login", status_code=302)
        front.clear_cookie(resp, SESSION_COOKIE)
        return resp

    return [
        Route("/", index, methods=["GET"]),
        Route("/login", login, methods=["GET"]),
        Route("/oauth/callback", _callback_endpoint(front), methods=["GET"]),
        Route("/logout", logout, methods=["POST"]),
    ]


def _callback_endpoint(front: _Front) -> Callable[[Request], Awaitable[Response]]:
    async def callback(request: Request) -> Response:
        query = request.query_params
        if "error" in query:
            name = html.escape(query["error"][:_ERROR_NAME_CHARS])
            body = f"<!doctype html><title>Login refused</title><p>Login refused: {name}</p>"
            resp: Response = HTMLResponse(body, status_code=403)
            front.clear_cookie(resp, LOGIN_COOKIE)
            return resp
        code, state = query.get("code"), query.get("state")
        expected = front.login_state(request)
        if not code or not state or expected is None or not _same(expected, state):
            return PlainTextResponse("login did not start here", 400)
        if not await front.login.finish(code, state):
            resp = PlainTextResponse("login refused", 403)
            front.clear_cookie(resp, LOGIN_COOKIE)
            return resp
        resp = RedirectResponse("/", status_code=302)
        cookie = issue_session(front.signer, front.memory)
        front.set_cookie(resp, SESSION_COOKIE, cookie, front.config.session_ttl_s)
        front.clear_cookie(resp, LOGIN_COOKIE)
        log.info("browser session issued")
        return resp

    return callback


def _load_page() -> str:
    return resources.files("pdl_assistant").joinpath("templates/chat.html").read_text("utf-8")


async def _healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


def build_app(
    config: AssistantConfig,
    secrets: AssistantSecrets,
    agent: Agent,
    login: GatewayLogin,
    memory: MemoryStore,
    *,
    signer: SessionSigner | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> Starlette:
    """The assistant ASGI app; ``on_shutdown`` (if given) runs when the lifespan ends."""
    front = _Front(
        config=config,
        login=login,
        memory=memory,
        signer=signer or SessionSigner(secrets.cookie_secret, config.session_ttl_s),
        login_signer=TimestampSigner(secrets.cookie_secret, salt=_LOGIN_SALT),
        limiter=LoginLimiter(LOGIN_LIMIT, LOGIN_WINDOW_S),
        page=_load_page(),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()

    return Starlette(
        routes=[
            Route("/healthz", _healthz, methods=["GET"]),
            *_page_routes(front),
            *chat_routes(agent, memory, lambda request: front.session(request) is not None),
            *openai_routes(config, secrets, agent, memory),
        ],
        middleware=[Middleware(SecurityHeaders)],
        lifespan=lifespan,
    )
