# pdl-gateway

The private data layer gateway is a single OAuth 2.1 authorization server and scope-filtering MCP proxy. Agents connect to one endpoint, `/mcp`, with a bearer token. The gateway exposes each backend MCP server's tools as `<backend>_<tool>`, but only to tokens holding that backend's scope, and it appends one line per tool call to a JSONL audit log (`gateway_audit_search` for holders of `gateway:audit`).

Run it with `uv run pdl-gateway --config gateway.yaml --secrets secrets.yaml`, or let it read the paths from `GATEWAY_CONFIG` / `GATEWAY_SECRETS`; the container defaults are `/etc/pdl/gateway.yaml` and `/etc/pdl/secrets.yaml`. See `gateway.example.yaml` for backends, scopes, grants, the listen address and the `db_path` / `audit_dir` under `/data`. See `secrets.example.yaml` for the hashed approval passphrase and the hashed client secrets. Backend credentials come from the environment variables the config names.

There are two OAuth flows:
- **Interactive clients** such as Claude Code register dynamically, then use the authorization code flow with PKCE. You approve each request on the `/approve` page by entering the approval passphrase. The client then gets the `interactive-default` grant's scopes, plus a refresh token.
- **Headless clients** such as `personal-assistant` use `client_credentials` with their client secret and get their configured grant's scopes.

Accepted trade-off: `POST /approve` is throttled for the whole process. After 5 wrong passphrases in 10 minutes, every approval attempt is refused with `429` for 10 minutes. This deliberately ignores which request or client sent the guesses. As a result, anyone who can reach the gateway on the network can lock you out of approvals for 10 minutes. In exchange, nobody can brute-force the passphrase by opening new approval requests.
