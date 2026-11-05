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
| `model_think` | unset | Thinking for reasoning models: `false` turns it off, `true` forces it on, unset keeps the model's default. Turn it off when short answers time out |
| `session_ttl_s` | `43200` | Browser session lifetime |
| `system_prompt_extra` | `""` | Text appended to the system prompt |
| `ntfy_url` | unset | Self-hosted ntfy base URL for notifications; unset means they are only logged |
| `ntfy_topic` | `briefing` | ntfy topic to publish to (letters, digits, `_`, `-`; up to 64) |
| `ntfy_token_file` | unset | File holding an ntfy access token (sent as a bearer token) |
| `briefing_timezone` | `America/Chicago` | Time zone for the briefing date and the retry cut-off |
| `briefing_retry_until` | `10:00` | Local `HH:MM` after which an offline model skips the day |
| `briefing_retry_interval_s` | `900` | Seconds between probes while the model is offline |
| `briefing_news_count` | `3` | News items in the briefing |
| `flagger_config_file` | unset | Path to the flagger checks YAML (see `flagger.example.yaml`); unset disables the flagger |
| `flagger_lookback_days` | `45` | Days of history the flagger scans (1-365) |
| `flagger_require_brain` | `false` | Skip a flagger run instead of degrading when the model is offline (`1/true/yes` or `0/false/no`) |

The secrets file holds `api_tokens` (name to PBKDF2 hash) and `cookie_secret`.

## Briefing

`pdl-assistant-briefing` builds a morning summary and pushes it to your phone through ntfy. In the cluster it is the `briefing` CronJob (07:30 America/Chicago; `deploy/k8s/README.md`, "Briefing").

- **Sections.** Home Studio listening and taste, bank spending, new email and a few news items picked from your taste profile, each summarised by the model. A section whose tool is unavailable says so instead of failing the briefing. The text is capped at 3500 characters.
- **Offline rule.** The model server may be asleep. The job probes Ollama; while it is offline and the local time is before `briefing_retry_until` (default `10:00` in `briefing_timezone`), it sleeps `briefing_retry_interval_s` (default 900) and probes again. The cut-off is checked after each failed probe, so the final probe can land up to one retry interval after `briefing_retry_until`. Once a probe fails at or past the cut-off it records `skipped` (note `brain offline`), sends nothing, and exits 0; the next briefing opens with a line saying yesterday's was skipped.
- **Failures.** A build or delivery error records `failed` with the text and the error, and exits 1.
- **`--now`.** One probe, no retry. If the model is offline it exits 2 and records nothing; otherwise it builds and sends immediately. Locally: `uv run pdl-assistant-briefing --config assistant.yaml --now`.
- **`--test-notification`.** Sends "Test from the assistant" through the configured notifier and exits 0 or 1, without touching the model or the database.

Exit codes: 0 sent or skipped, 1 failed or bad configuration, 2 `--now` with the model offline.

### Phone setup (ntfy)

1. Install the ntfy app.
2. Add the server `https://ntfy.lab`. Import the lab CA in the app's certificate settings; if that is not possible, use `http://ntfy.lab` as a fallback (warning: this sends the ntfy login in clear over the LAN).
3. Log in as the owner user created in the deploy steps.
4. Subscribe to the topic (`briefing` by default, `ntfy_topic`).
5. Check with `pdl-assistant-briefing --test-notification`.

## Flagger

`pdl-assistant-flagger` runs your threshold checks against the gateway and pushes at most one notification per run, listing the new flags. It is meant to run hourly. It makes no model call and runs whether or not the model server is up.

Set `flagger_config_file` to a YAML file of checks (see `flagger.example.yaml`). Three check types:

```yaml
checks:
  - name: large-transaction
    type: transaction_over
    amount: 500
    direction: spend        # or "any"
  - name: recurring-change
    type: recurring_change
    percent: 20
  - name: watched-sender
    type: email_from
    senders: [billing@example.com]
```

- **No repeats.** Each flag is identified by its check name and a key (the transaction or email id). Flags are stored in the assistant's SQLite database (the `flags` table in `db_path`); a flag is pushed once, and a push that fails stays pending and is retried, together with any new flags, on the next run. Each run sends one notification listing every flag not yet sent: at most 10 lines, then "and N more"; every pending flag counts as sent, and the ones past the 10 lines stay readable in the database. If the process crashes between sending and marking, the next run can repeat that notification once. A merchant that bills both a subscription and one-off purchases under the same description can produce a one-time false recurring-change flag.
- **Lookback.** Checks scan the last `flagger_lookback_days` days (default 45).
- **`--dry-run`.** Evaluates the checks and logs what would be recorded and sent; writes nothing and sends nothing.
- **Offline model.** The flagger does not need the model. With `flagger_require_brain: true` it probes the model first and skips the run silently (exit 0) when it is offline, which is the original design's rule.
- **Briefing.** When `flagger_config_file` is set, the morning briefing's Flags line lists the flags raised since the previous briefing, up to 5 and then "and N more" (no model call). The line only appears once that file exists.

Exit codes: 0 quiet, notified or skipped; 1 for a failed push, every check failing, or bad configuration.

## Container

The image (`ghcr.io/jamilcpu/pdl-assistant`) runs as uid 10001, listens on 8790, keeps state under `/data` and has a health check on `/healthz` (set `ASSISTANT_PORT` if you change `listen_port`). For the cluster deployment, see `deploy/k8s/README.md`.

`ASSISTANT_GATEWAY_PUBLIC_URL` (optional) is the gateway address the browser is sent to for login when it differs from `ASSISTANT_GATEWAY_URL` (inside the cluster the assistant talks to the gateway service over HTTP, while the browser needs the public ingress host).
