import imaplib
import logging
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage

import pytest

from pdl_email.imap_source import ImapSource

from .fake_imap import FakeImap


def _multipart() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Ann Example <Ann@Example.com>"
    msg["To"] = "me@example.test"
    msg["Cc"] = "Bob <bob@example.com>"
    msg["Subject"] = "Plans"
    msg["Date"] = "Tue, 03 Nov 2026 10:00:00 +0200"
    msg["Message-ID"] = " <abc@example.com> "
    msg.set_content("plain part")
    msg.add_alternative("<p>html part</p>", subtype="html")
    msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="x.pdf")
    return msg.as_bytes()


def _html_only(date_header: bytes = b"Tue, 03 Nov 2026 10:00:00 -0000") -> bytes:
    return (
        b"From: carol@example.com\r\nTo: me@example.test\r\n"
        b"Subject: =?utf-8?q?Caf=C3=A9?=\r\nDate: " + date_header + b"\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>Hello</p>"
    )


def _items(*pairs: tuple[int, bytes]) -> list:
    out: list = []
    for uid, raw in pairs:
        out.append((f"{uid} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw))
        out.append(b" FLAGS (\\Seen))")
    return out


def _source(fake: FakeImap) -> ImapSource:
    return ImapSource("imap.example.test", "me", "pw", connect=fake)


def _open(fake: FakeImap) -> ImapSource:
    src = _source(fake)
    assert src.open() == (777, 12)
    return src


def test_open_sequence():
    fake = FakeImap()
    _open(fake)
    assert fake.calls == [
        ("connect", "imap.example.test", 993),
        ("login", "me", "pw"),
        ("select", '"INBOX"', True),
        ("status", '"INBOX"', "(UIDVALIDITY UIDNEXT)"),
    ]


def test_fetch_since_parses_two_messages():
    fake = FakeImap(fetch_items=_items((10, _multipart()), (11, _html_only())))
    src = _open(fake)
    first, second = list(src.fetch_since(10))
    assert fake.calls[-2:] == [
        ("uid", "SEARCH", None, "UID 10:*"),
        ("uid", "FETCH", "10,11", "(RFC822 FLAGS)"),
    ]
    assert (first.uid, second.uid) == (10, 11)
    assert first.uidvalidity == 777 and first.mailbox == "INBOX"
    assert first.from_addr == "ann@example.com" and first.from_name == "Ann Example"
    assert first.to_addrs == ("me@example.test", "bob@example.com")
    assert first.has_attachments is True
    assert first.body_text.startswith("plain part")
    assert first.snippet == "plain part"
    assert first.message_id_header == "<abc@example.com>"
    assert first.sent_at == datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    assert first.sent_at.utcoffset() == timedelta(0)
    assert second.subject == "Café"
    assert second.body_text == "Hello" and second.has_attachments is False
    assert second.from_name is None and second.message_id_header is None
    assert second.sent_at.utcoffset() == timedelta(0)
    assert first.id != second.id


def test_naive_date_is_utc_and_invalid_date_warns(caplog):
    naive = _html_only(b"Tue, 03 Nov 2026 10:00:00")
    bad = _html_only(b"garbage")
    src = _open(FakeImap(fetch_items=_items((10, naive), (11, bad))))
    with caplog.at_level(logging.WARNING):
        a, b = list(src.fetch_since(10))
    assert a.sent_at == datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    assert b.sent_at.tzinfo == UTC
    assert abs(b.sent_at - datetime.now(UTC)) < timedelta(minutes=1)
    assert "Date" in caplog.text


def test_offset_date_normalised_to_utc():
    raw = _html_only(b"Tue, 03 Nov 2026 10:00:00 +0500")
    src = _open(FakeImap(search=b"10", fetch_items=_items((10, raw))))
    (m,) = list(src.fetch_since(10))
    assert m.sent_at == datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
    assert m.sent_at.utcoffset() == timedelta(0)


def test_uid_from_filter_and_limit_and_batches():
    fake = FakeImap(search=b"9 12 10 11 13")
    src = _open(fake)
    assert list(src.fetch_since(10, limit=3)) == []
    fetches = [c for c in fake.calls if c[:2] == ("uid", "FETCH")]
    assert fetches == [("uid", "FETCH", "10,11,12", "(RFC822 FLAGS)")]

    many = FakeImap(search=" ".join(str(i) for i in range(1, 121)).encode())
    list(_open(many).fetch_since(1))
    sizes = [len(c[2].split(",")) for c in many.calls if c[:2] == ("uid", "FETCH")]
    assert sizes == [50, 50, 20]


def test_empty_search_does_not_fetch():
    fake = FakeImap(search=b"")
    assert list(_open(fake).fetch_since(12)) == []
    assert not [c for c in fake.calls if c[:2] == ("uid", "FETCH")]


def test_malformed_items_skipped_with_warning(caplog):
    items = _items((10, _html_only())) + [(b"garbage no uid", b"raw"), (b"11 (UID 11", 5)]
    src = _open(FakeImap(search=b"10 11 12", fetch_items=items))
    with caplog.at_level(logging.WARNING):
        got = list(src.fetch_since(10))
    assert [m.uid for m in got] == [10]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_search_since_found_and_none():
    fake = FakeImap(search=b"31 30 40")
    assert _open(fake).search_since(date(2026, 1, 5)) == 30
    assert ("uid", "SEARCH", None, "SINCE 05-Jan-2026") in fake.calls
    assert _open(FakeImap(search=b"")).search_since(date(2026, 12, 31)) == 12


def test_open_rejects_bad_status():
    with pytest.raises(RuntimeError):
        _source(FakeImap(status=b"INBOX ()")).open()


def test_close_ignores_errors():
    fake = FakeImap()
    src = _open(fake)
    fake.logout_error = OSError("boom")
    src.close()
    src.close()
    _source(FakeImap()).close()


def test_malformed_encoded_header_does_not_abort():
    bad = _html_only().replace(b"=?utf-8?q?Caf=C3=A9?=", b"=?utf-8?b?a?=")
    src = _open(FakeImap(fetch_items=_items((10, bad), (11, _html_only()))))
    first, second = list(src.fetch_since(10))
    assert first.uid == 10 and "=?utf-8?b?a?=" in first.subject
    assert second.subject == "Café"


def test_message_whose_parsing_raises_is_skipped(monkeypatch, caplog):
    src = _open(FakeImap(fetch_items=_items((10, _html_only()), (11, _html_only()))))
    real = ImapSource._build

    def flaky(self, uid, msg):
        if uid == 10:
            raise UnicodeError("boom")
        return real(self, uid, msg)

    monkeypatch.setattr(ImapSource, "_build", flaky)
    with caplog.at_level(logging.WARNING):
        got = list(src.fetch_since(10))
    assert [m.uid for m in got] == [11]
    assert "uid=10" in caplog.text


def test_overflowing_date_falls_back_to_now(caplog):
    raw = _html_only(b"Tue, 03 Nov 99999999999 10:00:00 +0000")
    src = _open(FakeImap(search=b"10", fetch_items=_items((10, raw))))
    with caplog.at_level(logging.WARNING):
        (m,) = list(src.fetch_since(10))
    assert abs(m.sent_at - datetime.now(UTC)) < timedelta(minutes=1)


def test_select_no_raises():
    fake = FakeImap(statuses={"select": ("NO", b"mailbox missing")})
    with pytest.raises(imaplib.IMAP4.error, match="select.*mailbox missing"):
        _source(fake).open()


def test_login_no_raises():
    fake = FakeImap(statuses={"login": ("NO", b"bad creds")})
    with pytest.raises(imaplib.IMAP4.error, match="login"):
        _source(fake).open()


def test_search_no_raises_and_text_not_parsed_as_uids():
    fake = FakeImap(statuses={"SEARCH": ("NO", b"too many 12 13")})
    src = _open(fake)
    with pytest.raises(imaplib.IMAP4.error, match="SEARCH"):
        list(src.fetch_since(10))
    with pytest.raises(imaplib.IMAP4.error):
        src.search_since(date(2026, 1, 5))


def test_fetch_no_raises():
    fake = FakeImap(statuses={"FETCH": ("NO", b"x" * 500)})
    src = _open(fake)
    with pytest.raises(imaplib.IMAP4.error, match="FETCH") as exc:
        list(src.fetch_since(10))
    assert len(str(exc.value)) < 300


def test_mailbox_is_quoted():
    fake = FakeImap()
    src = ImapSource("h", "u", "p", mailbox='[Gmail]/"All" \\Mail', connect=fake)
    src.open()
    assert ("select", '"[Gmail]/\\"All\\" \\\\Mail"', True) in fake.calls
    plain = FakeImap()
    ImapSource("h", "u", "p", mailbox="[Gmail]/All Mail", connect=plain).open()
    assert ("select", '"[Gmail]/All Mail"', True) in plain.calls


@pytest.mark.parametrize("name", ["INBOX\nX", "a\rb", "a\x00b", "a\x7fb"])
def test_mailbox_with_control_chars_rejected(name):
    with pytest.raises(ValueError):
        ImapSource("h", "u", "p", mailbox=name)


def test_flags_first_fetch_header():
    raw = _html_only()
    header = b"1 (FLAGS (\\Seen) UID 10 RFC822 {%d}" % len(raw)
    src = _open(FakeImap(search=b"10", fetch_items=[(header, raw), b")"]))
    (m,) = list(src.fetch_since(10))
    assert m.uid == 10


def test_fetch_before_open_raises():
    with pytest.raises(RuntimeError, match="open"):
        list(_source(FakeImap()).fetch_since(1))
