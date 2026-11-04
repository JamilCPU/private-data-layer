import httpx
import pytest
import respx

from pdl_search.searx import SearchHit, SearxClient, SearxError

from .conftest import BASE_URL

SEARCH_URL = f"{BASE_URL}/search"


def _result(n: int, **kw) -> dict:
    base = {
        "title": f"Title {n}",
        "url": f"https://example.com/{n}",
        "content": f"content {n}",
        "engine": "duckduckgo",
    }
    base.update(kw)
    return base


@respx.mock
async def test_search_maps_results(client: SearxClient):
    long = "word  \n  " * 100
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _result(1, content=long, publishedDate="2026-10-01T00:00:00"),
                    _result(2),
                    _result(3),
                ]
            },
        )
    )
    hits = await client.search("python asyncio", recency="week", limit=2)

    params = route.calls.last.request.url.params
    assert params["q"] == "python asyncio"
    assert params["format"] == "json"
    assert params["time_range"] == "week"
    assert params["categories"] == "general"
    assert params["safesearch"] == "1"
    assert "language" not in params
    assert len(hits) == 2
    assert isinstance(hits[0], SearchHit)
    assert hits[0].title == "Title 1"
    assert hits[0].url == "https://example.com/1"
    assert hits[0].engine == "duckduckgo"
    assert hits[0].published == "2026-10-01T00:00:00"
    assert "\n" not in hits[0].snippet and "  " not in hits[0].snippet
    assert len(hits[0].snippet) == 300
    assert hits[1].snippet == "content 2"
    assert hits[1].published is None


@respx.mock
async def test_search_caps_title(client: SearxClient):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"results": [_result(1, title="t" * 500)]})
    )
    hits = await client.search("q")
    assert len(hits[0].title) == 200


@respx.mock
async def test_search_without_recency_omits_time_range(client: SearxClient):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await client.search("q") == []
    assert "time_range" not in route.calls.last.request.url.params


async def test_search_rejects_bad_recency(client: SearxClient):
    with pytest.raises(ValueError):
        await client.search("q", recency="hour")


@respx.mock
async def test_search_backend_down(client: SearxClient):
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SearxError, match="searxng unreachable"):
        await client.search("q")


@respx.mock
async def test_search_non_json(client: SearxClient):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(SearxError, match="searxng returned non-JSON"):
        await client.search("q")


@respx.mock
async def test_search_rate_limited(client: SearxClient):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(SearxError, match="searxng rate limited"):
        await client.search("q")


@respx.mock
async def test_search_server_error_names_status(client: SearxClient):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(SearxError, match="500"):
        await client.search("q")


@respx.mock
@pytest.mark.parametrize("body", [{}, {"results": "x"}, ["a"]])
async def test_search_missing_results_list(client: SearxClient, body):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(SearxError, match="no results list"):
        await client.search("q")


@respx.mock
async def test_search_dedupes_urls(client: SearxClient):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"results": [_result(1), _result(1, title="dup"), _result(2)]}
        )
    )
    hits = await client.search("q")
    assert [h.url for h in hits] == ["https://example.com/1", "https://example.com/2"]
    assert hits[0].title == "Title 1"


@respx.mock
@pytest.mark.parametrize(("limit", "expected"), [(0, 1), (-5, 1), (50, 10)])
async def test_search_clamps_limit(client: SearxClient, limit: int, expected: int):
    results = [_result(n) for n in range(20)]
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": results}))
    assert len(await client.search("q", limit=limit)) == expected


@respx.mock
async def test_search_skips_malformed_items(client: SearxClient):
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"results": ["junk", {"title": "no url"}, _result(1)]}
        )
    )
    hits = await client.search("q")
    assert [h.url for h in hits] == ["https://example.com/1"]


async def test_injected_http_client_not_closed_by_aclose():
    http = httpx.AsyncClient()
    c = SearxClient(BASE_URL, http=http)
    await c.aclose()
    assert not http.is_closed
    await http.aclose()
