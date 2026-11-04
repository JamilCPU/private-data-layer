"""Step 4 synthetic end to end: search-mcp behind a local gateway with SearXNG stubbed.

Usage: step4_check.py <gateway_url> <secrets_dir> <stub_port>
Starts a local HTTP stub that answers SearXNG's JSON search API and serves one HTML page, then
checks the headless client sees the search tools and the interactive client does not.
Run it with search-mcp started against ``SEARCH_SEARXNG_URL=http://127.0.0.1:<stub_port>``.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

GW, SD, STUB_PORT = sys.argv[1].rstrip("/"), Path(sys.argv[2]), int(sys.argv[3])
CB = "http://localhost:3999/callback"
SEARCH_TOOLS = {"search_web_search", "search_fetch_page"}
PAGE = (
    "<html><head><title>Stub Page</title></head><body><h1>Hello</h1><p>"
    + "Readable text. " * 40
    + "</p></body></html>"
)
HITS = {
    "results": [
        {
            "title": f"Result {i}",
            "url": f"http://127.0.0.1:{STUB_PORT}/page{i}",
            "content": "snippet",
            "engine": "stub",
        }
        for i in range(3)
    ]
}


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/search":
            body, ctype = json.dumps(HITS).encode(), "application/json"
        elif path == "/healthz":
            body, ctype = b"OK", "text/plain"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_stub():
    server = HTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def mcp_tools(tok, calls):
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    async with (
        streamable_http_client(f"{GW}/mcp", http_client=http) as (rs, ws, _),
        ClientSession(rs, ws) as s,
    ):
        await s.initialize()
        names = sorted(t.name for t in (await s.list_tools()).tools)
        out = []
        for n, a in calls:
            r = await s.call_tool(n, a)
            out.append((n, r.isError, r.content[0].text if r.content else "", r.structuredContent))
        return names, out


def show(out):
    for n, e, txt, _ in out:
        print(f"  {n}: isError={e} {txt[:140]!r}")


async def main():
    c = httpx.Client(follow_redirects=False, timeout=30)
    prm = c.get(f"{GW}/.well-known/oauth-protected-resource/mcp").json()
    pa = (SD / "personal-assistant.secret").read_text().strip()
    tok = c.post(
        f"{GW}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "personal-assistant",
            "client_secret": pa,
        },
    ).json()["access_token"]
    names, out = await mcp_tools(
        tok,
        [
            ("search_web_search", {"query": "k3s release notes", "recency": "month"}),
            ("search_fetch_page", {"url": f"http://127.0.0.1:{STUB_PORT}/page0"}),
            ("search_fetch_page", {"url": "http://gateway.lab/"}),
            ("search_web_search", {"query": "  "}),
        ],
    )
    print(f"assistant tools ({len(names)}):", names)
    assert SEARCH_TOOLS <= set(names)
    show(out)
    assert not out[0][1] and len(out[0][3]["hits"]) == 3
    # the stub's own page is a loopback address: the guard must refuse it (the stub only backs SearXNG)
    assert out[1][1] and "refused" in out[1][2]
    assert out[2][1] and "refused" in out[2][2]
    assert out[3][1]
    passphrase = (SD / "approval.passphrase").read_text().strip()
    reg = c.post(
        f"{GW}/register",
        json={
            "client_name": "step4 e2e",
            "redirect_uris": [CB],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(prm["scopes_supported"]),
        },
    ).json()
    assert "client_id" in reg, f"registration failed: {reg}"
    v = secrets.token_urlsafe(48)
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    a = c.get(
        f"{GW}/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CB,
            "code_challenge": ch,
            "code_challenge_method": "S256",
            "state": "s",
            "scope": " ".join(prm["scopes_supported"]),
            "resource": f"{GW}/mcp",
        },
    )
    req = parse_qs(urlparse(a.headers["location"]).query)["request"][0]
    ok = c.post(
        f"{GW}/approve", data={"request": req, "passphrase": passphrase, "decision": "approve"}
    )
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    tk = c.post(
        f"{GW}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CB,
            "client_id": reg["client_id"],
            "code_verifier": v,
            "resource": f"{GW}/mcp",
        },
    ).json()
    names, out = await mcp_tools(tk["access_token"], [("search_web_search", {"query": "x"})])
    print(f"interactive tools ({len(names)}):", names)
    assert not (SEARCH_TOOLS & set(names)) and out[0][1] and "unknown tool" in out[0][2]
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    start_stub()
    asyncio.run(main())
