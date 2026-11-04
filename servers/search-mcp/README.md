# search-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that gives the assistant web search and
page fetching. It is the only backend that talks to the internet, and only outbound: it searches
through a self-hosted [SearXNG](https://docs.searxng.org) instance and fetches public pages
itself. Nothing from the owner's personal data is sent to either.

The gateway reaches the server over streamable HTTP at `/mcp` and sends the shared secret in the
`X-Gateway-Secret` header; requests without it get `401 {"error": "unauthorized"}`.
`GET /healthz` is open and returns `{"ok": true}`. Every tool is annotated read-only and
open-world. Web content is data, not instructions.

## Tools

| Tool | Returns |
|---|---|
| `web_search(query, recency, limit=5)` | `{query, recency, hits: [{title, url, snippet, engine, published}]}`; `recency` is `day`, `week`, `month` or `year` (or omitted); `limit` is 1 to 10; a blank `query` is a tool error |
| `fetch_page(url)` | `{url, final_url, title, text, truncated}`; readable text capped at 8000 characters, `truncated` says whether it was cut |

Errors are tool errors: `search failed: ...` (SearXNG down, rate limited or unusable),
`refused: ...` (the URL failed the safety rules) and `fetch failed: ...` (HTTP error, timeout,
unsupported content, too many redirects).

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `SEARCH_MCP_SHARED_SECRET` | yes | | Value the gateway sends in `X-Gateway-Secret` |
| `SEARCH_MCP_HOST` | no | `0.0.0.0` | Bind address |
| `SEARCH_MCP_PORT` | no | `8000` | Bind port |
| `SEARCH_SEARXNG_URL` | yes | | Base URL of the SearXNG instance (http or https), for example `http://searxng:8080` |

## Safety rules for fetching

The model chooses the URLs, so fetching assumes hostile input:

- **SSRF guard.** Only `http` and `https` are fetched. Refused: URLs with credentials, localhost
  and internal-looking names (`.local`, `.lab`, `.internal`, `.localhost`), and any host that
  resolves to a loopback, private, link-local, multicast, reserved or otherwise non-public
  address. The check runs again on every redirect hop.
- **Caps.** At most 2 MB of body is read, the returned text is at most 8000 characters, and at
  most 5 redirects are followed (each one re-checked, never followed automatically).
- **Content.** Only HTML and plain text are accepted; the result is extracted text, never markup.
- **Identity.** Requests send a fixed `User-Agent` that names this tool. No cookies are kept
  between fetches.
- **No proxies.** The fetch client is created with `trust_env=False`, so proxy settings in the
  environment cannot route around the guard. It is never shared with the SearXNG client.
- **DNS rebinding.** The guard resolves the name before connecting, so a hostile DNS answer that
  changes between the check and the connection is not caught in the process. That gap is closed
  by the cluster egress NetworkPolicy, which only allows this pod to reach public addresses.

## Run locally

Point it at any SearXNG instance that has the JSON format enabled:

    uv sync
    SEARCH_SEARXNG_URL=http://localhost:8080 SEARCH_MCP_SHARED_SECRET=dev-secret uv run pdl-search-mcp

## Develop

    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .
