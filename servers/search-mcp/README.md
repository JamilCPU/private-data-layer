# pdl-search-mcp

Web search and page fetch MCP backend for the private data layer.

- Search goes through a self-hosted SearXNG instance (`SearxClient` in `pdl_search.searx`).
- Page fetch and text extraction arrive in a later task (trafilatura).
- The MCP server entry point (`pdl-search-mcp`) arrives in a later task.

Run tests: `uv sync && uv run pytest -q`
