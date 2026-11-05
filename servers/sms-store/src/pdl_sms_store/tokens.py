"""Device token hashing, verification and the tokens file."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path

import yaml

_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
_ITERATIONS = 200_000
_DEVICE_NAME = re.compile(r"[a-z0-9_-]{1,32}")


class TokensError(ValueError):
    """The device tokens file is missing, empty or malformed."""


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str, *, iterations: int = _ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt_b64url>$<digest_b64url>``."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_token(token: str, hashed: str) -> bool:
    """Constant-time check of ``token`` against ``hashed``; False if malformed."""
    try:
        algorithm, iterations_text, salt_text, digest_text = hashed.split("$")
        iterations = int(iterations_text)
        if algorithm != _ALGORITHM or iterations < 1:
            return False
        salt, expected = _b64d(salt_text), _b64d(digest_text)
    except (ValueError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def load_device_tokens(path: Path) -> Mapping[str, str]:
    """Read ``devices: {name: hash}``; empty or missing is an error."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TokensError(f"cannot read device tokens {path}: {exc}") from exc
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict) or not devices:
        raise TokensError(f"{path} must define a non-empty 'devices' mapping")
    out: dict[str, str] = {}
    for name, hashed in devices.items():
        if not isinstance(name, str) or _DEVICE_NAME.fullmatch(name) is None:
            raise TokensError(f"invalid device name {name!r}; use [a-z0-9_-], 1-32 characters")
        if not isinstance(hashed, str) or not hashed:
            raise TokensError(f"device {name!r} needs a token hash string")
        out[name] = hashed
    return out


def find_device(token: str, devices: Mapping[str, str]) -> str | None:
    """Return the device whose hash matches ``token``; every entry is always verified."""
    matched: str | None = None
    for name, hashed in devices.items():
        if verify_token(token, hashed) and matched is None:
            matched = name
    return matched
