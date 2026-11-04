# Deploying to the cluster

The manifests in `base/` follow the lab's GitOps conventions: one folder per app under
`apps/<name>/` in the `lab-gitops` repository, namespace `lab`, stateful pods pinned with the
`role: primary-worker` label, state under that node's `/data/lab/<app>` directory, images in the
lab registry, Traefik publishing apps as `<name>.lab`, and **no secrets or host addresses in
committed files**: those are created out of band with `kubectl` and this folder's helper.

## To deploy

1. **Images.** CI publishes `pdl-gateway` (this repo) and `homestudio-mcp`
   (`home-studio-server`) to GHCR. Mirror them into the lab registry from a node that can reach
   both, for example with skopeo:
   ```bash
   skopeo copy --dest-tls-verify=false docker://ghcr.io/jamilcpu/homestudio-mcp:latest docker://registry.lab:5000/jamil/homestudio-mcp:latest
   docker build -t registry.lab:5000/jamil/pdl-gateway:latest gateway/ && docker push registry.lab:5000/jamil/pdl-gateway:latest
   ```
   (`pdl-gateway` is built from source until its GHCR package is public.)
2. **TLS.** `bash make-lab-tls.sh gateway.lab gateway-lab-tls` creates a lab CA (first run) and
   the server certificate, and prints the `kubectl create secret tls` command. Install
   `lab-ca.pem` on each client machine; Claude Code runs on Node, which ignores the OS trust
   store, so also set `NODE_EXTRA_CA_CERTS` to that file where Claude Code runs.
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
5. **Automations backend.** Once the Automations MCP server's certificate is available as a
   ConfigMap (`automations-ca`, key `automations-ca.pem`), add `AUTOMATIONS_MCP_TOKEN` to
   `gateway-secrets`, mount the ConfigMap at `/etc/pdl/automations-ca.pem`, and uncomment the
   `automations` backend in `gateway-configmap.yaml`. The gateway refuses to start if a
   configured `ca_file` is missing, which is why the backend ships commented out.
6. **Verify.** `curl https://gateway.lab/healthz` (with the lab CA installed) answers
   `{"ok": true}`; `claude mcp add --transport http pdl-gateway https://gateway.lab/mcp`
   leads to the approval page; after approving, the Home Studio read tools are listed and a call
   shows up in `/data/lab/private-data-layer/audit/`.

## Notes

- The gateway pod stays in `ContainerCreating` until `gateway-secrets` exists;
  `homestudio-mcp` until `homestudio-mcp-config` exists too.
- `homestudio-mcp` is amd64-only today, hence the same node pin as the gateway.
- Scopes, grants and backends are in the ConfigMap's `gateway.yaml`; changing a grant takes
  effect for existing tokens on their next use (the gateway narrows live tokens to the current
  grant) but a restart is needed to reload the file.
