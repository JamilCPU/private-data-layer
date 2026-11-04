# pdl-assistant

A personal assistant that sits in front of the private data layer. It talks to a local model through Ollama and calls the gateway's MCP tools under its own headless identity (`client_credentials` as `personal-assistant`). Conversations, preferences and browser sessions live in one SQLite file (`db_path`). Tool results are passed to the model as framed data, never as instructions.

When the model cannot be reached (the workstation is asleep or the GPU is busy), the assistant does not fail. It answers "Brain is offline (workstation asleep or GPU busy). Try again later." and the reply is flagged `offline`.

## Two doors

- **Browser.** `GET /` serves the chat page. Without a session, it redirects to `/login`, which sends you to the gateway's approval page (OAuth authorization code + PKCE; the assistant registers itself with the gateway on first use). After you approve with the gateway passphrase, `/oauth/callback` sets the `pdl_session` cookie (HttpOnly, SameSite=Lax, Secure when `public_url` is https) for `session_ttl_s`, and you land on the chat page. The token the gateway returns is discarded: it only proves the approval. A signed, 10-minute `pdl_login` cookie ties the callback to the browser that started the login. `/login` accepts at most 30 starts per 10 minutes for the whole process. `POST /logout` revokes the session. The chat page uses `POST /api/chat`, `GET /api/conversations` and `GET /api/conversations/{id}/messages`.
- **API.** `GET /v1/models` and `POST /v1/chat/completions` speak the OpenAI chat-completions format (non-streaming only) for clients such as Open WebUI. They need `Authorization: Bearer <token>` with a token created by `pdl-assistant-new-api-token`. Only the last `user` message is used as the prompt; the assistant keeps its own history. To continue a stored conversation, put its id (digits) in the request's `user` field; otherwise each request starts a new conversation. Messages are limited to 4000 characters.

Only `/healthz`, `/login`, `/oauth/callback` and `POST /logout` (which only clears the cookie and redirects) are reachable without a cookie or token. Cookie-authenticated POSTs (`/api/chat`, `/logout`) are refused with 403 when the browser sends an `Origin` other than `public_url`'s. `POST /api/chat` and `POST /v1/chat/completions` require `Content-Type: application/json` (else 415) and a body of at most 64 KiB (else 413).

## Run locally

```bash
uv sync
# API token: the token goes to a 0600 file, the hash line to stdout
uv run pdl-assistant-new-api-token --name openwebui --out openwebui.token
```

Create `secrets.yaml` from `secrets.example.yaml`: paste the printed line under `api_tokens:` and set `cookie_secret` to a random string of at least 32 characters (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). On Windows, write files with `Set-Content -Encoding utf8` or from bash, not PowerShell `>` (UTF-16). Put the gateway client secret for `personal-assistant` (from `pdl-new-client-secret`) in the file named by `client_secret_file`. Copy `assistant.example.yaml`, point it at your gateway, Ollama and those files, then:

```bash
uv run pdl-assistant --config assistant.example.yaml
```

## Configuration

`pdl-assistant --config PATH --secrets PATH`. The config path may also come from `ASSISTANT_CONFIG`; without one, the whole config comes from the environment. The secrets path comes from `--secrets`, else `ASSISTANT_SECRETS`, else `secrets_file` in the config. Every config field can be overridden by `ASSISTANT_<FIELD>` (for example `ASSISTANT_MODEL`).

| Field | Default | Meaning |
|---|---|---|
| `gateway_url` | required | Gateway base URL (`/token`, `/mcp`, `/register`, `/authorize`) |
| `ollama_url` | required | Ollama base URL |
| `model` | required | Ollama model name; also the model id shown on `/v1/models` |
| `client_secret_file` | required | File holding the `personal-assistant` client secret |
| `secrets_file` | required | Secrets YAML (`api_tokens`, `cookie_secret`) |
| `db_path` | required | SQLite file for conversations, preferences and sessions |
| `public_url` | required | URL the browser uses; the OAuth callback is `<public_url>/oauth/callback` |
| `client_id` | `personal-assistant` | Headless gateway client id |
| `listen_host` / `listen_port` | `0.0.0.0` / `8790` | Listen address |
| `max_tool_steps` | `6` | Model/tool rounds per message |
| `model_timeout_s` / `tool_timeout_s` | `120` / `60` | Timeouts |
| `session_ttl_s` | `43200` | Browser session lifetime |
| `system_prompt_extra` | `""` | Text appended to the system prompt |
| `ntfy_url` | unset | Self-hosted ntfy base URL for notifications; unset means they are only logged |
| `ntfy_topic` | `briefing` | ntfy topic to publish to |
| `ntfy_token_file` | unset | File holding an ntfy access token (sent as a bearer token) |

The secrets file holds `api_tokens` (name to PBKDF2 hash) and `cookie_secret`.

## Container

The image (`ghcr.io/jamilcpu/pdl-assistant`) runs as uid 10001, listens on 8790, keeps state under `/data` and has a health check on `/healthz` (set `ASSISTANT_PORT` if you change `listen_port`). For the cluster deployment, see `deploy/k8s/README.md`.

`ASSISTANT_GATEWAY_PUBLIC_URL` (optional) is the gateway address the browser is sent to for login when it differs from `ASSISTANT_GATEWAY_URL` (inside the cluster the assistant talks to the gateway service over HTTP, while the browser needs the public ingress host).
