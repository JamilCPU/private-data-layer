Helper commands live in the gateway package; run them from `gateway/` with `uv run`.
`uv run pdl-new-client-secret` prints a new OAuth client secret once, plus its PBKDF2 hash to store in config; `uv run pdl-set-passphrase` prompts for the approval passphrase and prints its PBKDF2 hash.
Secrets never go in chat or git: copy printed secrets straight into your password manager or the deploy environment.

`trust-lab-ca.ps1` imports the lab CA into the Windows user trust store and sets `NODE_EXTRA_CA_CERTS` for Claude Code (run it once per workstation; `*.lab` name resolution is a separate, DNS-side step).

`e2e/step3_check.py` is the step 3 synthetic end-to-end check (bank and email tools through the gateway, scope filtering for an interactive client, audit log). Run it from `gateway/` against a local gateway that has the bank and email backends configured: `uv run python ../scripts/e2e/step3_check.py http://localhost:8787 <secrets_dir>`, where `<secrets_dir>` holds `personal-assistant.secret` (the headless client's secret) and `approval.passphrase` (the approval page passphrase). Both files stay outside the repository.
