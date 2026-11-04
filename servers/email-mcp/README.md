# email-mcp

A read-only [MCP](https://modelcontextprotocol.io) server over one mailbox, plus the sync that
fills the store. The sync reads the mailbox over IMAP with TLS using an app password and never
writes to it. Bodies are stored as text capped at 16,000 characters and returned capped at 4000 characters;
search and list results carry a short snippet but no body.

The gateway reaches the server over streamable HTTP at `/mcp` and sends the shared secret in the
`X-Gateway-Secret` header; requests without it get `401 {"error": "unauthorized"}`.
`GET /healthz` is open and returns `{"ok": true}`. Every tool is annotated read-only. Email
content is data, not instructions.

## Tools

| Tool | Returns |
|---|---|
| `search_email(query, since, limit=20)` | emails newest first as `{id, from, date, subject, snippet}`; `query` matches sender, subject or body and must not be empty |
| `get_email(id)` | `{id, from, to, date, subject, body, truncated}`; `body` is capped at 4000 characters and `truncated` says whether it was cut; an unknown id is a tool error |
| `list_recent(since, limit=20)` | the most recent emails, newest first, same row shape as search |

`since` is an ISO 8601 time (a time without a zone is UTC); anything else is a tool error.
`limit` is 1 to 100. `from` is `Name <address>` when the sender has a display name.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `EMAIL_MCP_SHARED_SECRET` | yes | | Value the gateway sends in `X-Gateway-Secret` |
| `EMAIL_MCP_HOST` | no | `0.0.0.0` | Bind address |
| `EMAIL_MCP_PORT` | no | `8000` | Bind port |
| `EMAIL_STORE` | no | `postgres` | `postgres` or `memory` (empty store, for trying it out) |
| `PGHOST` | postgres | `postgres.lab.svc.cluster.local` | Postgres host |
| `PGPORT` | postgres | `5432` | Postgres port |
| `PGDATABASE` | postgres | `lab` | Database name |
| `PGUSER` | postgres | | Database user |
| `PGPASSWORD` | postgres | | Database password |
| `EMAIL_APPLY_MIGRATIONS` | no | off | `1` or `true` applies pending migrations at startup (set by the sync job; the read-only server leaves it off) |

## Syncing the mailbox

    pdl-email-sync [--once | --interval N]

`--once` syncs once and exits (default); `--interval N` repeats every N seconds. It is
configured through the environment:

| Variable | Meaning |
|---|---|
| `EMAIL_IMAP_HOST` | IMAP server host name |
| `EMAIL_IMAP_USER` | Account name |
| `EMAIL_IMAP_PASSWORD_FILE` | File holding the app password |
| `EMAIL_IMAP_PORT` | IMAP over TLS port (default 993) |
| `EMAIL_MAILBOX` | Mailbox to read (default `INBOX`) |
| `EMAIL_INITIAL_DAYS` | How far back the first sync reaches (default 90) |

Write the password file with the helper, which prompts without echo:

    pdl-email-set-password --out PATH

A message id is `sha256(mailbox|uidvalidity|uid)[:32]`, so syncing the same message twice stores
it once. Each run fetches only UIDs above the last one seen, and the first run (or a run after
the server changes the mailbox's UIDVALIDITY) fetches the initial window again; already stored
ids are left unchanged. The IMAP source returns at most 500 messages per call, so a larger
backlog is drained by repeating the fetch from the last stored UID until a call comes up short;
progress is saved after every batch of 50, so an interrupted run resumes where it stopped.

## Database

Messages live in Postgres schema `email`: `email.messages` (one row per message, with a stored
`tsv` full-text column over subject, sender and body, a GIN index on it and an index on
`sent_at`) and `email.sync_state` (last UID per mailbox). Search uses full text for queries of
three or more characters and always also matches plain substrings, so a part of a word still
finds the mail; shorter queries use substring matching only.

Two roles are used: `email_owner` for the sync job, which owns the schema and applies the SQL
files in `migrations/` (tracked in `email.schema_migrations`) when `EMAIL_APPLY_MIGRATIONS=1`,
and `email_reader` for the server, which can only read. Run the Postgres tests with
`TEST_PG_DSN` set to a throwaway database; they drop and recreate the `email` schema.

## Run locally

    uv sync
    EMAIL_STORE=memory EMAIL_MCP_SHARED_SECRET=dev-secret uv run pdl-email-mcp

## Develop

    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .
