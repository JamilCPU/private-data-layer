# private-data-layer

the owner's private data layer and personal assistant: every personal data source behind a small
MCP server, one gateway that decides per agent which tools exist, and a local assistant as the
first agent on it.

Design (source of truth): `docs/superpowers/specs/2026-11-02-private-data-layer-design.md`.
Plans: `docs/superpowers/plans/`.

Layout (design section 6): `gateway/`, `assistant/`, `servers/{email,sms,bank,search}-mcp/`,
`sms-store/`, `pixel-sms-forwarder/`, `deploy/` (compose + k8s), `scripts/` (helpers).

This repo is private because its docs hold lab topology. It still never holds secrets: client
secrets, passphrases and tokens are created by the helper scripts in `scripts/` and live only
where they are used.
