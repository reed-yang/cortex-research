"""Resolve the `[web]` front-door settings through an exact installed runtime.

⟦P7⟧ The supervisor builds the Web adapter's environment, so it is the one
process that must know whether `config.toml` fixes the listener port and names
a public door. It learns that the way it learns the product paths: by asking
the INSTALLED generation's own interpreter, never by importing
`cortex_platform` into the distribution (which manages foreign generations
across an upgrade). A generation that predates the section answers
"unsupported", which means exactly what it did before: an ephemeral port and
no public door.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_FIELDS = frozenset({"port", "public_origin", "access_issuer", "access_audience"})
_PUBLIC_ORIGIN = re.compile(
    r"^https://(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_ACCESS_ISSUER = re.compile(
    r"^(?:https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com"
    r"|http://127\.0\.0\.1:[1-9][0-9]{0,4})$"
)
_ACCESS_AUDIENCE = re.compile(r"^[0-9a-f]{64}$")
# The leading newline matters: an installed-runtime shim may prefix this code
# with simple statements ending in `;`, after which a compound statement can
# only follow a line break.
_SCRIPT = """
import json, sys
from pathlib import Path
try:
    from cortex_platform.product.config import load_config, web_settings
except ImportError:
    print(json.dumps({"supported": False}))
    raise SystemExit(0)
from cortex_platform.product.config import ConfigError
path = Path(sys.argv[1])
try:
    config = load_config(path) if path.is_file() else {}
except ConfigError as exc:
    print(json.dumps({"error": str(exc)[:300]}))
    raise SystemExit(3)
web = web_settings(config)
print(json.dumps({
    "supported": True,
    "port": web.port,
    "public_origin": web.public_origin,
    "access_issuer": web.access_issuer,
    "access_audience": web.access_audience,
}, sort_keys=True, separators=(",", ":")))
"""


class InstalledWebSettingsError(RuntimeError):
    """The installed generation could not say what shape its front door has."""


@dataclass(frozen=True)
class InstalledWebSettings:
    """The front door's configured shape; every `None` means "as before"."""

    port: int | None = None
    public_origin: str | None = None
    access_issuer: str | None = None
    access_audience: str | None = None

    @property
    def public_door(self) -> bool:
        return self.public_origin is not None


EPHEMERAL = InstalledWebSettings()


def _exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON object field")
        value[name] = item
    return value


def validate_web_settings(raw: Mapping[str, object]) -> InstalledWebSettings:
    """Re-check what the installed runtime said before it reaches a child.

    The values become the adapter's environment, and the adapter re-validates
    them again; three checks of four public identifiers is cheap, and the
    distribution cannot import the product's validator to share it.
    """

    if set(raw) != _FIELDS:
        raise InstalledWebSettingsError("installed web settings are unavailable")
    port = raw["port"]
    origin = raw["public_origin"]
    issuer = raw["access_issuer"]
    audience = raw["access_audience"]
    if port is not None and (type(port) is not int or not 1024 <= port <= 65_535):
        raise InstalledWebSettingsError("installed web settings are unavailable")
    identity = (origin, issuer, audience)
    if any(item is not None for item in identity):
        if (
            not all(isinstance(item, str) for item in identity)
            or _PUBLIC_ORIGIN.fullmatch(origin) is None
            or _ACCESS_ISSUER.fullmatch(issuer) is None
            or _ACCESS_AUDIENCE.fullmatch(audience) is None
        ):
            raise InstalledWebSettingsError("installed web settings are unavailable")
    return InstalledWebSettings(
        port=port,
        public_origin=origin,
        access_issuer=issuer,
        access_audience=audience,
    )


def _refusal_reason(stdout: str) -> str:
    try:
        raw = json.loads(stdout)
    except ValueError:
        return "configuration could not be read"
    reason = raw.get("error") if isinstance(raw, dict) else None
    if not isinstance(reason, str) or not reason.isprintable():
        return "configuration could not be read"
    return reason[:300]


def resolve_installed_web_settings(
    runtime: Path,
    *,
    config_file: Path,
    home: Path,
    timeout: float = 30,
) -> InstalledWebSettings:
    """Ask one installed Python runtime how `config.toml` shapes the front door."""

    executable = runtime if runtime.name == "python" else runtime / "bin" / "python"
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-c", _SCRIPT, str(config_file)],
            check=False,
            capture_output=True,
            text=True,
            env={
                "HOME": str(home.expanduser().absolute()),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": "",
            },
            timeout=timeout,
        )
        if completed.returncode == 3:
            # The installed generation refused the configuration. Name the
            # offending field: "unavailable" sends the operator to the wrong
            # place, the field name sends them to the line to fix.
            raise InstalledWebSettingsError(
                f"configuration refused: {_refusal_reason(completed.stdout)}"
            )
        if completed.returncode != 0:
            raise InstalledWebSettingsError("installed web settings are unavailable")
        raw = json.loads(completed.stdout, object_pairs_hook=_exact_object)
        if not isinstance(raw, dict) or raw.get("supported") not in {True, False}:
            raise InstalledWebSettingsError("installed web settings are unavailable")
        if raw["supported"] is False:
            if set(raw) != {"supported"}:
                raise InstalledWebSettingsError(
                    "installed web settings are unavailable"
                )
            return EPHEMERAL
        del raw["supported"]
        return validate_web_settings(raw)
    except InstalledWebSettingsError:
        raise
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise InstalledWebSettingsError(
            "installed web settings are unavailable"
        ) from exc
