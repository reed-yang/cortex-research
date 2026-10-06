"""Blog fetch: trafilatura extraction, the Jina fallback and its limits."""

from __future__ import annotations

import socket

import httpx
import pytest

from cortex_research import blog_fetch
from cortex_research.provider_http import PolicyRefusal, ProviderError

PARAGRAPHS = [
    "Diffusion models learn to reverse a gradual noising process over many steps.",
    "This post walks through the score matching objective and its practical variants.",
    "We then compare samplers, step counts and guidance strengths on small images.",
    "Finally we discuss why classifier-free guidance trades diversity for fidelity.",
    "扩散模型通过逐步去噪生成图像，本文用简单的例子解释其中的关键公式。",
]
ARTICLE_HTML = (
    "<html><head><title>Understanding Diffusion | Synthetic Blog</title>"
    "<meta property='og:title' content='Understanding Diffusion'>"
    "<meta name='author' content='Ada Example'>"
    "<meta property='article:published_time' content='2026-09-01'></head><body>"
    "<nav>Home About</nav><article><h1>Understanding Diffusion</h1>"
    + "".join(f"<p>{text}</p>" for text in PARAGRAPHS)
    + "</article><footer>footer links</footer></body></html>"
)
JINA_TEXT = (
    "Title: Understanding Diffusion\n"
    "URL Source: https://blog.example/post\n"
    "Published Time: 2026-09-01\n\n"
    "Markdown Content:\n" + "\n\n".join(PARAGRAPHS)
)


def resolver(host: str, port: int, type: int = 0):  # noqa: A002
    table = {"blog.example": "93.184.216.34", "moved.example": "93.184.216.35",
             "intranet.example": "172.16.5.5"}
    if host not in table:
        raise socket.gaierror(socket.EAI_NONAME, "unknown")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]


class Site:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def html(body: str = ARTICLE_HTML, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def fetch(site: Site, jina: Site | None = None, **kwargs) -> blog_fetch.BlogArticle:
    return blog_fetch.fetch_blog(
        kwargs.pop("url", "https://blog.example/post"),
        transport=httpx.MockTransport(site),
        jina_transport=httpx.MockTransport(jina) if jina else None,
        resolver=resolver,
        **kwargs,
    )


def test_an_article_is_extracted_from_the_origin_page() -> None:
    site = Site(html())
    article = fetch(site)
    assert article.content_source == "origin"
    assert article.title == "Understanding Diffusion"
    assert article.author == "Ada Example"
    assert article.final_url == "https://blog.example/post"
    assert "score matching objective" in article.markdown
    assert "footer links" not in article.markdown
    assert article.page_html == ARTICLE_HTML.encode()
    assert article.jina_text is None and article.origin_failure is None
    rendered = blog_fetch.article_markdown(article)
    assert rendered.startswith("# Understanding Diffusion\n")
    assert "Source: <https://blog.example/post>" in rendered


def test_a_short_extraction_falls_back_to_jina_and_says_so() -> None:
    site = Site(html("<html><head><title>Tiny</title></head><body><p>Too short.</p></body></html>"))
    jina = Site(httpx.Response(200, text=JINA_TEXT))
    article = fetch(site, jina, jina_key="jina-key")
    assert article.content_source == "jina"
    assert article.title == "Understanding Diffusion" and article.date == "2026-09-01"
    assert article.markdown.startswith(PARAGRAPHS[0])
    assert article.jina_text == JINA_TEXT
    # The origin page was fetched, so its HTML is real and kept.
    assert article.page_html is not None
    assert article.origin_failure["category"] == "invalid_response"
    request = jina.requests[0]
    assert str(request.url) == "https://r.jina.ai/https://blog.example/post"
    assert request.headers["authorization"] == "Bearer jina-key"
    # The Jina key never reaches the blog's own host.
    assert all("authorization" not in r.headers for r in site.requests)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_an_origin_failure_falls_back_to_jina_without_claiming_html(status: int) -> None:
    site = Site(html("blocked", status=status))
    jina = Site(httpx.Response(200, text=JINA_TEXT))
    article = fetch(site, jina)
    assert article.content_source == "jina"
    assert article.page_html is None
    assert "authorization" not in jina.requests[0].headers
    expected = {403: "upstream_error", 404: "not_found", 500: "transient"}[status]
    assert article.origin_failure["category"] == expected


def test_a_non_html_page_falls_back_to_jina() -> None:
    site = Site(httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}))
    jina = Site(httpx.Response(200, text=JINA_TEXT))
    article = fetch(site, jina)
    assert article.content_source == "jina" and article.page_html is None


def test_a_redirect_to_a_private_address_is_refused_and_never_sent_to_jina() -> None:
    site = Site(httpx.Response(302, headers={"location": "https://intranet.example/admin"}))
    jina = Site()
    with pytest.raises(PolicyRefusal):
        fetch(site, jina)
    assert jina.requests == []


@pytest.mark.parametrize(
    "url", ["ftp://blog.example/post", "https://blog.example:8443/post", "https://u:p@blog.example/"]
)
def test_unsafe_urls_are_refused_before_any_request(url: str) -> None:
    site, jina = Site(), Site()
    with pytest.raises(PolicyRefusal):
        fetch(site, jina, url=url)
    assert site.requests == [] and jina.requests == []


def test_an_oversized_page_falls_back_to_jina() -> None:
    huge = "<html><body>" + "x" * (blog_fetch.MAX_HTML_BYTES + 10) + "</body></html>"
    site = Site(html(huge))
    jina = Site(httpx.Response(200, text=JINA_TEXT))
    article = fetch(site, jina)
    assert article.content_source == "jina" and article.page_html is None
    assert article.origin_failure["category"] == "invalid_response"


def test_a_failing_jina_reports_its_own_category() -> None:
    site = Site(html("gone", status=404))
    jina = Site(httpx.Response(429))
    with pytest.raises(ProviderError) as caught:
        fetch(site, jina)
    assert caught.value.category == "rate_limited"


def test_a_short_jina_answer_is_invalid() -> None:
    site = Site(html("gone", status=404))
    jina = Site(httpx.Response(200, text="Title: x\n\nMarkdown Content:\nshort"))
    with pytest.raises(ProviderError) as caught:
        fetch(site, jina)
    assert caught.value.category == "invalid_response"


def test_redirects_are_followed_and_the_final_url_recorded() -> None:
    site = Site(httpx.Response(301, headers={"location": "https://moved.example/post"}), html())
    article = fetch(site)
    assert article.final_url == "https://moved.example/post"
    assert article.requested_url == "https://blog.example/post"
    assert site.requests[1].headers["host"] == "moved.example"


def test_fetch_title_reads_title_and_og_title_within_two_mebibytes() -> None:
    site = Site(html())
    url, title, og_title = blog_fetch.fetch_title(
        "https://blog.example/post", transport=httpx.MockTransport(site), resolver=resolver
    )
    assert url == "https://blog.example/post"
    assert title == "Understanding Diffusion | Synthetic Blog"
    assert og_title == "Understanding Diffusion"

    big = Site(html("<title>x</title>" + "y" * (blog_fetch.MAX_TITLE_PAGE_BYTES + 1)))
    with pytest.raises(ProviderError) as caught:
        blog_fetch.fetch_title(
            "https://blog.example/post", transport=httpx.MockTransport(big), resolver=resolver
        )
    assert caught.value.category == "invalid_response"


def test_html_is_decoded_by_its_declared_charset() -> None:
    body = "<meta charset='gbk'><title>中文标题</title>".encode("gbk")
    assert blog_fetch.page_titles(blog_fetch.decode_html(body, "text/html"))[0] == "中文标题"
    assert blog_fetch.decode_html("é".encode("latin-1"), "text/html; charset=latin-1") == "é"
