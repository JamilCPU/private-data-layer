# bank-mcp

A read-only [MCP](https://modelcontextprotocol.io) server over imported bank statements, plus
the importer that fills the store. An assistant can answer questions such as "what did I spend
on food in October?" or "did the rent payment go out?" without being able to change anything.

The gateway reaches the server over streamable HTTP at `/mcp` and sends the shared secret in the
`X-Gateway-Secret` header; requests without it get `401 {"error": "unauthorized"}`.
`GET /healthz` is open and returns `{"ok": true}`. Every tool is annotated read-only.

## Tools

| Tool | Returns |
|---|---|
| `spend_summary(period="this month", group_by="category")` | `since`, `until`, `group_by`, `rows` of `{key, total, count}`, and `total`; `group_by` is `category`, `month` or `account` |
| `search_transactions(query, since, until, min_amount, max_amount, limit=50)` | transactions newest first as `{posted_on, amount, description, category, account}` |
| `list_accounts` | `{id, name, institution, currency}` per account |
| `recurring_charges` | `{description, amount, occurrences, last_on, cadence_days}` per repeating charge |

`period` accepts `2026-10`, `October`, `last 30 days`, `this month`, `last month`; anything else
is a tool error. `since` and `until` are ISO dates, amounts are decimal strings, `limit` is 1
to 200. Amounts in results are strings with two decimals: transactions are signed (spending is
negative), summary totals are positive.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BANK_MCP_SHARED_SECRET` | yes | | Value the gateway sends in `X-Gateway-Secret` |
| `BANK_MCP_HOST` | no | `0.0.0.0` | Bind address |
| `BANK_MCP_PORT` | no | `8000` | Bind port |
| `BANK_STORE` | no | `postgres` | `postgres` or `memory` (empty store, for trying it out) |
| `PGHOST` | postgres | `postgres.lab.svc.cluster.local` | Postgres host |
| `PGPORT` | postgres | `5432` | Postgres port |
| `PGDATABASE` | postgres | `lab` | Database name |
| `PGUSER` | postgres | | Database user |
| `PGPASSWORD` | postgres | | Database password |

## Importing statements

    pdl-bank-import --inbox DIR --rules FILE --mappings FILE [--processed DIR] [--once | --interval N] [--memory]

It scans `DIR` for statement files, parses them, categorizes each row with the rules file and
stores the new ones. `--processed DIR` receives files after import, `--once` scans once and
exits (default), `--interval N` rescans every N seconds, `--memory` is a dry run against an
in-memory store. See `rules.example.yaml` and `csv-mappings.example.yaml`.

Supported formats: CSV (column layout described in the mappings file) and OFX/QFX.

Re-importing is safe. A transaction id is a hash of account, date, amount and normalised
description; rows identical within one file get a numbered suffix, so the same file always yields
the same ids and nothing is stored twice.

## Run locally

    uv sync
    BANK_STORE=memory BANK_MCP_SHARED_SECRET=dev-secret uv run pdl-bank-mcp

## Develop

    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .
