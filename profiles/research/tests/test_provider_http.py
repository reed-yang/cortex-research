"""The public-web fetch policy and the shared failure categories.

Provider-free: every response comes from `httpx.MockTransport` and every name
resolves through a fake resolver.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from cortex_research.provider_http import (
    CATEGORIES,
    Deadline,
    PolicyRefusal,
    ProviderError,
    check_public_url,
    public_address,
    safe_get,
    status_category,
)

ADDRESSES = {
    "blog.example": "93.184.216.34",
    "other.example": "93.184.216.35",
    "internal.example": "10.0.0.7",
    "mixed.example": ["93.184.216.36", "127.0.0.1"],
}


def resolver(host: str, port: int, type: int = 0):  # noqa: A002 - getaddrinfo's name
    value = ADDRESSES.get(host)
    if value is None:
        raise socket.gaierror(socket.EAI_NONAME, "unknown")
    values = value if isinstance(value, list) else [value]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in values]


def fetch(handler, url: str = "https://blog.example/post", **kwargs):
    return safe_get(
        url,
        provider="blog",
        max_bytes=kwargs.pop("max_bytes", 1024),
        deadline=Deadline(30),
        transport=httpx.MockTransport(handler),
        resolver=resolver,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (200, None),
        (401, "auth"),
        (403, "auth"),
        (402, "payment"),
        (429, "rate_limited"),
        (404, "not_found"),
        (410, "not_found"),
        (408, "transient"),
        (500, "transient"),
        (503, "transient"),
        (400, "upstream_error"),
        (422, "upstream_error"),
    ],
)
def test_status_categories(status: int, category: str | None) -> None:
    assert status_category(status) == category
    assert category is None or category in CATEGORIES


def test_an_unknown_category_is_refused() -> None:
    with pytest.raises(ValueError):
        ProviderError("mystery", "x")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
     "100.64.0.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "fe80::1",
     "fc00::1", "::ffff:127.0.0.1", "ff02::1"],
)
def test_non_public_addresses(address: str) -> None:
    assert not public_address(address)


def test_public_addresses() -> None:
    assert public_address("93.184.216.34")
    assert public_address("2606:2800:220:1:248:1893:25c8:1946")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://blog.example/file",
        "file:///etc/passwd",
        "https://user:secret@blog.example/",
        "https://blog.example:8443/",
        "http://blog.example:443/",
        "https:///no-host",
        "https://blog.example:notaport/",
    ],
)
def test_the_url_policy_refuses(url: str) -> None:
    with pytest.raises(PolicyRefusal) as caught:
        check_public_url(url, provider="blog")
    assert caught.value.category == "not_found"
    assert "secret" not in caught.value.message


def test_default_ports_are_admitted() -> None:
    assert check_public_url("HTTPS://Blog.Example:443/A#frag", provider="blog")[2] == (
        "https://blog.example:443/A"
    )


def test_the_connection_is_pinned_to_the_checked_address() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok", headers={"content-type": "text/html"})

    page = fetch(handler)
    assert page.body == b"ok"
    assert page.url == "https://blog.example/post"
    request = seen[0]
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "blog.example"
    assert request.extensions["sni_hostname"] == "blog.example"
    assert "cookie" not in request.headers and "authorization" not in request.headers


@pytest.mark.parametrize("host", ["internal.example", "mixed.example", "127.0.0.1", "[::1]"])
def test_a_host_with_any_private_answer_is_refused(host: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may leave")

    with pytest.raises(PolicyRefusal):
        fetch(handler, f"https://{host}/")


def test_a_redirect_to_a_private_address_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        return httpx.Response(302, headers={"location": "http://internal.example/admin"})

    with pytest.raises(PolicyRefusal):
        fetch(handler)


def test_a_redirect_to_another_scheme_or_port_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://other.example:8080/"})

    with pytest.raises(PolicyRefusal):
        fetch(handler)


def test_redirects_are_rechecked_and_bounded() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(f"{request.headers['host']}{request.url.path}")
        number = len(hops)
        return httpx.Response(302, headers={"location": f"https://other.example/{number}"})

    with pytest.raises(ProviderError) as caught:
        fetch(handler)
    assert caught.value.category == "upstream_error"
    assert len(hops) == 6  # the first request and five redirects


def test_cookies_are_never_sent_across_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://other.example/next", "set-cookie": "sid=abc"},
            )
        return httpx.Response(200, text="done")

    page = fetch(handler)
    assert page.redirects == ("https://blog.example/post",)
    assert page.url == "https://other.example/next"
    assert all("cookie" not in request.headers for request in seen)
    assert seen[1].headers["host"] == "other.example"


def test_a_cookie_is_not_replayed_on_a_same_host_redirect() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                302, headers={"location": "/next", "set-cookie": "sid=abc; Path=/"}
            )
        return httpx.Response(200, text="done")

    page = fetch(handler)
    assert page.url == "https://blog.example/next"
    assert [request.url.host for request in seen] == ["93.184.216.34", "93.184.216.34"]
    assert all("cookie" not in request.headers for request in seen)


def test_a_body_over_the_limit_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    with pytest.raises(ProviderError) as caught:
        fetch(handler, max_bytes=1024)
    assert caught.value.category == "invalid_response"


def test_a_malformed_compressed_body_is_an_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A stream, so the body is decoded while it is read, as from a server.
        return httpx.Response(200, stream=httpx.ByteStream(b"not gzip at all"), headers={"content-encoding": "gzip"})

    with pytest.raises(ProviderError) as caught:
        fetch(handler)
    assert caught.value.category == "invalid_response"


def test_a_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as caught:
        fetch(handler)
    assert caught.value.category == "transient"


def test_an_unresolvable_host_is_not_found() -> None:
    with pytest.raises(ProviderError) as caught:
        fetch(lambda request: httpx.Response(200), "https://nowhere.example/")
    assert caught.value.category == "not_found"
    assert not isinstance(caught.value, PolicyRefusal)


def test_an_exhausted_deadline_is_transient() -> None:
    now = [0.0]
    deadline = Deadline(10, clock=lambda: now[0])
    now[0] = 11.0
    with pytest.raises(ProviderError) as caught:
        deadline.timeout(5, provider="x")
    assert caught.value.category == "transient"
