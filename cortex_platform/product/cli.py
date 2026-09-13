"""Command-line interface for the portable Cortex product shell."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Mapping, Sequence

from .config import ConfigError, initialize
from .diagnostics import doctor
from .lifecycle import LifecycleError, daemon_status, start_daemon, stop_daemon
from .paths import resolve_paths
from .runtime_update.cli import add_runtime_parser, run_runtime_command
from .runtime_update.service import RuntimeUpdateError
from .transport_cli import add_transport_parser, run_transport_command


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config-file")
    parser.add_argument("--config-dir")
    parser.add_argument("--data-dir")
    parser.add_argument("--state-dir")
    parser.add_argument("--cache-dir")
    parser.add_argument("--log-dir")
    return parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()

    init_parser = subparsers.add_parser("init", parents=[common])
    init_parser.add_argument("--adopt-legacy", action="store_true")
    subparsers.add_parser("doctor", parents=[common])
    start_parser = subparsers.add_parser("start", parents=[common])
    start_parser.add_argument("--timeout", type=float, default=10.0)
    subparsers.add_parser("status", parents=[common])
    stop_parser = subparsers.add_parser("stop", parents=[common])
    stop_parser.add_argument("--timeout", type=float, default=10.0)
    add_runtime_parser(subparsers, common=common)
    add_transport_parser(subparsers, common=common)
    return parser


def _overrides(arguments: argparse.Namespace) -> dict[str, str]:
    return {
        name: value
        for name in (
            "config_file",
            "config_dir",
            "data_dir",
            "state_dir",
            "cache_dir",
            "log_dir",
        )
        if (value := getattr(arguments, name, None)) is not None
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> int:
    environment = dict(os.environ if environ is None else environ)
    arguments = _parser().parse_args(argv)
    try:
        paths = resolve_paths(
            environ=environment,
            platform=platform,
            cli_overrides=_overrides(arguments),
        )
        if arguments.command == "init":
            result = initialize(
                paths,
                environ=environment,
                adopt_legacy=arguments.adopt_legacy,
            )
            print("initialized" if result.created_config else "already initialized")
            if result.adopted_roots:
                print(f"adopted legacy roots: {len(result.adopted_roots)}")
            return 0
        if arguments.command == "doctor":
            report = doctor(paths, environ=environment)
            print(report.render())
            return 0 if report.healthy else 1
        if arguments.command == "start":
            initialize(paths, environ=environment)
            print(
                start_daemon(
                    paths, environ=environment, timeout=arguments.timeout
                ).state
            )
            return 0
        if arguments.command == "status":
            status = daemon_status(paths)
            print(status.state)
            return 0 if status.state == "running" else 3
        if arguments.command == "stop":
            status = stop_daemon(paths, timeout=arguments.timeout)
            print(status.state)
            return 0 if status.state == "stopped" else 3
        if arguments.command == "runtime":
            return run_runtime_command(arguments, paths, environ=environment)
        if arguments.command == "transport":
            return run_transport_command(arguments, paths)
    except (ConfigError, LifecycleError, RuntimeUpdateError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
