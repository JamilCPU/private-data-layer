"""Text extraction from MIME messages."""

from __future__ import annotations

import email.message
from email.header import decode_header, make_header
from email.utils import getaddresses
from html import unescape
from html.parser import HTMLParser

BODY_MAX_CHARS = 16_000
_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})
_SKIP_TAGS = frozenset({"script", "style", "head"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _collapse(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return _collapse(unescape(parser.text()))


def snippet(text: str, n: int = 200) -> str:
    return " ".join(text.split())[:n]


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _is_attachment(part: email.message.Message) -> bool:
    disposition = str(part.get("Content-Disposition", "")).lower()
    return disposition.startswith("attachment") or part.get_filename() is not None


def extract_body(msg: email.message.Message) -> tuple[str, bool]:
    plain: email.message.Message | None = None
    html: email.message.Message | None = None
    has_attachments = False
    for part in msg.walk():
        if part.is_multipart():
            continue
        if _is_attachment(part):
            has_attachments = True
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = part
        elif ctype == "text/html" and html is None:
            html = part
    if plain is not None:
        text = _decode_part(plain)
    elif html is not None:
        text = html_to_text(_decode_part(html))
    else:
        text = ""
    return text[:BODY_MAX_CHARS], has_attachments


def header_text(msg: email.message.Message, name: str) -> str:
    value = msg.get(name)
    if value is None:
        return ""
    raw = str(value)
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def parse_addresses(value: str) -> list[tuple[str | None, str]]:
    result: list[tuple[str | None, str]] = []
    for name, addr in getaddresses([value]):
        if addr:
            result.append((name or None, addr.lower()))
    return result
