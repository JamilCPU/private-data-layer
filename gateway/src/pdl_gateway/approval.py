"""The human approval page: shows the client and scopes, asks for the approval passphrase."""

import asyncio
import logging
from html import escape

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from pdl_gateway.oauth import ApprovalError, ApprovalView, GatewayAuthProvider
from pdl_gateway.throttle import AttemptThrottle

logger = logging.getLogger(__name__)

UNKNOWN_REQUEST = "This approval request is unknown or has expired. Start again from your client."
_DECISIONS = {"approve": True, "deny": False}
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}"
    ".error{color:#b00020}button{margin-right:.5rem}code{word-break:break-all}"
)


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Approve access</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )
    return HTMLResponse(document, status_code=status_code, headers=_HEADERS)


def _unknown() -> HTMLResponse:
    return _page(f"<h1>Approve access</h1><p>{escape(UNKNOWN_REQUEST)}</p>", status_code=400)


def _form(view: ApprovalView, error: str | None = None) -> HTMLResponse:
    scopes = "".join(f"<li><code>{escape(s)}</code></li>" for s in view.scopes)
    notice = f'<p class="error">{escape(error)}</p>' if error else ""
    body = (
        "<h1>Approve access</h1>"
        f"<p><strong>{escape(view.client_name)}</strong> wants access to:</p>"
        f"<ul>{scopes}</ul>"
        f"<p>After you decide you will be sent to <code>{escape(view.redirect_uri)}</code>.</p>"
        f"{notice}"
        '<form method="post">'
        f'<input type="hidden" name="request" value="{escape(view.request_id)}">'
        "<p><label>Approval passphrase "
        '<input type="password" name="passphrase" autocomplete="current-password" '
        "required autofocus></label></p>"
        '<button type="submit" name="decision" value="approve">Approve</button>'
        '<button type="submit" name="decision" value="deny">Deny</button>'
        "</form>"
    )
    return _page(body)


def _field(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value if isinstance(value, str) and value else None


def _throttled(throttle: AttemptThrottle) -> HTMLResponse:
    minutes = round(throttle.window_s / 60)
    message = f"Too many attempts; try again in {minutes} minutes"
    return _page(f"<h1>Approve access</h1><p>{escape(message)}</p>", status_code=429)


def build_approval_routes(
    provider: GatewayAuthProvider, throttle: AttemptThrottle | None = None
) -> list[Route]:
    """``GET /approve`` renders the form; ``POST /approve`` completes or denies the request.

    Passphrase checks are serialised so concurrent guesses cannot overrun the throttle.
    """
    throttle = throttle if throttle is not None else AttemptThrottle()
    lock = asyncio.Lock()

    async def show(request: Request) -> Response:
        request_id = request.query_params.get("request")
        view = provider.approval_view(request_id) if request_id else None
        return _form(view) if view is not None else _unknown()

    async def submit(request: Request) -> Response:
        if throttle.blocked():
            return _throttled(throttle)
        form = await request.form()
        request_id = _field(form, "request")
        passphrase = _field(form, "passphrase")
        approved = _DECISIONS.get(_field(form, "decision") or "")
        if request_id is None or passphrase is None or approved is None:
            return _page("<h1>Approve access</h1><p>Incomplete form.</p>", status_code=400)
        async with lock:
            if throttle.blocked():
                return _throttled(throttle)
            try:
                url = await run_in_threadpool(
                    provider.complete_approval, request_id, passphrase, approved
                )
            except ApprovalError:
                throttle.record_failure()
                view = provider.approval_view(request_id)
                return _form(view, "Wrong passphrase") if view is not None else _unknown()
            except LookupError:
                return _unknown()
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

    return [
        Route("/approve", show, methods=["GET"]),
        Route("/approve", submit, methods=["POST"]),
    ]
