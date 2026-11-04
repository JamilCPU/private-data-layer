# Step 2 verification — assistant harness (chat)

## Deployed (2026-11-04)

`assistant.lab` runs on the cluster from `lab-gitops/apps/private-data-layer` (Flux): image
`registry.lab/lab/pdl-assistant:438a646`, gateway `registry.lab/lab/pdl-gateway:395260f`. TLS from
the lab CA; out-of-band Secret/ConfigMaps as in `deploy/k8s/README.md`. One deploy defect found and
fixed on the way: inside the cluster `gateway.lab` resolves to the gateway *Service* through the
namespace search path, so the assistant now calls the gateway over the in-cluster service
(`ASSISTANT_GATEWAY_URL=http://gateway:8787`) and sends browsers to
`ASSISTANT_GATEWAY_PUBLIC_URL=https://gateway.lab`.

## Verified from a workstation over TLS (lab CA validated)

- `GET /healthz` → `{"ok": true}`; `GET /` without a session → 302 `/login`; `/v1/models` without a
  bearer → 401.
- Login: `/login` sets the signed `pdl_login` cookie and redirects to the gateway's `/authorize`
  with PKCE S256 and `redirect_uri=https://assistant.lab/oauth/callback` (the assistant registered
  itself at the gateway, which accepts https redirects under its own domain). The gateway's approve
  page names the assistant client and shows the redirect target; after the passphrase and Approve,
  the callback sets `pdl_session` and `/` answers 200.
- Chat: `POST /api/chat` (session + JSON + same origin) → a new conversation and the reply
  `Brain is offline (workstation asleep or GPU busy). Try again later.` with `offline: true` and no
  tool calls, because the workstation firewall blocks inbound 11434 (Ollama itself listens on all
  interfaces). `POST /v1/chat/completions` with the helper-minted bearer → the same sentence in the
  OpenAI response shape; `GET /v1/models` lists the configured model.
- Logout revokes the session: a replayed cookie gets 401.

## Pending

- The live model run (plan Task 9): "what did I listen to this week?" answered from Home Studio
  data through the gateway. Needs either the workstation firewall to allow inbound 11434 from the
  LAN (then the in-cluster assistant works as deployed) or a GPU window for a local run of the
  assistant on the workstation against `https://gateway.lab`. The model smoke test (tool calling
  on the gateway's tool set) decides the default model; `qwen3.5:27b` is the first candidate.
- cert-manager with the `lab-ca` ClusterIssuer is now available: the hand-made
  `gateway-lab-tls` / `assistant-lab-tls` Secrets can be replaced by the
  `cert-manager.io/cluster-issuer: lab-ca` Ingress annotation for automatic renewal.
