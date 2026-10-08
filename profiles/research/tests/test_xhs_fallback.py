"""Weekly-fallback verification: page titles under the fetch policy, provider-free."""

from __future__ import annotations

import socket

import httpx
import pytest

from cortex_research import xhs_fallback
from cortex_research.provider_http import PolicyRefusal, ProviderError


def resolver(host: str, port: int, type: int = 0):  # noqa: A002
    table = {"blog.example": "93.184.216.34", "arxiv.org": "93.184.216.35",
             "intranet.example": "172.16.5.5"}
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]


def site(pages: dict[str, httpx.Response], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return pages[request.headers["host"] + request.url.path]

    return httpx.MockTransport(handler)


def html(title: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=f"<html><head><title>{title}</title><meta property='og:title' content='Short'></head></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )


@pytest.mark.parametrize(
    ("url", "paper"),
    [
        ("https://arxiv.org/abs/2501.01234", True),
        ("https://export.arxiv.org/pdf/2501.01234", True),
        ("https://arxiv.org./abs/2501.01234", True),
        ("https://openreview.net/forum?id=x", True),
        ("https://doi.org/10.1/x", True),
        ("https://aclanthology.org/2024.acl-long.1/", True),
        ("https://blog.example/notes.pdf", True),
        ("https://blog.example/post", False),
        ("https://notarxiv.org/post", False),
    ],
)
def test_paper_pages_are_recognized(url: str, paper: bool) -> None:
    assert xhs_fallback.is_paper_url(url) is paper


def test_a_blog_page_is_fetched_and_a_paper_page_is_not() -> None:
    seen: list[httpx.Request] = []
    transport = site({"blog.example/post": html("Attention Sinks | Lab")}, seen)
    checked = xhs_fallback.check_blog_page(
        "https://blog.example/post", transport=transport, resolver=resolver
    )
    assert checked.to_dict() == {
        "check": "blog", "requested_url": "https://blog.example/post",
        "final_url": "https://blog.example/post", "title": "Attention Sinks | Lab",
        "og_title": "Short", "paper_host": False,
    }
    assert "authorization" not in seen[0].headers
    skipped = xhs_fallback.check_blog_page(
        "https://arxiv.org/abs/2501.01234", transport=transport, resolver=resolver
    )
    assert (skipped.paper_host, skipped.final_url, len(seen)) == (True, None, 1)


def test_the_arxiv_abs_page_is_the_only_page_an_id_fetches() -> None:
    seen: list[httpx.Request] = []
    transport = site({"arxiv.org/abs/2501.01234": html("[2501.01234] A Paper")}, seen)
    checked = xhs_fallback.check_arxiv_page("2501.01234", transport=transport, resolver=resolver)
    assert (checked.check, checked.requested_url, checked.title) == (
        "arxiv", "https://arxiv.org/abs/2501.01234", "[2501.01234] A Paper",
    )
    for value in ("2501.01234v2", "../x", "arXiv:2501.01234", 2501):
        with pytest.raises(ValueError):
            xhs_fallback.check_arxiv_page(value, transport=transport, resolver=resolver)
    assert len(seen) == 1


def test_a_refused_or_failed_fetch_leaves_as_its_provider_error() -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(PolicyRefusal):
        xhs_fallback.check_blog_page(
            "https://intranet.example/post", transport=site({}, seen), resolver=resolver
        )
    assert seen == []
    failing = httpx.MockTransport(lambda request: httpx.Response(503))
    with pytest.raises(ProviderError) as caught:
        xhs_fallback.check_blog_page("https://blog.example/post", transport=failing,
                                     resolver=resolver)
    assert caught.value.category == "transient"
