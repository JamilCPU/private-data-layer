# Step 3 verification: bank-mcp and email-mcp (2026-11-04)

Branch `feat/step3-bank-email`, PR #11. Plan: `2026-11-04-step3-bank-email.md`.

## Synthetic end to end on the dev workstation (plain processes)

Stack: `bank-mcp` on `127.0.0.1:8801` and `email-mcp` on `127.0.0.1:8802`, both with
`*_STORE=postgres` against a throwaway Postgres 16 (a separate database, created for this run);
`pdl-gateway` on `http://127.0.0.1:8787` with backends `bank` and `email` only, the real
`secrets.yaml` (hashed passphrase and `personal-assistant` secret), scopes and grants as in
`gateway.example.yaml` (interactive grant = `homestudio:read automations:read`).

Data: the four bank fixtures (`sample.csv`, `sample_debit_credit.csv`, `sample.ofx`, `sample.qfx`)
dropped in an inbox and imported with the real importer (`pdl-bank-import --once` with
`BANK_APPLY_MIGRATIONS=1`): 4 files imported, 10 new transactions, 0 for the QFX copy of the OFX
rows (same ids), all four files moved to `processed/`; five synthetic emails written through the
Postgres store after `apply_migrations` (two from a "landlord" sender, one 5000-character body).

Checks (SDK client, `tools/call` through the gateway):

- Headless `personal-assistant` (client credentials): token scope holds all grant scopes; tools =
  `bank_list_accounts`, `bank_recurring_charges`, `bank_search_transactions`, `bank_spend_summary`,
  `email_get_email`, `email_list_recent`, `email_search_email`, `gateway_audit_search`.
- `bank_spend_summary(period="january 2026", group_by="category")` → since/until 2026-01-01..31,
  rows transport 135.00 (3), groceries, subscriptions; income excluded. `group_by="account"` keys
  are account names. `bank_search_transactions(query="grocer", limit=5)` → the grocer rows with
  category and account name. `bank_recurring_charges` → empty (one month of fixtures).
  `bank_spend_summary(period="next tuesday")` → tool error `unrecognised period`.
- `email_search_email(query="landlord")` → the two landlord messages (sender, date, subject,
  snippet; no body). `email_list_recent(limit=3)` newest first. `email_get_email(id)` → full text
  with `truncated: false`; unknown id → tool error.
- Interactive client (DCR + PKCE + passphrase approval, replaying the browser flow): token scope
  `homestudio:read automations:read`; `tools/list` = none of the bank/email tools;
  `bank_spend_summary` and `email_search_email` → `unknown tool: <name>`.
- `gateway_audit_search(since=5 min ago)` → 12 entries covering every bank and email call above.

Script: `scripts/e2e/step3_check.py` (adapted from the step 1 cluster check).

## Unit and integration tests

`bank-mcp` and `email-mcp` suites run with warnings as errors; the Postgres store tests run when
`TEST_PG_DSN` is set (CI provides a `postgres:16` service; locally the throwaway database was
used). Coverage above 97% in both packages with Postgres, above 91% without.

## Cluster (2026-11-04)

Deployed through lab-gitops (`apps/private-data-layer`, PR #12) with images built on the primary
node (`pdl-bank-mcp` and `pdl-email-mcp` at the merged sha). Out-of-band objects: the two backend
secrets added to `gateway-secrets`, ConfigMap `bank-rules` from the example YAMLs, and the Lab Data
Store's schemas, roles, Secret and files area (its own app). Observed:

- `bank-mcp`, `email-mcp` and the restarted `gateway` pods Running; CronJobs `bank-importer` and
  `email-sync` registered.
- `bank-importer` run (`kubectl create job --from=cronjob/bank-importer`): migrations applied once
  as `bank_owner` (`bank.schema_migrations` holds `001_bank.sql`), empty inbox → `imported=0`,
  job Complete. The email migrations were applied once by a one-off pod as `email_owner` (the sync
  CronJob waits for the owner's IMAP ConfigMap and Secret).
- Through `https://gateway.lab` over TLS: the headless client lists 20 tools (Home Studio,
  Automations, the 7 bank/email tools, audit); `bank_spend_summary`, `bank_list_accounts`,
  `bank_recurring_charges`, `email_list_recent` and `email_search_email` answer (empty, no errors)
  from the cluster database; the interactive client lists the 12 Home Studio/Automations tools
  only, and bank/email calls answer `unknown tool`; the audit log holds every call.

## Real data

Pending (needs the owner): statements in the inbox, the IMAP app password written with
`pdl-email-set-password` on the primary node, then the two spec questions through the assistant
once the model path is open.
