"""Sanitized local diagnostics for the Cortex product shell."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .config import ConfigError, detect_legacy_roots, load_config, web_settings
from .paths import PathRegistry


# Checks that inform rather than judge: their status is free text and their
# presence never makes `cortex doctor` exit non-zero.
_ADVISORY_CHECKS = frozenset(
    {
        "secret_refs",
        "transport_secret_refs",
        "runtime_provider",
        "web",
        "web_public_door",
        "web_public_door_insecure_issuer",
    }
)
#: The Access team-domain form of `web.access_issuer`. The schema also accepts
#: `http://127.0.0.1:<port>` so an acceptance can stand a JWKS server up on
#: this machine; on a real installation that form fetches the door's signing
#: keys over plaintext from a port any local process can bind.
_ACCESS_TEAM_ISSUER = re.compile(
    r"^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$"
)


def _web_check(
    config: Mapping[str, object], *, loaded: bool = True
) -> list[tuple[str, str]]:
    """Say what shape the Web front door has, and the one way it is half-built.

    ⟦P7⟧ The section state is always printed, because "ephemeral port, no
    public door" is the answer an operator needs to read BEFORE pointing a
    tunnel ingress at a port that will move on the next start. The advisory
    covers the only combination the schema allows but the tunnel cannot use: a
    public origin with no fixed port. The audience is abbreviated because it is
    an identifier, not because it is a secret -- nothing in this section is.
    """

    if not loaded:
        # An unreadable configuration is not "ephemeral, no public door"; it
        # is unknown, and `config: invalid` above says why.
        return [("web", "unknown (configuration invalid)")]
    # "configured:" because this line is read from `config.toml`, not from
    # the running adapter; the live door is `/_cortex/health`'s `public_door`.
    web = web_settings(config)
    port = f"port {web.port} (fixed)" if web.port is not None else "port ephemeral"
    if not web.public_door:
        return [("web", f"configured: {port}; public door none")]
    audience = str(web.access_audience)
    checks = [
        (
            "web",
            f"configured: {port}; public door {web.public_origin} "
            f"(issuer {web.access_issuer}, audience {audience[:8]}...)",
        )
    ]
    if web.port is None:
        checks.append(
            (
                "web_public_door",
                "web.public_origin is set but web.port is not: the listener port "
                "changes on every start, so a tunnel ingress has nothing stable "
                "to target; set web.port",
            )
        )
    if _ACCESS_TEAM_ISSUER.fullmatch(str(web.access_issuer)) is None:
        checks.append(
            (
                "web_public_door_insecure_issuer",
                "web.access_issuer is not an Access team domain "
                "(https://<team>.cloudflareaccess.com): the public door's signing "
                "keys are fetched over plaintext HTTP from a port any local process "
                "can bind; use the team domain on a real installation",
            )
        )
    return checks


def _transport_secret_check(
    config: Mapping[str, object],
) -> list[tuple[str, str]]:
    """Say plainly whether the bot token can be resolved by a supervised daemon.

    ⟦AMD-8⟧ applies to the transport credential too, and more sharply: the
    distribution supervisor hands `cortexd` a fully replaced environment
    (`distribution/lifecycle.py`), so an `env://` reference resolves to nothing
    there. Unlike the engine's aliases the daemon does not drop it -- the launch
    is refused with a typed `transport_credential_unavailable` the moment a
    window opens -- but "the moment a window opens" is the operator-present
    ceremony, which is the worst time to learn it. Advisory, and named without
    its reference so nothing here can carry a secret.
    """

    from .transports.worker_rpc import TELEGRAM_SECRET_ALIAS

    transports = config.get("transports")
    if not isinstance(transports, Mapping) or not transports.get(
        "telegram_bot_identity"
    ):
        # No bot is configured, so there is no adapter and nothing to resolve.
        return []
    references = config.get("secret_refs")
    reference = (
        references.get(TELEGRAM_SECRET_ALIAS)
        if isinstance(references, Mapping)
        else None
    )
    if not isinstance(reference, str) or not reference:
        return [
            (
                "transport_secret_refs",
                f"a Telegram bot is configured but secret_refs.{TELEGRAM_SECRET_ALIAS} "
                "is missing; every send in a window will be refused with "
                "transport_credential_unavailable",
            )
        ]
    scheme = reference.split("://", 1)[0]
    if scheme == "keychain":
        return []
    return [
        (
            "transport_secret_refs",
            f"secret_refs.{TELEGRAM_SECRET_ALIAS} uses {scheme}://; the "
            "supervised daemon is started with a replaced environment, so only "
            "keychain:// resolves there and a window would refuse every send",
        )
    ]


def _runtime_provider_check(
    config: Mapping[str, object], *, environ: Mapping[str, str]
) -> list[tuple[str, str]]:
    """Say plainly whether a managed TURN can name a model provider.

    ⟦BLOCK-1⟧ The sibling of `_transport_secret_check`, and it exists for the
    same reason and the sharper version of it. A provider that cannot be named
    is not refused anywhere an operator looks before the window: `managed_worker
    .state` read `bound`, the `turn_bridge` block was present, and every step-4
    predicate in the runbook passed -- and then the first message of an
    operator-present ceremony came back `runtime_execution_failed`, twice, on
    two separate real runs.

    Three ways to half-configure one, all of them silent before: a credential
    alias with no endpoint to spend it against, an endpoint (or a model) with no
    credential alias, and a well-formed `keychain://` reference with nothing
    stored behind it. The first two the daemon now refuses at bind; this says so
    in the foreground, before `cortex start`. Advisory, and named without its
    reference so nothing here can carry a secret.
    """

    from .transports.managed_worker import (
        PROVIDER_BASE_URL_KEYS,
        PROVIDER_SECRET_ALIASES,
    )

    runtime = config.get("runtime")
    runtime = dict(runtime) if isinstance(runtime, Mapping) else {}
    references = config.get("secret_refs")
    references = dict(references) if isinstance(references, Mapping) else {}
    aliases = sorted(set(references) & set(PROVIDER_SECRET_ALIASES))
    endpoint = str(runtime.get("base_url") or "") or None
    model = str(runtime.get("model") or "") or None
    override = any(environ.get(key) for key in PROVIDER_BASE_URL_KEYS)

    if not (endpoint or model or runtime.get("provider")):
        # `[runtime]` asks for nothing, which is a complete answer for an
        # installation that never runs a turn. The provider aliases alone are
        # not an ask: the engine resolves the same three names for its own
        # research effects.
        return []
    if not aliases:
        return [
            (
                "runtime_provider",
                "[runtime] names a provider endpoint or model but no "
                f"secret_refs alias out of {sorted(PROVIDER_SECRET_ALIASES)}; "
                "the daemon refuses to bind with provider_credential_missing",
            )
        ]
    if not endpoint and not override:
        return [
            (
                "runtime_provider",
                f"secret_refs.{aliases[0]} is set but [runtime] base_url is "
                "missing; the daemon refuses to bind with "
                "provider_endpoint_missing",
            )
        ]

    checks: list[tuple[str, str]] = []
    if not model:
        checks.append(
            (
                "runtime_provider",
                "[runtime] model is unset, so a turn asks the provider for "
                "whatever the fork defaults to",
            )
        )
    from .secrets import SecretResolutionError, SecretResolver

    resolver = SecretResolver(environment=environ)
    for alias in aliases:
        reference = str(references[alias])
        scheme = reference.split("://", 1)[0]
        if scheme != "keychain":
            checks.append(
                (
                    "runtime_provider",
                    f"secret_refs.{alias} uses {scheme}://; the supervised "
                    "daemon is started with a replaced environment, so only "
                    "keychain:// resolves there and every turn would be "
                    "refused with provider_credential_unavailable",
                )
            )
            continue
        try:
            resolver.resolve(alias, reference)
        except SecretResolutionError:
            # The alias, never the reference and never the value.
            checks.append(
                (
                    "runtime_provider",
                    f"secret_refs.{alias} resolves to nothing; every turn "
                    "would be refused with provider_credential_unavailable",
                )
            )
    return checks


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[tuple[str, str], ...]

    @property
    def healthy(self) -> bool:
        return all(
            name in _ADVISORY_CHECKS
            or status in {"ok", "none", "detected", "running", "stopped"}
            for name, status in self.checks
        )

    def render(self) -> str:
        return "\n".join(f"{name}: {status}" for name, status in self.checks)


def doctor(paths: PathRegistry, *, environ: Mapping[str, str]) -> DoctorReport:
    checks: list[tuple[str, str]] = []
    config: Mapping[str, object] = {}
    loaded = False
    try:
        config = load_config(paths.config_file)
    except ConfigError:
        checks.append(("config", "invalid"))
    else:
        loaded = True
        checks.append(("config", "ok"))

    # ⟦AMD-8⟧ The supervised daemon resolves `keychain://` only, and dropped
    # every other reference in silence -- on no surface an operator could read.
    # Advisory, never fatal: the rule is correct, and the aliases are named
    # without their references so nothing here can carry a secret.
    from .engine.service import unusable_secret_aliases

    # Reported only when there is something to report: a configuration whose
    # references the daemon can all honour says nothing here.
    unusable = unusable_secret_aliases(config)
    if unusable:
        checks.append(
            (
                "secret_refs",
                f"{len(unusable)} unusable under the supervised daemon "
                "(env:// resolves only on the foreground path): "
                + ", ".join(unusable),
            )
        )

    checks.extend(_transport_secret_check(config))
    checks.extend(_runtime_provider_check(config, environ=environ))
    checks.extend(_web_check(config, loaded=loaded))

    missing = sum(not path.is_dir() for path in paths.directories())
    checks.append(("paths", "ok" if missing == 0 else f"missing ({missing} roles)"))
    legacy = detect_legacy_roots(paths, environ=environ)
    checks.append(("legacy roots", "detected" if legacy else "none"))
    from .lifecycle import daemon_status

    checks.append(("daemon", daemon_status(paths).state))
    return DoctorReport(tuple(checks))
