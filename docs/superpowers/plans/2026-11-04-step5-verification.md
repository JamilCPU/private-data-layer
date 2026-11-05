# Step 5 verification: sms-store, the phone forwarder and sms-mcp (2026-11-04)

Branch `feat/step5-sms`, PR #17 (merged); policy follow-up PR #18 and lab-gitops #20, #21. Plan:
`2026-11-04-step5-sms.md`. App release: `sms-forwarder-v0.1.1` (debug APK built on the primary
node from the final branch head).

## Unit and integration tests

sms-store 125 tests (92%, Postgres cases green in CI), sms-mcp 44 (+15 Postgres cases in CI),
the Android app 69 JVM unit tests plus `assembleDebug` on the primary node, gateway 231; CI green
including the new `sms-forwarder` job.

## Cluster

Deployed through lab-gitops (`apps/private-data-layer`): `sms-store` (Ingress `sms.lab` with a
cert-manager certificate, only `/messages` and `/healthz`), `sms-mcp`, the gateway's `sms` backend.
Prerequisites: the Lab Data Store `sms` source (schema, `sms_owner`/`sms_reader`, Secret keys;
lab-gitops #19), `SMS_MCP_SECRET` in `gateway-secrets`, Secret `sms-device-tokens` with the
`pixel` token, the pairing URI (with the lab CA embedded) kept in the local secrets folder.

Found and fixed on the way: the NetworkPolicy admitting Traefik by namespace + pod selector did
not match under the k3s policy controller (502 from `sms.lab`); the namespace selector alone works
and a connection from another `lab` pod is still refused.

Observed over TLS from the workstation:

- `POST https://sms.lab/messages` with the device token: `{"accepted": 1, "duplicates": 0}`; the
  same batch again: `{"accepted": 0, "duplicates": 1}`; a well-formed wrong token: 401; `GET /`:
  404; `GET /healthz`: 200.
- Through `https://gateway.lab` the headless client lists `sms_search_sms` and `sms_list_recent_sms`;
  `sms_search_sms` finds the synthetic text by a word from its body within seconds of the post, and
  `sms_list_recent_sms` returns it newest first. The synthetic row was deleted afterwards.
- The interactive grant is unchanged (`homestudio:read automations:read`), so interactive clients do
  not see the sms tools (the gateway's scope filtering was verified in steps 1, 3 and 4).

## Real phone (needs the owner)

Install the release APK on the Pixel, paste the pairing URI from the local secrets folder, confirm
the pairing, grant the three runtime permissions, then send a text to the phone: it should be
visible through `sms_list_recent_sms` (or the assistant) within a minute (the receiver triggers a
sweep 5 seconds after the broadcast; a 15-minute periodic sweep catches anything missed).
