import hmac

import pytest

from pdl_sms_store import tokens
from pdl_sms_store.tokens import (
    TokensError,
    find_device,
    hash_token,
    load_device_tokens,
    verify_token,
)


def test_hash_format_and_verify():
    h = hash_token("tok", iterations=10)
    assert h.startswith("pbkdf2_sha256$10$") and h.count("$") == 3
    assert verify_token("tok", h)
    assert not verify_token("nope", h)
    assert hash_token("tok", iterations=10) != h  # random salt


def test_default_iterations():
    assert hash_token("t").split("$")[1] == "200000"


@pytest.mark.parametrize(
    "bad",
    ["", "junk", "md5$1$aa$bb", "pbkdf2_sha256$0$aa$bb", "pbkdf2_sha256$x$aa$bb", "a$b$c$d$e"],
)
def test_malformed_hash_is_false(bad):
    assert verify_token("tok", bad) is False


def test_lone_surrogate_token_is_false_not_error():
    h = hash_token("tok", iterations=5)
    assert verify_token("bad" + chr(0xD800) + "token", h) is False


def test_verify_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(tokens.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert verify_token("tok", hash_token("tok", iterations=5))
    assert calls


def test_find_device_checks_every_entry(monkeypatch):
    devices = {"a": hash_token("ta", iterations=5), "b": hash_token("tb", iterations=5)}
    seen = []
    real = tokens.verify_token
    monkeypatch.setattr(tokens, "verify_token", lambda t, h: seen.append(h) or real(t, h))
    assert find_device("ta", devices) == "a"
    assert len(seen) == 2
    assert find_device("tb", devices) == "b"
    assert find_device("wrong", devices) is None
    assert find_device("x", {}) is None


def write(tmp_path, text):
    p = tmp_path / "tokens.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_ok(tmp_path):
    p = write(tmp_path, "devices:\n  pixel-1: h1\n  tab_2: h2\n")
    assert load_device_tokens(p) == {"pixel-1": "h1", "tab_2": "h2"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "devices:\n",
        "devices: {}\n",
        "other: 1\n",
        "- a\n",
        "devices:\n  Bad Name: h\n",
        "devices:\n  " + "a" * 33 + ": h\n",
        "devices:\n  ok: ''\n",
        "devices:\n  ok: 5\n",
        "devices: [unclosed\n",
        "devices:\n  1: h\n",
    ],
)
def test_load_rejects(tmp_path, text):
    with pytest.raises(TokensError):
        load_device_tokens(write(tmp_path, text))


def test_load_missing_file(tmp_path):
    with pytest.raises(TokensError):
        load_device_tokens(tmp_path / "nope.yaml")
