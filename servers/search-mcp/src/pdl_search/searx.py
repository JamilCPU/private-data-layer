"""Async client for a self-hosted SearXNG instance (JSON API)."""

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RECENCY = ("day", "week", "month", "year")
MIN_LIMIT = 1
MAX_LIMIT = 10
SNIPPET_MAX = 300
HTTP_RATE_LIMITED = 429


class SearxError(Exception):
    """The SearXNG backend failed or returned something unusable."""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    engine: str
    published: str | None


def _snippet(content: object) -> str:
    text = " ".join(str(content or "").split())
    return text[:SNIPPET_MAX]


def _to_hit(item: object) -> SearchHit | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not url:
        return None
    published = item.get("publishedDate")
    return SearchHit(
        title=str(item.get("title") or ""),
        url=url,
        snippet=_snippet(item.get("content")),
        engine=str(item.get("engine") or ""),
        published=published if isinstance(published, str) and published else None,
    )


def _parse_hits(results: list, limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in results:
        hit = _to_hit(item)
        if hit is None or hit.url in seen:
            continue
        seen.add(hit.url)
        hits.append(hit)
        if len(hits) >= limit:
            break
    return hits


class SearxClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def search(
        self,
        query: str,
        *,
        recency: str | None = None,
        limit: int = 5,
        categories: str = "general",
    ) -> list[SearchHit]:
        if recency is not None and recency not in RECENCY:
            raise ValueError(f"recency must be one of {RECENCY}, got {recency!r}")
        limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))
        params = {"q": query, "format": "json", "categories": categories, "safesearch": "1"}
        if recency is not None:
            params["time_range"] = recency
        payload = await self._get_json(params)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise SearxError("searxng returned no results list")
        return _parse_hits(results, limit)

    async def _get_json(self, params: dict[str, str]) -> object:
        try:
            resp = await self._http.get(f"{self._base_url}/search", params=params)
        except httpx.HTTPError as exc:
            log.warning("searxng request failed: %s", exc)
            raise SearxError(f"searxng unreachable: {exc}") from exc
        if resp.status_code == HTTP_RATE_LIMITED:
            raise SearxError("searxng rate limited")
        if resp.status_code >= 400:
            raise SearxError(f"searxng returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SearxError("searxng returned non-JSON") from exc

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
