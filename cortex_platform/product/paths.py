"""Portable path resolution for the local Cortex product shell."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_ROLE_ENVIRONMENT = {
    "config_dir": "CORTEX_CONFIG_DIR",
    "data_dir": "CORTEX_DATA_DIR",
    "state_dir": "CORTEX_STATE_DIR",
    "cache_dir": "CORTEX_CACHE_DIR",
    "log_dir": "CORTEX_LOG_DIR",
}


@dataclass(frozen=True)
class PathRegistry:
    """Resolved filesystem roles for one Cortex product installation."""

    config_dir: Path
    config_file: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    log_dir: Path

    def directories(self) -> tuple[Path, ...]:
        """Return all directory roles in deterministic order."""

        return (
            self.config_dir,
            self.data_dir,
            self.state_dir,
            self.cache_dir,
            self.log_dir,
        )

    @property
    def daemon_metadata_file(self) -> Path:
        return self.state_dir / "cortexd.json"

    @property
    def control_database_file(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def daemon_log_file(self) -> Path:
        return self.log_dir / "cortexd.log"

    @property
    def daemon_start_lock_file(self) -> Path:
        return self.state_dir / ".cortexd.start.lock"

    @property
    def daemon_lifetime_lock_file(self) -> Path:
        return self.state_dir / ".cortexd.instance.lock"

    @property
    def daemon_metadata_lock_file(self) -> Path:
        return self.state_dir / ".cortexd.metadata.lock"

    @property
    def runtime_update_root(self) -> Path:
        return self.state_dir / "runtime-update"


def _home(environ: Mapping[str, str]) -> Path:
    value = environ.get("HOME")
    if not value:
        raise ValueError("HOME is required to resolve Cortex paths")
    return Path(value).resolve(strict=False)


def _expand(
    value: str | os.PathLike[str],
    *,
    home: Path,
    relative_to: Path | None = None,
) -> Path:
    text = os.fspath(value)
    if text == "~":
        path = home
    elif text.startswith("~/"):
        path = home / text[2:]
    else:
        path = Path(text)
    if not path.is_absolute():
        path = (relative_to or Path.cwd()) / path
    return path.resolve(strict=False)


def _xdg_root(
    environ: Mapping[str, str], name: str, fallback: Path, home: Path
) -> Path:
    value = environ.get(name)
    return _expand(value, home=home) if value else fallback


def _platform_defaults(
    environ: Mapping[str, str], platform: str
) -> dict[str, Path]:
    home = _home(environ)
    if platform == "darwin":
        support = home / "Library" / "Application Support" / "Cortex"
        return {
            "config_dir": support,
            "data_dir": support / "Data",
            "state_dir": support / "State",
            "cache_dir": home / "Library" / "Caches" / "Cortex",
            "log_dir": home / "Library" / "Logs" / "Cortex",
        }
    if platform.startswith("linux"):
        config = _xdg_root(
            environ, "XDG_CONFIG_HOME", home / ".config", home
        ) / "cortex"
        data = _xdg_root(
            environ, "XDG_DATA_HOME", home / ".local" / "share", home
        ) / "cortex"
        state = _xdg_root(
            environ, "XDG_STATE_HOME", home / ".local" / "state", home
        ) / "cortex"
        cache = _xdg_root(
            environ, "XDG_CACHE_HOME", home / ".cache", home
        ) / "cortex"
        return {
            "config_dir": config,
            "data_dir": data,
            "state_dir": state,
            "cache_dir": cache,
            "log_dir": state / "logs",
        }
    raise ValueError(f"unsupported platform: {platform}")


def resolve_paths(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    cli_overrides: Mapping[str, str | os.PathLike[str] | None] | None = None,
) -> PathRegistry:
    """Resolve path roles using CLI, environment, config, then defaults."""

    environment = dict(os.environ if environ is None else environ)
    platform_name = sys.platform if platform is None else platform
    overrides = dict(cli_overrides or {})
    defaults = _platform_defaults(environment, platform_name)
    home = _home(environment)

    initial_config_dir_value = (
        overrides.get("config_dir")
        or environment.get("CORTEX_CONFIG_DIR")
        or defaults["config_dir"]
    )
    initial_config_dir = _expand(initial_config_dir_value, home=home)
    config_file_value = (
        overrides.get("config_file")
        or environment.get("CORTEX_CONFIG_FILE")
        or initial_config_dir / "config.toml"
    )
    config_file = _expand(config_file_value, home=home)

    config_paths: Mapping[str, object] = {}
    if config_file.is_file():
        from .config import load_config

        loaded = load_config(config_file)
        config_paths = loaded.get("paths", {})

    resolved: dict[str, Path] = {}
    for role, environment_name in _ROLE_ENVIRONMENT.items():
        cli_value = overrides.get(role)
        environment_value = environment.get(environment_name)
        config_value = config_paths.get(role)
        if cli_value is not None:
            value = cli_value
            relative_to = None
        elif environment_value:
            value = environment_value
            relative_to = None
        elif config_value is not None:
            value = str(config_value)
            relative_to = config_file.parent
        else:
            value = defaults[role]
            relative_to = None
        resolved[role] = _expand(value, home=home, relative_to=relative_to)

    return PathRegistry(config_file=config_file, **resolved)
