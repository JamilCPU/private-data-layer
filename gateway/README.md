# pdl-gateway

The private data layer gateway is a single OAuth 2.1 authorization server and scope-filtering MCP proxy. Agents connect to one endpoint, `/mcp`, with a bearer token. The gateway exposes each backend MCP server's tools as `<backend>_<tool>`, but only to tokens holding that backend's scope, and it appends one line per tool call to a JSONL audit log (`gateway_audit_search` for holders of `gateway:audit`).

Run it with `uv run pdl-gateway --config gateway.yaml --secrets secrets.yaml`, or let it read the paths from `GATEWAY_CONFIG` / `GATEWAY_SECRETS`; the container defaults are `/etc/pdl/gateway.yaml` and `/etc/pdl/secrets.yaml`. See `gateway.example.yaml` for backends, scopes, grants, the listen address and the `db_path` / `audit_dir` under `/data`. See `secrets.example.yaml` for the hashed approval passphrase and the hashed client secrets. Create the hashes with `pdl-set-passphrase` (at least 12 characters) and `pdl-new-client-secret`; on Windows, write their output with `Set-Content -Encoding utf8` (or from bash), not PowerShell `>`, which writes UTF-16. Backend credentials come from the environment variables the config names.

There are two OAuth flows:
- **Interactive clients** such as Claude Code register dynamically, then use the authorization code flow with PKCE. You approve each request on the `/approve` page by entering the approval passphrase. The client then gets the `interactive-default` grant's scopes, plus a refresh token. Interactive clients are offered and granted only the `interactive-default` scopes, whatever they ask for.
- **Headless clients** such as `personal-assistant` use `client_credentials` with their client secret and get their configured grant's scopes.

Issued tokens follow the current config: a token's scopes are cut down to what its grant allows now (the headless grant, or `interactive-default` for interactive clients), and removing a headless grant kills its tokens on the next restart.

The gateway is read-only in v1. A backend tool whose own annotations say `readOnlyHint: false` or `destructiveHint: true` is hidden and answers `unknown tool`; unannotated tools are exposed and marked read-only.

Accepted trade-off: `POST /approve` is throttled for the whole process. After 5 wrong passphrases in 10 minutes, every approval attempt is refused with `429` for 10 minutes. This deliberately ignores which request or client sent the guesses. As a result, anyone who can reach the gateway on the network can lock you out of approvals for 10 minutes. In exchange, nobody can brute-force the passphrase by opening new approval requests. Wrong client secrets on `/token` (`client_credentials`) have their own lock with the same rule: 5 failures in 10 minutes refuse every `client_credentials` request with `429` for 10 minutes.

Backend `url` and `ca_file` values may contain `${NAME}` placeholders that are expanded from the environment at startup (an unset name refuses to start); use this to point at a node address injected by Kubernetes rather than committing it.
