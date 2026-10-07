"""Public-web blog fetching: the origin page through trafilatura, then Jina.

The origin fetch follows `provider_http.safe_get`: http and https on default
ports only, every hop resolved and held to public addresses, at most five
redirects, no cookies or credentials, 20 s per request and 5 MiB of HTML.
When the origin fetch fails, is not HTML, or trafilatura extracts under 200
characters, Jina Reader (`https://r.jina.ai/<url>`, key optional) is tried and
recorded as `content_source="jina"`. A URL the policy refuses is never handed
to Jina instead. Raw HTML is returned only when the origin fetch succeeded.

The article's own images are then copied under the same policy, so the stored
version shows them without loading anything remote.
"""

from __future__ import annotations

import hashlib
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from .provider_http import (
    Deadline,
    FetchedPage,
    PolicyRefusal,
    ProviderError,
    Resolver,
    USER_AGENT,
    client,
    read_bounded,
    safe_get,
    status_category,
)

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_TITLE_PAGE_BYTES = 2 * 1024 * 1024
MAX_JINA_BYTES = 5 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
DEADLINE_SECONDS = 120.0
MIN_ARTICLE_CHARACTERS = 200
JINA_BASE = "https://r.jina.ai"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ARTICLE_IMAGES = 60
MAX_ARTICLE_IMAGE_BYTES = 60 * 1024 * 1024
IMAGE_DEADLINE_SECONDS = 120.0
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_META_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?([A-Za-z0-9._-]{1,40})""", re.IGNORECASE)
# `![alt](src)` or `![alt](src "title")`, as trafilatura and Jina write images.
_MARKDOWN_IMAGE_RE = re.compile(r'!\[([^\]\n]*)\]\(\s*<?([^\s()<>]+)>?((?:\s+"[^"\n]*")?)\s*\)')
_IMAGE_ACCEPT = "image/png,image/jpeg,image/gif,image/webp;q=0.9,*/*;q=0.1"


def _site_status(status: int) -> str | None:
    """A blog host refusing us is that page's problem, never a credential's.

    `auth` would stop the whole drain tick, so a 401/403/451 from an arbitrary
    site is `upstream_error` instead.
    """

    category = status_category(status)
    return "upstream_error" if category in ("auth", "payment") else category


@dataclass(frozen=True)
class BlogImage:
    """One copied article image, stored as `assets/<name>`."""

    name: str  # page-<NN>-<first 12 hex of its SHA-256>.<ext>
    url: str
    data: bytes


@dataclass(frozen=True)
class BlogArticle:
    requested_url: str
    final_url: str
    content_source: str  # "origin" or "jina"
    title: str | None
    author: str | None
    date: str | None
    markdown: str
    page_html: bytes | None
    jina_text: str | None
    origin_failure: Mapping[str, Any] | None
    images: tuple[BlogImage, ...] = ()
    images_not_copied: int = 0

    def metadata(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "content_source": self.content_source,
            "title": self.title,
            "author": self.author,
            "date": self.date,
            "characters": len(self.markdown),
            "raw_html": self.page_html is not None,
            "raw_jina": self.jina_text is not None,
            "origin_failure": dict(self.origin_failure) if self.origin_failure else None,
            "images": len(self.images),
            "images_not_copied": self.images_not_copied,
        }


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.og_title: str | None = None
        self._in_title = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "meta" and self.og_title is None:
            values = {name.lower(): value or "" for name, value in attrs}
            if values.get("property", "").lower() == "og:title" or values.get(
                "name", ""
            ).lower() == "og:title":
                content = " ".join(values.get("content", "").split())
                self.og_title = content or None

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            text = " ".join("".join(self._parts).split())
            self.title = text or None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._parts.append(data)


def page_titles(html: str) -> tuple[str | None, str | None]:
    """The page's `<title>` and `og:title`, whitespace-collapsed."""

    parser = _TitleParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page simply has no title
        pass
    return parser.title, parser.og_title


def decode_html(body: bytes, content_type: str) -> str:
    """Decode by the declared charset, else a `<meta charset>`, else UTF-8."""

    charset = None
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip("\"'")
    if charset is None:
        match = _META_CHARSET_RE.search(body[:4096])
        charset = match.group(1).decode("ascii") if match else None
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def _is_html(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in _HTML_TYPES


def fetch_page(
    url: str,
    *,
    max_bytes: int = MAX_HTML_BYTES,
    deadline: Deadline | None = None,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> FetchedPage:
    """The origin page under the public-web policy."""

    return safe_get(
        url,
        provider="blog",
        max_bytes=max_bytes,
        deadline=deadline or Deadline(DEADLINE_SECONDS),
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        transport=transport,
        resolver=resolver,
        accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        status_map=_site_status,
    )


def fetch_title(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
    deadline: Deadline | None = None,
) -> tuple[str, str | None, str | None]:
    """Link verification: at most 2 MiB, then `<title>` and `og:title`."""

    page = fetch_page(
        url,
        max_bytes=MAX_TITLE_PAGE_BYTES,
        deadline=deadline,
        transport=transport,
        resolver=resolver,
    )
    if not _is_html(page.content_type):
        raise ProviderError("invalid_response", "blog: page is not HTML")
    title, og_title = page_titles(decode_html(page.body, page.content_type))
    return page.url, title, og_title


def extract_article(html: str, url: str) -> tuple[str, dict[str, str | None]]:
    """trafilatura's Markdown body and its title, author and date."""

    import trafilatura
    from trafilatura.metadata import extract_metadata

    body = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_images=True,
        include_comments=False,
        with_metadata=False,
    )
    metadata = extract_metadata(html, default_url=url)
    title, og_title = page_titles(html)
    found = {
        "title": (metadata.title if metadata else None) or og_title or title,
        "author": metadata.author if metadata else None,
        "date": metadata.date if metadata else None,
    }
    return (body or "").strip(), found


def _parse_jina(text: str) -> tuple[str | None, str | None, str]:
    """Jina's `Title:` / `Published Time:` header lines and the body after
    `Markdown Content:`; the whole text when that marker is absent."""

    title = date = None
    head, marker, body = text.partition("Markdown Content:")
    if not marker:
        return None, None, text.strip()
    for line in head.splitlines():
        name, _, value = line.partition(":")
        if name.strip() == "Title" and value.strip():
            title = value.strip()
        elif name.strip() == "Published Time" and value.strip():
            date = value.strip()
    return title, date, body.strip()


def jina_read(
    url: str,
    *,
    api_key: str | None = None,
    base: str = JINA_BASE,
    deadline: Deadline | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Jina Reader's text for `url`. The key, when given, goes to Jina only."""

    deadline = deadline or Deadline(DEADLINE_SECONDS)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/plain"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with client(transport) as http:
        try:
            with http.stream(
                "GET",
                base.rstrip("/") + "/" + url,
                headers=headers,
                timeout=deadline.timeout(REQUEST_TIMEOUT_SECONDS * 3, provider="jina"),
            ) as response:
                category = status_category(response.status_code)
                if category is not None:
                    raise ProviderError(
                        category, f"jina: HTTP {response.status_code}", status=response.status_code
                    )
                body = read_bounded(response, MAX_JINA_BYTES, provider="jina", deadline=deadline)
        except httpx.TimeoutException as error:
            raise ProviderError("transient", "jina: request timed out") from error
        except httpx.TransportError as error:
            raise ProviderError("transient", "jina: connection failed") from error
    return body.decode("utf-8", errors="replace")


def fetch_blog(
    url: str,
    *,
    jina_key: str | None = None,
    jina_base: str = JINA_BASE,
    transport: httpx.BaseTransport | None = None,
    jina_transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
    deadline_seconds: float = DEADLINE_SECONDS * 2,
) -> BlogArticle:
    """Fetch and extract one blog post, with the Jina fallback."""

    deadline = Deadline(deadline_seconds)
    origin_failure: dict[str, Any] | None = None
    page_html: bytes | None = None
    final_url = url
    try:
        page = fetch_page(url, deadline=deadline, transport=transport, resolver=resolver)
    except PolicyRefusal:
        raise
    except ProviderError as error:
        origin_failure = {"category": error.category, "message": error.message}
    else:
        final_url = page.url
        if not _is_html(page.content_type):
            origin_failure = {"category": "invalid_response", "message": "blog: page is not HTML"}
        else:
            page_html = page.body
            html = decode_html(page.body, page.content_type)
            try:
                markdown, found = extract_article(html, page.url)
            except Exception as error:  # noqa: BLE001 - extraction failure falls back
                markdown, found = "", {}
                origin_failure = {
                    "category": "invalid_response",
                    "message": f"blog: extraction failed ({type(error).__name__})",
                }
            if len(markdown) >= MIN_ARTICLE_CHARACTERS:
                markdown, images, not_copied = copy_images(
                    markdown, page.url, transport=transport, resolver=resolver
                )
                return BlogArticle(
                    requested_url=url,
                    final_url=page.url,
                    content_source="origin",
                    title=found.get("title"),
                    author=found.get("author"),
                    date=found.get("date"),
                    markdown=markdown,
                    page_html=page_html,
                    jina_text=None,
                    origin_failure=None,
                    images=images,
                    images_not_copied=not_copied,
                )
            if origin_failure is None:
                origin_failure = {
                    "category": "invalid_response",
                    "message": f"blog: extraction produced {len(markdown)} characters",
                }
    text = jina_read(
        final_url, api_key=jina_key, base=jina_base, deadline=deadline, transport=jina_transport
    )
    title, date, markdown = _parse_jina(text)
    if len(markdown) < MIN_ARTICLE_CHARACTERS:
        raise ProviderError(
            "invalid_response", f"jina: reader produced {len(markdown)} characters"
        )
    markdown, images, not_copied = copy_images(
        markdown, final_url, transport=transport, resolver=resolver
    )
    return BlogArticle(
        requested_url=url,
        final_url=final_url,
        content_source="jina",
        title=title,
        author=None,
        date=date,
        markdown=markdown,
        page_html=page_html,
        jina_text=text,
        origin_failure=origin_failure,
        images=images,
        images_not_copied=not_copied,
    )


def _image_extension(data: bytes) -> str | None:
    """PNG, JPEG, GIF or WebP by signature; the reader serves nothing else."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def copy_images(
    markdown: str,
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
    deadline: Deadline | None = None,
) -> tuple[str, tuple[BlogImage, ...], int]:
    """Copy the article's images and point its Markdown at the copies.

    Each image source is resolved against the page URL and fetched under the
    page's own policy, once per URL. A PNG, JPEG, GIF or WebP of at most
    `MAX_IMAGE_BYTES` is copied, up to `MAX_ARTICLE_IMAGES` images and
    `MAX_ARTICLE_IMAGE_BYTES` in all within `IMAGE_DEADLINE_SECONDS`, and its
    reference becomes `assets/<name>`. Any other image keeps its absolute URL,
    which the reader names but never loads. A failed image never fails the
    article. Returns the Markdown, the copies, and how many image URLs were not
    copied.
    """

    deadline = deadline or Deadline(IMAGE_DEADLINE_SECONDS)
    copies: dict[str, BlogImage | None] = {}
    images: list[BlogImage] = []
    total = 0

    def copy(url: str) -> BlogImage | None:
        if urlsplit(url).scheme not in ("http", "https"):
            return None
        room = MAX_ARTICLE_IMAGE_BYTES - total
        if len(images) >= MAX_ARTICLE_IMAGES or room <= 0 or deadline.expired():
            return None
        try:
            fetched = safe_get(
                url,
                provider="blog",
                max_bytes=min(MAX_IMAGE_BYTES, room),
                deadline=deadline,
                request_timeout=REQUEST_TIMEOUT_SECONDS,
                transport=transport,
                resolver=resolver,
                accept=_IMAGE_ACCEPT,
                status_map=_site_status,
            )
        except ProviderError:
            return None
        extension = _image_extension(fetched.body)
        if extension is None:
            return None
        digest = hashlib.sha256(fetched.body).hexdigest()[:12]
        return BlogImage(f"page-{len(images) + 1:02d}-{digest}.{extension}", url, fetched.body)

    def replace(match: re.Match[str]) -> str:
        nonlocal total
        alt, source, title = match.groups()
        url = urljoin(base_url, source)
        if url not in copies:
            copies[url] = copy(url)
            if copies[url] is not None:
                images.append(copies[url])
                total += len(copies[url].data)
        image = copies[url]
        return f"![{alt}]({f'assets/{image.name}' if image else url}{title})"

    text = _MARKDOWN_IMAGE_RE.sub(replace, markdown)
    return text, tuple(images), len(copies) - len(images)


def article_markdown(article: BlogArticle) -> str:
    """`article.md`: the title, the known byline fields, then the body."""

    lines = [f"# {article.title}" if article.title else "# Untitled article", ""]
    if article.author:
        lines.append(f"Author: {article.author}  ")
    if article.date:
        lines.append(f"Date: {article.date}  ")
    lines.append(f"Source: <{article.final_url}>")
    lines.extend(["", article.markdown.strip(), ""])
    return "\n".join(lines)
