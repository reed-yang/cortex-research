"""Versioned, secret-reference-only Cortex product configuration."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .paths import PathRegistry


CONFIG_VERSION = 1
_TOP_LEVEL_KEYS = {
    "config_version",
    "paths",
    "asset_roots",
    "secret_refs",
    "transports",
    "runtime",
    "web",
    "readings",
}
# ⟦P7⟧ The web section is the Web front door's shape and nothing else: a fixed
# loopback port so a tunnel ingress has a stable target, and the ONE public
# origin the boundary may trust, with the identity check that origin must pass
# at the adapter. Every value here is public (a port, a hostname, an issuer
# URL, an application audience tag); the Cloudflare Access signing keys are
# fetched from the issuer, never written here.
_WEB_KEYS = {"port", "public_origin", "access_issuer", "access_audience"}
#: A `[web] port` is loopback-only and unprivileged; 0 (ephemeral) is what the
#: absence of the key means, so it is not a value.
_WEB_PORT_RANGE = range(1024, 65_536)
#: A public origin is exactly `https://<fqdn>`: lowercase labels, at least one
#: dot, no port, no path. An IP literal or a single label is not a public name.
_PUBLIC_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_ACCESS_TEAM_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ACCESS_AUDIENCE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
#: The loopback form exists for an acceptance that stands a JWKS server up on
#: this machine; the tunnel's real issuer is always the Access team domain.
_LOOPBACK_ISSUER_PATTERN = re.compile(r"^http://127\.0\.0\.1:(?:[1-9][0-9]{0,4})$")
# ⟦P5.4c/P5.4e⟧ The runtime section carries WHICH provider a managed turn
# talks to, and never the credential to talk to it. The key still comes from a
# `secret_refs` alias, for the same reason the bot token has no field here
# (D-P5-4); the endpoint is here because it is not a secret and because the
# supervised daemon has no other way to learn it -- the distribution supervisor
# replaces `cortexd`'s environment with seven keys, so the allowlisted
# `*_BASE_URL` variables the P5.4c fix read can only ever be set by an
# acceptance driver that starts the daemon itself (⟦BLOCK-1⟧).
_RUNTIME_KEYS = {"model", "provider", "base_url"}
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
#: The fork's own provider vocabulary (`custom`, `anthropic`, `openai`, ...),
#: which selects its API mode. A plain lowercase token, never a URL.
_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
#: `http://` is accepted for these and nothing else. A plaintext provider
#: endpoint that leaves the machine would put the API key on the wire; a
#: loopback one cannot, and it is what an acceptance needs to stand a fake
#: provider up on the one port the seatbelt permits.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PATH_KEYS = {"config_dir", "data_dir", "state_dir", "cache_dir", "log_dir"}
_ASSET_ROOT_KEYS = {"legacy-config", "legacy-state"}
# The transports section carries operating mode and nothing else. There is no
# field here for a bot token and there never will be one (D-P5-4): the
# credential is resolved from a `secret_refs` alias in the parent process and
# revealed into one allowlisted worker environment key, so a raw secret has no
# route into a file the product writes.
_TRANSPORT_KEYS = {
    "telegram_mode",
    "telegram_bot_identity",
    "telegram_base_url",
    "telegram_allowed_user_ids",
}
_BOT_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TELEGRAM_MODES = {"active", "shadow"}
DEFAULT_TELEGRAM_MODE = "shadow"
_SECRET_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_KEYCHAIN_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(^|[_-])(api[_-]?key|token|password|secret|credential)([_-]|$)",
    re.IGNORECASE,
)


class ConfigError(ValueError):
    """Raised when product configuration violates the v0.1 schema."""


@dataclass(frozen=True)
class InitResult:
    created_config: bool
    updated_config: bool
    adopted_roots: tuple[str, ...]


def _string_mapping(value: object, *, section: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{section} must be a TOML table")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ConfigError(f"{section} entries must be strings")
        result[key] = item
    return result


def _validate_asset_roots(value: object) -> dict[str, str]:
    roots = _string_mapping(value, section="asset_roots")
    unknown = set(roots) - _ASSET_ROOT_KEYS
    if unknown:
        raise ConfigError(f"unsupported asset root fields: {sorted(unknown)}")
    for name, root in roots.items():
        if "\0" in root or not Path(root).is_absolute():
            raise ConfigError(f"asset_roots.{name} must be an absolute filesystem path")
    return roots


def _validate_secret_reference(name: str, reference: str) -> None:
    """Accept the two reference syntaxes, and say what each one can be used for.

    Both are valid configuration, but they are not interchangeable at runtime:
    ⟦AMD-8⟧ makes `keychain://` the ONLY scheme the supervised daemon resolves,
    because reading a variable under launchd would depend on whatever
    environment launchd happened to export. `env://` is therefore a
    FOREGROUND-ONLY reference -- accepted here, and unusable by `cortexd`, which
    reports the aliases it had to drop through `cortex doctor`'s `secret_refs`
    check rather than dropping them in silence.
    """

    if not _SECRET_ALIAS_PATTERN.fullmatch(name) or _CREDENTIAL_KEY_PATTERN.search(
        name
    ):
        raise ConfigError(f"secret_refs.{name} is not a supported logical alias")
    try:
        parsed = urlsplit(reference)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"secret_refs.{name} must be a safe external reference") from exc
    if (
        parsed.scheme not in {"keychain", "env"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"secret_refs.{name} must be a safe external reference")
    if parsed.scheme == "keychain":
        account = parsed.path.removeprefix("/")
        if (
            not _KEYCHAIN_IDENTIFIER_PATTERN.fullmatch(parsed.netloc)
            or parsed.path != f"/{account}"
            or not _KEYCHAIN_IDENTIFIER_PATTERN.fullmatch(account)
        ):
            raise ConfigError(
                f"secret_refs.{name} reference must use keychain://service/account"
            )
        return
    if (
        not _ENVIRONMENT_NAME_PATTERN.fullmatch(parsed.netloc)
        or parsed.path
    ):
        raise ConfigError(
            f"secret_refs.{name} reference must use env://VARIABLE_NAME "
            "(foreground only; the supervised daemon resolves keychain:// only)"
        )


def _validate_runtime_base_url(value: str) -> None:
    """A credential-free provider endpoint, validated the way `model` is.

    ⟦BLOCK-1⟧ Deliberately narrow. No userinfo (that is a credential in a URL),
    no query and no fragment (the fork splits a query off the base URL and
    replays it on every request, so one written here would be a per-request
    parameter nobody reviewed), and a hostname rather than an arbitrary
    authority. The port is left free: production is 443 and an acceptance needs
    an ephemeral one, and the seatbelt -- which permits exactly one outbound
    port -- is what actually constrains it.
    """

    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - raises for a malformed port
    except ValueError as exc:
        raise ConfigError(
            "runtime.base_url must be an https:// endpoint without credentials"
        ) from exc
    host = parsed.hostname
    if (
        parsed.scheme not in {"https", "http"}
        or not host
        or not _HOSTNAME_PATTERN.fullmatch(host)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            "runtime.base_url must be an https:// endpoint without credentials"
        )
    if parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ConfigError(
            "runtime.base_url may only use http:// for a loopback host"
        )


@dataclass(frozen=True)
class WebSettings:
    """The Web front door's shape, as the supervisor and `doctor` read it.

    `port` is the fixed loopback listener port, or None for the ephemeral one
    the adapter has always chosen. `public_origin` names the ONE public door;
    when it is set the two access fields are set too, by construction.
    """

    port: int | None = None
    public_origin: str | None = None
    access_issuer: str | None = None
    access_audience: str | None = None

    @property
    def public_door(self) -> bool:
        return self.public_origin is not None


def _validate_web_public_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError("web.public_origin must be https://<hostname>")
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - raises for a malformed port
    except ValueError as exc:
        raise ConfigError("web.public_origin must be https://<hostname>") from exc
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or not _PUBLIC_HOSTNAME_PATTERN.fullmatch(host)
        or value != f"https://{host}"
    ):
        raise ConfigError("web.public_origin must be https://<hostname>")
    return value


def _validate_web_access_issuer(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            "web.access_issuer must be https://<team>.cloudflareaccess.com"
        )
    if _LOOPBACK_ISSUER_PATTERN.fullmatch(value):
        return value
    prefix, separator, suffix = value.partition("://")
    team = suffix.removesuffix(".cloudflareaccess.com")
    if (
        prefix != "https"
        or not separator
        or team == suffix
        or not _ACCESS_TEAM_PATTERN.fullmatch(team)
    ):
        raise ConfigError(
            "web.access_issuer must be https://<team>.cloudflareaccess.com"
        )
    return value


def _validate_web(value: object) -> dict[str, object]:
    """Validate `[web]`, failing closed on a public door without an identity check.

    ⟦P7⟧ A `public_origin` is the boundary's permission to accept mutations
    from a browser on the public internet. The adapter only grants it to a
    request carrying a Cloudflare Access assertion it verified against the
    issuer for the audience, so a public origin with either of those missing
    is not "a public door without login" -- it is a configuration error, here,
    before anything listens.
    """

    if not isinstance(value, dict):
        raise ConfigError("web must be a TOML table")
    unknown = set(value) - _WEB_KEYS
    if unknown:
        raise ConfigError(f"unsupported web fields: {sorted(unknown)}")
    web: dict[str, object] = {}
    port = value.get("port")
    if port is not None:
        if type(port) is not int or port not in _WEB_PORT_RANGE:
            raise ConfigError("web.port must be an integer between 1024 and 65535")
        web["port"] = port
    origin = value.get("public_origin")
    if origin is not None:
        web["public_origin"] = _validate_web_public_origin(origin)
    issuer = value.get("access_issuer")
    if issuer is not None:
        web["access_issuer"] = _validate_web_access_issuer(issuer)
    audience = value.get("access_audience")
    if audience is not None:
        if not isinstance(audience, str) or not _ACCESS_AUDIENCE_PATTERN.fullmatch(
            audience
        ):
            raise ConfigError(
                "web.access_audience must be the 64-hex Access application tag"
            )
        web["access_audience"] = audience
    access = {"access_issuer", "access_audience"} & set(web)
    if "public_origin" in web and len(access) != 2:
        raise ConfigError(
            "web.public_origin requires both web.access_issuer and "
            "web.access_audience; a public door without an identity check is "
            "not configurable"
        )
    if access and "public_origin" not in web:
        raise ConfigError(
            "web.access_issuer and web.access_audience require web.public_origin"
        )
    return web


def web_settings(config: Mapping[str, object]) -> WebSettings:
    """The front door's shape from a validated configuration."""

    web = config.get("web")
    if not isinstance(web, Mapping):
        return WebSettings()
    port = web.get("port")
    origin = web.get("public_origin")
    issuer = web.get("access_issuer")
    audience = web.get("access_audience")
    return WebSettings(
        port=port if type(port) is int else None,
        public_origin=origin if isinstance(origin, str) and origin else None,
        access_issuer=issuer if isinstance(issuer, str) and issuer else None,
        access_audience=audience if isinstance(audience, str) and audience else None,
    )


def _validate_transports(value: object) -> dict[str, str]:
    transports = _string_mapping(value, section="transports")
    unknown = set(transports) - _TRANSPORT_KEYS
    if unknown:
        raise ConfigError(f"unsupported transport fields: {sorted(unknown)}")
    mode = transports.get("telegram_mode")
    if mode is not None and mode not in _TELEGRAM_MODES:
        raise ConfigError("transports.telegram_mode must be 'active' or 'shadow'")
    identity = transports.get("telegram_bot_identity")
    if identity is not None and not _BOT_IDENTITY_PATTERN.fullmatch(identity):
        raise ConfigError("transports.telegram_bot_identity is not a public bot name")
    base_url = transports.get("telegram_base_url")
    if base_url is not None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ConfigError(
                "transports.telegram_base_url must be an HTTPS URL without a query"
            )
    users = transports.get("telegram_allowed_user_ids")
    if users is not None:
        for item in users.split(","):
            if not item.strip().isdigit():
                raise ConfigError(
                    "transports.telegram_allowed_user_ids must be comma-separated ids"
                )
    return transports


def telegram_allowed_user_ids(config: Mapping[str, object]) -> frozenset[int]:
    """The inbound allowlist, empty unless the operator named someone."""

    transports = config.get("transports")
    if not isinstance(transports, Mapping):
        return frozenset()
    raw = transports.get("telegram_allowed_user_ids")
    if not isinstance(raw, str) or not raw.strip():
        return frozenset()
    return frozenset(int(item) for item in raw.split(",") if item.strip())


def telegram_setting(config: Mapping[str, object], name: str) -> str | None:
    transports = config.get("transports")
    if not isinstance(transports, Mapping):
        return None
    value = transports.get(name)
    return value if isinstance(value, str) and value else None


def telegram_mode(config: Mapping[str, object]) -> str:
    """The configured Telegram mode, defaulting to the harmless one.

    `shadow` is the second fence behind the transport activation gate: the
    adapter still refuses to touch its client in that mode, so a
    misconfiguration fails towards not sending.
    """

    transports = config.get("transports")
    if not isinstance(transports, Mapping):
        return DEFAULT_TELEGRAM_MODE
    mode = transports.get("telegram_mode")
    if mode not in _TELEGRAM_MODES:
        return DEFAULT_TELEGRAM_MODE
    return str(mode)


def validate_config(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a TOML table")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    # Forward compatibility, in one direction only. P5 widened this schema with
    # `[transports]` at the same `config_version`, so a config written by the
    # newer generation is unreadable to the older one -- and
    # `resolve_installed_product_paths` runs the INSTALLED generation's own
    # interpreter and turns any config refusal into `InstalledProductPathsError`,
    # so a downgrade takes `cortex-dist start|stop|status|record-proof` with it,
    # product-wide. An unknown SECTION is therefore ignored (and dropped on the
    # next rewrite, which `_render_config` does by construction). An unknown KEY
    # inside a known section stays fatal, because that is where a typo silently
    # changes behaviour, and so does an unknown top-level scalar.
    unknown -= {name for name in unknown if isinstance(raw[name], dict)}
    if unknown:
        raise ConfigError(f"unsupported configuration fields: {sorted(unknown)}")
    if (
        type(raw.get("config_version")) is not int
        or raw["config_version"] != CONFIG_VERSION
    ):
        raise ConfigError(f"config_version must be {CONFIG_VERSION}")

    validated: dict[str, object] = {"config_version": CONFIG_VERSION}
    if "paths" in raw:
        paths = _string_mapping(raw["paths"], section="paths")
        unknown_paths = set(paths) - _PATH_KEYS
        if unknown_paths:
            raise ConfigError(f"unsupported path fields: {sorted(unknown_paths)}")
        validated["paths"] = paths
    if "asset_roots" in raw:
        validated["asset_roots"] = _validate_asset_roots(raw["asset_roots"])
    if "secret_refs" in raw:
        references = _string_mapping(raw["secret_refs"], section="secret_refs")
        for name, reference in references.items():
            _validate_secret_reference(name, reference)
        validated["secret_refs"] = references
    if "transports" in raw:
        validated["transports"] = _validate_transports(raw["transports"])
    if "runtime" in raw:
        runtime = _string_mapping(raw["runtime"], section="runtime")
        unknown_runtime = set(runtime) - _RUNTIME_KEYS
        if unknown_runtime:
            raise ConfigError(
                f"unsupported runtime fields: {sorted(unknown_runtime)}"
            )
        model = runtime.get("model")
        if model is not None and not _MODEL_PATTERN.fullmatch(model):
            raise ConfigError("runtime.model must be a plain model identifier")
        provider = runtime.get("provider")
        if provider is not None and not _PROVIDER_PATTERN.fullmatch(provider):
            raise ConfigError("runtime.provider must be a plain provider name")
        base_url = runtime.get("base_url")
        if base_url is not None:
            _validate_runtime_base_url(base_url)
        validated["runtime"] = runtime
    if "web" in raw:
        validated["web"] = _validate_web(raw["web"])
    if "readings" in raw:
        readings = _string_mapping(raw["readings"], section="readings")
        if set(readings) != {"papers_root"}:
            raise ConfigError("readings requires only papers_root")
        root = Path(readings["papers_root"])
        if not root.is_absolute() or root == Path(root.anchor) or "\0" in str(root) or ".." in root.parts:
            raise ConfigError("readings.papers_root must be an absolute non-root path")
        validated["readings"] = readings
    return validated


def load_config(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("configuration could not be read") from exc
    return validate_config(raw)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
    # `[web] port` is the one integer the schema carries; everything else is a
    # string, and a validated config holds nothing else.
    if type(value) is int:
        return str(value)
    assert isinstance(value, str)
    return _toml_string(value)


def _render_config(config: Mapping[str, object]) -> str:
    validated = validate_config(dict(config))
    lines = [f"config_version = {CONFIG_VERSION}"]
    for section in ("paths", "asset_roots", "secret_refs", "transports", "runtime", "web", "readings"):
        entries = validated.get(section)
        if not entries:
            continue
        lines.extend(("", f"[{section}]"))
        assert isinstance(entries, dict)
        for key in sorted(entries):
            lines.append(f"{_toml_string(key)} = {_toml_value(entries[key])}")
    return "\n".join(lines) + "\n"


def write_config(path: Path, config: Mapping[str, object]) -> None:
    content = _render_config(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def detect_legacy_roots(
    paths: PathRegistry, *, environ: Mapping[str, str]
) -> dict[str, Path]:
    home_value = environ.get("HOME")
    if not home_value:
        return {}
    home = Path(home_value).resolve(strict=False)
    candidates = {
        "legacy-config": home / ".config" / "cortex",
        "legacy-state": home / ".local" / "state" / "cortex",
    }
    primary = set(paths.directories())
    return {
        name: path
        for name, path in candidates.items()
        if path.exists() and path not in primary
    }


def initialize(
    paths: PathRegistry,
    *,
    environ: Mapping[str, str],
    adopt_legacy: bool = False,
) -> InitResult:
    """Create the native layout and optionally map legacy roots in place."""

    for directory in paths.directories():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    created = not paths.config_file.exists()
    config: dict[str, object]
    if created:
        config = {"config_version": CONFIG_VERSION}
    else:
        config = load_config(paths.config_file)

    adopted: dict[str, Path] = {}
    if adopt_legacy:
        detected = detect_legacy_roots(paths, environ=environ)
        roots = dict(config.get("asset_roots", {}))
        adopted = {
            name: path
            for name, path in detected.items()
            if roots.get(name) != str(path)
        }
        if adopted:
            roots.update({name: str(path) for name, path in adopted.items()})
            config["asset_roots"] = roots

    if created or adopted:
        write_config(paths.config_file, config)
    return InitResult(
        created_config=created,
        updated_config=bool(adopted) and not created,
        adopted_roots=tuple(sorted(adopted)),
    )
