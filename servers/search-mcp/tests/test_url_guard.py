import pytest

from pdl_search.url_guard import ParsedTarget, UrlRefused, check_url, resolve_host


def _public(_host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    ("url", "port"),
    [("https://example.com/a", None), ("http://example.com:8080/", 8080)],
)
def test_accepts_public_urls(url: str, port: int | None):
    target = check_url(url, resolve=_public)
    assert isinstance(target, ParsedTarget)
    assert target.host == "example.com"
    assert target.port == port


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https:///nohost",
        "https://user:pw@example.com/",
        "https://user@example.com/",
        "http://example.com:notaport/",
    ],
)
def test_refuses_malformed_or_unsafe_urls(url: str):
    with pytest.raises(UrlRefused):
        check_url(url, resolve=_public)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://LOCALHOST./",
        "http://app.localhost/",
        "http://gateway.lab/",
        "http://printer.local/",
        "http://foo.internal/",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
    ],
)
def test_refuses_private_hosts_without_resolving(url: str):
    def boom(_host: str) -> list[str]:
        raise AssertionError("resolver must not be called")

    with pytest.raises(UrlRefused):
        check_url(url, resolve=boom)


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "0.0.0.0",
        "240.0.0.1",
    ],
)
def test_refuses_private_resolution(addr: str):
    with pytest.raises(UrlRefused, match="non-public"):
        check_url("https://example.com/", resolve=lambda _h: [addr])


def test_refuses_mixed_public_and_private():
    with pytest.raises(UrlRefused):
        check_url("https://example.com/", resolve=lambda _h: ["93.184.216.34", "10.0.0.5"])


def test_refuses_unresolvable():
    def fail(_host: str) -> list[str]:
        raise OSError("nope")

    with pytest.raises(UrlRefused, match="cannot resolve"):
        check_url("https://example.com/", resolve=fail)


def test_refuses_empty_resolution():
    with pytest.raises(UrlRefused, match="cannot resolve"):
        check_url("https://example.com/", resolve=lambda _h: [])


def test_refuses_garbage_resolution():
    with pytest.raises(UrlRefused):
        check_url("https://example.com/", resolve=lambda _h: ["not-an-ip"])


def test_resolve_host_returns_unique_addresses(monkeypatch: pytest.MonkeyPatch):
    infos = [
        (2, 1, 6, "", ("1.2.3.4", 0)),
        (2, 2, 17, "", ("1.2.3.4", 0)),
        (10, 1, 6, "", ("2001:db8::1", 0, 0, 0)),
    ]
    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_k: infos)
    assert resolve_host("example.com") == ["1.2.3.4", "2001:db8::1"]
