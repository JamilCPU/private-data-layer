"""Step 3 synthetic e2e against a local gateway (plain http): headless + interactive clients.
Usage: step3_check.py <gateway_url> <secrets_dir>"""

import asyncio
import base64
import hashlib
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

GW, SD = sys.argv[1].rstrip("/"), Path(sys.argv[2])
CB = "http://localhost:3999/callback"
BANK_EMAIL = {
    "bank_spend_summary",
    "bank_search_transactions",
    "bank_list_accounts",
    "bank_recurring_charges",
    "email_search_email",
    "email_get_email",
    "email_list_recent",
}


def client():
    return httpx.Client(follow_redirects=False, timeout=30)


async def mcp_tools(tok, calls):
    async with (
        streamable_http_client(
            f"{GW}/mcp",
            http_client=httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=60),
        ) as (rs, ws, _),
        ClientSession(rs, ws) as s,
    ):
        await s.initialize()
        names = sorted(t.name for t in (await s.list_tools()).tools)
        out = []
        for n, a in calls:
            r = await s.call_tool(n, a)
            out.append(
                (n, r.isError, (r.content[0].text if r.content else ""), r.structuredContent)
            )
        return names, out


def show(out, width=160):
    for n, e, txt, _ in out:
        print(f"  {n}: isError={e} {txt[:width]!r}")


async def main():
    c = client()
    print("healthz", c.get(f"{GW}/healthz").json())
    prm = c.get(f"{GW}/.well-known/oauth-protected-resource/mcp").json()
    print("PRM scopes", prm["scopes_supported"])
    pa = (SD / "personal-assistant.secret").read_text().strip()
    t = c.post(
        f"{GW}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "personal-assistant",
            "client_secret": pa,
        },
    )
    print("assistant token", t.status_code, t.json().get("scope"))
    tok = t.json()["access_token"]
    names, out = await mcp_tools(
        tok,
        [
            ("bank_list_accounts", {}),
            ("bank_spend_summary", {"period": "january 2026", "group_by": "category"}),
            ("bank_spend_summary", {"period": "2026-01", "group_by": "account"}),
            ("bank_search_transactions", {"query": "grocer", "limit": 5}),
            ("bank_recurring_charges", {}),
            ("bank_spend_summary", {"period": "next tuesday"}),
            ("email_search_email", {"query": "landlord"}),
            ("email_list_recent", {"limit": 3}),
            ("email_get_email", {"id": "nope"}),
        ],
    )
    print(f"assistant tools ({len(names)}):", names)
    assert BANK_EMAIL <= set(names), "assistant must see all 7 bank/email tools"
    show(out)
    # get_email on the first landlord hit
    hit = out[6][3]["result"][0]["id"] if out[6][3] and out[6][3].get("result") else None
    if hit:
        _, o2 = await mcp_tools(tok, [("email_get_email", {"id": hit})])
        show(o2, 220)
        body = o2[0][3]
        assert body["truncated"] is False and "rent" in body["body"].lower()
    # interactive (simulated browser) must NOT see them
    passphrase = (SD / "approval.passphrase").read_text().strip()
    reg = c.post(
        f"{GW}/register",
        json={
            "client_name": "step3 e2e",
            "redirect_uris": [CB],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(prm["scopes_supported"]),
        },
    )
    cid = reg.json()["client_id"]
    v = secrets.token_urlsafe(48)
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    a = c.get(
        f"{GW}/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": CB,
            "code_challenge": ch,
            "code_challenge_method": "S256",
            "state": "s1",
            "scope": " ".join(prm["scopes_supported"]),
            "resource": f"{GW}/mcp",
        },
    )
    req = parse_qs(urlparse(a.headers["location"]).query)["request"][0]
    ok = c.post(
        f"{GW}/approve", data={"request": req, "passphrase": passphrase, "decision": "approve"}
    )
    q = parse_qs(urlparse(ok.headers["location"]).query)
    tk = c.post(
        f"{GW}/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": CB,
            "client_id": cid,
            "code_verifier": v,
            "resource": f"{GW}/mcp",
        },
    )
    print("interactive token", tk.status_code, tk.json().get("scope"))
    names, out = await mcp_tools(
        tk.json()["access_token"],
        [("bank_spend_summary", {}), ("email_search_email", {"query": "landlord"})],
    )
    print(f"interactive tools ({len(names)}):", names)
    assert not (BANK_EMAIL & set(names))
    show(out)
    assert all(e for _, e, _, _ in out) and all("unknown tool" in txt for _, _, txt, _ in out)
    since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    _, out = await mcp_tools(tok, [("gateway_audit_search", {"since": since, "limit": 50})])
    entries = (out[0][3] or {}).get("entries", [])
    print("audit entries last 5 min:", len(entries), "tools:", sorted({e["tool"] for e in entries}))
    print("ALL CHECKS PASSED")


asyncio.run(main())
