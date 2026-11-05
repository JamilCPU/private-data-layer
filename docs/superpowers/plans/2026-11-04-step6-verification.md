# Step 6 verification: the flagger (2026-11-04)

Branch `feat/step6-flagger`, PR #20 (merged); lab-gitops #22 and #23. Plan:
`2026-11-04-step6-flagger.md` (see its "Amendments during execution").

## Unit and integration tests

assistant 399 tests (98.9%), bank-mcp 175 tests (92%, Postgres cases green in CI); ruff clean; CI
green on every job. One whole-branch review, one fix wave and a scoped re-review closed the branch.

## Cluster

Deployed through lab-gitops (`apps/private-data-layer`): the `flagger` CronJob (five past every
hour) with the assistant image, the briefing CronJob with the Flags switch, and new `pdl-assistant`
and `pdl-bank-mcp` images. Prerequisite: ConfigMap `flagger-config` created from
`assistant/flagger.example.yaml`.

Found and fixed on the way: a short git sha made only of digits and one `e` is YAML scientific
notation, so kustomize read the image tag as a number and refused to build. Tags like that must be
quoted in `kustomization.yaml`.

Observed with manual runs (`kubectl -n lab create job --from=cronjob/flagger <name>`), counting
messages in the ntfy topic before and after each run:

- **No matching data:** the log ends with `flagger quiet new=0 notified=0 failed_checks=-`; all
  three checks reached their tools through the gateway; the topic count did not change.
- **One synthetic transaction above the threshold** (inserted straight into the `bank` schema, in a
  synthetic account): `flagger notified new=1 notified=1 failed_checks=-`; exactly one new message
  in the topic, titled `Flagged: large-transaction`, with the amount, description, date and account
  name on one line.
- **Run again with the same data:** `flagger quiet new=0 notified=0`; no new message. The flag row
  in the assistant database carried the transaction id as its key and a `notified_at` timestamp.

Afterwards the synthetic account and transaction, the flag row and the test jobs were removed. The
bank tables are empty again until real statements are imported.

## Not covered here

- The notification arriving on the phone (the ntfy app is not set up on the Pixel yet; the server
  side is the same path the briefing test notification used in step 4).
- The `recurring_change` and `email_from` checks firing on real data: they ran against empty
  stores on the cluster. Their behaviour is pinned by the unit tests.
- The briefing's Flags section in a real briefing, which needs the model.
