"""Run an operator command with explicitly configured engine credentials.

Usage: python -m tools.with_engine_secrets [--config PATH] -- COMMAND [ARG ...]
This is a trusted checkout helper, never a managed model-tool entry point.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess

from cortex_platform.product.config import ConfigError, load_config
from cortex_platform.product.engine.bindings import engine_secret_aliases
from cortex_platform.product.paths import resolve_paths
from cortex_platform.product.secrets import SecretResolutionError, SecretResolver


def command_environment(config: dict, environment: dict[str, str]) -> dict[str, str]:
    child = dict(environment)
    references = config.get("secret_refs", {})
    resolver = SecretResolver(environment=environment)
    for alias, name in engine_secret_aliases().items():
        if alias in references:
            child[name] = resolver.resolve(alias, references[alias]).reveal()
    return child


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    try:
        config = load_config(args.config or resolve_paths().config_file)
        environment = command_environment(config, dict(os.environ))
    except (ConfigError, SecretResolutionError):
        parser.exit(2, "Configured engine credentials could not be resolved; check configuration and stores.\n")
    try:
        return subprocess.run(command, env=environment, check=False).returncode
    except OSError:
        parser.exit(2, "The requested command could not be started.\n")


if __name__ == "__main__":
    raise SystemExit(main())
