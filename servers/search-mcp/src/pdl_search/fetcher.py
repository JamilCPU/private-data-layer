"""Fetch a web page for a language model: SSRF-guarded, size-capped, text only."""

import asyncio
import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
import trafilatura

from .url_guard import UrlRefused, check_url, resolve_host

log = logging.getLogger(__name__)

PAGE_MAX_CHARS = 8000
USER_AGENT = "pdl-search-mcp/0.1 (+private lab assistant)"
ACCEPT = "text/html,text/plain;q=0.9,*/*;q=0.1"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = frozenset({"text/plain"})
TITLE_SCAN_BYTES = 64 * 1024
TITLE_MAX = 300
TITLE_RE = re.compile(r"<title(?:\s[^>]{0,200})?>([^<]{0,1000})</title", re.IGNORECASE)
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

__all__ = ["PAGE_MAX_CHARS", "USER_AGENT", "FetchError", "Page", "UrlRefused", "fetch_page"]


class FetchError(Exception):
    """The page could not be fetched or is not readable text."""


@dataclass(frozen=True)
class Page:
    url: str
    final_url: str
    title: str
    text: str
    truncated: bool
    content_type: str


class _TextStripper(HTMLParser):
    """Linear-time tag stripper that drops script/style content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _strip_tags(markup: str) -> str:
    stripper = _TextStripper()
    stripper.feed(markup)
    stripper.close()
    return "".join(stripper.parts)


def _tidy(text: str) -> str:
    """Collapse whitespace inside lines, drop blank lines, keep paragraph breaks as newlines."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _extract_html(markup: str) -> str:
    try:
        extracted = trafilatura.extract(
            markup, include_comments=False, include_tables=True, favor_recall=True
        )
    except Exception:  # trafilatura can fail in many ways on hostile markup
        log.warning("trafilatura failed, using tag-stripped fallback", exc_info=True)
        extracted = None
    return extracted or _strip_tags(markup)


def _title(markup: str, fallback: str) -> str:
    match = TITLE_RE.search(markup[:TITLE_SCAN_BYTES])
    if match is None:
        return fallback
    title = " ".join(html.unescape(match.group(1)).split())[:TITLE_MAX]
    return title or fallback


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def _read_body(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            return b"".join(chunks)[:max_bytes], True
    return b"".join(chunks), False


def _next_url(current: str, response: httpx.Response) -> str:
    location = response.headers.get("location")
    if not location:
        raise FetchError(f"HTTP {response.status_code} redirect without Location")
    return urljoin(current, location)


async def _build_page(
    url: str, final_url: str, response: httpx.Response, *, max_bytes: int, max_chars: int
) -> Page:
    ctype = _content_type(response)
    if ctype not in HTML_TYPES | TEXT_TYPES:
        raise FetchError(f"unsupported content type: {ctype or '(none)'}")
    body, cut = await _read_body(response, max_bytes)
    markup = _decode(body, response.charset_encoding)
    if ctype in HTML_TYPES:
        title = _title(markup, final_url)
        text = _tidy(await asyncio.to_thread(_extract_html, markup))
    else:
        title, text = final_url, _tidy(markup)
    if len(text) > max_chars:
        text, cut = text[:max_chars], True
    return Page(url, final_url, title, text, cut, ctype)


async def fetch_page(
    url: str,
    *,
    http: httpx.AsyncClient,
    resolve: Callable[[str], list[str]] = resolve_host,
    max_bytes: int = 2_000_000,
    max_chars: int = PAGE_MAX_CHARS,
    max_redirects: int = 5,
) -> Page:
    """Fetch `url`, guarding every hop against SSRF. Raises UrlRefused or FetchError."""
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    current = url
    try:
        for _ in range(max_redirects + 1):
            await asyncio.to_thread(check_url, current, resolve=resolve)
            async with http.stream(
                "GET", current, headers=headers, follow_redirects=False
            ) as response:
                if response.status_code in REDIRECT_STATUSES:
                    current = _next_url(current, response)
                    continue
                if response.status_code >= 300:
                    raise FetchError(f"HTTP {response.status_code}")
                return await _build_page(
                    url, current, response, max_bytes=max_bytes, max_chars=max_chars
                )
        raise FetchError("too many redirects")
    except httpx.HTTPError as exc:
        log.warning("fetch failed: %s", type(exc).__name__)
        raise FetchError(f"request failed: {exc}") from exc
    finally:
        http.cookies.clear()  # never carry cookies between fetches
