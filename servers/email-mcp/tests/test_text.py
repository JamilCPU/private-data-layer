from email import message_from_string
from email.message import EmailMessage

from pdl_email.models import message_id
from pdl_email.text import (
    BODY_MAX_CHARS,
    extract_body,
    header_text,
    html_to_text,
    parse_addresses,
    snippet,
)


def test_html_strips_tags_scripts_and_entities():
    html = (
        "<html><head><title>t</title></head><body><style>p{}</style>"
        "<script>alert(1)</script><p>Hello &amp;   world</p><div>Next<br/>line</div>"
        "<ul><li>a</li><li>b</li></ul></body></html>"
    )
    assert html_to_text(html) == "Hello & world\nNext\nline\na\nb"


def test_html_collapses_blank_lines():
    assert html_to_text("<p>a</p>\n\n\n<p></p><p>b</p>") == "a\nb"


def test_snippet_collapses_whitespace_and_caps():
    assert snippet("a  b\n\nc") == "a b c"
    assert snippet("x" * 500) == "x" * 200
    assert snippet("hello world", 5) == "hello"


def test_message_id_is_deterministic():
    assert message_id("INBOX", 1, 2) == message_id("INBOX", 1, 2)
    assert message_id("INBOX", 1, 2) != message_id("INBOX", 2, 2)
    assert len(message_id("INBOX", 1, 2)) == 32


def _alt() -> EmailMessage:
    msg = EmailMessage()
    msg.set_content("plain body")
    msg.add_alternative("<p>html body</p>", subtype="html")
    return msg


def test_extract_prefers_plain():
    assert extract_body(_alt()) == ("plain body\n", False)


def test_extract_falls_back_to_html():
    msg = EmailMessage()
    msg.set_content("<p>Hi <b>there</b></p>", subtype="html")
    assert extract_body(msg) == ("Hi there", False)


def test_extract_flags_attachment_and_ignores_it_as_body():
    msg = _alt()
    msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="a.pdf")
    body, has_att = extract_body(msg)
    assert body == "plain body\n"
    assert has_att is True


def test_extract_text_attachment_not_used_as_body():
    msg = EmailMessage()
    msg.set_content("<p>real</p>", subtype="html")
    msg.add_attachment("secret", subtype="plain", filename="n.txt")
    assert extract_body(msg) == ("real", True)


def test_extract_empty_when_no_text_parts():
    msg = EmailMessage()
    msg.set_content(b"x", maintype="application", subtype="octet-stream")
    assert extract_body(msg) == ("", False)


def test_extract_caps_body():
    msg = EmailMessage()
    msg.set_content("y" * (BODY_MAX_CHARS + 500))
    assert len(extract_body(msg)[0]) == BODY_MAX_CHARS


def test_extract_charset_decoding_and_fallback():
    latin = message_from_string(
        "Content-Type: text/plain; charset=iso-8859-1\n"
        "Content-Transfer-Encoding: quoted-printable\n\ncaf=E9"
    )
    assert extract_body(latin)[0] == "café"
    bogus = message_from_string(
        "Content-Type: text/plain; charset=nonsense-9\nContent-Transfer-Encoding: 8bit\n\nplain"
    )
    assert extract_body(bogus)[0] == "plain"


def test_header_text_decodes_rfc2047():
    msg = message_from_string("Subject: =?utf-8?q?Caf=C3=A9_menu?=\n\nx")
    assert header_text(msg, "Subject") == "Café menu"
    assert header_text(msg, "X-Missing") == ""


def test_parse_addresses_lowercases_and_skips_empty():
    out = parse_addresses('"Ann" <Ann@Example.com>, bob@example.test, ')
    assert out == [("Ann", "ann@example.com"), (None, "bob@example.test")]
