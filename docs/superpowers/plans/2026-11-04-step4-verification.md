# Step 4 verification: search-mcp, SearXNG, ntfy and the briefing (2026-11-04)

Branch `feat/step4-search-briefing`, PR #14 (merged), DNS egress follow-up PR #15. Plan:
`2026-11-04-step4-search-briefing.md`. lab-gitops PRs #17 and #18.

## Synthetic end to end on the dev workstation

`scripts/e2e/step4_check.py` against a local gateway with the `search` backend and `search-mcp`
pointed at a local stub of SearXNG's JSON API: the headless `personal-assistant` client lists
`search_web_search` and `search_fetch_page`; a search returns the stubbed hits with the query and
recency echoed; `fetch_page` refuses the stub's own loopback address and `http://gateway.lab/`;
a blank query is a tool error; an interactive client (DCR + PKCE + passphrase) lists neither tool
and gets `unknown tool`.

## Cluster

Deployed through lab-gitops (`apps/searxng`, `apps/ntfy`, `apps/private-data-layer`) with images
built on the primary node. Out-of-band objects: `SEARCH_MCP_SECRET` added to `gateway-secrets`,
Secret `searxng` (secret key), ConfigMap `briefing-config`, the ntfy data directories owned by
uid 1000, the ntfy `owner` user (password kept in a 0600 file on the primary node, never in chat),
the assistant's publish token in Secret `ntfy-publish`.

Found and fixed on the way: under the k3s policy controller the egress rule that named kube-dns
through a cross-namespace pod selector did not match, so neither search-mcp nor SearXNG could
resolve names; the rule now names the cluster DNS Service address and the pod range (PR #15,
lab-gitops #18). The SearXNG image starts as root and drops privileges in its entrypoint, which
cannot work with all capabilities dropped; it runs correctly as its own uid 977, so the
Deployment sets that.

Observed through `https://gateway.lab` over TLS:

- `search-mcp`, `searxng`, `ntfy` and the restarted `gateway` pods Running; CronJob `briefing`
  registered (07:30 America/Chicago); Ingress `ntfy.lab` with a cert-manager certificate.
- Headless client: 22 tools including the two search tools; `search_web_search("k3s release
  notes", recency="month", limit=3)` returns three live results from SearXNG;
  `search_fetch_page("https://docs.k3s.io/")` returns the page's readable text;
  `search_fetch_page` of `http://gateway.lab/` and of an in-cluster service name is refused;
  from inside the pod a LAN address is unreachable while public hosts are.
- Interactive client: neither search tool is listed.
- A one-off job running `pdl-assistant-briefing --test-notification` posted to ntfy (HTTP 200);
  polling the `briefing` topic as the owner over TLS shows the message; anonymous access is 403.

## Pending

- The real briefing needs the model (the workstation's GPU window): run
  `kubectl -n lab create job --from=cronjob/briefing briefing-now` and check the phone and the
  `briefings` row. One `skipped` row already exists from a job started outside the retry window.
- Phone: install the ntfy app, add server `https://ntfy.lab` with the lab CA imported in the app's
  certificate settings, log in as `owner`, subscribe to `briefing`.
