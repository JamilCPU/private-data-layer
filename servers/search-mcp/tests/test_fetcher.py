import asyncio

import httpx
import pytest
import respx

from pdl_search import fetcher
from pdl_search.fetcher import USER_AGENT, FetchError, Page, fetch_page
from pdl_search.url_guard import UrlRefused

HTML = (
    "<html><head><title> My &amp; Page </title><style>p{x:y}</style></head><body>"
    "<script>var secret = 1;</script><p>Hello   world.</p><p>Second paragraph.</p>"
    "</body></html>"
)
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}
TEXT_HEADERS = {"content-type": "text/plain"}


def _resolve(host: str) -> list[str]:
    return ["10.0.0.9"] if host == "private.example.org" else ["93.184.216.34"]


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


async def _fetch(http: httpx.AsyncClient, url: str = "https://example.com/p", **kw) -> Page:
    return await fetch_page(url, http=http, resolve=_resolve, **kw)


@respx.mock
async def test_html_page(http):
    route = respx.get("https://example.com/p").respond(200, text=HTML, headers=HTML_HEADERS)
    page = await _fetch(http)
    assert page.title == "My & Page"
    assert "Hello world." in page.text
    assert "secret" not in page.text and "p{x:y}" not in page.text
    assert page.truncated is False
    assert page.content_type == "text/html"
    assert page.final_url == page.url == "https://example.com/p"
    request = route.calls.last.request
    assert request.headers["user-agent"] == USER_AGENT
    assert "cookie" not in request.headers
    assert "text/html" in request.headers["accept"]


@respx.mock
async def test_fallback_when_trafilatura_returns_none(http, monkeypatch):
    monkeypatch.setattr(fetcher.trafilatura, "extract", lambda *_a, **_k: None)
    respx.get("https://example.com/p").respond(200, text=HTML, headers=HTML_HEADERS)
    page = await _fetch(http)
    assert "Hello world." in page.text
    assert "Second paragraph." in page.text
    assert "secret" not in page.text and "<p>" not in page.text


@respx.mock
async def test_fallback_when_trafilatura_raises(http, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("bad")

    monkeypatch.setattr(fetcher.trafilatura, "extract", boom)
    respx.get("https://example.com/p").respond(200, text=HTML, headers=HTML_HEADERS)
    assert "Hello world." in (await _fetch(http)).text


@respx.mock
async def test_html_without_title_uses_final_url(http):
    respx.get("https://example.com/p").respond(
        200, text="<p>just text here</p>", headers={"content-type": "application/xhtml+xml"}
    )
    page = await _fetch(http)
    assert page.title == "https://example.com/p"


@respx.mock
async def test_plain_text_passthrough(http):
    respx.get("https://example.com/p").respond(
        200, text="line one\n\n  line   two", headers=TEXT_HEADERS
    )
    page = await _fetch(http)
    assert page.text == "line one\nline two"
    assert page.title == "https://example.com/p"


@respx.mock
async def test_redirect_followed_and_final_url_reported(http):
    respx.get("https://example.com/a").respond(302, headers={"location": "/b"})
    respx.get("https://example.com/b").respond(200, text="done", headers=TEXT_HEADERS)
    page = await _fetch(http, "https://example.com/a")
    assert page.url == "https://example.com/a"
    assert page.final_url == "https://example.com/b"


@respx.mock
async def test_redirect_to_private_host_is_refused_before_request(http):
    respx.get("https://example.com/a").respond(
        302, headers={"location": "https://private.example.org/secret"}
    )
    private = respx.get("https://private.example.org/secret").respond(200, text="leak")
    with pytest.raises(UrlRefused):
        await _fetch(http, "https://example.com/a")
    assert private.called is False


@respx.mock
async def test_redirect_to_ip_literal_and_bad_scheme_refused(http):
    respx.get("https://example.com/a").respond(
        302, headers={"location": "http://169.254.169.254/latest"}
    )
    with pytest.raises(UrlRefused):
        await _fetch(http, "https://example.com/a")
    respx.get("https://example.com/b").respond(301, headers={"location": "file:///etc/passwd"})
    with pytest.raises(UrlRefused):
        await _fetch(http, "https://example.com/b")


@respx.mock
async def test_initial_private_url_never_requested(http):
    route = respx.get("https://private.example.org/").respond(200, text="x")
    with pytest.raises(UrlRefused):
        await _fetch(http, "https://private.example.org/")
    assert route.called is False


@respx.mock
async def test_too_many_redirects(http):
    respx.get(url__regex=r"https://example\.com/.*").respond(302, headers={"location": "/loop"})
    with pytest.raises(FetchError, match="too many redirects"):
        await _fetch(http, "https://example.com/loop", max_redirects=5)


@respx.mock
async def test_redirect_without_location(http):
    respx.get("https://example.com/p").respond(302)
    with pytest.raises(FetchError, match="Location"):
        await _fetch(http)


@respx.mock
async def test_big_body_is_truncated(http):
    body = ("word " * 600_000)[:2_000_001]
    respx.get("https://example.com/p").respond(200, content=body.encode(), headers=TEXT_HEADERS)
    page = await _fetch(http)
    assert page.truncated is True
    assert len(page.text) <= 8000


@respx.mock
async def test_body_exactly_max_bytes_not_truncated(http):
    respx.get("https://example.com/p").respond(200, content=b"x" * 100, headers=TEXT_HEADERS)
    page = await _fetch(http, max_bytes=100)
    assert page.truncated is False


@respx.mock
async def test_text_capped_at_max_chars(http):
    respx.get("https://example.com/p").respond(200, text="a" * 500, headers=TEXT_HEADERS)
    page = await _fetch(http, max_chars=100)
    assert len(page.text) == 100 and page.truncated is True


@respx.mock
async def test_unsupported_content_type(http):
    respx.get("https://example.com/p").respond(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}
    )
    with pytest.raises(FetchError, match="unsupported content type"):
        await _fetch(http)


@respx.mock
async def test_missing_content_type(http):
    respx.get("https://example.com/p").respond(200, content=b"zzz")
    with pytest.raises(FetchError, match="unsupported content type"):
        await _fetch(http)


@respx.mock
@pytest.mark.parametrize("status", [404, 500, 304])
async def test_http_errors(http, status):
    respx.get("https://example.com/p").respond(status)
    with pytest.raises(FetchError, match=f"HTTP {status}"):
        await _fetch(http)


@respx.mock
async def test_connection_error(http):
    respx.get("https://example.com/p").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(FetchError, match="request failed"):
        await _fetch(http)


@respx.mock
async def test_unknown_charset_falls_back_to_utf8(http):
    respx.get("https://example.com/p").respond(
        200, content=b"caf\xc3\xa9", headers={"content-type": "text/plain; charset=bogus-9"}
    )
    assert (await _fetch(http)).text == "café"


@respx.mock
async def test_cookies_not_persisted(http):
    respx.get("https://example.com/a").respond(
        200, text="hi", headers={**TEXT_HEADERS, "set-cookie": "sid=1; Path=/"}
    )
    route = respx.get("https://example.com/b").respond(200, text="hi", headers=TEXT_HEADERS)
    await _fetch(http, "https://example.com/a")
    await _fetch(http, "https://example.com/b")
    assert "cookie" not in route.calls.last.request.headers


@respx.mock
async def test_cookie_set_on_hop_one_not_sent_on_hop_two(http):
    respx.get("https://example.com/a").respond(
        302, headers={"location": "/b", "set-cookie": "sid=x; Path=/"}
    )
    second = respx.get("https://example.com/b").respond(200, text="ok", headers=TEXT_HEADERS)
    await _fetch(http, "https://example.com/a")
    assert "cookie" not in second.calls.last.request.headers


@respx.mock
async def test_preexisting_client_cookies_are_not_sent(http):
    http.cookies.set("sid", "mine", domain="example.com")
    route = respx.get("https://example.com/p").respond(200, text="ok", headers=TEXT_HEADERS)
    await _fetch(http)
    assert "cookie" not in route.calls.last.request.headers


@respx.mock
async def test_second_hop_carries_fixed_headers(http):
    respx.get("https://example.com/a").respond(302, headers={"location": "/b"})
    second = respx.get("https://example.com/b").respond(200, text="ok", headers=TEXT_HEADERS)
    await _fetch(http, "https://example.com/a")
    headers = second.calls.last.request.headers
    assert headers["user-agent"] == USER_AGENT
    assert "text/html" in headers["accept"]


@respx.mock
async def test_exactly_five_redirects_succeed(http):
    for n in range(5):
        respx.get(f"https://example.com/{n}").respond(302, headers={"location": f"/{n + 1}"})
    respx.get("https://example.com/5").respond(200, text="end", headers=TEXT_HEADERS)
    page = await _fetch(http, "https://example.com/0", max_redirects=5)
    assert page.final_url == "https://example.com/5"


@respx.mock
async def test_sixth_redirect_refused(http):
    for n in range(6):
        respx.get(f"https://example.com/{n}").respond(302, headers={"location": f"/{n + 1}"})
    final = respx.get("https://example.com/6").respond(200, text="end", headers=TEXT_HEADERS)
    with pytest.raises(FetchError, match="too many redirects"):
        await _fetch(http, "https://example.com/0", max_redirects=5)
    assert final.called is False


@respx.mock
async def test_overall_deadline(http):
    async def slow(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, text="late", headers=TEXT_HEADERS)

    respx.get("https://example.com/p").mock(side_effect=slow)
    with pytest.raises(FetchError, match="timed out"):
        await _fetch(http, timeout_s=0.05)


async def test_timeout_applied_to_each_request(http):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, text="ok", headers=TEXT_HEADERS)

    with respx.mock:
        respx.get("https://example.com/p").mock(side_effect=handler)
        await _fetch(http, timeout_s=7.0)
    assert seen == [{"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}]


async def test_invalid_url_is_refused(http):
    with pytest.raises(UrlRefused, match="invalid url"):
        await _fetch(http, "http://exa\tmple.com/")


@respx.mock
async def test_redirect_to_whitespace_host_is_refused(http):
    respx.get("https://example.com/a").respond(302, headers={"location": "http://a b.com/"})
    with pytest.raises(UrlRefused):
        await _fetch(http, "https://example.com/a")


@respx.mock
async def test_idna_error_becomes_url_refused(http):
    def bad(_host: str) -> list[str]:
        raise UnicodeError("label too long")

    with pytest.raises(UrlRefused, match="cannot resolve"):
        await fetch_page("https://example.com/", http=http, resolve=bad)


async def test_httpx_invalid_url_backstop(http, monkeypatch):
    def boom(*_a, **_k):
        raise httpx.InvalidURL("bad")

    monkeypatch.setattr(http, "build_request", boom)
    with pytest.raises(UrlRefused, match="invalid url"):
        await _fetch(http)
