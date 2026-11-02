"""PBKDF2 secret hashing and secret generation."""

import base64
import binascii
import hashlib
import hmac
import os
import secrets

_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
_DEFAULT_ITERATIONS = 200_000


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_secret(secret: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt_b64url>$<digest_b64url>``."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_secret(secret: str, encoded: str) -> bool:
    """Constant-time check of ``secret`` against ``encoded``; False if malformed."""
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_text)
        if iterations < 1:
            return False
        salt = _b64d(salt_text)
        expected = _b64d(digest_text)
    except (ValueError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def new_secret(nbytes: int = 32) -> str:
    """Return a URL-safe random secret with ``nbytes`` bytes of entropy."""
    return secrets.token_urlsafe(nbytes)
