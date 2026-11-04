# pdl-email-mcp

Read-only email backend for the private data layer: an IMAP sync that stores
messages (text only, bodies capped at 16 000 chars) in Postgres and an MCP
server that exposes search over them. Passwords never leave the host.

This package is built up over several tasks; the MCP server and CLIs land later.

## Tests

    uv sync
    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .
