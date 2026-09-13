"""Strict configuration model for private Cortex remote access."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
_TOP_LEVEL_KEYS = {
    "schema_version",
    "provider",
    "public_origin",
    "access_gateway",
    "web_upstream",
    "daemon_upstream",
    "identity",
    "session_bootstrap_secret_ref",
}
_IDENTITY_KEYS = {
    "allowed_logins",
    "allowed_sources",
    "service_tag",
    "tag_owners",
    "app_capability",
    "allowed_capability_roles",
}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_TAG = re.compile(r"^tag:[a-z][a-z0-9-]{0,62}$")
_GROUP = re.compile(r"^group:[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,126}$")
_CAPABILITY = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?"
    r"(?:/[a-zA-Z0-9][a-zA-Z0-9._/-]{0,126})$"
)
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_KEYCHAIN_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class ConfigError(ValueError):
    """Raised when a private-access configuration fails closed validation."""


@dataclass(frozen=True)
class LoopbackEndpoint:
    host: str
    port: int

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.authority}"


@dataclass(frozen=True)
class IdentityPolicy:
    allowed_logins: tuple[str, ...]
    allowed_sources: tuple[str, ...]
    service_tag: str
    tag_owners: tuple[str, ...]
    app_capability: str
    allowed_capability_roles: tuple[str, ...]


@dataclass(frozen=True)
class AccessConfig:
    schema_version: int
    provider: str
    public_origin: str
    public_hostname: str
    access_gateway: LoopbackEndpoint
    web_upstream: LoopbackEndpoint
    daemon_upstream: LoopbackEndpoint
    identity: IdentityPolicy
    session_bootstrap_secret_ref: str

    @property
    def public_port(self) -> int:
        return 443


def _require_mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _require_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ConfigError(f"{name} must be a non-empty canonical string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ConfigError(f"{name} contains a control character")
    return value


def _string_list(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty array")
    items = tuple(_require_string(item, name=name) for item in value)
    if len(set(items)) != len(items):
        raise ConfigError(f"{name} contains duplicate values")
    return items


def _parse_loopback(value: object, *, name: str) -> LoopbackEndpoint:
    raw = _require_string(value, name=name)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name} must be a canonical loopback HTTP URL") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.netloc != f"127.0.0.1:{port}"
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise ConfigError(f"{name} must be http://127.0.0.1:<port>")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ConfigError(f"{name} must be loopback-only")
    except ValueError as exc:
        raise ConfigError(f"{name} must be loopback-only") from exc
    return LoopbackEndpoint("127.0.0.1", port)


def _parse_origin(value: object) -> tuple[str, str]:
    raw = _require_string(value, name="public_origin")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("public_origin must be an exact tailnet HTTPS origin") from exc
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.netloc != hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in hostname
        or len(hostname) > 253
        or any(not _DNS_LABEL.fullmatch(label) for label in hostname.split("."))
        or not hostname.endswith(".ts.net")
        or hostname.count(".") < 3
    ):
        raise ConfigError("public_origin must be https://<node>.<tailnet>.ts.net")
    canonical = f"https://{hostname}"
    if raw.rstrip("/") != canonical:
        raise ConfigError("public_origin must use canonical lowercase origin syntax")
    return canonical, hostname


def _validate_login(value: str) -> str:
    if value != value.casefold() or not _EMAIL.fullmatch(value) or "*" in value:
        raise ConfigError("identity.allowed_logins must contain lowercase exact logins")
    return value


def _validate_selector(value: str, *, owner: bool = False) -> str:
    if "*" in value or value.startswith("autogroup:"):
        raise ConfigError("identity selectors must be explicit and cannot use wildcards")
    if _EMAIL.fullmatch(value):
        return value
    if _GROUP.fullmatch(value):
        return value
    if _TAG.fullmatch(value) and not owner:
        return value
    raise ConfigError("identity selector is not a supported user, group, or tag")


def _parse_secret_reference(value: object) -> str:
    raw = _require_string(value, name="session_bootstrap_secret_ref")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("session bootstrap secret must be an external reference") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("session bootstrap secret must be an external reference")
    if parsed.scheme == "keychain":
        account = parsed.path.removeprefix("/")
        if (
            not _KEYCHAIN_PART.fullmatch(parsed.netloc)
            or parsed.path != f"/{account}"
            or not _KEYCHAIN_PART.fullmatch(account)
        ):
            raise ConfigError(
                "session bootstrap secret must use keychain://service/account"
            )
        return raw
    if (
        parsed.scheme == "env"
        and _ENVIRONMENT_NAME.fullmatch(parsed.netloc)
        and not parsed.path
    ):
        return raw
    raise ConfigError(
        "session bootstrap secret must use keychain://service/account or env://NAME"
    )


def parse_config(raw: object) -> AccessConfig:
    table = _require_mapping(raw, name="configuration")
    unknown = set(table) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(table)
    if unknown:
        raise ConfigError(f"unsupported configuration fields: {sorted(unknown)}")
    if missing:
        raise ConfigError(f"missing configuration fields: {sorted(missing)}")
    if type(table["schema_version"]) is not int or table["schema_version"] != 1:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")
    if table["provider"] != "tailscale-serve":
        raise ConfigError("provider must be tailscale-serve")

    public_origin, public_hostname = _parse_origin(table["public_origin"])
    access_gateway = _parse_loopback(table["access_gateway"], name="access_gateway")
    web_upstream = _parse_loopback(table["web_upstream"], name="web_upstream")
    daemon_upstream = _parse_loopback(table["daemon_upstream"], name="daemon_upstream")
    authorities = {
        access_gateway.authority,
        web_upstream.authority,
        daemon_upstream.authority,
    }
    if len(authorities) != 3:
        raise ConfigError("access, Web, and daemon loopback endpoints must be distinct")

    identity_raw = _require_mapping(table["identity"], name="identity")
    unknown_identity = set(identity_raw) - _IDENTITY_KEYS
    missing_identity = _IDENTITY_KEYS - set(identity_raw)
    if unknown_identity:
        raise ConfigError(f"unsupported identity fields: {sorted(unknown_identity)}")
    if missing_identity:
        raise ConfigError(f"missing identity fields: {sorted(missing_identity)}")

    logins = tuple(
        _validate_login(item)
        for item in _string_list(
            identity_raw["allowed_logins"], name="identity.allowed_logins"
        )
    )
    sources = tuple(
        _validate_selector(item)
        for item in _string_list(
            identity_raw["allowed_sources"], name="identity.allowed_sources"
        )
    )
    service_tag = _require_string(identity_raw["service_tag"], name="service_tag")
    if not _TAG.fullmatch(service_tag):
        raise ConfigError("identity.service_tag must be an exact tag:<name>")
    owners = tuple(
        _validate_selector(item, owner=True)
        for item in _string_list(
            identity_raw["tag_owners"], name="identity.tag_owners"
        )
    )
    capability = _require_string(
        identity_raw["app_capability"], name="identity.app_capability"
    )
    if (
        not _CAPABILITY.fullmatch(capability)
        or "*" in capability
        or capability.startswith(("tailscale.com/", "tailscale.io/"))
    ):
        raise ConfigError("identity.app_capability must be an exact namespaced capability")
    roles = _string_list(
        identity_raw["allowed_capability_roles"],
        name="identity.allowed_capability_roles",
    )
    if any(not _ROLE.fullmatch(role) for role in roles):
        raise ConfigError("identity.allowed_capability_roles contains an invalid role")
    if not set(logins).issubset(set(sources)):
        raise ConfigError("every allowed login must also be an allowed policy source")

    return AccessConfig(
        schema_version=SCHEMA_VERSION,
        provider="tailscale-serve",
        public_origin=public_origin,
        public_hostname=public_hostname,
        access_gateway=access_gateway,
        web_upstream=web_upstream,
        daemon_upstream=daemon_upstream,
        identity=IdentityPolicy(
            allowed_logins=logins,
            allowed_sources=sources,
            service_tag=service_tag,
            tag_owners=owners,
            app_capability=capability,
            allowed_capability_roles=roles,
        ),
        session_bootstrap_secret_ref=_parse_secret_reference(
            table["session_bootstrap_secret_ref"]
        ),
    )


def load_config(path: Path) -> AccessConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("private-access configuration could not be read") from exc
    return parse_config(raw)


def config_fingerprint(config: AccessConfig) -> str:
    material = {
        "schema_version": config.schema_version,
        "provider": config.provider,
        "public_origin": config.public_origin,
        "access_gateway": config.access_gateway.url,
        "web_upstream": config.web_upstream.url,
        "daemon_upstream": config.daemon_upstream.url,
        "identity": {
            "allowed_logins": config.identity.allowed_logins,
            "allowed_sources": config.identity.allowed_sources,
            "service_tag": config.identity.service_tag,
            "tag_owners": config.identity.tag_owners,
            "app_capability": config.identity.app_capability,
            "allowed_capability_roles": config.identity.allowed_capability_roles,
        },
        "session_bootstrap_secret_ref": config.session_bootstrap_secret_ref,
    }
    encoded = json.dumps(
        material, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
