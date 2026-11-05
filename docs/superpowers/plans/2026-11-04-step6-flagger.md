# Step 6: the flagger — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An hourly job that runs a small set of named, threshold-based checks over the owner's data through the gateway and sends one phone notification when a check trips, never repeating a flag.

**Architecture:** A fourth entry point of the existing `assistant/` package (`pdl-assistant-flagger`), run by a k8s CronJob. Checks are declared in a YAML file (out-of-band ConfigMap) and are fully deterministic: each one calls read tools through the gateway as the `personal-assistant` client, compares structured results against its threshold with `Decimal` arithmetic, and yields `Flag(check, key, summary)` values. Flags are stored in the assistant's SQLite memory with a unique `(check, key)`, so a flag is recorded once; every run sends one ntfy notification listing the flags not yet notified and marks them. The morning briefing's Flags section lists the flags raised since the last briefing. No model call is involved anywhere in the flagger, so tool content never passes through a prompt.

**Tech Stack:** Python 3.12, the existing `pdl_assistant` modules (`GatewayClient`, `MemoryStore`, `Notifier`, `AssistantConfig`), PyYAML, `decimal`, pytest + pytest-asyncio, ruff, uv; k8s CronJob.

**Spec:** `docs/superpowers/specs/2026-11-02-private-data-layer-design.md` section 5.3 (Flagger, Delivery, Memory, offline rule), 7 step 6, 8, 10.

## Global Constraints

- Three check types, exactly: `transaction_over` ("any transaction > $X"), `recurring_change` ("recurring charge changed by > N%"), `email_from` ("email from a sender in `watch_senders`"). No free-form anomaly detection. Thresholds live in config, never in code.
- The flagger speaks only when a check trips: no notification for a run with nothing new. One notification per run at most, listing every not-yet-notified flag (at most `MAX_FLAG_LINES = 10` lines, then "and N more").
- No repeats: a flag is identified by `(check name, key)` (`key` = the transaction id or the email id); it is stored once and notified once. A failed notification leaves it un-notified, so the next run retries; nothing is lost and nothing repeats after a success.
- Money is `Decimal` parsed from the tools' string amounts; never `float`. Dates ISO 8601.
- The flagger makes no model call. **Deviation from the spec's offline rule, recorded:** the spec says the flagger skips silently when the brain is offline; because the checks need no model, the flagger runs regardless by default, and `flagger_require_brain: true` restores the spec's behaviour (probe Ollama first, skip silently when offline).
- A check that errors (gateway down, tool error, malformed result) is logged at WARNING and skipped for that run; the other checks still run. The job never wakes the workstation and never writes anywhere except its own SQLite memory.
- Tool content (descriptions, senders, subjects) appears in the notification as plain text, each line capped at `MAX_LINE_CHARS = 160`; the ntfy token is never logged.
- Public repo: the committed example config holds no real senders or amounts beyond obvious defaults; the cluster's config is an out-of-band ConfigMap. No hostnames beyond `*.lab`/service names in `deploy/`.
- Frozen dataclasses, type hints, functions ≤ 50 lines, files ≤ 400, logging not print, TDD, coverage gate from `pyproject`, ruff clean, conventional commits.

## Review Focus

1. The done criterion: a new over-threshold transaction yields exactly one notification; running the job again (same data) sends nothing; a second new transaction later yields one more notification mentioning only the new one. Test in Task 3.
2. The notifier fails (ntfy down): the flag stays un-notified and the next successful run sends it once, together with anything newer. Test in Task 3.
3. A statement imported late: a transaction dated 30 days ago that first appears today is still flagged (the window is `flagger_lookback_days`, dedupe is by key, not by date). Test in Task 2.
4. Threshold edges: an amount exactly equal to the threshold does not trip (`>` not `>=`); a recurring change of exactly N% does not trip; income is ignored when `direction: spend`; a zero or negative threshold, or an empty `senders` list, is a config error. Tests in Tasks 1 and 2.
5. A tool returns an error or a malformed payload (no `structuredContent`, an amount that is not a number): that check is skipped with a WARNING, the others run, and nothing is flagged from the bad payload. Test in Task 2.

---

## File structure

```
assistant/flagger.example.yaml                       # the three starter checks
assistant/src/pdl_assistant/flagger_config.py        # YAML → frozen check configs, validation
assistant/src/pdl_assistant/flagger_checks.py        # the three checks over gateway tools → Flag values
assistant/src/pdl_assistant/flagger.py               # run_flagger: evaluate, record, notify once
assistant/src/pdl_assistant/cli_flagger.py           # pdl-assistant-flagger
assistant/src/pdl_assistant/{config,memory,briefing_sections,cli_briefing}.py   # modified
assistant/tests/{test_flagger_config,test_flagger_checks,test_flagger,test_cli_flagger}.py (+ memory/briefing tests)
deploy/k8s/base/flagger-cronjob.yaml, kustomization.yaml, deploy/k8s/README.md, assistant/README.md, README.md
docs/superpowers/plans/2026-11-04-step6-verification.md
```

---

### Task 1: Flagger config and flag memory

**Files:**
- Create: `assistant/src/pdl_assistant/flagger_config.py`, `assistant/flagger.example.yaml`, `assistant/tests/test_flagger_config.py`
- Modify: `assistant/src/pdl_assistant/config.py` (fields `flagger_config_file: str | None = None`, `flagger_lookback_days: int = 45`, `flagger_require_brain: bool = False`; env `ASSISTANT_FLAGGER_CONFIG_FILE`, `ASSISTANT_FLAGGER_LOOKBACK_DAYS`, `ASSISTANT_FLAGGER_REQUIRE_BRAIN`; `flagger_lookback_days` must be 1..365), `assistant/src/pdl_assistant/memory.py`, `assistant/tests/{test_config,test_memory}.py`, `assistant/assistant.example.yaml`

**Interfaces:**
- Produces (`flagger_config.py`): frozen `TransactionOver(name: str, amount: Decimal, direction: Literal["spend", "any"] = "spend")`, `RecurringChange(name: str, percent: Decimal)`, `EmailFrom(name: str, senders: tuple[str, ...])`; `Check = TransactionOver | RecurringChange | EmailFrom`; `class FlaggerConfigError(ValueError)`; `load_checks(path: Path) -> tuple[Check, ...]`. YAML shape:
  ```yaml
  checks:
    - {name: large-transaction, type: transaction_over, amount: "500"}
    - {name: recurring-change, type: recurring_change, percent: "20"}
    - {name: watched-sender, type: email_from, senders: ["billing@example.com"]}
  ```
  Rules: `checks` is a non-empty list; `name` matches `[a-z0-9-]{1,40}` and is unique; unknown `type` or unknown keys → error naming the check; `amount` and `percent` parse with `Decimal(str(value))`, must be finite and `> 0` (`percent` ≤ 1000); `direction` only `spend`/`any`; `senders` a non-empty list of non-blank strings, lower-cased, at most 50; at most 20 checks. Every error is `FlaggerConfigError` with the file name.
- Produces (`memory.py`): table `flags(id INTEGER PRIMARY KEY AUTOINCREMENT, check_name TEXT NOT NULL, key TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT, UNIQUE(check_name, key))` (`CREATE TABLE IF NOT EXISTS`); frozen `StoredFlag(id: int, check_name: str, key: str, summary: str, created_at: str, notified_at: str | None)`; `add_flag(check_name: str, key: str, summary: str, created_at: str) -> bool` (True only when newly inserted; `INSERT OR IGNORE`); `unnotified_flags() -> list[StoredFlag]` (oldest first); `mark_flags_notified(ids: Sequence[int], at: str) -> None`; `flags_since(since: datetime) -> list[StoredFlag]` (`created_at >= since`, oldest first; stored timestamps are aware UTC ISO strings).

- [ ] **Step 1: Write the failing tests** — config: the example file loads to the three checks with `Decimal("500")`, `Decimal("20")`; each rule above has a rejecting case (parametrised: zero/negative/NaN amount, percent 0 and 1001, empty senders, duplicate names, bad name, unknown type, unknown key, empty `checks`, 21 checks, not a mapping); senders are lower-cased. Memory: `add_flag` True then False for the same `(check, key)`, a different key is new; `unnotified_flags` order; `mark_flags_notified` removes them from the un-notified list and sets `notified_at`; `flags_since` boundary; an existing database without the table gains it on open. `AssistantConfig`: defaults, env overrides, `flagger_lookback_days` bounds.
- [ ] **Step 2: Run, expect failure** (`uv run pytest tests/test_flagger_config.py tests/test_memory.py tests/test_config.py -q --no-cov`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite and ruff, expect pass.**
- [ ] **Step 5: Commit** `feat(assistant): flagger check config and flag memory`.

### Task 2: The three checks

**Files:**
- Create: `assistant/src/pdl_assistant/flagger_checks.py`, `assistant/tests/test_flagger_checks.py`

**Interfaces:**
- Consumes: `Check` types from Task 1; `GatewayClient.call_tool(name, arguments) -> ToolResult(text, is_error, structured)`; list-returning tools put their rows under `structured["result"]`.
- Produces: frozen `Flag(check: str, key: str, summary: str)`; `class CheckError(Exception)`; `async def evaluate(check: Check, gateway, *, today: date, lookback_days: int) -> list[Flag]` (raises `CheckError` for a tool error or malformed payload); `async def evaluate_all(checks, gateway, *, today, lookback_days) -> tuple[list[Flag], list[str]]` (flags from every check that worked, plus the names of checks that failed, each logged at WARNING); constants `MAX_LINE_CHARS = 160`, `MAX_RECURRING = 25`.
- Rules per type:
  - `transaction_over`: `bank_search_transactions(since=<today - lookback_days>, max_amount=<-amount>, limit=200)` and, when `direction == "any"`, also `min_amount=<amount>`; a row trips when `abs(Decimal(row["amount"])) > amount` (re-checked client-side; spend means `amount < 0`). Key = the row's `id` when present, else `sha256(posted_on|amount|description|account)[:32]`. Summary: `"<name>: <amount> <description> on <posted_on> (<account>)"`.
  - `recurring_change`: `bank_recurring_charges()` (first `MAX_RECURRING` rows); for each `{description, amount, last_on}`: `bank_search_transactions(query=<description>, since=<last_on>, limit=20)`; a spending row dated after `last_on` trips when `abs(abs(row_amount) - amount) / amount * 100 > percent`. Key = the row id (same rule as above). Summary: `"<name>: <description> charged <row amount> on <posted_on>, usually <amount>"`.
  - `email_from`: per sender, `email_search_email(query=<sender>, since=<today - lookback_days as ISO datetime>, limit=20)`; a row trips when the sender string occurs (case-insensitively) in the row's `from`. Key = the row's `id`. Summary: `"<name>: email from <from> — <subject> (<date>)"`.
  - Every summary is collapsed to one line and cut to `MAX_LINE_CHARS`.
- **Also in this task:** `bank_search_transactions` rows currently carry `posted_on, amount, description, category, account` and no `id`; add `"id"` to that row shape in `servers/bank-mcp/src/pdl_bank/server.py` (with its test and README table) so flags key on the stable transaction id.

- [ ] **Step 1: Write the failing tests** with a `FakeGateway` (canned `ToolResult`s per tool name and arguments, recording calls): each check's happy path with exact summaries and keys; Review Focus 3 (a row dated 30 days ago inside the window is flagged); Review Focus 4 (equal to the threshold does not trip; exactly N% does not trip; income ignored for `spend`, flagged for `any`); Review Focus 5 (`is_error`, `structured=None`, a non-numeric amount, a row missing `from` → `CheckError`; `evaluate_all` returns the other checks' flags and the failed check's name); the recurring check ignores rows dated on or before `last_on` and income rows, and stops at `MAX_RECURRING`; summaries are single-line and capped; the bank server returns `id` in search rows.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** (one small function per check type plus shared parsing helpers `_rows(result) -> list[dict]` and `_amount(row) -> Decimal`).
- [ ] **Step 4: Run both packages' suites and ruff, expect pass.**
- [ ] **Step 5: Commit** `feat(assistant): transaction, recurring-change and watched-sender checks`.

### Task 3: Runner, CLI and the briefing's Flags section

**Files:**
- Create: `assistant/src/pdl_assistant/flagger.py`, `assistant/src/pdl_assistant/cli_flagger.py`, `assistant/tests/{test_flagger,test_cli_flagger}.py`
- Modify: `assistant/pyproject.toml` (script `pdl-assistant-flagger = pdl_assistant.cli_flagger:main`), `assistant/src/pdl_assistant/briefing_sections.py`, `assistant/src/pdl_assistant/cli_briefing.py`, their tests, `assistant/README.md`

**Interfaces:**
- Consumes: Tasks 1 and 2; `Notifier.send(title, body)`, `NotifyError`; `OllamaClient.probe()`; `build_clients`/`base_parser` from `__main__`.
- Produces (`flagger.py`): frozen `FlaggerOutcome(new_flags: int, notified: int, failed_checks: tuple[str, ...], status: Literal["quiet", "notified", "notify_failed", "skipped_offline"])`; `async def run_flagger(checks, gateway, memory, notifier, *, now: Callable[[], datetime], lookback_days: int, probe: Callable[[], Awaitable[bool]] | None = None, dry_run: bool = False) -> FlaggerOutcome`: when `probe` is given and returns False → `skipped_offline` with no tool calls; `evaluate_all`; `add_flag` each (count the new ones); `pending = memory.unnotified_flags()`; none → `quiet`; else one `notifier.send(title, body)` with title `"Flagged: <summary of the single flag's check name>"` for one flag or `"Flagged: <n> items"`, body = up to `MAX_FLAG_LINES = 10` summaries one per line plus `"and <k> more"`; success → `mark_flags_notified` → `notified`; `NotifyError` → `notify_failed` (flags stay pending). `dry_run` logs what would be sent and writes nothing.
- Produces (`cli_flagger.py`): `main(argv) -> int` with `--config/--secrets` (as the other entry points) and `--dry-run`; requires `flagger_config_file` (unset → stderr `pdl-assistant-flagger: flagger_config_file is not set`, exit 1); `FlaggerConfigError`/`ConfigError` → exit 1; exit 0 for `quiet`, `notified`, `skipped_offline`; exit 1 for `notify_failed` or when every check failed; one INFO line `flagger <status> new=<n> notified=<m> failed_checks=<names>`; clients closed in `finally`.
- Briefing: `SectionRunner.__init__` gains `flag_lines: Callable[[datetime], list[str]] | None = None`; the `flags` section returns `"Flags: no checks configured yet."` when it is None, `"Flags: nothing new"` when it returns no lines, else `"Flags: "` followed by up to 5 summaries joined with `"; "` and `" (and <k> more)"`; no model call. `cli_briefing` passes a function over `memory.flags_since(since)` when `flagger_config_file` is set.

- [ ] **Step 1: Write the failing tests** — Review Focus 1 as one scenario test over a real `MemoryStore` (tmp file), a `FakeGateway` whose transaction list grows between runs and a recording notifier: run 1 → one notification with the first transaction; run 2 → none; a second transaction appears → run 3 → one notification mentioning only it; Review Focus 2 (notifier raising `NotifyError` then working: the second run sends both the old and the new flag once); the 10-line cap with "and N more"; `probe` False → `skipped_offline`, zero tool calls; `dry_run` writes nothing and sends nothing; a failed check appears in `failed_checks` while the others' flags are sent; CLI exit codes and the INFO line (caplog); briefing Flags section in its three states.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite and ruff, expect pass.**
- [ ] **Step 5: README** (assistant: "Flagger" — what it does, the three check types and the config file, no-repeat rule, `--dry-run`, the offline note with `flagger_require_brain`).
- [ ] **Step 6: Commit** `feat(assistant): hourly flagger with one notification per new flag, Flags in the briefing`.

### Task 4: Deploy

**Files:**
- Create: `deploy/k8s/base/flagger-cronjob.yaml`
- Modify: `deploy/k8s/base/kustomization.yaml`, `deploy/k8s/base/briefing-cronjob.yaml` (add `ASSISTANT_FLAGGER_CONFIG_FILE` and the config mount so the briefing can list flags), `deploy/k8s/README.md` ("Flagger" subsection), `README.md` (status: steps 1–6)

**Decisions:** CronJob `flagger` (labels `app: flagger`): `schedule: "5 * * * *"`, `concurrencyPolicy: Forbid`, history 1/3, `backoffLimit: 0`, `activeDeadlineSeconds: 300`, `restartPolicy: Never`; the pod spec is the briefing CronJob's (same image `registry.lab/lab/pdl-assistant`, node selector, `enableServiceLinks: false`, securityContext, `envFrom assistant-config`, env, secrets, `lab-ca`, `/data` hostPath, `/tmp`, the ntfy token mount and `ASSISTANT_NTFY_URL`/`ASSISTANT_NTFY_TOPIC`), plus `ASSISTANT_FLAGGER_CONFIG_FILE=/etc/pdl/flagger/flagger.yaml` from ConfigMap `flagger-config` (key `flagger.yaml`, out of band, created from `assistant/flagger.example.yaml`), command `["pdl-assistant-flagger"]`. README: create the ConfigMap (`kubectl -n lab create configmap flagger-config --from-file=flagger.yaml=assistant/flagger.example.yaml`), edit thresholds and senders there, run once with `kubectl -n lab create job --from=cronjob/flagger flagger-now`, where flags are kept, how to stop it (`suspend`).

- [ ] **Step 1: Write the manifest and docs; `kubectl kustomize deploy/k8s/base`; PyYAML parse.**
- [ ] **Step 2: Commit** `feat(deploy): hourly flagger CronJob`.

### Task 5: Verification

- [ ] Cluster: image built on the primary node, lab-gitops PR, ConfigMap `flagger-config` created. Run the job once with no matching data → `flagger quiet`, no notification. Seed one synthetic over-threshold transaction in the `bank` schema → run → exactly one new message in the ntfy topic naming it; run again → no new message. Remove the synthetic transaction and its flag row afterwards.
- [ ] Write `docs/superpowers/plans/2026-11-04-step6-verification.md`.

## Self-review notes
- Spec 5.3 Flagger (named checks, thresholds in config, speaks only when tripped, no free-form detection) → Tasks 1–3; Delivery (ntfy) → Task 3; Memory (flag history) → Task 1; "anything the flagger raised" in the briefing → Task 3; hourly schedule → Task 4; step 6 done criterion → Review Focus 1 and Task 5. The offline rule is the one recorded deviation (Global Constraints).
- Names consistent: `TransactionOver`, `RecurringChange`, `EmailFrom`, `load_checks`, `FlaggerConfigError`, `StoredFlag`, `add_flag`, `unnotified_flags`, `mark_flags_notified`, `flags_since`, `Flag`, `CheckError`, `evaluate`, `evaluate_all`, `run_flagger`, `FlaggerOutcome`.
- Execution order: Task 1 with Task 4 in parallel (different trees), then 2, then 3, then 5. Reviewers: opus for Tasks 2 and 3; sonnet for 1 and 4.
