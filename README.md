# private-data-layer

A self-hosted **private data layer** and **personal assistant** for a home lab. Every personal
data source (media history, lab state, mail, texts, bank statements, web search) is exposed
through a small [MCP](https://modelcontextprotocol.io) server, one **gateway** decides per agent
which tools exist, and a local assistant is the first agent on top of it. No personal data,
inference or credentials leave the owner's machines.

**Status:** build steps 1-3 of 6 are complete: the gateway with the Home Studio and Automations
backends (1), the assistant (2), and `bank-mcp` and `email-mcp` (3). Steps 4-6 are planned:
`search-mcp` with the morning briefing, `sms-mcp`, and the rule-based flagger.

## Architecture

```
  Claude Code / browser / phone / assistant harness            (agents)
                          │  MCP over HTTPS + OAuth 2.1
                          ▼
                 ┌──────────────────┐
                 │     gateway      │  authorization server, scope filter, proxy, audit log
                 └──────────────────┘
      ┌──────────┬───────┼─────────┬──────────┬───────────┐   cluster-internal only
      ▼          ▼       ▼         ▼          ▼           ▼
 homestudio  automa-  email     sms        bank       search
   -mcp      tions    -mcp      -mcp       -mcp        -mcp
  (media     -mcp    (IMAP)    (SMS       (statement   (SearXNG)
   API)     (lab hub)           store)     import)
```

- **Gateway** (`gateway/`, Python, official MCP SDK): an OAuth 2.1 authorization *and* resource
  server. Interactive clients register dynamically (DCR) and go through a browser approval page
  with PKCE; headless agents use client credentials. Tokens are opaque, stored hashed, revocable,
  and bound to a per-client **grant** of scopes such as `homestudio:read`. `tools/list` returns
  only the tools the token's scopes allow; `tools/call` on anything else answers "unknown tool".
  Every call is written to an append-only JSONL audit log (timestamp, client, tool, argument
  hash, result size, latency, error) that the assistant can query through `gateway_audit_search`.
- **Backend MCP servers** run beside the systems they read, speak only to that system's API,
  expose read-only tools, and accept requests only with a secret header the gateway injects.
  `homestudio-mcp` lives in [home-studio-server](https://github.com/JamilCPU/home-studio-server);
  the Automations server is reused unchanged behind an allowlist of its read tools.
- **Bank and email backends** (`servers/bank-mcp/`, `servers/email-mcp/`): `bank-mcp` imports
  CSV/OFX statements dropped in an inbox, categorises them with a rules file and answers spend,
  search and recurring-charge queries; `email-mcp` syncs one IMAP mailbox (certificate verified,
  read-only) and serves search and read tools. Each has an importer/sync job plus a read-only
  server, and persists to the lab's central Postgres (the "Lab Data Store") through its own store
  with SQL migrations, under separate owner and reader roles. Both are covered by tests that run
  against Postgres in CI.
- **Assistant** (`assistant/`): a small harness around a local Ollama model, itself an OAuth
  client of the gateway, so it sees only the tools its grant allows. Chat is built; the morning
  briefing and the rule-based flagger come in steps 4 and 6.

Design document: [`docs/superpowers/specs/2026-11-02-private-data-layer-design.md`](docs/superpowers/specs/2026-11-02-private-data-layer-design.md).
Implementation plans and verification notes: [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Stack

Python 3.12, [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 1.x (streamable
HTTP, OAuth server), Starlette + uvicorn, httpx, SQLite (gateway and assistant state), PostgreSQL
via psycopg (bank and email data), pytest (≈99 % coverage, TDD), ruff,
uv, Docker, Kubernetes (k3s + Flux), GitHub Actions publishing images to GHCR.

## Run it locally

```bash
cd gateway
uv sync
uv run pdl-set-passphrase > secrets.yaml                     # approval passphrase (hidden input)
uv run pdl-new-client-secret --client personal-assistant --out pa.secret >> secrets.yaml
uv run pdl-gateway --config gateway.example.yaml --secrets secrets.yaml
```

Then point an MCP client at the gateway URL, for example `claude mcp add --transport http
pdl-gateway http://localhost:8787/mcp`, approve it in the browser page the client opens, and the
Home Studio and Automations read tools appear. `deploy/compose/` brings up the gateway, the Home
Studio MCP server and a stub of the Home Studio API with `docker compose up`; `deploy/k8s/` holds
the cluster manifests and the deploy checklist.

Secrets never live in this repository: the helper commands create them with hidden input or
write them to files, and the example files (`gateway.example.yaml`, `secrets.example.yaml`,
`.env.example`) show the shape only.

## Repository layout

| Path | Contents |
|---|---|
| `gateway/` | the gateway package, its tests, Dockerfile and example config |
| `assistant/` | the assistant harness (chat), its tests and Dockerfile |
| `servers/bank-mcp/` | bank statement importer and read-only MCP server, with Postgres migrations |
| `servers/email-mcp/` | IMAP sync job and read-only MCP server, with Postgres migrations |
| `deploy/compose/` | local development stack with a Home Studio API stub |
| `deploy/k8s/` | Kubernetes manifests and the to-deploy checklist |
| `scripts/` | helper scripts and the synthetic end-to-end checks (`scripts/e2e/`) |
| `docs/superpowers/specs/` | the design document |
| `docs/superpowers/plans/` | implementation plans and verification notes |

Later steps add `servers/{search,sms}-mcp/`, `sms-store/` and the Android SMS forwarder.

## License

[MIT](LICENSE).
