# Step 1: Gateway + homestudio-mcp + automations passthrough — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code session configured with only the gateway URL completes a browser approval, sees exactly the Home Studio and Automations read tools, gets "unknown tool" for anything else, and every call lands in the audit log.

**Architecture:** One Python process, `pdl-gateway`, is an OAuth 2.1 authorization server + resource server (the official MCP SDK's auth routes, plus our own client-credentials grant and passphrase-gated approval page) and an MCP streamable-HTTP server whose tool catalogue is the union of its backends' tools, prefixed by backend name and filtered by the caller's scopes. Backends are MCP servers reached as a client over streamable HTTP with a shared-secret or bearer header. `homestudio-mcp` is a new FastMCP server in the public `home-studio-server` repo that wraps that server's REST API; two read endpoints are added to the REST API for it. The Automations MCP server is proxied unchanged behind an allowlist of its read tools.

**Tech Stack:** Python 3.12+, `mcp>=1.30,<2` (official SDK: `mcp.server.auth`, lowlevel `Server`, `streamablehttp_client`), Starlette, uvicorn, httpx, PyYAML, pydantic v2, SQLite (stdlib), pytest + pytest-asyncio + respx + asgi-lifespan, uv for env management, GitHub Actions, GHCR for images, Docker Compose, Kustomize.

**Spec:** `docs/superpowers/specs/2026-11-02-private-data-layer-design.md` (sections 4, 5.1, 5.2, 6, 7 step 1, 8).

## Global Constraints

- Public repo `home-studio-server`: no IPs, hostnames or secrets in code, docs, tests or CI. Configuration by environment variables only; no default URL that names a machine.
- Private repo `private-data-layer`: no secrets in git. Lab names (`gateway.lab`, `192.168.0.x`) are allowed in `deploy/` and docs.
- Never ask Jamil to paste a token: secrets are created by helper commands that use hidden input or generate the value and write it to a file.
- Read-only: no `:write` scope exists; the scope grammar is `<family>:<verb>`. Scopes in v1: `homestudio:read automations:read email:read sms:read bank:read search:read gateway:audit`.
- Grants (spec 5.1): `personal-assistant` (headless) → all six `:read` plus `gateway:audit`; `interactive-default` (every DCR client, e.g. Claude Code) → `homestudio:read automations:read`. Unknown headless clients are refused.
- `tools/call` on a tool the caller may not see answers `unknown tool: <name>` (an error result), never "forbidden".
- Tool names exposed by the gateway are `<backend>_<tool>` (e.g. `homestudio_get_taste_profile`, `automations_list_devices`).
- Tool descriptions are written for a 32B model: plain, specific, one sentence of when to use it.
- Audit entry fields: timestamp, client, tool, argument hash, result size, latency, error. Append-only JSONL, one file per UTC day.
- Backend MCP servers require a gateway-injected header and expose `/healthz`.
- Tests: pytest, 80%+ line coverage per package (`--cov-fail-under=80`), TDD. Files ≤ 400 lines, functions ≤ 50 lines, frozen dataclasses, no mutation of inputs.
- Conventional commits. Each repo's PR is merged by the builder once CI is green.
- Warstation is in use for gaming: no Docker Desktop, no image builds, no GPU use, no parallel heavy test suites on it. Unit tests, git and `gh` are fine. Image builds happen in GitHub Actions.
- Nothing deploys to the cluster yet (waits on the `jw` migration). Produce Compose for local dev, k8s manifests under `deploy/k8s/`, and a "to deploy" checklist.
- Pi3 is read from, never disturbed: no restart of `home-studio` on Pi3 without Jamil's go-ahead.

## Review Focus

1. **Expired or revoked access token on `/mcp`** → `401` with `WWW-Authenticate: Bearer` so the client refreshes or re-authorizes; never a 500. (Task 7 test `test_expired_token_is_401`.)
2. **One backend unreachable** → `tools/list` still returns the other backends' tools and a call to the dead backend returns an error result `backend unavailable: <name>`, with an audit entry. (Task 7 tests `test_list_tools_skips_unreachable_backend`, `test_call_on_unreachable_backend_is_error_result`.)
3. **Authorization code replay** → the second `/token` exchange with the same code fails with `invalid_grant`; an expired approval request → `400` page, no code. (Task 4 tests `test_code_is_single_use`, `test_expired_approval_request_is_400`.)
4. **Headless client id used in the browser flow** (`/authorize?client_id=personal-assistant`) → `unauthorized_client`, no approval page; a DCR client's registration asking for `bank:read` → `400 invalid_client_metadata`. (Task 4 tests `test_headless_client_cannot_use_authorize`, `test_dcr_cannot_request_scope_outside_interactive_grant`.)
5. **Arguments that carry personal data** (an email query, a transaction search) → the audit log holds only a SHA-256 of the canonical JSON and the result size, never the arguments or the result. (Task 5 test `test_entry_holds_hash_not_arguments`.)

---

## File structure

### Repo `private-data-layer` (private, `F:\Dev\private-data-layer`)

```
gateway/
  pyproject.toml                   # package pdl-gateway, deps, pytest + coverage config, console scripts
  src/pdl_gateway/
    __init__.py
    secrets.py        # hash_secret / verify_secret / new_secret (PBKDF2-SHA256)
    config.py         # frozen config dataclasses, load_config, load_secrets, ConfigError
    store.py          # SQLite: DCR clients, pending approvals, auth codes, hashed tokens
    oauth.py          # GatewayAuthProvider (SDK OAuthAuthorizationServerProvider) + client credentials
    approval.py       # GET/POST /approve page (passphrase gate)
    token_route.py    # /token wrapper: client_credentials here, everything else to the SDK handler
    auth_app.py       # assembles SDK auth routes + ours + protected-resource metadata
    audit.py          # AuditEntry, AuditLog (JSONL per day), hash_args
    backends.py       # Backend protocol, RemoteBackend (MCP client), build_backends
    audit_backend.py  # in-process backend exposing audit_search under gateway:audit
    proxy.py          # lowlevel MCP Server: scope filtering, prefixing, unknown tool, audit
    app.py            # build_app(): Starlette app with auth middleware, /healthz, /mcp
    cli_secrets.py    # pdl-new-client-secret, pdl-set-passphrase
    __main__.py       # pdl-gateway entry point (uvicorn)
  tests/
    conftest.py       # config/secrets fixtures, stub backend FastMCP apps, in-process gateway client
    test_secrets.py test_config.py test_store.py test_oauth_flows.py test_approval.py
    test_audit.py test_backends.py test_proxy.py test_app.py test_cli_secrets.py
  gateway.example.yaml
  secrets.example.yaml
  Dockerfile
deploy/compose/
  docker-compose.yml  gateway.dev.yaml  .env.example  README.md
  stub-homestudio/    app.py  Dockerfile  test_stub.py  pyproject.toml
deploy/k8s/
  base/ (kustomization + manifests)   README.md (to-deploy checklist)
scripts/README.md                     # points at the console scripts
.github/workflows/ci.yml
```

### Repo `home-studio-server` (public, `F:\Dev\YoutubeBrainwash`)

```
src/store/videos.py        # history(since=...), RatingEntry, list_ratings()
src/api/routes_library.py  # GET /history?since=, GET /ratings
src/api/models.py          # rating_entry_out
docs/api.md                # the two endpoints
tests/store/test_videos.py tests/api/test_library.py
mcp/
  pyproject.toml           # package homestudio-mcp
  homestudio_mcp/ __init__.py client.py server.py middleware.py __main__.py
  tests/ test_client.py test_server.py test_middleware.py test_main.py
  Dockerfile
.github/workflows/ci.yml   # server tests, mcp tests, GHCR image on master
```

---

## Task 1: Gateway package skeleton, CI, and secret hashing

**Files:**
- Create: `gateway/pyproject.toml`, `gateway/src/pdl_gateway/__init__.py`, `gateway/src/pdl_gateway/secrets.py`, `gateway/tests/__init__.py`, `gateway/tests/test_secrets.py`, `.github/workflows/ci.yml`, `scripts/README.md`

**Interfaces:**
- Produces: `hash_secret(secret: str, *, iterations: int = 200_000) -> str` returning `"pbkdf2_sha256$<iterations>$<salt_b64url>$<digest_b64url>"`; `verify_secret(secret: str, encoded: str) -> bool` (constant-time, `False` on malformed input); `new_secret(nbytes: int = 32) -> str` (urlsafe).

- [ ] **Step 1: Create `gateway/pyproject.toml`**

Name `pdl-gateway`, version `0.1.0`, `requires-python = ">=3.12"`, build backend hatchling with `packages = ["src/pdl_gateway"]`. Dependencies: `mcp>=1.30,<2`, `starlette>=0.40`, `uvicorn[standard]>=0.30`, `httpx>=0.27`, `pyyaml>=6`, `pydantic>=2`, `python-multipart>=0.0.9`. Dev group (`[dependency-groups] dev`): `pytest>=8`, `pytest-asyncio>=0.24`, `pytest-cov>=5`, `respx>=0.21`, `asgi-lifespan>=2`, `ruff>=0.6`. Console scripts: `pdl-gateway = "pdl_gateway.__main__:main"`, `pdl-new-client-secret = "pdl_gateway.cli_secrets:new_client_secret"`, `pdl-set-passphrase = "pdl_gateway.cli_secrets:set_approval_passphrase"`. Pytest: `testpaths = ["tests"]`, `asyncio_mode = "auto"`, `addopts = "--cov=pdl_gateway --cov-report=term-missing --cov-fail-under=80"`. Ruff: line length 100, target py312.

- [ ] **Step 2: Write the failing tests in `gateway/tests/test_secrets.py`**

```python
from pdl_gateway.secrets import hash_secret, new_secret, verify_secret

def test_hash_round_trip():
    encoded = hash_secret("hunter2")
    assert encoded.startswith("pbkdf2_sha256$200000$")
    assert verify_secret("hunter2", encoded)
    assert not verify_secret("hunter3", encoded)

def test_hash_is_salted():
    assert hash_secret("x") != hash_secret("x")

def test_malformed_encoding_is_false():
    assert not verify_secret("x", "not-a-hash")
    assert not verify_secret("x", "pbkdf2_sha256$abc$def")

def test_new_secret_is_long_and_unique():
    a, b = new_secret(), new_secret()
    assert a != b and len(a) >= 40
```

- [ ] **Step 3: Run to verify failure**

Run (from `gateway/`): `uv sync && uv run pytest tests/test_secrets.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: pdl_gateway.secrets`

- [ ] **Step 4: Implement `gateway/src/pdl_gateway/secrets.py`**

`hashlib.pbkdf2_hmac("sha256", ...)` with a 16-byte `os.urandom` salt, base64url without padding, `hmac.compare_digest` for comparison.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_secrets.py -v --no-cov` → PASS (4 tests)

- [ ] **Step 6: Add CI at `.github/workflows/ci.yml`**

Jobs: `gateway` on `ubuntu-latest`: checkout, `astral-sh/setup-uv@v5`, `uv sync` and `uv run pytest -q` and `uv run ruff check .` with `working-directory: gateway`. `stub` job (added in Task 10; leave a comment placeholder now). `image` job: on `push` to `main` only, `needs: gateway`, `docker/login-action@v3` to `ghcr.io` with `GITHUB_TOKEN`, `docker/build-push-action@v6` building `gateway/` to `ghcr.io/jamilcpu/pdl-gateway:latest` and `:${{ github.sha }}` (the Dockerfile lands in Task 8; until then this job will fail on main, which is acceptable because Task 8 merges before the first main push after this one, or else mark the job `if: hashFiles('gateway/Dockerfile') != ''`). Permissions: `contents: read`, `packages: write`.

- [ ] **Step 7: Write `scripts/README.md`**

Three lines: the helper commands live in the gateway package (`uv run pdl-new-client-secret`, `uv run pdl-set-passphrase`), what each prints, and that secrets never go in chat or git.

- [ ] **Step 8: Commit**

```bash
git add gateway .github scripts
git commit -m "feat(gateway): package skeleton, CI, and PBKDF2 secret hashing"
```

---

## Task 2: Configuration loading

**Files:**
- Create: `gateway/src/pdl_gateway/config.py`, `gateway/gateway.example.yaml`, `gateway/secrets.example.yaml`, `gateway/tests/test_config.py`

**Interfaces:**
- Produces (all `@dataclass(frozen=True)`):
  - `BackendConfig(name: str, url: str, scope: str, allow_tools: tuple[str, ...] | None = None, secret_header: str | None = None, secret_env: str | None = None, bearer_env: str | None = None, ca_file: str | None = None)`
  - `GrantConfig(name: str, kind: Literal["headless", "interactive"], scopes: tuple[str, ...])`
  - `GatewayConfig(issuer_url: str, resource_url: str, listen_host: str, listen_port: int, scopes: tuple[str, ...], backends: tuple[BackendConfig, ...], grants: tuple[GrantConfig, ...], db_path: str, audit_dir: str, interactive_grant: str = "interactive-default", access_token_ttl_s: int = 3600, refresh_token_ttl_s: int = 43200, auth_code_ttl_s: int = 300, tools_cache_ttl_s: int = 60)` with method `grant(name: str) -> GrantConfig | None` and property `interactive_scopes -> tuple[str, ...]`.
  - `GatewaySecrets(approval_passphrase_hash: str, client_secret_hashes: Mapping[str, str])`
  - `ConfigError(ValueError)`
  - `load_config(path: Path) -> GatewayConfig`, `load_secrets(path: Path) -> GatewaySecrets`, `check_secrets(config: GatewayConfig, secrets: GatewaySecrets) -> None` (raises `ConfigError` naming any headless grant without a client secret hash).

- [ ] **Step 1: Write `gateway/gateway.example.yaml`** (this exact content is also what the tests load)

```yaml
issuer_url: https://gateway.lab
resource_url: https://gateway.lab/mcp
listen_host: 0.0.0.0
listen_port: 8787
db_path: /data/gateway.db
audit_dir: /data/audit
scopes: [homestudio:read, automations:read, email:read, sms:read, bank:read, search:read, gateway:audit]
backends:
  - name: homestudio
    url: http://homestudio-mcp:8000/mcp
    scope: homestudio:read
    secret_header: X-Gateway-Secret
    secret_env: HOMESTUDIO_MCP_SECRET
  - name: automations
    url: https://automations-mcp:7702/mcp
    scope: automations:read
    bearer_env: AUTOMATIONS_MCP_TOKEN
    ca_file: /etc/pdl/automations-ca.pem
    allow_tools: [list_devices, get_device, list_programs, get_program, get_program_logs, get_usb_power, recent_history]
grants:
  - name: personal-assistant
    kind: headless
    scopes: [homestudio:read, automations:read, email:read, sms:read, bank:read, search:read, gateway:audit]
  - name: interactive-default
    kind: interactive
    scopes: [homestudio:read, automations:read]
```

`gateway/secrets.example.yaml`:

```yaml
# Generated by `uv run pdl-set-passphrase` and `uv run pdl-new-client-secret --client <id>`.
approval_passphrase: pbkdf2_sha256$200000$REPLACE$REPLACE
client_secrets:
  personal-assistant: pbkdf2_sha256$200000$REPLACE$REPLACE
```

- [ ] **Step 2: Write the failing tests in `gateway/tests/test_config.py`**

```python
EXAMPLE = Path(__file__).resolve().parents[1] / "gateway.example.yaml"

def test_loads_example():
    cfg = load_config(EXAMPLE)
    assert cfg.issuer_url == "https://gateway.lab"
    assert [b.name for b in cfg.backends] == ["homestudio", "automations"]
    assert cfg.backends[1].allow_tools == ("list_devices", "get_device", "list_programs",
        "get_program", "get_program_logs", "get_usb_power", "recent_history")
    assert cfg.interactive_scopes == ("homestudio:read", "automations:read")
    assert cfg.grant("personal-assistant").kind == "headless"
    assert cfg.access_token_ttl_s == 3600

def test_backend_scope_must_be_declared(tmp_path):      # scope "nope:read" → ConfigError mentioning "nope:read"
def test_grant_scope_must_be_declared(tmp_path):        # grant with "bank:write" → ConfigError mentioning "bank:write"
def test_interactive_grant_must_exist(tmp_path):        # no interactive-default → ConfigError
def test_duplicate_backend_names_rejected(tmp_path):    # two "homestudio" → ConfigError
def test_backend_needs_one_auth_method(tmp_path):       # neither secret_env nor bearer_env → ConfigError
def test_secrets_round_trip(tmp_path):                  # load_secrets reads both keys
def test_check_secrets_requires_headless_hash(tmp_path):  # personal-assistant hash missing → ConfigError naming it
```

Each `tmp_path` test writes a YAML variant derived from the example (load the example as a dict with `yaml.safe_load`, mutate a copy, dump to `tmp_path / "g.yaml"`).

- [ ] **Step 3: Run to verify failure** → `ModuleNotFoundError: pdl_gateway.config`

- [ ] **Step 4: Implement `config.py`**

Parse with `yaml.safe_load`, build via small `_backend(raw: dict) -> BackendConfig` and `_grant(raw: dict) -> GrantConfig` helpers, validate in `_validate(cfg)`. Error messages name the offending value.

- [ ] **Step 5: Run to verify pass** → `uv run pytest tests/test_config.py -v --no-cov` PASS

- [ ] **Step 6: Commit** — `feat(gateway): typed config and secrets loading with validation`

---

## Task 3: Token store (SQLite)

**Files:**
- Create: `gateway/src/pdl_gateway/store.py`, `gateway/tests/test_store.py`

**Interfaces:**
- Consumes: `mcp.shared.auth.OAuthClientInformationFull`, `mcp.server.auth.provider.AuthorizationCode`.
- Produces:
  - `StoredToken(kind: Literal["access", "refresh"], client_id: str, scopes: tuple[str, ...], expires_at: int | None)` frozen
  - `PendingApproval(request_id: str, client_id: str, client_name: str | None, params_json: str, expires_at: float)` frozen
  - `class TokenStore:` `__init__(self, path: str | Path)` (creates tables), `save_client(info) -> None`, `get_client(client_id: str) -> OAuthClientInformationFull | None`, `save_pending(p: PendingApproval) -> None`, `pop_pending(request_id: str, now: float) -> PendingApproval | None` (deletes; `None` if missing or expired), `save_code(code: AuthorizationCode) -> None`, `get_code(code: str) -> AuthorizationCode | None`, `delete_code(code: str) -> None`, `save_token(kind, token: str, client_id: str, scopes: Sequence[str], expires_at: int | None) -> None`, `load_token(kind, token: str, now: int) -> StoredToken | None` (`None` if unknown, revoked or expired), `revoke(token: str) -> int` (rows revoked, any kind), `revoke_client_tokens(client_id: str) -> int`, `close() -> None`.
  - Tokens are stored as `sha256(token).hexdigest()`; plaintext never touches disk. Clients and codes stored as JSON text (`model_dump_json` / `model_validate_json`).

- [ ] **Step 1: Write the failing tests in `gateway/tests/test_store.py`**

```python
def test_client_round_trip(tmp_path)          # save_client then get_client equal; unknown id → None
def test_pending_is_single_use_and_expires(tmp_path)  # pop twice → second None; expired (now > expires_at) → None
def test_code_round_trip_and_delete(tmp_path)
def test_token_hashing(tmp_path)              # raw token string not present in the db file bytes
def test_load_token_expired_is_none(tmp_path)  # expires_at=now-1 → None; expires_at=None → found
def test_revoke_any_kind(tmp_path)            # revoke(access token) → load None; revoke returns 1; revoke unknown → 0
def test_revoke_client_tokens(tmp_path)       # two tokens for client a, one for b → revoke_client_tokens("a") == 2
```

- [ ] **Step 2: Run to verify failure** → import error
- [ ] **Step 3: Implement `store.py`** — one `sqlite3.connect(path, check_same_thread=False)` connection, `PRAGMA journal_mode=WAL`, tables `clients`, `pending`, `codes`, `tokens(hash PK, kind, client_id, scopes_json, expires_at, revoked INTEGER DEFAULT 0, created_at)`.
- [ ] **Step 4: Run to verify pass**
- [ ] **Step 5: Commit** — `feat(gateway): SQLite token store with hashed tokens and revocation`

---

## Task 4: OAuth provider, approval page, client-credentials grant, auth routes

**Files:**
- Create: `gateway/src/pdl_gateway/oauth.py`, `gateway/src/pdl_gateway/approval.py`, `gateway/src/pdl_gateway/token_route.py`, `gateway/src/pdl_gateway/auth_app.py`, `gateway/tests/test_oauth_flows.py`, `gateway/tests/test_approval.py`, `gateway/tests/conftest.py`

**Interfaces:**
- Consumes: Task 2 config/secrets, Task 3 store, Task 1 `verify_secret`; SDK `OAuthAuthorizationServerProvider`, `AuthorizationParams`, `AuthorizationCode`, `RefreshToken`, `AccessToken`, `AuthorizeError`, `TokenError`, `construct_redirect_uri`, `create_auth_routes`, `build_metadata`, `create_protected_resource_routes`, `ClientRegistrationOptions`, `RevocationOptions`, `TokenHandler`, `ClientAuthenticator`, `MetadataHandler`.
- Produces:
  - `class GatewayAuthProvider:` `__init__(self, config: GatewayConfig, secrets: GatewaySecrets, store: TokenStore, clock: Callable[[], float] = time.time)`; implements every provider method; plus `complete_approval(request_id: str, passphrase: str, approved: bool) -> str` (redirect URL; raises `ApprovalError(str)` for wrong passphrase, `LookupError` for unknown/expired request), `issue_client_credentials(client_id: str, client_secret: str) -> OAuthToken` (raises `TokenError("invalid_client")`).
  - `class ApprovalError(Exception)`
  - `build_approval_routes(provider: GatewayAuthProvider) -> list[Route]` (`GET /approve`, `POST /approve`)
  - `build_token_endpoint(provider: GatewayAuthProvider, sdk_handler: TokenHandler) -> Callable[[Request], Awaitable[Response]]`
  - `build_auth_routes(config: GatewayConfig, provider: GatewayAuthProvider) -> list[Route]` — the complete list: metadata (with `grant_types_supported = ["authorization_code", "refresh_token", "client_credentials"]`), `/authorize`, `/token` (wrapped), `/register`, `/revoke`, `/approve`, `/.well-known/oauth-protected-resource/mcp`.
  - Behaviour pinned by the spec: `get_client` returns a synthesized `OAuthClientInformationFull(client_id=<grant name>, token_endpoint_auth_method="none", grant_types=["client_credentials"], redirect_uris=None, scope=" ".join(grant.scopes), client_name=<grant name>)` for headless grants, the stored record for DCR clients, else `None`. `authorize()` for a headless client raises `AuthorizeError("unauthorized_client")`. Approval requests expire after `auth_code_ttl_s`. Codes carry the interactive grant's scopes whatever the request asked. Access tokens: `expires_in = access_token_ttl_s`; interactive flows also get a refresh token expiring at `issued + refresh_token_ttl_s`; refresh rotation keeps the original refresh expiry (so a session must re-approve after `refresh_token_ttl_s`). Client-credentials tokens have no refresh token. `load_access_token` returns `AccessToken(token, client_id, scopes, expires_at, resource=config.resource_url, claims={"client_name": <registered client_name or grant name>})`.
- `conftest.py` produces fixtures used by later tasks too: `config` (the example config with `tmp_path` db/audit paths and `issuer_url`/`resource_url` set to `http://localhost:8787` and `http://localhost:8787/mcp`), `secrets` (passphrase `"open sesame"`, personal-assistant secret `"pa-secret"`), `store`, `provider`, and `auth_app` (a Starlette app with only `build_auth_routes` routes) plus an `httpx.AsyncClient(transport=ASGITransport(app=auth_app), base_url="http://localhost:8787")` fixture named `http`.

- [ ] **Step 1: Write the failing flow tests in `gateway/tests/test_oauth_flows.py`**

Helper `async def dcr(http) -> dict` posts `/register` with `{"redirect_uris": ["http://localhost:6274/callback"], "client_name": "Claude Code", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}` and returns the JSON. Helper `pkce() -> tuple[verifier, challenge]`. Helper `async def approve_flow(http, client, passphrase="open sesame") -> str` runs `/authorize` (expects 302 to `/approve?request=...`), `GET /approve` (200, contains "Claude Code" and "homestudio:read"), `POST /approve` form `{request, passphrase, decision: "approve"}` (303 to the redirect_uri with `code` and `state`), returns the code.

```python
async def test_metadata_advertises_endpoints(http)
    # GET /.well-known/oauth-authorization-server → registration_endpoint, revocation_endpoint,
    # code_challenge_methods_supported == ["S256"], "client_credentials" in grant_types_supported
    # GET /.well-known/oauth-protected-resource/mcp → authorization_servers == ["http://localhost:8787/"] or without slash
async def test_dcr_then_code_flow_issues_interactive_scopes(http, provider)
    # token response scope == "homestudio:read automations:read", refresh_token present,
    # expires_in == 3600; provider.load_access_token(token).claims["client_name"] == "Claude Code"
async def test_state_round_trips(http)
async def test_wrong_passphrase_rerenders_form(http)      # POST /approve bad passphrase → 200, "Wrong passphrase" in body, no redirect
async def test_deny_redirects_with_access_denied(http)    # decision=deny → 303, "error=access_denied" in Location
async def test_code_is_single_use(http)                   # second /token with same code → 400 invalid_grant
async def test_expired_approval_request_is_400(http, provider)  # advance provider clock past auth_code_ttl_s → GET /approve 400
async def test_headless_client_cannot_use_authorize(http) # client_id=personal-assistant → 400 or redirect carrying unauthorized_client; no /approve
async def test_dcr_cannot_request_scope_outside_interactive_grant(http)  # scope "bank:read" → 400 invalid_client_metadata
async def test_refresh_rotates_and_keeps_session_expiry(http, provider)
    # refresh → new access + new refresh; old refresh now invalid (second refresh → 400); new refresh expires_at == old expires_at
async def test_client_credentials_issues_assistant_scopes(http)
    # POST /token grant_type=client_credentials client_id=personal-assistant client_secret=pa-secret
    # → 200, scope has all seven scopes, no refresh_token
async def test_client_credentials_wrong_secret_is_401(http)
async def test_client_credentials_unknown_client_is_401(http)  # same body/status as wrong secret
async def test_revoke_access_token(http, provider)        # POST /revoke token=<access> client_id=<dcr id> → 200; load_access_token → None
async def test_revoke_via_refresh_token_kills_both(http, provider)
```

- [ ] **Step 2: Write the failing page tests in `gateway/tests/test_approval.py`**

```python
async def test_get_approve_unknown_request_is_400(http)
async def test_approve_page_lists_scopes_and_client(http)   # contains client name, each scope, a password input, Approve and Deny buttons
async def test_post_approve_missing_fields_is_400(http)
```

- [ ] **Step 3: Run to verify failure** → import errors

- [ ] **Step 4: Implement `oauth.py`**

Keep it under 300 lines: a `_issue_tokens(client_id, scopes, *, with_refresh: bool, refresh_expires_at: int | None) -> OAuthToken` helper does all token minting (`secrets.token_urlsafe(32)` for each, stored through `TokenStore.save_token`). `authorize()` serializes `params.model_dump(mode="json")` into `PendingApproval.params_json`. `complete_approval()` verifies the passphrase with `verify_secret` before anything else, builds the `AuthorizationCode` with `scopes=list(config.interactive_scopes)`, `expires_at=now + auth_code_ttl_s`, `resource=config.resource_url`.

- [ ] **Step 5: Implement `approval.py`**

Plain HTML (no template engine): a `<form method="post">` with hidden `request`, `<input type="password" name="passphrase" autofocus>`, two submit buttons `name="decision" value="approve|deny"`. Page title "Approve access". Wrong passphrase re-renders with `<p class="error">Wrong passphrase</p>`. `LookupError` → 400 page "This approval request is unknown or has expired. Start again from your client."

- [ ] **Step 6: Implement `token_route.py`**

`grant_type == "client_credentials"`: credentials from form `client_id`/`client_secret` or `Authorization: Basic`; success → `JSONResponse(OAuthToken.model_dump(exclude_none=True), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})`; failure → `401 {"error": "invalid_client", "error_description": "unknown client or wrong secret"}`. Anything else → `await sdk_handler.handle(request)` (Starlette caches the parsed form, so the SDK may call `request.form()` again).

- [ ] **Step 7: Implement `auth_app.py`**

Call `create_auth_routes(...)` with `ClientRegistrationOptions(enabled=True, valid_scopes=list(config.interactive_scopes), default_scopes=list(config.interactive_scopes))` and `RevocationOptions(enabled=True)`; rebuild the list replacing the route whose `path == "/token"` with one whose endpoint is `request_response(build_token_endpoint(provider, TokenHandler(provider, ClientAuthenticator(provider))))` wrapped in the same CORS helper the SDK uses, and the metadata route with a `MetadataHandler(metadata)` where `metadata = build_metadata(...)` then `model_copy(update={"grant_types_supported": [...]})`. Append approval routes and `create_protected_resource_routes(AnyHttpUrl(config.resource_url), [AnyHttpUrl(config.issuer_url)], scopes_supported=list(config.scopes), resource_name="private-data-layer gateway")`.

- [ ] **Step 8: Run to verify pass** → `uv run pytest tests/test_oauth_flows.py tests/test_approval.py -v --no-cov` PASS

- [ ] **Step 9: Commit** — `feat(gateway): OAuth 2.1 server with DCR, PKCE approval page, client credentials and revocation`

---

## Task 5: Audit log and audit backend

**Files:**
- Create: `gateway/src/pdl_gateway/audit.py`, `gateway/src/pdl_gateway/audit_backend.py`, `gateway/tests/test_audit.py`

**Interfaces:**
- Produces:
  - `AuditEntry(ts: str, client_id: str, client_name: str | None, tool: str, args_sha256: str, result_bytes: int, latency_ms: int, error: str | None)` frozen; `AuditEntry.to_json() -> str`, `AuditEntry.from_json(line: str) -> AuditEntry`.
  - `hash_args(arguments: Mapping[str, Any]) -> str` — sha256 hex of `json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)`.
  - `class AuditLog:` `__init__(self, directory: Path)`, `record(entry: AuditEntry) -> None` (append to `audit-YYYY-MM-DD.jsonl` by the entry's UTC date, `mkdir(parents=True)`), `search(since: datetime, *, until: datetime | None = None, client_id: str | None = None, limit: int = 100) -> list[AuditEntry]` (newest first across the day files in range).
  - `class AuditBackend:` `name = "gateway"`, `scope = "gateway:audit"`; `__init__(self, log: AuditLog)`; `async list_tools() -> list[mcp.types.Tool]` with one tool `audit_search` (description: "Search the gateway audit log of every tool call by every agent. Use it when Jamil asks what an agent read, or when. Arguments: since (ISO 8601, required), client_id (optional), limit (1-500, default 100)."), `async call_tool(name: str, arguments: dict) -> mcp.types.CallToolResult` returning `structuredContent={"entries": [asdict(e) ...]}` and the same as JSON text; bad `since` → error result `"since must be an ISO 8601 timestamp"`.

- [ ] **Step 1: Write the failing tests in `gateway/tests/test_audit.py`**

```python
def test_entry_holds_hash_not_arguments(tmp_path)   # record an entry built from args {"query": "landlord rent"}; file bytes do not contain "landlord"; args_sha256 == hash_args(args)
def test_one_file_per_utc_day(tmp_path)             # two entries on different days → two files named audit-2026-11-02.jsonl / audit-2026-11-03.jsonl
def test_search_filters_and_orders(tmp_path)        # since, client_id, limit respected; newest first
def test_hash_args_is_canonical()                   # key order does not change the hash
async def test_audit_backend_lists_one_tool(tmp_path)
async def test_audit_backend_search(tmp_path)       # structuredContent["entries"][0]["tool"] == recorded tool
async def test_audit_backend_bad_since_is_error(tmp_path)
```

- [ ] **Step 2: Run to verify failure** → import errors
- [ ] **Step 3: Implement `audit.py` and `audit_backend.py`**
- [ ] **Step 4: Run to verify pass**
- [ ] **Step 5: Commit** — `feat(gateway): append-only JSONL audit log and gateway audit_search tool`

---

## Task 6: Remote backends (MCP client side)

**Files:**
- Create: `gateway/src/pdl_gateway/backends.py`, `gateway/tests/test_backends.py`
- Modify: `gateway/tests/conftest.py` (add stub backend fixtures)

**Interfaces:**
- Consumes: `mcp.client.streamable_http.streamablehttp_client`, `mcp.client.session.ClientSession`, `mcp.types.Tool`, `mcp.types.CallToolResult`; Task 2 `BackendConfig`, `GatewayConfig`, `ConfigError`.
- Produces:
  - `class Backend(Protocol): name: str; scope: str; async def list_tools(self) -> list[Tool]; async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult`
  - `class BackendUnavailable(Exception)` (message names the backend)
  - `class RemoteBackend:` `__init__(self, name: str, scope: str, url: str, headers: Mapping[str, str], *, allow_tools: frozenset[str] | None = None, ca_file: str | None = None, timeout_s: float = 30.0, httpx_client_factory: McpHttpClientFactory = create_mcp_http_client)`; `list_tools()` returns only allowed tools (all when `allow_tools is None`); `call_tool()` of a name outside the allowlist raises `LookupError` (the proxy turns it into "unknown tool"); transport/connection failures raise `BackendUnavailable`. Each operation opens its own `streamablehttp_client` session (`initialize`, act, close).
  - `build_backends(config: GatewayConfig, env: Mapping[str, str]) -> tuple[Backend, ...]` — headers `{secret_header: env[secret_env]}` or `{"Authorization": "Bearer " + env[bearer_env]}`; missing env var → `ConfigError` naming the variable; `ca_file` passed through (the factory builds `httpx.AsyncClient(verify=ca_file)` when set).
- `conftest.py` additions: `stub_backend_app(tools: dict[str, Callable]) -> Starlette` builds a `FastMCP(name, stateless_http=True, json_response=True)` with the given tools registered and returns `streamable_http_app()`; fixtures `homestudio_stub` (tools `get_taste_profile() -> dict`, `list_queue() -> dict`) and `automations_stub` (tools `list_devices() -> list`, `reboot_device(device: str) -> dict`, `recent_history(limit: int = 20) -> list`); `asgi_factory(app) -> McpHttpClientFactory` returning a factory that yields `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://stub")`. Wrap stub apps in `asgi_lifespan.LifespanManager` inside an async fixture so the session manager task group is running. (If the ASGI route proves unreliable, fall back to running the stub under uvicorn on `127.0.0.1:0` in a thread; keep the same fixture names.)

- [ ] **Step 1: Write the failing tests in `gateway/tests/test_backends.py`**

```python
async def test_list_tools_passthrough(homestudio_stub, asgi_factory)   # names == {"get_taste_profile", "list_queue"}; header X-Gateway-Secret sent (stub asserts via middleware capturing headers)
async def test_allowlist_filters_list_and_call(automations_stub, asgi_factory)  # allow {"list_devices", "recent_history"} → reboot_device absent; call_tool("reboot_device") raises LookupError
async def test_call_tool_returns_result(automations_stub, asgi_factory)   # call list_devices → CallToolResult with content
async def test_unreachable_raises_backend_unavailable()                   # url http://127.0.0.1:9 → BackendUnavailable
def test_build_backends_headers(config, monkeypatch)                      # env set → two backends with the right header keys; order preserved
def test_build_backends_missing_env_is_config_error(config, monkeypatch)  # names HOMESTUDIO_MCP_SECRET
```

- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement `backends.py`**
- [ ] **Step 4: Run to verify pass** → `uv run pytest tests/test_backends.py -v --no-cov`
- [ ] **Step 5: Commit** — `feat(gateway): remote MCP backends with allowlists and injected auth headers`

---

## Task 7: Proxy server (scope filtering, prefixing, unknown tool, audit)

**Files:**
- Create: `gateway/src/pdl_gateway/proxy.py`, `gateway/tests/test_proxy.py`

**Interfaces:**
- Consumes: Task 5 `AuditLog`, `AuditEntry`, `hash_args`; Task 6 `Backend`, `BackendUnavailable`; SDK `mcp.server.lowlevel.Server`, `mcp.server.auth.middleware.bearer_auth.AuthenticatedUser`.
- Produces:
  - `Caller(client_id: str, client_name: str | None, scopes: frozenset[str])` frozen
  - `caller_from_request(request: Any) -> Caller | None` — reads `request.user` (an `AuthenticatedUser`), `client_name` from `access_token.claims`.
  - `prefixed(backend: str, tool: str) -> str` → `f"{backend}_{tool}"`; `split_tool_name(name: str, backend_names: Sequence[str]) -> tuple[str, str] | None` (longest matching `<backend>_` prefix).
  - `build_proxy_server(backends: Sequence[Backend], audit: AuditLog, *, tools_cache_ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> Server` — `Server("private-data-layer")` with `@server.list_tools()` and `@server.call_tool(validate_input=False)` handlers. The caller is `caller_from_request(server.request_context.request)`; no caller → empty list / unknown tool. Tool lists per backend are cached for `tools_cache_ttl_s`; a `BackendUnavailable` during listing logs a warning and contributes no tools. Exposed tools copy `description`, `inputSchema`, `outputSchema`, and set `annotations=ToolAnnotations(readOnlyHint=True)`. `call_tool`: unknown prefix, scope not held, or tool not in that backend's current list → `CallToolResult(isError=True, content=[TextContent(text=f"unknown tool: {name}")])` and an audit entry with `error="unknown tool"`; `BackendUnavailable` → error result `f"backend unavailable: {backend}"` and audit `error="backend unavailable"`; success → the backend's `CallToolResult` as is, audit entry with `result_bytes = len(json.dumps([c.model_dump() for c in result.content]))` and `latency_ms`.
  - Test helper in `conftest.py`: `async def gateway_session(server: Server, caller: Caller | None) -> ClientSession` that runs the lowlevel server over an in-memory transport (`mcp.shared.memory.create_connected_server_and_client_session`) with `request_ctx` patched so `server.request_context.request` is a stub object whose `.user` is an `AuthenticatedUser(AccessToken(...))` built from `caller`. Simplest: monkeypatch `pdl_gateway.proxy.caller_from_request` to return `caller` for the duration of the test.

- [ ] **Step 1: Write the failing tests in `gateway/tests/test_proxy.py`**

```python
async def test_list_tools_filtered_by_scope(...)        # caller scopes {homestudio:read} → exactly {"homestudio_get_taste_profile", "homestudio_list_queue"}
async def test_list_tools_two_scopes_and_allowlist(...) # + automations:read with allowlist → automations_list_devices, automations_recent_history present; automations_reboot_device absent
async def test_tools_are_marked_read_only(...)          # annotations.readOnlyHint is True
async def test_call_allowed_tool_audited(...)           # audit file has entry tool == "homestudio_get_taste_profile", error None, args_sha256 == hash_args({})
async def test_call_out_of_scope_is_unknown_tool(...)   # caller lacks automations:read → isError, text "unknown tool: automations_list_devices"; audit error == "unknown tool"
async def test_call_nonexistent_is_unknown_tool(...)    # "bank_spend_summary" → same shape
async def test_call_filtered_write_tool_is_unknown_tool(...)  # "automations_reboot_device" with automations:read → unknown tool
async def test_no_caller_sees_nothing(...)              # caller None → [] and unknown tool
async def test_list_tools_skips_unreachable_backend(...)  # one RemoteBackend at 127.0.0.1:9 → others listed
async def test_call_on_unreachable_backend_is_error_result(...)  # "backend unavailable: dead"
async def test_tool_cache_refreshes_after_ttl(...)      # clock advanced past ttl → backend list_tools called again (counting stub)
def test_split_tool_name_longest_prefix()               # backends ["home", "home_studio"], "home_studio_x" → ("home_studio", "x")
```

- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement `proxy.py`** (≤ 250 lines; cache as a small frozen `_CachedList(tools, fetched_at)` map rebuilt on refresh, no in-place mutation of tool objects)
- [ ] **Step 4: Run to verify pass**
- [ ] **Step 5: Commit** — `feat(gateway): scope-filtered MCP proxy with audit and unknown-tool semantics`

---

## Task 8: App assembly, entry point, Dockerfile, end-to-end in-process test

**Files:**
- Create: `gateway/src/pdl_gateway/app.py`, `gateway/src/pdl_gateway/__main__.py`, `gateway/Dockerfile`, `gateway/tests/test_app.py`
- Modify: `.github/workflows/ci.yml` (drop the `hashFiles` guard if used)

**Interfaces:**
- Consumes: everything above; SDK `StreamableHTTPSessionManager`, `StreamableHTTPASGIApp` (from `mcp.server.streamable_http_manager` / FastMCP's import path in 1.30: check `mcp/server/fastmcp/server.py` for the exact class used around `streamable_http_app()`), `AuthenticationMiddleware`, `BearerAuthBackend`, `ProviderTokenVerifier`, `AuthContextMiddleware`, `RequireAuthMiddleware`, `build_resource_metadata_url`.
- Produces:
  - `build_app(config: GatewayConfig, secrets: GatewaySecrets, backends: Sequence[Backend], *, store: TokenStore | None = None, audit: AuditLog | None = None) -> Starlette` — middleware `[AuthenticationMiddleware(backend=BearerAuthBackend(ProviderTokenVerifier(provider), resource_server_url=AnyHttpUrl(config.resource_url))), AuthContextMiddleware]`; routes: `build_auth_routes(...)`, `Route("/healthz", ...)` → `{"ok": true}`, `Mount("/mcp", app=RequireAuthMiddleware(StreamableHTTPASGIApp(manager), required_scopes=[], resource_metadata_url=build_resource_metadata_url(AnyHttpUrl(config.resource_url))))`; lifespan runs `manager.run()`. The `AuditBackend` is appended to `backends` automatically.
  - `main(argv: list[str] | None = None) -> int` — `--config` (default env `GATEWAY_CONFIG`, else `/etc/pdl/gateway.yaml`), `--secrets` (env `GATEWAY_SECRETS`, else `/etc/pdl/secrets.yaml`); loads, `check_secrets`, `build_backends(config, os.environ)`, `uvicorn.run(app, host=config.listen_host, port=config.listen_port)`; config errors print to stderr and return 1.

- [ ] **Step 1: Write the failing tests in `gateway/tests/test_app.py`**

Fixture `gateway_app` = `build_app(config, secrets, [homestudio_backend, automations_backend])` where the backends are `RemoteBackend`s over the ASGI stubs from Task 6; the app runs under `LifespanManager`; `mcp_client(token) -> ClientSession` uses `streamablehttp_client("http://localhost:8787/mcp", headers={"Authorization": f"Bearer {token}"}, httpx_client_factory=asgi_factory(gateway_app))`.

```python
async def test_healthz_is_open(gateway_app)
async def test_mcp_without_token_is_401_with_resource_metadata(gateway_app)   # WWW-Authenticate contains resource_metadata=
async def test_expired_token_is_401(gateway_app, provider)                     # clock advanced past access ttl
async def test_claude_code_flow_sees_interactive_tools(gateway_app)
    # DCR + approve + token, then list_tools == {"homestudio_get_taste_profile", "homestudio_list_queue", "automations_list_devices", "automations_recent_history"}
    # call "bank_spend_summary" → isError "unknown tool: bank_spend_summary"
    # call "automations_reboot_device" → unknown tool
    # call "gateway_audit_search" → unknown tool (interactive grant lacks gateway:audit)
    # audit file lists the four calls with client_name "Claude Code"
async def test_assistant_flow_sees_audit_tool(gateway_app)
    # client_credentials token → "gateway_audit_search" present; call it with since=today → entries include the earlier calls
async def test_revoked_token_is_401(gateway_app)
def test_main_bad_config_returns_1(tmp_path, capsys)
def test_main_runs_uvicorn(tmp_path, monkeypatch)   # monkeypatch uvicorn.run to capture host/port; returns 0
```

- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement `app.py` and `__main__.py`**
- [ ] **Step 4: Run the whole suite with coverage** → `uv run pytest -q` → PASS, coverage ≥ 80% (raise coverage with targeted tests if under, do not lower the threshold)
- [ ] **Step 5: Write `gateway/Dockerfile`**

`python:3.12-slim`, install `uv` via `COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv`, `uv sync --frozen --no-dev` (commit `uv.lock`), non-root user, `EXPOSE 8787`, `VOLUME /data`, `CMD ["pdl-gateway"]`, `HEALTHCHECK` curl-free using `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz')"`. Do not build it on Warstation; CI builds it.

- [ ] **Step 6: `uv run ruff check .` clean; commit** — `feat(gateway): assemble app, entry point and Dockerfile`

---

## Task 9: Secret helper commands

**Files:**
- Create: `gateway/src/pdl_gateway/cli_secrets.py`, `gateway/tests/test_cli_secrets.py`

**Interfaces:**
- Produces: `new_client_secret(argv: list[str] | None = None) -> int` with args `--client ID` (required) and `--out PATH` (optional): generates `new_secret()`, writes it to `PATH` with mode `0o600` (or prints it once to stdout when `--out` is absent and stdout is a TTY; when stdout is not a TTY and `--out` is absent, refuse with exit 2 and a message), and prints the YAML line `  <ID>: <hash>` to stderr-free stdout after a comment line `# add under client_secrets:`. `set_approval_passphrase(argv=None, *, getpass_fn: Callable[[str], str] = getpass.getpass) -> int`: asks twice with hidden input, exit 1 with "passphrases differ" on mismatch, prints `approval_passphrase: <hash>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_new_client_secret_writes_file_and_prints_hash(tmp_path, capsys)   # file mode 0o600 on POSIX (skip the mode assert on Windows), verify_secret(file contents, printed hash)
def test_new_client_secret_refuses_to_print_when_piped(monkeypatch, capsys)  # isatty False, no --out → exit 2
def test_set_passphrase_mismatch(capsys)      # getpass_fn returns "a" then "b" → 1
def test_set_passphrase_prints_hash(capsys)   # verify_secret("pw", printed hash)
```

- [ ] **Step 2–4: fail → implement → pass**
- [ ] **Step 5: Commit** — `feat(gateway): helper commands for client secrets and the approval passphrase`

---

## Task 10: Compose for local dev, Home Studio stub, k8s manifests, deploy checklist

**Files:**
- Create: `deploy/compose/docker-compose.yml`, `deploy/compose/gateway.dev.yaml`, `deploy/compose/.env.example`, `deploy/compose/README.md`, `deploy/compose/stub-homestudio/{pyproject.toml,app.py,Dockerfile,test_stub.py}`, `deploy/k8s/base/{kustomization.yaml,namespace.yaml,gateway-configmap.yaml,gateway-pvc.yaml,gateway-deployment.yaml,gateway-service.yaml,gateway-ingress.yaml,homestudio-mcp-deployment.yaml,homestudio-mcp-service.yaml,homestudio-mcp-networkpolicy.yaml}`, `deploy/k8s/gateway-secret.example.yaml`, `deploy/k8s/README.md`
- Modify: `.github/workflows/ci.yml` (add `stub` test job: `uv run pytest -q` in `deploy/compose/stub-homestudio`)

**Interfaces:**
- Stub: a Starlette app on `:8765` answering `GET /api/taste`, `GET /api/history?since&limit`, `GET /api/ratings?since&limit`, `GET /api/now`, `GET /api/queue`, `GET /api/status` with the envelope `{"ok": true, "data": ..., "error": null}` and fixture data shaped exactly as `docs/api.md` in `home-studio-server` (two history entries, one rating, an idle now-playing, a two-item queue); `since` filters by `started_at`/`created_at` string comparison; `limit` honoured.
- Compose services: `gateway` (build `../../gateway`, port `8787:8787`, mounts `./gateway.dev.yaml:/etc/pdl/gateway.yaml:ro` and `./secrets.dev.yaml:/etc/pdl/secrets.yaml:ro`, volume `gateway-data:/data`, env `HOMESTUDIO_MCP_SECRET` from `.env`), `homestudio-mcp` (image `ghcr.io/jamilcpu/homestudio-mcp:latest`, env `HOMESTUDIO_API_URL=http://homestudio-stub:8765`, `HOMESTUDIO_MCP_SHARED_SECRET=${HOMESTUDIO_MCP_SECRET}`), `homestudio-stub` (build `./stub-homestudio`). `gateway.dev.yaml` = example config with `issuer_url: http://localhost:8787`, `resource_url: http://localhost:8787/mcp`, `db_path: /data/gateway.db`, `audit_dir: /data/audit`, only the `homestudio` backend at `http://homestudio-mcp:8000/mcp`, plus the `automations` backend commented out with instructions. `.env.example`: `HOMESTUDIO_MCP_SECRET=change-me-run-pdl-new-client-secret`. `secrets.dev.yaml` is gitignored; `README.md` shows the three commands to create it (`uv run pdl-set-passphrase > secrets.dev.yaml`, `uv run pdl-new-client-secret --client personal-assistant --out ./pa.secret >> secrets.dev.yaml`, then `docker compose up --build`) and how to point Claude Code at it (`claude mcp add --transport http pdl-gateway http://localhost:8787/mcp`).
- k8s: namespace `pdl`; gateway Deployment (1 replica, `nodeSelector: {kubernetes.io/hostname: jw}`, image `ghcr.io/jamilcpu/pdl-gateway:latest`, readiness + liveness `GET /healthz :8787`, envFrom Secret `gateway-secrets` for `HOMESTUDIO_MCP_SECRET` and `AUTOMATIONS_MCP_TOKEN`, volumes: ConfigMap `gateway-config` at `/etc/pdl/gateway.yaml`, Secret key `secrets.yaml` at `/etc/pdl/secrets.yaml`, PVC `gateway-data` (5Gi, `local-path`) at `/data`); Service `gateway:8787`; Ingress class `traefik`, host `gateway.lab`, TLS secret `gateway-lab-tls`, annotation `cert-manager.io/cluster-issuer: lab-ca`; homestudio-mcp Deployment (image `ghcr.io/jamilcpu/homestudio-mcp:latest`, env `HOMESTUDIO_API_URL` from ConfigMap value `http://192.168.0.103:8765`, `HOMESTUDIO_MCP_SHARED_SECRET` from the same Secret key `HOMESTUDIO_MCP_SECRET`, readiness `GET /healthz :8000`), ClusterIP Service `homestudio-mcp:8000`, NetworkPolicy allowing ingress to `app=homestudio-mcp` only from pods labelled `app=gateway`. `gateway-secret.example.yaml` documents the Secret's keys with placeholder values and is not in the kustomization. `kustomization.yaml` lists the resources and an `images:` block so the registry can be swapped.
- `deploy/k8s/README.md` "To deploy" checklist: (1) `jw` Ready and `lab-gitops` + registry exist (migration session); (2) cert-manager with ClusterIssuer `lab-ca` installed, CA cert installed on Warstation and the Pixel; (3) Pi-hole record `gateway.lab → <traefik VIP or jw>`; (4) create namespace and Secret from the helper outputs (`kubectl -n pdl create secret generic gateway-secrets --from-file=secrets.yaml=... --from-literal=HOMESTUDIO_MCP_SECRET=... --from-literal=AUTOMATIONS_MCP_TOKEN=...`); (5) copy `deploy/k8s/base` into `lab-gitops/apps/private-data-layer/` and set the `automations` backend URL once the migration publishes the Automations MCP Service name; (6) verify `curl -k https://gateway.lab/healthz`, then the Claude Code approval, then an audit entry.

- [ ] **Step 1: Write `deploy/compose/stub-homestudio/test_stub.py`** (httpx `ASGITransport`): `test_envelope_on_every_route`, `test_history_since_filters`, `test_ratings_limit` — fail, implement `app.py`, pass.
- [ ] **Step 2: Write the compose, dev config, `.env.example`, README**
- [ ] **Step 3: Write the k8s manifests and README**; verify with `uv run --with kustomize` is not available, so validate structure with `python -c "import yaml,glob; [list(yaml.safe_load_all(open(f))) for f in glob.glob('deploy/k8s/base/*.yaml')]"` → no exception.
- [ ] **Step 4: Commit** — `feat(deploy): compose for local dev with a Home Studio stub, k8s manifests and deploy checklist`

Then open the PR for the repo: branch `feat/step1-gateway` (create it at Task 1 before the first commit; the spec commit is already on `main`), `gh pr create --fill`, wait for CI, merge with `gh pr merge --merge --delete-branch`.

---

## Task 11: Home Studio REST API: `since` on history and `GET /api/ratings`

Repo: `F:\Dev\YoutubeBrainwash` (`JamilCPU/home-studio-server`, default branch `master`). Branch `feat/homestudio-mcp` for Tasks 11 and 12.

**Files:**
- Modify: `src/store/videos.py` (`history`, new `RatingEntry`, `list_ratings`), `src/api/routes_library.py`, `src/api/models.py`, `docs/api.md`, `tests/store/test_videos.py`, `tests/api/test_library.py`

**Interfaces:**
- Produces:
  - `history(conn, *, unrated_only: bool = False, since: str | None = None, limit: int = 50, offset: int = 0) -> list[HistoryEntry]` — `since` is an ISO 8601 UTC string already normalized by the route; SQL adds `p.started_at >= ?`.
  - `RatingEntry(id: int, video_id: str, title: str, channel_name: str | None, liked: bool | None, satisfaction: int | None, notes: str | None, created_at: str)` frozen dataclass.
  - `list_ratings(conn, *, since: str | None = None, limit: int = 50) -> list[RatingEntry]` — every rating row (not only the latest per video), newest first (`ORDER BY r.id DESC`), joined to `videos` for title/channel.
  - `rating_entry_out(r: RatingEntry) -> dict` → `{"id", "video_id", "title", "channel", "liked", "satisfaction", "notes", "created_at"}`.
  - Route changes: `GET /history` gains `since: str | None = Query(default=None)`; `GET /ratings` with `since` and `limit: int = Query(default=50, ge=1, le=200)` → `ok({"ratings": [...]})`. Both parse `since` with `_normalize_since(value: str) -> str` (`datetime.fromisoformat`; a trailing `Z` accepted; naive → UTC; returns `dt.astimezone(timezone.utc).isoformat()`); invalid → `HTTPException(422, "since: must be an ISO 8601 timestamp")`.

- [ ] **Step 1: Store tests** in `tests/store/test_videos.py`: `test_history_since_filters_by_started_at`, `test_list_ratings_newest_first_all_rows` (two ratings on one video → both returned, newest first), `test_list_ratings_since_and_limit`. Run → FAIL (TypeError / ImportError).
- [ ] **Step 2: Implement store changes**; run → PASS.
- [ ] **Step 3: API tests** in `tests/api/test_library.py`: `test_history_since_query`, `test_history_bad_since_422` (error text contains `since`), `test_get_ratings_shape` (set of keys exact), `test_get_ratings_since_and_limit`, `test_get_ratings_limit_out_of_range_422`. Run → FAIL.
- [ ] **Step 4: Implement route + model changes**; run `pytest tests/api/test_library.py tests/store/test_videos.py -q` → PASS; then `pytest -q` (whole suite, ~540 tests, single process) → PASS.
- [ ] **Step 5: Document in `docs/api.md`**: add `since` to `GET /api/history` (query table, 422 case) and a `### GET /api/ratings` section under "Ratings and history" with request, response example and errors, in the file's existing style.
- [ ] **Step 6: Commit** — `feat(api): since filter on history and GET /api/ratings`

---

## Task 12: `homestudio-mcp` server in `home-studio-server/mcp/`

**Files:**
- Create: `mcp/pyproject.toml`, `mcp/homestudio_mcp/__init__.py`, `mcp/homestudio_mcp/client.py`, `mcp/homestudio_mcp/server.py`, `mcp/homestudio_mcp/middleware.py`, `mcp/homestudio_mcp/__main__.py`, `mcp/tests/{__init__,test_client,test_server,test_middleware,test_main}.py`, `mcp/Dockerfile`, `mcp/README.md`, `.github/workflows/ci.yml`
- Modify: `README.md` (one paragraph pointing at `mcp/README.md`), `.gitignore` if needed (`mcp/.venv` is covered by `.venv/`)

**Interfaces:**
- `pyproject.toml`: name `homestudio-mcp`, `requires-python >=3.11`, deps `mcp>=1.30,<2`, `httpx>=0.27`, `uvicorn>=0.30`; dev group `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `ruff`; script `homestudio-mcp = "homestudio_mcp.__main__:main"`; pytest `asyncio_mode = "auto"`, `addopts = "--cov=homestudio_mcp --cov-fail-under=80"`.
- `client.py`: `class HomeStudioClient:` `__init__(self, base_url: str, timeout_s: float = 5.0)`, `async get(self, path: str, params: Mapping[str, Any] | None = None) -> Any` → the envelope's `data`; non-`ok` envelope → `ToolError(error)`; timeout → `ToolError("Home Studio timed out")`; connection error → `ToolError("Home Studio unreachable")`; non-JSON/odd shape → `ToolError("Home Studio returned an unexpected response (HTTP <status>)")`. `ToolError` is `mcp.server.fastmcp.exceptions.ToolError`.
- `server.py`: `build_server(client: HomeStudioClient, *, host: str = "0.0.0.0", port: int = 8000) -> FastMCP` (name `homestudio`, `streamable_http_path="/mcp"`, `stateless_http=True`, `json_response=True`), with `@mcp.custom_route("/healthz", methods=["GET"])` returning `{"ok": true}`; tools and exact descriptions:
  - `get_taste_profile() -> dict | None` — "Jamil's YouTube listening taste profile: a summary, likes and dislikes. Use it to know what he enjoys before recommending or judging media."
  - `list_ratings(limit: int = 50, since: str | None = None) -> list[dict]` — "Ratings Jamil gave to videos (liked, 1-10 satisfaction, notes), newest first. Use it when asked what he liked or disliked recently. since is an ISO 8601 timestamp." Calls `GET /api/ratings`; returns the entries unchanged.
  - `get_listening_history(since: str | None = None, limit: int = 50) -> list[dict]` — "Videos played on the home speaker, newest first, with how long each was listened to and its rating. Use it for questions like what did I listen to this week. since is an ISO 8601 timestamp." Calls `GET /api/history`; trims each entry to `{"play_id", "video_id", "title", "channel", "duration_s", "started_at", "seconds_listened", "end_reason", "source", "rating"}` (no chapters, no thumbnail).
  - `get_now_playing() -> dict` — "What the home speaker is playing right now: state, title, channel, position and duration in seconds, and the output device." Trims to `{"state", "video_id", "title", "channel", "position_s", "duration_s", "output_device"}` (`video_id`/`title`/`channel` `None` when idle).
  - `list_queue() -> dict` — "Videos waiting to play next on the home speaker, in order, and the total remaining seconds." Trims items to `{"id", "video_id", "title", "channel", "duration_s", "source", "reason"}`, keeps `remaining_s`.
  - `limit` arguments: `Annotated[int, Field(ge=1, le=200)]`.
- `middleware.py`: `class SharedSecretMiddleware:` `__init__(self, app: ASGIApp, secret: str, *, header: str = "x-gateway-secret", open_paths: tuple[str, ...] = ("/healthz",))`; HTTP requests whose path is not open and whose header is missing or differs (constant-time) get `401 {"error": "unauthorized"}`; lifespan passes through; websocket closes with 1008.
- `__main__.py`: `main(argv: list[str] | None = None) -> int` reads env `HOMESTUDIO_API_URL` (required), `HOMESTUDIO_MCP_SHARED_SECRET` (required), `HOMESTUDIO_MCP_HOST` (default `0.0.0.0`), `HOMESTUDIO_MCP_PORT` (default `8000`); missing env → message on stderr, return 1; runs `uvicorn.run(SharedSecretMiddleware(build_server(...).streamable_http_app(), secret), host=..., port=...)`.
- Dockerfile: `python:3.12-slim`, uv, `uv sync --frozen --no-dev`, non-root, `EXPOSE 8000`, `CMD ["homestudio-mcp"]`.
- CI (`.github/workflows/ci.yml`): job `server` (`pip install -e .[dev]`, `pytest -q`), job `mcp` (`uv sync`, `uv run pytest -q`, `uv run ruff check .` in `mcp/`), job `image` on push to `master` needing both: build `mcp/` and push `ghcr.io/jamilcpu/homestudio-mcp:latest` and `:${{ github.sha }}` (permissions `packages: write`). No hostnames anywhere.

- [ ] **Step 1: Write the failing tests** (respx mocks `HOME = "http://home-studio.test:8765"`, tools through `create_connected_server_and_client_session` as in the Automations MCP tests):

```python
# test_client.py
async def test_get_unwraps_data(api)            # ok envelope → data
async def test_error_envelope_raises_tool_error(api)   # {"ok": false, "error": "video not found"} → ToolError("video not found")
async def test_timeout_and_unreachable(api)
async def test_non_json_is_tool_error(api)
# test_server.py
async def test_tools_registered()               # names == {"get_taste_profile","list_ratings","get_listening_history","get_now_playing","list_queue"}; every description non-empty and < 300 chars
async def test_taste_passthrough_and_null(api)
async def test_history_trimmed_and_params(api)  # since/limit forwarded as query; entry keys exactly the trimmed set
async def test_ratings_forwards_params(api)
async def test_now_playing_idle_and_playing(api)
async def test_queue_trimmed(api)
async def test_limit_out_of_range_is_tool_error()      # limit=0 → isError
async def test_healthz_route()                  # via the Starlette app with httpx ASGITransport → 200 {"ok": true}
# test_middleware.py
async def test_rejects_missing_or_wrong_header()
async def test_passes_matching_header_and_open_paths()
async def test_websocket_closed_1008()
# test_main.py
def test_main_requires_env(monkeypatch, capsys)  # → 1 and names the variable
def test_main_runs_uvicorn(monkeypatch)          # uvicorn.run captured with host/port
```

- [ ] **Step 2: Run to verify failure** (from `mcp/`: `uv sync && uv run pytest -q --no-cov`)
- [ ] **Step 3: Implement the four modules**
- [ ] **Step 4: Run to verify pass with coverage** → `uv run pytest -q` ≥ 80%; `uv run ruff check .` clean
- [ ] **Step 5: Write `mcp/README.md`** (what it is, the five tools, the env vars, "runs beside the gateway, not on the Pi", how to run locally with `uv run homestudio-mcp`), `mcp/Dockerfile`, CI workflow; add the README paragraph to the repo root README.
- [ ] **Step 6: Grep guard**: `git grep -nE "192\.168|\.lab\b|raspberrypi|pi3" -- mcp .github` → no matches.
- [ ] **Step 7: Commit** — `feat(mcp): homestudio-mcp read-only MCP server over the REST API`
- [ ] **Step 8: PR**: `gh pr create --base master --fill` with a body listing the two API additions, the five tools, env vars, and that nothing is deployed to the Pi by this PR. Wait for CI green, `gh pr merge --merge --delete-branch`. Record the PR URL.

---

## Task 13: End-to-end verification on Warstation (no Docker)

No new code unless a verification fails. Record results in `docs/superpowers/plans/2026-11-02-step1-verification.md` in `private-data-layer` (what ran, what was seen, what is pending).

- [ ] **Step 1: Secrets for the dev stack** (never pasted in chat): in `deploy/compose/`, `uv run --project ../../gateway pdl-set-passphrase > secrets.dev.yaml` (the builder types a passphrase into the hidden prompt and keeps it in a local note outside the repo), `uv run --project ../../gateway pdl-new-client-secret --client personal-assistant --out ./pa.secret >> secrets.dev.yaml`; `HOMESTUDIO_MCP_SECRET` generated with `python -c "import secrets;print(secrets.token_urlsafe(32))"` into a local `.env` (gitignored).
- [ ] **Step 2: Start the backends locally** (plain processes, no Docker): `homestudio-mcp` from `F:\Dev\YoutubeBrainwash\mcp` with `HOMESTUDIO_API_URL=http://192.168.0.103:8765` (read-only calls to the live Pi3 server), `HOMESTUDIO_MCP_SHARED_SECRET=<.env value>`, port 8766. Automations: run `pa-mcp --http --host 127.0.0.1 --port 7702` from `F:\Dev\personal-automations\mcp` with `PA_HUB_URL` set to the (currently down) hub and `PA_MCP_HTTP_TOKEN` fresh from `pa-mcp gen-token`, TLS cert/key from a self-signed pair generated with `openssl` in WSL into the scratchpad; the gateway backend gets `ca_file` pointing at that cert. `tools/list` works without the hub; calls will report `hub unreachable`, which is expected until the migration re-homes the hub. If TLS setup blocks, record it and verify the automations path with the in-process tests only.
- [ ] **Step 3: Start the gateway** with a local config (`issuer_url: http://127.0.0.1:8787`, `resource_url: http://127.0.0.1:8787/mcp`, `db_path`/`audit_dir` under the scratchpad, both backends) via `uv run pdl-gateway --config ... --secrets ...`. `curl http://127.0.0.1:8787/healthz` → `{"ok": true}`; `curl http://127.0.0.1:8787/.well-known/oauth-authorization-server` shows `registration_endpoint`.
- [ ] **Step 4: Claude Code as the client**: in a scratch directory, `claude mcp add --transport http -s local pdl-gateway http://127.0.0.1:8787/mcp`. Open an interactive `claude` session in a terminal tab, run `/mcp`, choose the server, authenticate; copy the printed URL into the built-in browser pane, enter the passphrase, Approve; back in Claude Code the server shows connected. Ask it: "List every MCP tool you have from pdl-gateway, names only." Expected names: `homestudio_get_taste_profile`, `homestudio_list_ratings`, `homestudio_get_listening_history`, `homestudio_get_now_playing`, `homestudio_list_queue`, `automations_list_devices`, `automations_get_device`, `automations_list_programs`, `automations_get_program`, `automations_get_program_logs`, `automations_get_usb_power`, `automations_recent_history` and nothing else. Ask it to call `homestudio_get_taste_profile` and `homestudio_get_now_playing` (real Pi3 data comes back), and to call a tool named `bank_spend_summary` (expect "unknown tool"). Then `claude mcp remove -s local pdl-gateway`.
- [ ] **Step 5: Headless path**: a 20-line script in the scratchpad reads `pa.secret`, posts `client_credentials` to `/token`, opens an MCP session with the SDK client, asserts `gateway_audit_search` is listed and returns the entries from Step 4 (client_name "Claude Code" or whatever Claude Code registered, tools called, `unknown tool` error on the bank call).
- [ ] **Step 6: Audit file**: `cat <audit_dir>/audit-<today>.jsonl` shows one line per call with hash, size, latency; no arguments.
- [ ] **Step 7: Note what is pending**: `list_ratings` against the live Pi3 needs the merged server deployed there (`scripts/deploy-pi3.sh` + `systemctl --user restart home-studio`), which waits for Jamil's go-ahead; `docker compose up` on a dev box waits for Docker Desktop to be allowed; cluster deploy waits for the migration. Commit the verification note.

---

## Self-review notes

- Spec coverage: 5.1 auth (both flows, DCR → interactive grant, revocable opaque tokens, scopes, filtering, audit, `/healthz`, config+secret split, TLS left to the Ingress/cert-manager manifests) → Tasks 2–8, 10. 5.2 common rules (separate container, read tools, 32B descriptions, trimmed JSON, `/healthz`, shared secret) → Task 12; homestudio tool list and missing endpoints → Tasks 11–12; automations allowlist → Tasks 2, 6, 7. `gateway:audit` tool → Task 5. 5.4 image per repo built in CI → Tasks 1, 8, 12. Section 6 layout → Task 1 and repo bootstrap. Section 7 step 1 "done" → Task 13. Section 8 rules → Global Constraints.
- Names used consistently: `Backend`, `RemoteBackend`, `AuditBackend`, `build_backends`, `build_proxy_server`, `build_app`, `GatewayAuthProvider`, `TokenStore`, `AuditLog`, `hash_args`, `prefixed`, `split_tool_name`, `Caller`.
- Open to the executor: exact SDK import path of the streamable-HTTP ASGI app class in 1.30 (read `mcp/server/fastmcp/server.py` near `streamable_http_app()`), and whether `asgi-lifespan` + `ASGITransport` carries the SDK's SSE/JSON responses cleanly (fallback: uvicorn thread on `127.0.0.1:0`).
