"""Verification of a weekly-fallback proposal: one page title, fetched by Cortex.

A model may propose a blog's link or a paper's arXiv ID; neither is applied
until the page it names is fetched here and its title checked by the caller.
Both checks reuse `blog_fetch.fetch_title`, so its public-web policy holds:
http and https on default ports, every hop resolved and held to public
addresses, at most five redirects, no credentials and at most 2 MiB.

A proposed blog link on a paper host (arXiv, OpenReview, DOI, ACL Anthology)
or ending in `.pdf` is never fetched, and one that redirects there is marked:
a paper page is never imported as a blog. A refused or failed fetch leaves as
the `ProviderError` the fetch raised. Nothing here calls a model, reads a
credential or writes a file.
"""

from __future__ import annotations

import re
import socket
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import blog_fetch
from .provider_http import Deadline, Resolver

#: Hosts whose pages are papers, with their subdomains. The product keeps an
#: equal copy (`cortex_platform.product.xhs.fallback.PAPER_HOSTS`).
PAPER_HOSTS = frozenset({"arxiv.org", "openreview.net", "doi.org", "aclanthology.org"})
ARXIV_ABS_BASE = "https://arxiv.org/abs"
DEADLINE_SECONDS = 60.0
_ARXIV_ID_RE = re.compile(r"[0-9]{4}\.[0-9]{4,5}\Z")


def is_paper_url(url: str) -> bool:
    """Whether a URL is a paper host's page or a PDF."""

    split = urlsplit(url)
    host = (split.hostname or "").lower()
    return split.path.lower().endswith(".pdf") or any(
        host == name or host.endswith("." + name) for name in PAPER_HOSTS
    )


@dataclass(frozen=True)
class PageCheck:
    """One fetched page's titles, for the caller's title match.

    `final_url` and both titles are None when the page was not fetched
    because the requested link is a paper page.
    """

    check: str  # "blog" or "arxiv"
    requested_url: str
    final_url: str | None
    title: str | None
    og_title: str | None
    paper_host: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_blog_page(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> PageCheck:
    """The titles of a proposed blog page, unless it is a paper page."""

    if is_paper_url(url):
        return PageCheck("blog", url, None, None, None, True)
    final_url, title, og_title = blog_fetch.fetch_title(
        url, transport=transport, resolver=resolver, deadline=Deadline(DEADLINE_SECONDS)
    )
    return PageCheck("blog", url, final_url, title, og_title, is_paper_url(final_url))


def check_arxiv_page(
    arxiv_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> PageCheck:
    """The titles of the arXiv abs page of a proposed, versionless ID."""

    if not isinstance(arxiv_id, str) or _ARXIV_ID_RE.fullmatch(arxiv_id) is None:
        raise ValueError("arXiv identifier is invalid")
    url = f"{ARXIV_ABS_BASE}/{arxiv_id}"
    final_url, title, og_title = blog_fetch.fetch_title(
        url, transport=transport, resolver=resolver, deadline=Deadline(DEADLINE_SECONDS)
    )
    return PageCheck("arxiv", url, final_url, title, og_title, False)
