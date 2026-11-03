# Local development with Docker Compose

Runs the gateway, `homestudio-mcp` and a fixture Home Studio stub on one machine. The stub
(`stub-homestudio/`) answers the read endpoints with fixed made-up data, so nothing here
touches the real Home Studio server.

All commands below run from this directory (`deploy/compose`).

## 1. Create the secrets

`.env` holds the shared secret the gateway sends to `homestudio-mcp`:

```sh
cp .env.example .env
# put a fresh value after HOMESTUDIO_MCP_SECRET=
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

## Notes

- Audit log and SQLite state live in the `gateway-data` volume. `docker compose down -v`
  deletes them.
- To try the `automations` backend, follow the comment in `gateway.dev.yaml`.
- `ghcr.io/jamilcpu/homestudio-mcp:latest` is built in the `home-studio-server` repo; log in
  to ghcr.io first if the pull is refused.
