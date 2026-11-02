"""Tests for the human approval page."""

import html

import httpx

from .oauth_helpers import begin, dcr

UNKNOWN = "This approval request is unknown or has expired. Start again from your client."


async def test_get_approve_unknown_request_is_400(http: httpx.AsyncClient) -> None:
    resp = await http.get("/approve", params={"request": "nope"})
    assert resp.status_code == 400
    assert UNKNOWN in resp.text
    missing = await http.get("/approve")
    assert missing.status_code == 400


async def test_approve_page_lists_scopes_and_client(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    request_id = await begin(http, client)
    resp = await http.get("/approve", params={"request": request_id})
    assert resp.status_code == 200
    page = resp.text
    assert "<title>Approve access</title>" in page
    assert "Claude Code" in page
    for scope in ("homestudio:read", "automations:read"):
        assert scope in page
    assert "bank:read" not in page
    assert 'type="password"' in page
    assert 'name="passphrase"' in page
    assert 'value="approve"' in page
    assert 'value="deny"' in page
    assert "http://localhost:6274/callback" in page
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


async def test_approve_page_escapes_client_name(http: httpx.AsyncClient) -> None:
    evil = "<script>alert(1)</script>"
    client = await dcr(http, client_name=evil)
    resp = await http.get(
        "/authorize",
        params={
            "client_id": client["client_id"],
            "response_type": "code",
            "code_challenge": "x" * 43,
            "state": "s",
        },
    )
    request_id = resp.headers["location"].split("request=")[1]
    page = (await http.get("/approve", params={"request": request_id})).text
    assert evil not in page
    assert html.escape(evil) in page


async def test_post_approve_missing_fields_is_400(http: httpx.AsyncClient) -> None:
    assert (await http.post("/approve", data={})).status_code == 400
    partial = {"request": "r", "passphrase": "open sesame"}
    assert (await http.post("/approve", data=partial)).status_code == 400
    bogus = {**partial, "decision": "maybe"}
    assert (await http.post("/approve", data=bogus)).status_code == 400
