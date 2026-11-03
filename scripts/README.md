Helper commands live in the gateway package; run them from `gateway/` with `uv run`.
`uv run pdl-new-client-secret` prints a new OAuth client secret once, plus its PBKDF2 hash to store in config; `uv run pdl-set-passphrase` prompts for the approval passphrase and prints its PBKDF2 hash.
Secrets never go in chat or git: copy printed secrets straight into your password manager or the deploy environment.
