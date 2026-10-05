"""Shared HTTP rules for the first-party provider clients.

`xhs_client`, `image_ocr`, `responses_client` and `blog_fetch` run only inside
an engine child, which imports them lazily per operation. None of the nine
arXiv bridge modules imports this module or them.

Every failure leaves as a `ProviderError` carrying one category from
`CATEGORIES`. Its message names the provider, the HTTP status or the refused
property, and never a credential, a signed URL, a header or a response body.
Each client builds its own `httpx.Client` with `trust_env=False`, so no proxy
variable, `.netrc` or certificate override from the environment is read, and
with no cookie persistence across requests.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

CATEGORIES: frozenset[str] = frozenset(
    {
        "auth",
        "payment",
        "rate_limited",
        "transient",
        "upstream_error",
        "not_found",
        "invalid_response",
        "url_expired",
    }
)
# Retried by the task queue later; never by a loop inside a client.
RETRYABLE: frozenset[str] = frozenset({"rate_limited", "transient"})

DEFAULT_PORTS = {"http": 80, "https": 443}
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
# One generic agent string; nothing about the operator or the installation.
USER_AGENT = "Mozilla/5.0 (compatible; CortexResearch/1.0)"

Resolver = Callable[..., Iterable[Any]]


class ProviderError(Exception):
    """A provider call that did not produce a usable answer."""

    def __init__(self, category: str, message: str, *, status: int | None = None) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"unsupported provider failure category: {category}")
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "message": self.message, "status": self.status}


class PolicyRefusal(ProviderError):
    """A URL or address the public-web fetch policy refuses.

    Always `not_found`, and never a reason to try another route to the same
    resource.
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__("not_found", f"{provider}: refused: {reason}")


def status_category(status: int) -> str | None:
    """Map an API status onto a category; `None` for a success."""

    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "payment"
    if status == 429:
        return "rate_limited"
    if status in (404, 410):
        return "not_found"
    if status == 408 or status >= 500:
        return "transient"
    return "upstream_error"


class Deadline:
    """An aggregate wall-clock budget shared by every request of one call."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + float(seconds)

    def remaining(self) -> float:
        return self._end - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def timeout(self, cap: float, *, provider: str) -> httpx.Timeout:
        """One request's timeout: its own cap, never past the deadline."""

        remaining = self.remaining()
        if remaining <= 0:
            raise ProviderError("transient", f"{provider}: deadline exceeded")
        return httpx.Timeout(min(float(cap), remaining))


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A client that reads nothing from the environment and follows nothing."""

    return httpx.Client(transport=transport, trust_env=False, follow_redirects=False)


def read_bounded(
    response: httpx.Response, limit: int, *, provider: str, deadline: Deadline | None = None
) -> bytes:
    """Read a streamed body, refusing it once it passes `limit` bytes."""

    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ProviderError("invalid_response", f"{provider}: body exceeds {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ProviderError("invalid_response", f"{provider}: body exceeds {limit} bytes")
        chunks.append(chunk)
        if deadline is not None and deadline.expired():
            raise ProviderError("transient", f"{provider}: deadline exceeded")
    return b"".join(chunks)


def request_json(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    timeout: httpx.Timeout,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    deadline: Deadline | None = None,
) -> Any:
    """One API request whose answer must be a JSON document."""

    try:
        with http.stream(
            method,
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
            params=params,
            json=json_body,
            timeout=timeout,
        ) as response:
            category = status_category(response.status_code)
            if category is not None:
                raise ProviderError(
                    category,
                    f"{provider}: HTTP {response.status_code}",
                    status=response.status_code,
                )
            body = read_bounded(response, max_bytes, provider=provider, deadline=deadline)
    except httpx.TimeoutException as error:
        raise ProviderError("transient", f"{provider}: request timed out") from error
    except httpx.TransportError as error:
        raise ProviderError(
            "transient", f"{provider}: connection failed ({type(error).__name__})"
        ) from error
    try:
        return json.loads(body)
    except ValueError as error:
        raise ProviderError("invalid_response", f"{provider}: answer is not JSON") from error


# -- public-web fetching ------------------------------------------------------


def public_address(value: str) -> bool:
    """Whether one resolved address is a globally routable unicast address."""

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_unspecified
    )


def check_public_url(url: str, *, provider: str) -> tuple[str, str, str]:
    """Refuse every URL the public-web fetch policy does not admit.

    Returns the lowercased scheme, the lowercased host and the URL rebuilt
    without a fragment. http and https only, default ports only, and no
    credentials.
    """

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise PolicyRefusal(provider, "malformed URL") from error
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise PolicyRefusal(provider, "scheme is not http or https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise PolicyRefusal(provider, "URL carries credentials")
    host = (parts.hostname or "").lower()
    if not host:
        raise PolicyRefusal(provider, "URL has no host")
    if port is not None and port != DEFAULT_PORTS[scheme]:
        raise PolicyRefusal(provider, "non-default port")
    rebuilt = urlunsplit((scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))
    return scheme, host, rebuilt


def resolve_public(host: str, scheme: str, *, provider: str, resolver: Resolver) -> str:
    """Resolve `host` once and return an address to connect to.

    Every address the host resolves to has to be public: a name with one
    private answer among public ones is refused rather than raced.
    """

    try:
        ipaddress.ip_address(host.strip("[]"))
        addresses = [host.strip("[]")]
    except ValueError:
        try:
            infos = list(resolver(host, DEFAULT_PORTS[scheme], type=socket.SOCK_STREAM))
        except socket.gaierror as error:
            if error.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)):
                raise ProviderError("not_found", f"{provider}: host does not resolve") from error
            raise ProviderError("transient", f"{provider}: name resolution failed") from error
        except OSError as error:
            raise ProviderError("transient", f"{provider}: name resolution failed") from error
        addresses = [str(info[4][0]) for info in infos]
    if not addresses:
        raise ProviderError("not_found", f"{provider}: host does not resolve")
    for address in addresses:
        if not public_address(address):
            raise PolicyRefusal(provider, "non-public address")
    return addresses[0].split("%", 1)[0]


@dataclass(frozen=True)
class FetchedPage:
    """One public-web response after every redirect was checked."""

    url: str
    status: int
    content_type: str
    body: bytes
    redirects: tuple[str, ...] = field(default_factory=tuple)


def safe_get(
    url: str,
    *,
    provider: str,
    max_bytes: int,
    deadline: Deadline,
    request_timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
    accept: str = "*/*",
    status_map: Callable[[int], str | None] = status_category,
) -> FetchedPage:
    """GET a public-web URL under the fetch policy.

    Each hop is checked, resolved, and connected to by its resolved address,
    with the original host as `Host` and as the TLS server name, so a second
    resolution cannot move the connection. Redirects are followed by hand, at
    most `MAX_REDIRECTS`, each checked again. No cookie and no credential is
    ever sent, so nothing can cross an origin.
    """

    redirects: list[str] = []
    current = url
    with client(transport) as http:
        while True:
            scheme, host, current = check_public_url(current, provider=provider)
            address = resolve_public(host, scheme, provider=provider, resolver=resolver)
            parts = urlsplit(current)
            literal = f"[{address}]" if ":" in address else address
            pinned = urlunsplit((scheme, literal, parts.path or "/", parts.query, ""))
            request = http.build_request(
                "GET",
                pinned,
                headers={
                    "Host": parts.netloc,
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                },
                timeout=deadline.timeout(request_timeout, provider=provider),
                extensions={"sni_hostname": host} if scheme == "https" else None,
            )
            http.cookies.clear()
            try:
                response = http.send(request, stream=True)
            except httpx.TimeoutException as error:
                raise ProviderError("transient", f"{provider}: request timed out") from error
            except httpx.TransportError as error:
                raise ProviderError(
                    "transient", f"{provider}: connection failed ({type(error).__name__})"
                ) from error
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderError(
                            "upstream_error", f"{provider}: redirect without a location"
                        )
                    if len(redirects) >= MAX_REDIRECTS:
                        raise ProviderError("upstream_error", f"{provider}: too many redirects")
                    redirects.append(current)
                    current = urljoin(current, location)
                    continue
                category = status_map(response.status_code)
                if category is not None:
                    raise ProviderError(
                        category,
                        f"{provider}: HTTP {response.status_code}",
                        status=response.status_code,
                    )
                try:
                    body = read_bounded(
                        response, max_bytes, provider=provider, deadline=deadline
                    )
                except httpx.TimeoutException as error:
                    raise ProviderError("transient", f"{provider}: request timed out") from error
                except httpx.TransportError as error:
                    raise ProviderError("transient", f"{provider}: body read failed") from error
                return FetchedPage(
                    url=current,
                    status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    body=body,
                    redirects=tuple(redirects),
                )
            finally:
                response.close()
