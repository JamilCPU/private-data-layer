"""Tests for PBKDF2 secret hashing."""

import pytest

from pdl_assistant.secrets import hash_secret, new_secret, verify_secret


def test_hash_format():
    parts = hash_secret("s").split("$")
    assert parts[0] == "pbkdf2_sha256"
    assert parts[1] == "200000"
    assert len(parts) == 4


def test_round_trip():
    encoded = hash_secret("hunter2", iterations=1000)
    assert verify_secret("hunter2", encoded)
    assert not verify_secret("wrong", encoded)


def test_salts_differ():
    assert hash_secret("x", iterations=10) != hash_secret("x", iterations=10)


@pytest.mark.parametrize(
    "bad", ["", "nope", "md5$1$a$b", "pbkdf2_sha256$0$a$b", "pbkdf2_sha256$x$a$b", "a$b$c"]
)
def test_malformed_is_false(bad: str):
    assert not verify_secret("s", bad)


def test_new_secret_unique_and_sized():
    assert new_secret() != new_secret()
    assert len(new_secret(32)) >= 40
