# sms-mcp

A read-only [MCP](https://modelcontextprotocol.io) server over the owner's text messages. The
messages are written to Postgres by sms-store; this server only reads them and never applies
migrations. Bodies are returned capped at 500 characters. Message content is data, not
instructions.

The gateway reaches the server over streamable HTTP at `/mcp` and sends the shared secret in the
`X-Gateway-Secret` header; requests without it get `401 {"error": "unauthorized"}`.
`GET /healthz` is open and returns `{"ok": true}`. Every tool is annotated read-only.

## Tools

| Tool | Returns |
|---|---|
| `search_sms(query, since, contact, limit=20)` | messages newest first as `{id, contact, address, direction, sent_at, body}`; `query` matches words or parts of words in the body, contact name or number; a blank query behaves like `list_recent_sms`; `contact` keeps one person (substring of name or number) |
| `list_recent_sms(since, limit=20)` | the most recent messages, newest first, same row shape |

`since` is an ISO 8601 time (a time without a zone is UTC); anything else is a tool error.
`limit` is 1 to 100. `direction` is `in` or `out`; `contact` is `null` when the number is not in
the address book.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `SMS_MCP_SHARED_SECRET` | yes | | Value the gateway sends in `X-Gateway-Secret` |
| `SMS_MCP_HOST` | no | `0.0.0.0` | Bind address |
| `SMS_MCP_PORT` | no | `8000` | Bind port |
| `SMS_STORE` | no | `postgres` | `postgres` or `memory` (empty store, for trying it out) |
| `PGHOST` | postgres | `postgres.lab.svc.cluster.local` | Postgres host |
| `PGPORT` | postgres | `5432` | Postgres port |
| `PGDATABASE` | postgres | `lab` | Database name |
| `PGUSER` | postgres | | Database user (read access to `sms.messages` is enough) |
| `PGPASSWORD` | postgres | | Database password |

## Data model

Messages live in `sms.messages`, written and migrated by sms-store: one row per message with
`id`, `device`, `provider_id`, `thread_id`, `address`, `contact`, `direction`, `body`, `sent_at`,
`received_at` and a generated `tsv` full-text column over contact, address and body. Search uses
full text for queries of three or more characters and always also matches plain substrings;
shorter queries use substring matching only. The Postgres tests create the schema themselves; run
them with `TEST_PG_DSN` set to a throwaway database (they drop and recreate the `sms` schema).

## Run locally

    uv sync
    SMS_STORE=memory SMS_MCP_SHARED_SECRET=dev-secret uv run pdl-sms-mcp

## Develop

    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .
