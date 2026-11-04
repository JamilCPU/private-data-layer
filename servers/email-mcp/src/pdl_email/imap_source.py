"""Read-only IMAP source. All server data is treated as untrusted bytes."""

from __future__ import annotations

import email
import email.errors
import imaplib
import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from pdl_email.models import Message, message_id
from pdl_email.text import extract_body, header_text, parse_addresses, snippet

log = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ERR_TEXT_MAX = 200


class ImapError(imaplib.IMAP4.error):
    """The server answered a command with a non-OK status."""


def _check(command: str, response: tuple[Any, Any]) -> list[Any]:
    """Return the data of an OK response; raise ImapError otherwise."""
    status, data = response
    if status != "OK":
        text = _joined(data or []).decode("utf-8", errors="replace")[:_ERR_TEXT_MAX]
        raise ImapError(f"IMAP {command} failed ({status}): {text}")
    return data


def _quote(mailbox: str) -> str:
    """IMAP quoted string: wrap in double quotes, escape backslash and quote."""
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


FETCH_BATCH = 50
_UIDVALIDITY = re.compile(rb"UIDVALIDITY\s+(\d+)")
_UIDNEXT = re.compile(rb"UIDNEXT\s+(\d+)")
_FETCH_UID = re.compile(rb"\bUID\s+(\d+)\b")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _parse_sent_at(msg: email.message.Message, uid: int) -> datetime:
    raw = msg.get("Date")
    try:
        parsed = parsedate_to_datetime(str(raw)) if raw else None
        if parsed is not None:
            parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed is None:
        log.warning("message uid=%s has a missing or invalid Date header", uid)
        return datetime.now(UTC)
    return parsed


def _joined(data: list[Any]) -> bytes:
    return b" ".join(item for item in data if isinstance(item, bytes))


class ImapSource:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        mailbox: str = "INBOX",
        port: int = 993,
        connect: Callable[..., Any] = imaplib.IMAP4_SSL,
    ) -> None:
        if _CONTROL.search(mailbox):
            raise ValueError("mailbox name contains control characters")
        self._host = host
        self._user = user
        self._password = password
        self._mailbox = mailbox
        self._port = port
        self._connect = connect
        self._conn: Any = None
        self._uidvalidity = 0

    def open(self) -> tuple[int, int]:
        self._conn = self._connect(self._host, self._port)
        _check("login", self._conn.login(self._user, self._password))
        quoted = _quote(self._mailbox)
        _check("select", self._conn.select(quoted, readonly=True))
        data = _check("status", self._conn.status(quoted, "(UIDVALIDITY UIDNEXT)"))
        raw = _joined(data)
        validity, nxt = _UIDVALIDITY.search(raw), _UIDNEXT.search(raw)
        if validity is None or nxt is None:
            raise RuntimeError("IMAP STATUS response lacked UIDVALIDITY/UIDNEXT")
        self._uidvalidity = int(validity.group(1))
        return self._uidvalidity, int(nxt.group(1))

    def _search_uids(self, criteria: str) -> list[int]:
        if self._conn is None:
            raise RuntimeError("open() first")
        data = _check("SEARCH", self._conn.uid("SEARCH", None, criteria))
        raw = data[0] if data and isinstance(data[0], bytes) else b""
        return sorted(int(tok) for tok in raw.split() if tok.isdigit())

    def search_since(self, since: date) -> int:
        """Smallest UID on or after ``since``, or UIDNEXT when nothing matches."""
        stamp = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
        uids = self._search_uids(f"SINCE {stamp}")
        if uids:
            return uids[0]
        data = _check("status", self._conn.status(_quote(self._mailbox), "(UIDNEXT)"))
        match = _UIDNEXT.search(_joined(data))
        if match is None:
            raise RuntimeError("IMAP STATUS response lacked UIDNEXT")
        return int(match.group(1))

    def fetch_since(self, uid_from: int, *, limit: int = 500) -> Iterator[Message]:
        uids = [u for u in self._search_uids(f"UID {uid_from}:*") if u >= uid_from][:limit]
        for start in range(0, len(uids), FETCH_BATCH):
            batch = uids[start : start + FETCH_BATCH]
            data = _check(
                "FETCH",
                self._conn.uid("FETCH", ",".join(str(u) for u in batch), "(RFC822 FLAGS)"),
            )
            parsed: dict[int, Message] = {}
            for item in data:
                msg = self._parse_item(item)
                if msg is not None:
                    parsed[msg.uid] = msg
            for uid in sorted(parsed):
                yield parsed[uid]

    def _parse_item(self, item: Any) -> Message | None:
        if not isinstance(item, tuple):
            return None  # trailing b")" / FLAGS continuation lines
        if len(item) < 2 or not isinstance(item[0], bytes) or not isinstance(item[1], bytes):
            log.warning("skipping malformed IMAP fetch item")
            return None
        uid_match = _FETCH_UID.search(item[0])
        if uid_match is None:
            log.warning("skipping IMAP fetch item without UID")
            return None
        uid = int(uid_match.group(1))
        try:
            return self._build(uid, email.message_from_bytes(item[1]))
        except (
            ValueError,
            LookupError,
            TypeError,
            OverflowError,
            email.errors.MessageError,
            UnicodeError,
        ) as exc:
            log.warning("skipping unparseable message uid=%s: %s", uid, exc)
            return None

    def _build(self, uid: int, msg: email.message.Message) -> Message:
        body, has_attachments = extract_body(msg)
        senders = parse_addresses(header_text(msg, "From"))
        from_name, from_addr = senders[0] if senders else (None, "")
        recipients = parse_addresses(
            ", ".join(filter(None, (header_text(msg, "To"), header_text(msg, "Cc"))))
        )
        mid = header_text(msg, "Message-ID").strip()
        return Message(
            id=message_id(self._mailbox, self._uidvalidity, uid),
            uid=uid,
            uidvalidity=self._uidvalidity,
            mailbox=self._mailbox,
            from_addr=from_addr,
            from_name=from_name,
            to_addrs=tuple(addr for _, addr in recipients),
            subject=header_text(msg, "Subject"),
            sent_at=_parse_sent_at(msg, uid),
            snippet=snippet(body),
            body_text=body,
            has_attachments=has_attachments,
            message_id_header=mid or None,
        )

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.logout()
        except Exception:  # closing must never raise
            log.debug("IMAP logout failed", exc_info=True)
