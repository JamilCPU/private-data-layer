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

### Briefing

The `briefing` CronJob runs `pdl-assistant-briefing` at 07:30 America/Chicago with the assistant image and the assistant's data volume. It needs the in-cluster `ntfy` Service (port 80) and two out-of-band objects:

1. **ntfy user and token.** `ntfy user add` prompts for a password, hence `-it`. The token is captured in a shell variable and piped into the Secret, so it is never echoed:
   ```bash
   kubectl -n lab exec -it deploy/ntfy -- ntfy user add owner
   kubectl -n lab exec deploy/ntfy -- ntfy access owner briefing rw
   tok=$(kubectl -n lab exec deploy/ntfy -- ntfy token add --label assistant owner \
     | grep -o 'tk_[A-Za-z0-9]*'); [ -n "$tok" ] || echo "no token found" >&2
   printf '%s' "$tok" | kubectl -n lab create secret generic ntfy-publish --from-file=token=/dev/stdin
   unset tok
   ```
2. **Config.**
   ```bash
   kubectl -n lab create configmap briefing-config \
     --from-literal=ASSISTANT_NTFY_TOPIC=briefing \
     --from-literal=ASSISTANT_BRIEFING_TIMEZONE=America/Chicago
   ```
   The CronJob's `timeZone` and `ASSISTANT_BRIEFING_TIMEZONE` must be changed together.
3. **Run it now** with `kubectl -n lab create job --from=cronjob/briefing briefing-now`, read `kubectl -n lab logs job/briefing-now`, then `kubectl -n lab delete job briefing-now`.

The pod stays in `ContainerCreating` until `ntfy-publish`, `briefing-config` and the assistant's own objects exist. The assistant Deployment must have run once first: its `own-data` init container sets the ownership of `/data`, which the job shares. Phone setup is in `assistant/README.md` ("Briefing").

### Flagger

The `flagger` CronJob runs `pdl-assistant-flagger` every hour at :05 with the assistant image and data volume. It evaluates rule-based checks (thresholds, watched senders) through the gateway's read tools, keeps the flags in the assistant's database and sends one ntfy notification per run that lists the new flags (up to 10 lines, then how many more). It uses the same `ntfy-publish` Secret and `briefing-config` topic as the briefing.

1. **Create the config** from the example:
   ```bash
   kubectl -n lab create configmap flagger-config --from-file=flagger.yaml=assistant/flagger.example.yaml
   ```
2. **Change thresholds and watched senders** with `kubectl -n lab edit configmap flagger-config`. The next hourly run picks it up; no restart is needed because each run is a fresh pod.
3. **Run it now** with `kubectl -n lab create job --from=cronjob/flagger flagger-now`, read `kubectl -n lab logs job/flagger-now`, then `kubectl -n lab delete job flagger-now`.
4. **Notifications** arrive on the same ntfy topic as the briefing. Flags are kept in the assistant's database, so nothing repeats, and the morning briefing lists the flags raised since the previous briefing (up to 5). A crash between sending and marking can repeat one notification once. A merchant that bills a subscription and one-off purchases under the same description can cause a one-time false recurring-change flag.
5. **Pause it** with `kubectl -n lab patch cronjob flagger -p '{"spec":{"suspend":true}}'` (set `false` to resume).

The pod stays in `ContainerCreating` until `flagger-config` exists. The assistant Deployment must have run once first, for the `/data` ownership.

## Bank and email

`bank-mcp` and `email-mcp` are read-only MCP servers over the `bank` and `email` sources in the
Lab Data Store (Postgres). Two CronJobs fill it: `bank-importer` (every 15 minutes, reads the
statement inbox) and `email-sync` (every 10 minutes, reads one IMAP mailbox). The servers
connect as the read-only roles `bank_reader` / `email_reader`; the jobs as the owners
`bank_owner` / `email_owner`, which also apply the schema migrations (`*_APPLY_MIGRATIONS=1`).
Only the `personal-assistant` grant sees these tools; `interactive-default` does not.

1. **Images.** Built on the primary node into the lab registry, like the gateway; the controller
   pins the sha in `kustomization.yaml` (the manifests ship the placeholder tag `latest`):
   ```bash
   for s in bank email; do
     docker build -t registry.lab:5000/lab/pdl-$s-mcp:<git-sha> servers/$s-mcp/ && docker push registry.lab:5000/lab/pdl-$s-mcp:<git-sha>
   done
   ```
2. **Lab Data Store.** The `lab-data-store` Secret (keys `BANK_OWNER_PASSWORD`,
   `BANK_READER_PASSWORD`, `EMAIL_OWNER_PASSWORD`, `EMAIL_READER_PASSWORD`) and the Postgres
   roles are owned by the Lab Data Store app, not by this repository. Register the `bank` and
   `email` sources in `lab-gitops` `apps/lab-data-store/sources.yaml` so it creates the roles
   and Secret keys, and wait for it before deploying these workloads.
3. **Gateway secrets.** Add the two shared secrets to the existing `gateway-secrets` Secret
   (the servers and the gateway read the same keys), then restart the gateway:
   ```bash
   kubectl -n lab patch secret gateway-secrets --type merge -p "{\"stringData\":{\"BANK_MCP_SECRET\":\"$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\",\"EMAIL_MCP_SECRET\":\"$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\"}}"
   kubectl -n lab rollout restart deploy/gateway
   ```
   (A fresh install can instead pass both as `--from-literal` when creating `gateway-secrets`.)
4. **Bank rules and CSV mappings.** Copy `servers/bank-mcp/rules.example.yaml` and
   `csv-mappings.example.yaml`, edit them for your accounts and categories, and load them as a
   ConfigMap (kept out of git because they describe your accounts):
   ```bash
   kubectl -n lab create configmap bank-rules --from-file=rules.yaml=rules.yaml --from-file=csv-mappings.yaml=csv-mappings.yaml
   ```
   Re-create it (`kubectl ... create ... --dry-run=client -o yaml | kubectl apply -f -`) after
   editing; the next importer run picks it up.
5. **Mailbox.** Host and user are a ConfigMap, the app password a Secret written on the primary
   node so it never travels through chat or git:
   ```bash
   kubectl -n lab create configmap email-sync-config --from-literal=EMAIL_IMAP_HOST=<imap-host> --from-literal=EMAIL_IMAP_USER=<mailbox-user>
   uv run --project servers/email-mcp pdl-email-set-password --out imap.password
   kubectl -n lab create secret generic email-imap --from-file=imap.password=imap.password
   ```
   The `email-sync` CronJob ships with `suspend: true`; once both objects exist, set it to `false`
   in lab-gitops (or `kubectl -n lab patch cronjob email-sync -p '{"spec":{"suspend":false}}'`
   until the next sync of the manifests).
   ```bash
   shred -u imap.password
   ```
6. **Statement inbox.** The importer reads `inbox` and moves handled files to `processed` under
   the Lab Data Store files directory on the primary node. Like the gateway's data directory, it
   must belong to uid 10001 because the pod runs non-root and writes there:
   ```bash
   sudo mkdir -p /data/lab/lab-data-store/files/bank/{inbox,processed}
   sudo chown -R 10001:10001 /data/lab/lab-data-store/files/bank
   ```
   Drop statements (CSV or OFX) into `inbox` with `scp`; the next run (at most 15 minutes)
   imports them. A statement that cannot be imported is moved to `inbox/failed/` (the job
   logs a warning and exits non-zero once); fix the CSV mapping, then move the file back into
   `inbox`.
7. **Manifests.** Copy `base/` to `lab-gitops/apps/private-data-layer/`, commit, push.
   `bank-mcp` and `email-mcp` stay in `CreateContainerConfigError` until `lab-data-store` and
   the new `gateway-secrets` keys exist (the gateway Deployment also blocks until the
   `BANK_MCP_SECRET` and `EMAIL_MCP_SECRET` keys are in `gateway-secrets`); the CronJobs until `bank-rules`, `email-sync-config`
   and `email-imap` exist.
8. **Watch a job.** Run one now instead of waiting for the schedule and read its log:
   ```bash
   kubectl -n lab create job --from=cronjob/bank-importer bank-import-now
   kubectl -n lab logs -f job/bank-import-now
   kubectl -n lab create job --from=cronjob/email-sync email-sync-now
   kubectl -n lab logs -f job/email-sync-now
   kubectl -n lab delete job bank-import-now email-sync-now
   ```
9. **Verify.** List tools through the gateway as the `personal-assistant` client: the seven
   tools `bank_spend_summary`, `bank_search_transactions`, `bank_list_accounts`,
   `bank_recurring_charges`, `email_search_email`, `email_get_email` and `email_list_recent`
   appear. A token for an interactive client (Claude Code via `claude mcp add`) lists none of
   them, and calling one answers "unknown tool".

## Search

`search-mcp` gives the `personal-assistant` grant web search and page fetching (`search_web_search`,
`search_fetch_page`) through the gateway; `interactive-default` does not see them. It queries
SearXNG (next section) and its image is built like the others.

1. **Image.** Built on the primary node; the controller pins the sha in `kustomization.yaml`
   (placeholder tag `latest`):
   ```bash
   docker build -t registry.lab:5000/lab/pdl-search-mcp:<git-sha> servers/search-mcp/ && docker push registry.lab:5000/lab/pdl-search-mcp:<git-sha>
   ```
2. **Gateway secret.** Add the shared secret to `gateway-secrets` (the server and the gateway
   read the same key), then restart the gateway:
   ```bash
   kubectl -n lab patch secret gateway-secrets --type merge -p "{\"stringData\":{\"SEARCH_MCP_SECRET\":\"$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\"}}"
   kubectl -n lab rollout restart deploy/gateway
   ```
3. **Egress policy.** `search-mcp-egress` limits the pod to cluster DNS, SearXNG on 8080 and public
   addresses on 80/443 (private, link-local, loopback and CGNAT ranges are excluded). It backs up
   the fetcher's own URL checks, which cannot see a DNS name that re-resolves to a private address
   between check and connect. `searxng-egress` does the same for SearXNG. Both need the NetworkPolicy
   controller that k3s ships.
4. **Verify.** As the `personal-assistant` client `search_web_search` returns results; a token for
   an interactive client (Claude Code) lists no `search_*` tool and a call answers "unknown tool".
   `search_fetch_page` with `http://gateway.lab/` is refused.

## Texts

`sms-store` is the write endpoint for the phone's SMS forwarder app, published at `https://sms.lab`
(only `POST /messages` and `/healthz`); `sms-mcp` gives the `personal-assistant` grant read-only
tools (`sms_*`) through the gateway, `interactive-default` does not see them. Both use the Lab Data
Store (roles `sms_owner` for the store, `sms_reader` for the MCP server; Secret `lab-data-store`
keys `SMS_OWNER_PASSWORD` / `SMS_READER_PASSWORD`).

1. **Lab Data Store registration.** The `sms` source (schema, the two roles and their passwords)
   is registered through the ingest-api sources list of the Lab Data Store; this is done for this
   cluster. A new cluster needs the same registration before the pods can start.
2. **Images.** Built on the primary node; the controller pins the sha in `kustomization.yaml`
   (placeholder tag `latest`):
   ```bash
   for n in sms-store sms-mcp; do docker build -t registry.lab:5000/lab/pdl-$n:<git-sha> servers/$n/ && docker push registry.lab:5000/lab/pdl-$n:<git-sha>; done
   ```
3. **Gateway secret.** Add the shared secret to `gateway-secrets` (server and gateway read the same
   key). The gateway reads it at start, so it is restarted after the manifests are applied (step 7):
   ```bash
   kubectl -n lab patch secret gateway-secrets --type merge -p "{\"stringData\":{\"SMS_MCP_SECRET\":\"$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\"}}"
   ```
4. **Device token.** Steps 4, 6 and 8 handle secrets: run them on the primary node in one private
   temporary directory, so nothing lands in the repository, and clean it up at the end of step 8.
   The command writes the token to `pixel.token` and prints the hashed `tokens.yaml` entry on stderr;
   put that single line under `devices:` in `tokens.yaml`, then create the Secret:
   ```bash
   REPO=~/private-data-layer   # your checkout of this repository
   umask 077 && cd "$(mktemp -d)" && pwd   # stay in this directory until the end of step 8
   uv run --project "$REPO/servers/sms-store" pdl-sms-new-device-token --name pixel --out pixel.token
   # the hashed entry is printed on stderr as "  pixel: <hash>"; paste that line as-is under "devices:" in tokens.yaml
   $EDITOR tokens.yaml
   kubectl -n lab create secret generic sms-device-tokens --from-file=tokens.yaml=tokens.yaml
   shred -u tokens.yaml
   ```
   `sms-store` stays in `ContainerCreating` until this Secret exists. Revoke a phone by deleting
   its line, re-creating the Secret and restarting `deploy/sms-store`.
5. **Network.** The Ingress publishes `sms.lab` with a certificate from the `lab-ca` issuer (Secret
   `sms-lab-tls`, issued automatically); `sms-store-from-traefik` admits only the Traefik pods
   (kube-system, `app.kubernetes.io/name: traefik`) on 8000. If the k3s controller fails to match
   that cross-namespace selector, drop the `podSelector` so the namespace selector stands alone
   (the same fallback as kube-dns in the search egress policy).
6. **Pairing URI.** The app is paired with
   `pdl-sms://pair?url=<url>&token=<token>&fp=<hex>&ca=<base64url>` (about 1.9 KB, so paste it into
   the app; it does not fit a QR code). `ca` is the lab CA certificate (DER, base64url without
   padding), `fp` its SHA-256 (the app refuses the link unless they match), `token` the contents of
   `pixel.token`, percent-encoded. Build the link into a local file and do not print it; run this in
   the directory of step 4, with `lab-ca.pem` copied there:
   ```bash
   ca=$(openssl x509 -in lab-ca.pem -outform DER | base64 -w0 | tr '+/' '-_' | tr -d '=')
   fp=$(openssl x509 -in lab-ca.pem -outform DER | sha256sum | cut -d' ' -f1)
   token=$(python -c "import urllib.parse; print(urllib.parse.quote(open('pixel.token').read().strip(), safe=''))")
   printf 'pdl-sms://pair?url=%s&token=%s&fp=%s&ca=%s\n' 'https%3A%2F%2Fsms.lab' "$token" "$fp" "$ca" > pair-uri.txt
   ```
   Get `pair-uri.txt` to the phone over a private channel (for example a copy to the phone's
   clipboard or a direct file transfer) and paste it into the app; the confirmation dialog shows the
   host and the first 16 characters of `fp`.
7. **Manifests.** Copy `base/` to `lab-gitops/apps/private-data-layer/`, commit, push. Once the
   pods are applied, restart the gateway so it picks up the new backend and `SMS_MCP_SECRET`:
   ```bash
   kubectl -n lab rollout restart deploy/gateway
   ```
8. **Verify** (same directory; the token files are removed at the end):
   ```bash
   # a synthetic message over the lab CA (body format: servers/sms-store/README.md)
   printf 'Authorization: Bearer %s\n' "$(cat pixel.token)" > auth.hdr
   curl --cacert lab-ca.pem -H @auth.hdr -H 'Content-Type: application/json' \
        -d '[{"provider_id":"test-1","date_ms":1767225600000,"address":"+10000000000","direction":"in","body":"synthetic test"}]' \
        https://sms.lab/messages
   curl --cacert lab-ca.pem https://sms.lab/healthz
   kubectl -n lab logs deploy/sms-store
   # done with the secrets: remove them, then the directory
   shred -u pixel.token pair-uri.txt auth.hdr && cd .. && rm -rf "$OLDPWD"
   ```
   The synthetic message stays in the store, so it shows up in `sms_*` results. Remove it as
   `sms_owner` (the password is `SMS_OWNER_PASSWORD` in Secret `lab-data-store`):
   ```sql
   DELETE FROM sms.messages WHERE provider_id = 'test-1';
   ```
   As the `personal-assistant` client the gateway tool list now includes the `sms_*` tools; a token
   for an interactive client lists none of them. Anything other than `/messages` and `/healthz` on
   `sms.lab` answers 404.

## SearXNG and ntfy

Both live in their own folders (`searxng/`, `ntfy/`; in `lab-gitops` as `apps/searxng` and
`apps/ntfy`), pinned by digest in their kustomizations.

1. **SearXNG.** Cluster-internal only (`searxng:8080`). Create its secret key, then apply the folder:
   ```bash
   kubectl -n lab create secret generic searxng --from-literal=secret_key="$(openssl rand -hex 32)"
   ```
   `settings.yml` is mounted by subPath and the root filesystem stays writable because the image
   writes `/etc/searxng/uwsgi.ini` at start. The image normally starts as root and drops
   privileges itself, which cannot work with all capabilities dropped, so the pod runs directly as
   the image's own user (uid/gid 977, non-root). Without egress to the search engines (or with the policy blocking them) searches
   return empty result sets, so test with a query after the first start.
2. **ntfy.** Message cache and user database live on the primary node and must belong to uid 1000
   (the `ntfy` user in the image):
   ```bash
   sudo mkdir -p /data/lab/ntfy/{cache,lib}
   sudo chown -R 1000:1000 /data/lab/ntfy
   ```
   The Ingress publishes `https://ntfy.lab` with a certificate from the `lab-ca` issuer (Secret
   `ntfy-lab-tls`, issued automatically). The phone needs the lab CA installed; `http://` to the
   Service is the fallback if the ntfy app cannot use the user CA store.
3. **Users and publish token.** Created out of band after the pod is up; see the "Briefing"
   subsection under "Assistant" for the user, the access rule and the `ntfy-publish` Secret.

## Notes

- The gateway pod stays in `ContainerCreating` until `gateway-secrets` exists;
  `homestudio-mcp` until `homestudio-mcp-config` exists too. Bank and email need more: see
  "Bank and email".
- `homestudio-mcp`, `bank-mcp`, `email-mcp`, `search-mcp`, `sms-store` and `sms-mcp` are amd64-only today, hence the same node pin as the gateway.
- Scopes, grants and backends are in the ConfigMap's `gateway.yaml`; changing a grant takes
  effect for existing tokens on their next use (the gateway narrows live tokens to the current
  grant) but a restart is needed to reload the file.
