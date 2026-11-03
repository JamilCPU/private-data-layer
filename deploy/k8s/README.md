# Kubernetes manifests

`base/` is a kustomize base for the `pdl` namespace: the gateway (the only Ingress,
`gateway.lab`) and `homestudio-mcp` (ClusterIP only, reachable from the gateway pods alone).
Everything stateful is pinned to the `primary-worker` node, where the PersistentVolume lives.

Secrets are never committed. `gateway-secret.example.yaml` documents the keys of the
`gateway-secrets` Secret; it is not part of the kustomization.

## To deploy

1. **Cluster prerequisites.** The `primary-worker` node is Ready (`kubectl get node primary-worker`), and `lab-gitops`
   and the container registry exist. The migration session delivers all three.
2. **TLS.** cert-manager is installed with the ClusterIssuer `lab-ca`
   (`kubectl get clusterissuer lab-ca`), and the lab CA certificate is installed on the dev workstation
   and on the Android phone.
3. **DNS.** Add a Pi-hole record `gateway.lab` pointing at the Traefik VIP, or at `primary-worker` if there
   is no VIP.
4. **Namespace and Secret.** Create the namespace, then the Secret from the helper outputs
   (`pdl-set-passphrase` and `pdl-new-client-secret`, assembled into one `secrets.yaml` as in
   `gateway/secrets.example.yaml`):

   ```sh
   kubectl create namespace pdl
   kubectl -n pdl create secret generic gateway-secrets \
     --from-file=secrets.yaml=./secrets.yaml \
     --from-literal=HOMESTUDIO_MCP_SECRET=<shared secret> \
     --from-literal=AUTOMATIONS_MCP_TOKEN=<Automations MCP token>
   ```

   The gateway sends `HOMESTUDIO_MCP_SECRET` to `homestudio-mcp`, which reads the same value as
   `HOMESTUDIO_MCP_SHARED_SECRET`.
5. **Manifests.** Copy `deploy/k8s/base` into `lab-gitops/apps/private-data-layer/`. In
   `gateway-configmap.yaml`, set the `automations` backend `url` to the Service name the
   migration publishes for the Automations MCP server (the default is
   `https://automations-mcp.pdl.svc:7702/mcp`). The gateway verifies that server against a CA
   file and refuses to start without it, so also create the ConfigMap the Deployment mounts at
   `/etc/pdl/automations-ca.pem`:

   ```sh
   kubectl -n pdl create configmap automations-ca --from-file=automations-ca.pem=./ca.pem
   ```
6. **Verify.**
   - `curl -k https://gateway.lab/healthz` returns OK (`-k` until the CA certificate is
     installed on the machine you test from).
   - Add the gateway to Claude Code and approve the request on the approval page:
     `claude mcp add --transport http pdl-gateway https://gateway.lab/mcp`.
   - Call a `homestudio_*` tool, then check that the audit log has an entry
     (`kubectl -n pdl exec deploy/gateway -- ls /data/audit`, or ask for `gateway_audit_search`).

## Notes

- The gateway runs one replica with the `Recreate` strategy: its SQLite state sits on a
  ReadWriteOnce volume.
- Mounted files are read once at start-up. After changing `gateway-config` or `gateway-secrets`,
  run `kubectl -n pdl rollout restart deploy/gateway`.
- `kustomization.yaml` has an `images:` block: change `newName` to move to another registry, or
  `newTag` to pin a build.
- `homestudio-mcp` reaches the Home Studio server on the Pi host at the URL in the
  `homestudio-mcp-config` ConfigMap.
