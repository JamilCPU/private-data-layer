"""SSRF guard: decide whether a URL may be fetched on behalf of a language model.

The guard refuses anything that is not plain http(s) to a public internet host. It is
called before every request, including every redirect hop.
"""

import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")
BLOCKED_SUFFIXES = (".localhost", ".lab", ".local", ".internal")
BLOCKED_NAMES = ("localhost",)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UrlRefused(Exception):
    """The URL points somewhere the fetcher must never go."""


@dataclass(frozen=True)
class ParsedTarget:
    scheme: str
    host: str
    port: int | None
    path: str


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to its unique addresses (IPv4 and IPv6), order preserved."""
    infos = socket.getaddrinfo(host, None)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_public(ip: IpAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)  # fec0::/10, IPv6 only
    ):
        return False
    return ip.is_global


def _parse_ip(text: str) -> IpAddress | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _split(url: str) -> tuple[str, str, int | None, str]:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError as exc:
        raise UrlRefused(f"malformed URL: {exc}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlRefused(f"scheme not allowed: {parts.scheme or '(none)'}")
    if not host:
        raise UrlRefused("URL has no host")
    if "@" in parts.netloc:
        raise UrlRefused("URLs with credentials are not allowed")
    return parts.scheme.lower(), host, port, parts.path or "/"


def _check_name(host: str) -> None:
    if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UrlRefused(f"host not allowed: {host}")


def _check_addresses(host: str, resolve: Callable[[str], list[str]]) -> None:
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError) as exc:  # UnicodeError: IDNA failures
        raise UrlRefused(f"cannot resolve {host}: {exc}") from exc
    if not addresses:
        raise UrlRefused(f"cannot resolve {host}: no addresses")
    for text in addresses:
        ip = _parse_ip(text)
        if ip is None or not _is_public(ip):
            log.warning("refused %s: resolves to non-public address", host)
            raise UrlRefused(f"{host} resolves to a non-public address")


def check_url(url: str, *, resolve: Callable[[str], list[str]] = resolve_host) -> ParsedTarget:
    """Return the parsed target or raise UrlRefused. Resolves DNS for named hosts."""
    if any(c.isspace() or not c.isprintable() for c in url):
        raise UrlRefused("invalid url: whitespace or control characters")
    scheme, host, port, path = _split(url)
    literal = _parse_ip(host)
    if literal is not None:
        if not _is_public(literal):
            raise UrlRefused(f"{host} is a non-public address")
    else:
        _check_name(host)
        _check_addresses(host, resolve)
    return ParsedTarget(scheme=scheme, host=host, port=port, path=path)
