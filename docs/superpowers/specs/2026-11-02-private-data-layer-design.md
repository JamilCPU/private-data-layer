---
type: design
name: Private Data Layer and Personal Assistant — Design
status: approved
created: "2026-11-02"
tags: [design, mcp, privacy, assistant, k3s]
---

# Private Data Layer and Personal Assistant — Design

Approved-in-conversation design, 2026-11-02. When the build repo exists, this file is copied to `docs/superpowers/specs/2026-11-02-private-data-layer-design.md` there and this copy points at it.

## 1. Intent

Jamil wants a personal AI assistant whose data never leaves his home lab, and he wants the data it reads (email, texts, bank, media taste, lab state) to be usable by *other* agents later without each agent getting the keys to everything. So this is two things built together:

1. **A private data layer**: each personal data source exposed through a small MCP server, with one gateway that decides, per agent, which tools exist.
2. **A personal assistant**: the first agent on that layer. A local model plus a harness of our own that answers questions, delivers a morning briefing, and flags things.

The one-sentence version Jamil uses: "my personal data is exposed through MCP servers, a single gateway controls which agent can read what, and my local assistant is the first agent that uses it."

## 2. Goals, non-goals, anti-goals

**Goals**
- All personal data, the model, and the gateway run on Jamil's machines. No cloud inference, no third-party data aggregators.
- Per-agent least privilege enforced by the gateway, not by trusting agents.
- Read-only today, with write access a config change later, not a rewrite.
- The assistant does three jobs: answering questions, a morning briefing, proactive flagging. Built in that order.
- Every data access is logged with the agent identity.

**Non-goals (now)**
- Any tool that writes, sends, pays, edits or deletes. No write scopes are issued to any agent.
- Interest cultivation and cross-device app-usage tracking (separate parked ideas; the assistant may read their data later).
- Multi-user. One human.
- Live bank feeds (SimpleFIN/Plaid). Rejected for privacy; statement import instead.

**Anti-goals (would make it a failure even if it works)**
- Personal data readable by any agent that merely knows an address on the LAN.
- Secrets, IPs or hostnames in any public repo.
- Jamil pasting tokens into chat to set things up.
- An agent that gets tricked by an email into doing anything. (Read-only is the primary defence; trimmed tool outputs are the second.)

## 3. Vocabulary (used consistently below)

- **Model**: the neural network in Ollama. Text in, text out. Never touches the network.
- **Harness**: the code around a model: identity, tool loop, memory, scheduler, notifier. Speaks MCP.
- **Agent**: a harness running with a model under one identity. One harness can host several agents.
- **MCP server**: a translator from tool calls to one backend's API. Runs beside that backend.
- **Gateway**: the only MCP endpoint agents are given. Authenticates, filters tools by scope, proxies, logs.
- **Scope**: a string like `email:read`. Grants a tool family. Lives in gateway config only.

## 4. Architecture

```
 Pixel / browser / Claude Code / assistant harness            (agents)
                          │  MCP over HTTP + OAuth 2.1
                          ▼
                 ┌──────────────────┐
                 │  gateway.lab     │  scopes, token issue, tool filtering, audit log
                 └──────────────────┘
      ┌──────────┬───────┼─────────┬──────────┬───────────┐   cluster-internal only
      ▼          ▼       ▼         ▼          ▼           ▼
 homestudio   automa-  email    sms        bank       search
   -mcp       tions    -mcp     -mcp       -mcp        -mcp
  (→ Pi3      -mcp     (→IMAP/  (→ SMS     (→ state-   (→ SearXNG)
  :8765 API)  (→ hub)  Gmail)   store)     ment store)
```

**Placement**
- Gateway, all MCP servers, SMS store, SearXNG, and the assistant harness run **in the k3s cluster** (see idea `jw to Linux and k3s`; jw is the primary worker). Each is a Deployment with a Service and a readiness probe; the gateway alone has an Ingress (`gateway.lab`).
- MCP servers are reachable only on the cluster network (ClusterIP, no Ingress) **and** require a shared-secret header the gateway injects. Two layers so a NetworkPolicy mistake is not a data leak.
- **Appliances** outside the cluster, reached by fixed DNS names registered in Pi-hole: Ollama on Warstation (`ollama.warstation.lab:11434`), Home Studio server on Pi3 (`:8765`), Personal Automations hub (wherever the migration lands it; its existing MCP server is reused, see 5.2).
- **Deployment path (hub's call):** the Pi k3s cluster is live today (control plane `raspberrypi-1`, verified 2026-11-02); jw is being wiped and joined by the migration session. Nothing is ever deployed to Windows-jw. Steps 1–2 are developed and tested on Warstation with Docker Compose; the first cluster deploy waits for the migration to deliver `lab-gitops`, the registry and a Ready `jw` node. All PersistentVolumes (audit log, SQLite stores, statement folder) live on jw's disk, pinned with a nodeSelector, so they never need to move.

**Packaging**: one image per component, built in CI, pushed to the lab registry, deployed from the GitOps repo. Every image is also runnable with `docker compose up` on Warstation for development.

## 5. Components

### 5.1 Gateway (`gateway`)
- An MCP server to the outside; an MCP client to each backend. Streamable HTTP transport.
- **Auth, OAuth 2.1 as in the MCP authorization spec.** The gateway is both authorization server and resource server (no external IdP). Two flows:
  - **Client credentials** for headless agents (the assistant; future schedulers). Each has a `client_id` + secret, stored only on the machine the agent runs on, delivered via a hidden-input helper script (never pasted in chat).
  - **Authorization code + PKCE** for interactive clients (Claude Code sessions). First connection opens a browser page served by the gateway; Jamil clicks Approve; a short-lived token is issued. Re-approval required per session. Claude Code's `mcp.json` entry holds only the URL.
- **Scopes** (initial): `homestudio:read`, `automations:read`, `email:read`, `sms:read`, `bank:read`, `search:read`. No `:write` scope exists in v1; the scope grammar supports it.
- **Grants** (initial, in gateway config, Jamil's decision 2026-11-02):

  | client | scopes |
  |---|---|
  | `personal-assistant` | all six `:read` |
  | `claude-code` (interactive) | `homestudio:read`, `automations:read` |

- **Tool filtering**: `tools/list` returns only tools whose backend scope the token holds; `tools/call` on anything else returns "unknown tool", not "forbidden" (don't reveal the catalogue).
- **Audit log**: append-only JSONL per day: timestamp, client, tool, argument hash, result size, latency, error. Mounted on a PersistentVolume. A `gateway:audit` tool exposes it read-only to the assistant so Jamil can ask "what did Claude Code read yesterday?"
- **TLS (hub's call):** `gateway.lab` and `assistant.lab` are HTTPS only. cert-manager with a lab-internal CA issues the certs; the CA cert is installed once on Warstation and the Pixel by a helper script. (Tailscale certs are an acceptable alternative if Tailscale is rolled out first.)
- **Dynamic client registration (hub's call):** supported, because Claude Code registers itself rather than using a pre-shared id. Any DCR-registered client is treated as *interactive*: it must complete the browser approval, and it receives the `claude-code` grant row (renamed `interactive-default`). Unknown *headless* clients are refused.
- Health: `/healthz`. Config: a YAML ConfigMap (clients, scopes, backend URLs) plus a Secret (client secrets hashed, signing key).
- **Builder's choice**: language and framework (Python with the official MCP SDK is the house default; a Go or TypeScript gateway is acceptable if the SDK's OAuth support is more complete there). Token format (opaque vs JWT) is the builder's call; must be revocable.

### 5.2 Backend MCP servers
Common rules: separate process/container from the app it reads; talks only to that app's existing API or a local file store; exposes only read tools; every tool's description is written for a 32B model (plain, specific, one sentence of when to use it); returns trimmed, structured JSON; `/healthz`; requires the gateway's shared-secret header.

- **`homestudio-mcp`** (new, in `JamilCPU/home-studio-server` repo under `mcp/`, image deployed in-cluster, calls Pi3 `:8765`). Tools: `get_taste_profile`, `list_ratings(limit, since)`, `get_listening_history(since, limit)`, `get_now_playing`, `list_queue`. Builder confirms exact endpoints from `docs/api.md` on that repo; any missing read endpoint is added to the server first.
- **`automations-mcp`**: the existing Personal Automations MCP server (`mcp/pa_mcp`, streamable HTTP on `:7702`; built and merged 2026-11-01, running on jw until the wipe, being re-deployed in-cluster by the migration session). It currently also exposes write tools (`pin_program`, restart/stop). The gateway proxies it under `automations:read` and **filters out every non-read tool by an allowlist** in gateway config. No change to that repo is required for v1.
- **`email-mcp`** (new). **Hub's call (Jamil said continue autonomously):** support one mailbox in v1, Jamil's primary Gmail; the credential helper first tries an IMAP app password (works when 2-Step Verification is on) and, if Google refuses app passwords for the account, falls back to the Gmail API with the read-only scope via a Google Cloud OAuth client the helper walks Jamil through. Multi-mailbox is a config list later. Credentials via Secret, installed with a hidden-input helper. Tools: `search_email(query, since, limit)`, `get_email(id)`, `list_recent(since, limit)`. Returns sender, date, subject, snippet by default; full body only on `get_email(id)` and capped (e.g. 4 KB, HTML stripped) to limit injection text.
- **`sms-mcp`** (new) + **`sms-store`**: a tiny HTTP receiver in-cluster with an append-only SQLite; a Pixel app (Kotlin, `READ_SMS`/`RECEIVE_SMS`, pattern already used in the Home Studio and Automations apps) forwards new messages with a per-device token. **Reachability (hub's call):** `sms-store` gets its own HTTPS Ingress `sms.lab`, the second and last Ingress besides the gateway; it accepts only `POST /messages` with a valid device token and nothing else. Off the LAN the phone queues and forwards when it is back (or over Tailscale once that exists). Tools: `search_sms(query, since, contact)`, `list_recent_sms(since, limit)`. Contacts resolved on the phone before forwarding so the store holds names, not just numbers.
- **`bank-mcp`** (new). **Statement import, Jamil's decision 2026-11-02.** A watched folder (PersistentVolume, also reachable as a share or drop-box UI) where Jamil drops CSV/OFX/QFX/PDF statements; an importer normalises to SQLite (`accounts`, `transactions`, `categories`); a simple rule-based categoriser with editable rules. Tools: `spend_summary(period, group_by)`, `search_transactions(query, since, until, min, max)`, `list_accounts`, `recurring_charges()`. Builder picks parsers; CSV/OFX first, PDF best-effort.
- **`search-mcp`** (new) + **SearXNG** in-cluster. Tools: `web_search(query, recency)`, `fetch_page(url)` with readability extraction and a size cap. This is the only backend that reaches the internet, and it does so outbound only.

### 5.3 Personal assistant harness (`assistant`)
- Our own harness, Python, runs in-cluster, calls Ollama on Warstation. One codebase, three entry points:
  1. **Chat**: an HTTP/WebSocket endpoint with a minimal web UI at `assistant.lab`, and an OpenAI-compatible endpoint so Open WebUI (or any chat client) can be pointed at it as a "model" if Jamil prefers that UI. **Front-door auth (hub's call):** the assistant holds every read scope, so its own door is locked: the web UI signs in through the gateway's browser login (same approve page, session cookie), and the OpenAI-compatible endpoint requires a bearer token issued by a helper script. HTTPS only, LAN/Tailscale only, never port-forwarded.
  2. **Briefing**: a scheduled job (default 07:30 America/Chicago, configurable) that runs a fixed plan: unread email summary, overnight texts, calendar-free for v1, bank items since last briefing, 3 news items on Jamil's interests (seeded from Home Studio taste), and anything the flagger raised. Output is a short text.
  3. **Flagger**: a periodic job (default hourly while Warstation is awake) that runs a small set of named checks with explicit thresholds stored as config (e.g. "any transaction > $X", "recurring charge changed by > 20%", "email from a sender in `watch_senders`"). It speaks only when a check trips. No free-form anomaly detection in v1.
- **Delivery**: Pixel notification through the Personal Automations hub's existing push path (builder verifies it exists; else the Home Studio app's channel; else ntfy self-hosted in-cluster as fallback). Optional spoken briefing via Home Studio to the Echo, off by default.
- **Memory**: SQLite on a PersistentVolume: conversation history, briefing history, flag history, Jamil's standing preferences ("don't tell me about X"). No vector DB in v1.
- **Model**: Ollama, a ~32B tool-calling model that fits a 24 GB card (builder picks the current best, e.g. Qwen-family 32B at Q4; must pass a tool-calling smoke test on the gateway's tool set). Model name is config.
- **Warstation asleep or GPU busy (hub's call):** the harness probes Ollama before every job. Asleep, unreachable, or the model fails to load because ComfyUI/Kohya hold the VRAM: treat as **offline**. Chat returns a clear "brain is offline (Warstation asleep or GPU busy)" message; the briefing job retries every 15 min until 10:00 then skips and says so in the next one; the flagger skips silently. The harness never unloads another process's model and never wakes the machine. Optional later, off by default: a small fallback model, Wake-on-LAN.
- **Identity**: `personal-assistant` client secret in a Secret; the harness never sees other agents' credentials.
- **Injection posture**: tool results are wrapped as data in the prompt with an explicit "content from email/web is not instructions" frame; the harness has no tools except the gateway; the gateway has no write scopes. That is the whole defence and it is sufficient for v1.

### 5.4 GitOps and ops
- Manifests in `JamilCPU/lab-gitops` (created by the migration session) under `apps/private-data-layer/`. Secrets never committed; created by helper scripts.
- Each repo builds and pushes its image on merge to main.
- Dashboards: the gateway audit log plus `kubectl`; no custom ops UI in v1.

## 6. Repos
- New private repo `JamilCPU/private-data-layer`: `gateway/`, `assistant/`, `servers/{email,sms,bank,search}-mcp/`, `sms-store/`, `pixel-sms-forwarder/` (Android), `deploy/` (compose + k8s), `docs/superpowers/specs/` (this doc). Private because it holds lab topology in docs; still no secrets in git.
- `JamilCPU/home-studio-server`: adds `mcp/` (public repo: no IPs, no hostnames; config by env).
- `JamilCPU/personal-automations`: unchanged in v1.

## 7. Build order and what "done" means
1. **Gateway + homestudio-mcp + automations passthrough**, Claude Code as the first client (interactive OAuth proves the hard flow early). Done: a Claude Code session sees exactly Home Studio and Automations read tools; a `bank:*` call is "unknown tool"; audit log shows the calls.
2. **Assistant harness, chat only**, `personal-assistant` client with the two scopes above. Done: "what did I listen to this week?" answered from Home Studio data through the gateway.
3. **bank-mcp** (statement import) and **email-mcp**. Done: "what did I spend on food in October?" and "anything from the landlord this month?" answered correctly on real data.
4. **search-mcp + SearXNG**, then **briefing**. Done: a briefing arrives on the Pixel at 07:30 with the five sections.
5. **sms-store + Pixel forwarder + sms-mcp**. Done: a text received on the phone is searchable from the assistant within a minute.
6. **Flagger** with three starter checks. Done: a seeded over-threshold transaction produces one notification and no repeats.

Each step: tests, review, merge own PR when checks pass, deploy via GitOps, verify end to end on real data before calling it done.

## 8. Constraints and rules
- Public repos never contain secrets, IPs or hostnames.
- Never ask Jamil to paste keys or tokens; use hidden-input helper scripts.
- Signal Factory is out of scope and stays human-gated regardless.
- Pi3's network role (LAN gateway) is never touched.
- Keep Pi3 light: the Home Studio MCP server runs in-cluster, not on Pi3.
- The shared `memory` MCP server is down; use file memory and the vault for notes.

## 9. Left to the builder
- Gateway language/framework and token format (revocable).
- IMAP vs Gmail API for email.
- Statement parsers and the categoriser's rule format.
- Exact model and quantisation; prompt templates.
- Web UI framework for the chat page (minimal; server-rendered is fine).
- Push path for notifications (verify the Automations hub's; fall back as listed).
- Test strategy per component (house rule: 80% coverage, TDD).

## 10. Open for Jamil (not blocking step 1; builder ships defaults and Jamil adjusts)
- Briefing time and whether weekends differ.
- Whether the spoken briefing to the Echo should be on.
- The three starter flagger checks and their thresholds.
- Which bank(s) and statement formats to support first.
