# Deploying to the cluster

The manifests in `base/` follow the lab's GitOps conventions: one folder per app under
`apps/<name>/` in the `lab-gitops` repository, namespace `lab`, stateful pods pinned with the
`role: primary-worker` label, state under that node's `/data/lab/<app>` directory, images in the
lab registry, Traefik publishing apps as `<name>.lab`, and **no secrets or host addresses in
committed files**: those are created out of band with `kubectl` and this folder's helper.

## To deploy

1. **Images.** `homestudio-mcp` is pulled straight from GHCR by digest (public package). The
   `pdl-gateway` GHCR package is private, so until it is made public the image is built on the
   primary node into the lab registry and pinned by tag in `kustomization.yaml`:
   ```bash
   docker build -t registry.lab:5000/lab/pdl-gateway:<git-sha> gateway/ && docker push registry.lab:5000/lab/pdl-gateway:<git-sha>
   ```
2. **TLS.** The cluster runs cert-manager with a `lab-ca` ClusterIssuer; both Ingresses carry
   `cert-manager.io/cluster-issuer: lab-ca`, so the certificates in `gateway-lab-tls` and
   `assistant-lab-tls` are issued and renewed automatically. Nothing to create by hand.
   Clients must trust the lab CA: export it once with
   `kubectl -n cert-manager get secret <issuer secret> -o jsonpath='{.data.tls\.crt}' | base64 -d > lab-ca.pem`
   and install it on each client machine (`scripts/trust-lab-ca.ps1` does it on Windows and sets
   `NODE_EXTRA_CA_CERTS` for Claude Code, which ignores the OS trust store).
   `make-lab-tls.sh` remains for clusters without cert-manager.
3. **Secrets and config.** From the gateway package (`uv run ...` in `gateway/`):
   ```bash
   uv run pdl-set-passphrase > secrets.yaml           # one hidden prompt, twice
   echo "client_secrets:" >> secrets.yaml
   uv run pdl-new-client-secret --client personal-assistant --out pa.secret >> secrets.yaml
   HS=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
   kubectl -n lab create secret generic gateway-secrets --from-file=secrets.yaml=secrets.yaml --from-literal=HOMESTUDIO_MCP_SECRET="$HS"
   kubectl -n lab create configmap homestudio-mcp-config --from-literal=HOMESTUDIO_API_URL=http://<home-studio-host>:8765
   ```
   `pa.secret` is the assistant's client secret: it goes only to the machine the assistant runs
   on. Delete the local `secrets.yaml` afterwards; the hashes it holds are in the cluster now.
4. **Manifests.** Copy `base/` to `lab-gitops/apps/private-data-layer/`, add the folder to
   `apps/kustomization.yaml`, commit and push. Flux applies it within a minute
   (`flux get kustomizations`, `kubectl -n lab get pods -l app.kubernetes.io/part-of=private-data-layer`).
5. **Automations backend.** Wired in the manifests: the Automations MCP server runs with host
   networking on the primary node and its certificate names only that node address, so the
   gateway reaches it at `https://${NODE_IP}:7702/mcp` (the address comes from the downward API
   and the gateway expands the placeholder at startup), trusts the hub certificate that the init
   container copies from the node data disk, and sends the bearer token from the existing
   `pa-mcp` Secret. Nothing to create by hand beyond what the Automations app already has.
6. **Verify.** `curl https://gateway.lab/healthz` (with the lab CA installed) answers
   `{"ok": true}`; `claude mcp add --transport http pdl-gateway https://gateway.lab/mcp`
   leads to the approval page; after approving, the Home Studio read tools are listed and a call
   shows up in `/data/lab/private-data-layer/audit/`.

## Assistant

The assistant (`assistant/`) is a chat UI and OpenAI-compatible API at `https://assistant.lab`.
It signs in through the gateway (OAuth, with the gateway's approval page), calls lab tools via
the gateway, and talks to an Ollama server for the model. Deploy it after the gateway.

1. **Image.** Built on the primary node into the lab registry, like the gateway; the controller
   pins the sha in `kustomization.yaml` (the manifest ships the placeholder tag `latest`):
   ```bash
   docker build -t registry.lab:5000/lab/pdl-assistant:<git-sha> assistant/ && docker push registry.lab:5000/lab/pdl-assistant:<git-sha>
   ```
2. **TLS.** `bash make-lab-tls.sh assistant.lab assistant-lab-tls`, then run the
   `kubectl create secret tls` command it prints.
3. **Lab CA.** The assistant reaches the gateway at `https://gateway.lab` and must trust the lab
   CA (`SSL_CERT_FILE` points at the mounted file):
   ```bash
   kubectl -n lab create configmap lab-ca --from-file=lab-ca.pem=lab-tls/lab-ca.pem
   ```
4. **Secrets.** `client.secret` is the `pa.secret` file that `pdl-new-client-secret --client
   personal-assistant --out pa.secret` wrote in step 3 of "To deploy" (its hash is already in
   the gateway's secrets). From `assistant/`:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"      # the cookie_secret value
   uv run pdl-assistant-new-api-token --name openwebui --out openwebui.token   # prints the hash line
   ```
   Write `assistant-secrets.yaml` with `cookie_secret:` and an `api_tokens:` block holding the
   printed hash line (shape: `assistant-secret.example.yaml`), then:
   ```bash
   kubectl -n lab create secret generic assistant-secrets --from-file=secrets.yaml=assistant-secrets.yaml --from-file=client.secret=pa.secret
   ```
   `openwebui.token` is the plaintext bearer for API clients; keep it only where they run.
   Delete the local `assistant-secrets.yaml` and the local copy of `pa.secret` afterwards
   (the Secret now holds both).
5. **Config.** The Ollama address and model are out of band (no workstation address in git):
   ```bash
   kubectl -n lab create configmap assistant-config \
     --from-literal=ASSISTANT_OLLAMA_URL=http://<workstation>:11434 \
     --from-literal=ASSISTANT_MODEL=qwen3.5:27b
   ```
   The workstation must accept inbound connections on 11434 from the LAN (Ollama listening on
   all interfaces via `OLLAMA_HOST`, and a firewall rule allowing the cluster nodes).
6. **Manifests.** Same as the gateway: copy `base/` to `lab-gitops`, commit, push.
7. **Verify.** Open `https://assistant.lab`, sign in (you are sent to the gateway approval page),
   and ask a question. For the API:
   `curl -H "Authorization: Bearer $(cat openwebui.token)" https://assistant.lab/v1/models`.

The pod stays in `ContainerCreating` until `assistant-secrets`, `assistant-config` and `lab-ca`
exist.

## Notes

- The gateway pod stays in `ContainerCreating` until `gateway-secrets` exists;
  `homestudio-mcp` until `homestudio-mcp-config` exists too.
- `homestudio-mcp` is amd64-only today, hence the same node pin as the gateway.
- Scopes, grants and backends are in the ConfigMap's `gateway.yaml`; changing a grant takes
  effect for existing tokens on their next use (the gateway narrows live tokens to the current
  grant) but a restart is needed to reload the file.
