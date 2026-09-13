"""Resolve canonical product paths through an exact installed runtime."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PRODUCT_PATH_ENVIRONMENT = frozenset(
    {
        "CORTEX_CACHE_DIR",
        "CORTEX_CONFIG_DIR",
        "CORTEX_CONFIG_FILE",
        "CORTEX_DATA_DIR",
        "CORTEX_LOG_DIR",
        "CORTEX_STATE_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)
_FIELDS = frozenset(
    {
        "cache_dir",
        "config_dir",
        "config_file",
        "control_database_file",
        "data_dir",
        "log_dir",
        "runtime_update_root",
        "state_dir",
    }
)
_SCRIPT = (
    "import json;"
    "from cortex_platform.product.paths import resolve_paths;"
    "p=resolve_paths();"
    "print(json.dumps({"
    "'cache_dir':str(p.cache_dir),"
    "'config_dir':str(p.config_dir),"
    "'config_file':str(p.config_file),"
    "'control_database_file':str(p.control_database_file),"
    "'data_dir':str(p.data_dir),"
    "'log_dir':str(p.log_dir),"
    "'runtime_update_root':str(p.runtime_update_root),"
    "'state_dir':str(p.state_dir)"
    "},sort_keys=True,separators=(',',':')))"
)


class InstalledProductPathsError(RuntimeError):
    """Installed product paths could not be resolved safely."""


@dataclass(frozen=True)
class InstalledProductPaths:
    """Closed canonical filesystem roles returned by an installed runtime."""

    config_file: Path
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    log_dir: Path
    control_database_file: Path
    runtime_update_root: Path


def _exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON object field")
        value[name] = item
    return value


def allowed_product_path_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    """Copy only supported product path overrides from an environment."""

    source = {} if environment is None else environment
    selected: dict[str, str] = {}
    for name, value in source.items():
        if name not in PRODUCT_PATH_ENVIRONMENT:
            continue
        if not isinstance(value, str):
            raise InstalledProductPathsError("installed product paths are unavailable")
        selected[name] = value
    return selected


def resolve_installed_product_paths(
    runtime: Path,
    *,
    home: Path,
    environment: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> InstalledProductPaths:
    """Ask one installed Python runtime for its canonical product paths."""

    executable = runtime if runtime.name == "python" else runtime / "bin" / "python"
    try:
        home_path = home.expanduser().absolute()
        child_environment = allowed_product_path_environment(environment)
        child_environment.update(
            {
                "HOME": str(home_path),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": "",
            }
        )
        completed = subprocess.run(
            [str(executable), "-I", "-c", _SCRIPT],
            check=False,
            capture_output=True,
            text=True,
            env=child_environment,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise InstalledProductPathsError(
                "installed product paths are unavailable"
            )
        raw = json.loads(completed.stdout, object_pairs_hook=_exact_object)
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise InstalledProductPathsError(
                "installed product paths are unavailable"
            )
        values: dict[str, Path] = {}
        for name, value in raw.items():
            if (
                not isinstance(value, str)
                or not value
                or not value.strip()
                or "\0" in value
            ):
                raise InstalledProductPathsError(
                    "installed product paths are unavailable"
                )
            path = Path(value)
            if not path.is_absolute():
                raise InstalledProductPathsError(
                    "installed product paths are unavailable"
                )
            values[name] = path
        return InstalledProductPaths(**values)
    except InstalledProductPathsError:
        raise
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise InstalledProductPathsError(
            "installed product paths are unavailable"
        ) from exc
