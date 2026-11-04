# Step 1 verification — 2026-11-02 (dev workstation, no Docker)

Stack under test (plain processes, scratchpad config, throwaway secrets made by the helper commands):
- `homestudio-mcp` (home-studio-server master `a6fdf95`) on `127.0.0.1:8766`, `HOMESTUDIO_API_URL` = the live Home Studio server (read-only calls).
- `pa-mcp --http` (personal-automations, unchanged) on `https://127.0.0.1:7702` with a self-signed cert; the automations hub is down during a migration, so `tools/list` works and calls answer "hub request timed out".
- `pdl-gateway` (this branch, `a1bcfeb`) on `http://127.0.0.1:8787`, backends `homestudio` and `automations` (allowlist), grants from `gateway.example.yaml`.

## Claude Code as the first client (the step-1 "done" test)
1. `claude mcp add --transport http -s local pdl-gateway http://127.0.0.1:8787/mcp` in a scratch project. First attempt against `3eee44b` failed: Claude Code asks for every scope in the protected-resource metadata and registration refused the extra ones. Fixed in `a1bcfeb` (interactive clients are advertised and mapped down to the interactive grant).
2. `claude mcp login --no-browser pdl-gateway` printed the `/authorize` URL; opened in a browser; the approve page showed "Claude Code (pdl-gateway)", `homestudio:read`, `automations:read` and the `localhost:3118/callback` target; passphrase entered; Approve → "Authenticated with pdl-gateway".
3. `claude mcp get pdl-gateway` → `✔ Connected`. Gateway log: `POST /register 201` → `GET /authorize 302` → `POST /approve 303` → `POST /token 200` → `/mcp` initialize → `ListToolsRequest` fanned out to both backends.
4. Not verified from inside a Claude Code chat: the CLI's own Anthropic login is missing in print mode (`claude auth status` → `loggedIn: false`), so `claude -p` cannot run. Run `claude login` once, or open `claude` in the scratch dir and ask it to list the pdl-gateway tools.

## Interactive client, exact tool set (SDK client replaying the browser flow)
- DCR with all seven scopes → 201; `/authorize` requesting `homestudio:read bank:read` → approve page; wrong passphrase re-renders; approve → 303 with `state`; token scope = `homestudio:read automations:read`, refresh token present; code replay → `invalid_grant`.
- `tools/list` = exactly 12: `automations_{get_device,get_program,get_program_logs,get_usb_power,list_devices,list_programs,recent_history}`, `homestudio_{get_listening_history,get_now_playing,get_taste_profile,list_queue,list_ratings}`.
- `homestudio_list_queue` → live data; `bank_spend_summary`, `automations_reboot_device`, `gateway_audit_search` → `unknown tool: <name>`.
- Refresh with a narrower `scope` → still the full interactive grant. `/revoke` → the token gets 401 on `/mcp`.

## Headless client (`personal-assistant`, client credentials)
- Wrong secret → 401 `invalid_client`; right secret → scope with all seven; `tools/list` = the 12 above + `gateway_audit_search`.
- `homestudio_get_taste_profile` and `homestudio_get_now_playing` returned the live Home Studio profile and idle player; `automations_list_devices` → tool error "hub request timed out" (expected while the hub is down); `gateway_audit_search(since=10 min ago)` returned every call above with `client_name`, hash, size, latency, error.

## Audit file
`audit-2026-11-03.jsonl` (UTC day) holds one line per call, fields `ts client_id client_name tool args_sha256 result_bytes latency_ms error`; no arguments or results.

## Pending
- `homestudio_list_ratings` against the live Home Studio server needs the merged server deployed there (the repo's deploy script + `systemctl --user restart home-studio`): waits for the owner's go-ahead (short playback interruption).
- `docker compose up` on a dev box: waits for Docker Desktop being allowed on the dev workstation; the compose stack is untested until then.
- Cluster deploy: waits for the migration (primary worker node Ready, `lab-gitops`, registry, cert-manager `lab-ca`, lab DNS record for `gateway.lab`), see `deploy/k8s/README.md`.
- Automations calls return "hub request timed out" until the hub is re-homed.
