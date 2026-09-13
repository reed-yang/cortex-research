"""Request authorization and proxy-header boundaries for private access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .config import AccessConfig


_HOP_BY_HOP = {
    "connection",
    "content-length",
    "expect",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_UNTRUSTED_PROXY_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
    "x-cortex-access-bootstrap",
}
_TAILSCALE_HEADERS = {
    "tailscale-app-capabilities",
    "tailscale-user-login",
    "tailscale-user-name",
    "tailscale-user-profile-pic",
}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    status: int
    category: str
    upstream_headers: tuple[tuple[str, str], ...] = ()


def _headers(raw: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in raw.items():
        key = name.casefold()
        if key in normalized:
            raise ValueError("duplicate header")
        normalized[key] = value.strip()
    return normalized


def _has_capability(headers: Mapping[str, str], config: AccessConfig) -> bool:
    raw = headers.get("tailscale-app-capabilities")
    if raw is None or len(raw.encode("utf-8")) > 16_384:
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(payload, dict) or set(payload) != {config.identity.app_capability}:
        return False
    entries = payload[config.identity.app_capability]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        return False
    allowed_roles = set(config.identity.allowed_capability_roles)
    for entry in entries:
        if (
            isinstance(entry, dict)
            and set(entry) == {"role"}
            and isinstance(entry["role"], str)
            and entry["role"] in allowed_roles
        ):
            return True
    return False


def _identity_allowed(headers: Mapping[str, str], config: AccessConfig) -> bool:
    if not _has_capability(headers, config):
        return False
    login = headers.get("tailscale-user-login")
    if login is not None:
        return login.casefold() in config.identity.allowed_logins and login == login.casefold()
    return any(source.startswith("tag:") for source in config.identity.allowed_sources)


def _safe_target(target: str) -> bool:
    if not target.startswith("/") or target.startswith("//"):
        return False
    parsed = urlsplit(target)
    return (
        not parsed.scheme
        and not parsed.netloc
        and "\0" not in target
        and "\r" not in target
        and "\n" not in target
    )


def _sanitize_headers(headers: Mapping[str, str], config: AccessConfig) -> tuple[tuple[str, str], ...]:
    connection_tokens = {
        item.strip().casefold()
        for item in headers.get("connection", "").split(",")
        if item.strip()
    }
    blocked = _HOP_BY_HOP | _UNTRUSTED_PROXY_HEADERS | _TAILSCALE_HEADERS | connection_tokens
    forwarded = [
        (name, value)
        for name, value in headers.items()
        if name not in blocked and name != "host"
    ]
    forwarded.extend(
        (
            ("host", config.public_hostname),
            ("x-forwarded-host", config.public_hostname),
            ("x-forwarded-proto", "https"),
        )
    )
    return tuple(sorted(forwarded))


def evaluate_request(
    *,
    method: str,
    target: str,
    headers: Mapping[str, str],
    client_host: str,
    config: AccessConfig,
) -> AccessDecision:
    """Authorize one Serve-proxied request and produce safe upstream headers."""

    if client_host not in {"127.0.0.1", "::1"}:
        return AccessDecision(False, 403, "non_loopback_proxy")
    if method != method.upper() or method not in {
        "GET",
        "HEAD",
        "OPTIONS",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        return AccessDecision(False, 405, "method_rejected")
    if not _safe_target(target):
        return AccessDecision(False, 400, "target_rejected")
    try:
        normalized = _headers(headers)
    except ValueError:
        return AccessDecision(False, 400, "duplicate_header")
    if normalized.get("host") != config.public_hostname:
        return AccessDecision(False, 421, "host_rejected")
    if not _identity_allowed(normalized, config):
        return AccessDecision(False, 403, "identity_rejected")

    origin = normalized.get("origin")
    fetch_site = normalized.get("sec-fetch-site")
    if origin is not None and origin != config.public_origin:
        return AccessDecision(False, 403, "origin_rejected")
    if fetch_site not in {None, "none", "same-origin"}:
        return AccessDecision(False, 403, "fetch_site_rejected")
    if method in _MUTATING_METHODS and (
        origin != config.public_origin
        or fetch_site != "same-origin"
        or normalized.get("x-cortex-web-client") != "v1"
    ):
        return AccessDecision(False, 403, "csrf_rejected")
    return AccessDecision(
        True,
        200,
        "authorized",
        _sanitize_headers(normalized, config),
    )
