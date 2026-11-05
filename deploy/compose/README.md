# Local development with Docker Compose

Runs the gateway, `homestudio-mcp`, a fixture Home Studio stub, the assistant, a dev Postgres, `bank-mcp` `email-mcp`, `sms-store`, `sms-mcp` and `search-mcp` (with a dev SearXNG) on one machine. The stub
(`stub-homestudio/`) answers the read endpoints with fixed made-up data, so nothing here
touches the real Home Studio server.

All commands below run from this directory (`deploy/compose`).

## 1. Create the secrets

`.env` holds the shared secrets the gateway sends to `homestudio-mcp`, `bank-mcp`, `email-mcp`, `search-mcp` and `sms-mcp` (generate a fresh value for each `*_SECRET`), SearXNG's own `SEARXNG_SECRET` and the dev Postgres password:

```sh
cp .env.example .env
# put a fresh value after each placeholder (HOMESTUDIO_MCP_SECRET, BANK_MCP_SECRET, EMAIL_MCP_SECRET, SEARCH_MCP_SECRET, SMS_MCP_SECRET, SEARXNG_SECRET, POSTGRES_PASSWORD)
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`secrets.dev.yaml` holds the hashed approval passphrase and the hashed client secret. Both
files are gitignored.

```sh
uv run --project ../../gateway pdl-set-passphrase > secrets.dev.yaml
echo "client_secrets:" >> secrets.dev.yaml
uv run --project ../../gateway pdl-new-client-secret --client personal-assistant --out ./pa.secret >> secrets.dev.yaml
```

`pdl-set-passphrase` asks for the approval passphrase twice (hidden input) and prints the
`approval_passphrase:` line. The `echo` adds the `client_secrets:` key that the second
command's `  personal-assistant: <hash>` line goes under. The plaintext client secret is
written once to `./pa.secret` (mode 0600; the command refuses to overwrite an existing file)
and is never printed. Keep it for the headless client; delete it when you do not need it.

On Windows, run these commands in bash (Git Bash), or write the output with
`Set-Content -Encoding utf8`, not PowerShell `>`: `>` writes UTF-16, which the gateway refuses.
The approval passphrase must be at least 12 characters.

`assistant-secrets.dev.yaml` (gitignored) holds the assistant's cookie signing secret and its
API token hashes. The gateway client secret it needs is the `./pa.secret` written above.

```sh
{ echo "cookie_secret: $(python -c 'import secrets; print(secrets.token_urlsafe(48))')"; echo "api_tokens:";   uv run --project ../../assistant pdl-assistant-new-api-token --name openwebui --out ./openwebui.token; } > assistant-secrets.dev.yaml
```

The token command prints one indented `  openwebui: <hash>` line under `api_tokens:`;
`openwebui.token` (gitignored) is the plaintext bearer for `/v1/*`.

## 2. Start the stack

```sh
docker compose up --build
```

The gateway listens on `http://localhost:8787`; `curl http://localhost:8787/healthz` should
return OK.

## 3. Point Claude Code at it

```sh
claude mcp add --transport http pdl-gateway http://localhost:8787/mcp
```

Claude Code registers itself and opens the gateway's approval page. Enter the approval
passphrase from step 1 to approve. It then sees `homestudio_*` tools (and no others, since
the `automations` backend is commented out in `gateway.dev.yaml`).

## 4. Try the assistant

The assistant runs at `http://localhost:8790` and reaches Ollama at `OLLAMA_URL` (default: the
Docker host's port 11434; set it in `.env`, along with `ASSISTANT_MODEL`). It calls the gateway
at `ASSISTANT_GATEWAY_URL` (`http://gateway:8787`, the compose service name) and its login sends
your browser to that same address. The assistant does not use OAuth discovery, so the gateway's
issuer string does not matter for this flow; the host browser only has to resolve `gateway`.
Dev-only step: map it to loopback in your hosts file (both ports are published on 127.0.0.1
only):

```
127.0.0.1 gateway
```

Remove that line again when you are done with the dev stack.

Then open `http://localhost:8790`, sign in through the gateway approval page with the passphrase
from step 1, and ask a question.

## 5. Bank and email (optional)

`postgres` here is a throwaway dev database with a single role (`lab_dev`) that owns and reads
everything. The cluster uses the Lab Data Store with separate roles per source (owner for the
importer and sync, read-only for the MCP servers); the services read `PG*` the same way in both.
Only the gateway grant `personal-assistant` sees the `bank_*` and `email_*` tools; the
interactive grant does not.

```sh
mkdir -p data/bank/inbox data/bank/processed
cp ../../servers/bank-mcp/rules.example.yaml bank-rules.dev.yaml
cp ../../servers/bank-mcp/csv-mappings.example.yaml bank-csv-mappings.dev.yaml
# drop a statement (CSV/OFX) into data/bank/inbox, then:
docker compose --profile jobs run --rm bank-import
```

On a Linux Docker host, `./data/bank` must be writable by uid 10001 (the image user), e.g.
`sudo chown -R 10001:10001 data/bank`; Docker Desktop handles this for you.

For mail, set `EMAIL_IMAP_HOST`/`EMAIL_IMAP_USER` in `.env`, write the app password to
`./imap.password` (gitignored; `uv run --project ../../servers/email-mcp pdl-email-set-password --out imap.password`),
and run `docker compose --profile jobs run --rm email-sync`. `imap.password` must exist as a
file before any `docker compose --profile jobs up` or `run`; otherwise Docker creates a directory
of that name in its place and the sync fails. The `jobs` profile keeps both
one-shot services out of a plain `docker compose up`. The servers are published on
127.0.0.1:8001 (bank) and 127.0.0.1:8002 (email).

## 6. Search (optional)

`search-mcp` (web search and page fetch) talks to a dev `searxng` container configured by
`searxng/settings.yml` (no secrets; the key is `SEARXNG_SECRET` from `.env`). Both start with a
plain `docker compose up`; `search-mcp` is published on 127.0.0.1:8003. Only the
`personal-assistant` grant sees the `search_*` tools. The compose network does not have the
cluster's egress NetworkPolicy, so `fetch_page` relies on the server's own address checks here.

## 7. Texts (required for `docker compose up`)

`sms-store` (the phone's write endpoint) and `sms-mcp` (read-only tools for the gateway) share the
dev Postgres. `sms-store` reads its device tokens from `./sms-tokens.dev.yaml`, which is gitignored
and must exist before `up` (otherwise Docker creates a directory of that name). Create it with a
dev device token:

```bash
uv run --project ../../servers/sms-store pdl-sms-new-device-token --name pixel --out pixel.token
# the stderr line it prints goes under `devices:` in the tokens file:
printf 'devices:
  <paste the printed line here>
' > sms-tokens.dev.yaml
```

`sms-store` is published on 127.0.0.1:8004 (plain HTTP, dev only) and `sms-mcp` on 127.0.0.1:8005.
Send a test message with `curl -H "Authorization: Bearer $(cat pixel.token)" -H 'Content-Type: application/json' -d '[...]' http://127.0.0.1:8004/messages`
(body format: `servers/sms-store/README.md`). Only the `personal-assistant` grant sees the `sms_*` tools.

## Notes

- Audit log and SQLite state live in the `gateway-data` volume. `docker compose down -v`
  deletes them.
- To try the `automations` backend, follow the comment in `gateway.dev.yaml`.
- `ghcr.io/jamilcpu/homestudio-mcp:latest` is built in the `home-studio-server` repo; log in
  to ghcr.io first if the pull is refused.
