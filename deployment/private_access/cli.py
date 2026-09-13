"""Command-line interface for private Cortex remote-access operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .operations import AccessManager, AccessOperationError


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("plan")
    generate = subparsers.add_parser("generate")
    generate.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("doctor")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--state-dir", type=Path, required=True)
    apply_parser.add_argument("--approval", required=True)
    apply_parser.add_argument("--reviewed-policy-sha256", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--state-dir", type=Path, required=True)
    rollback.add_argument("--approval", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        manager = AccessManager(config)
        if args.command == "validate":
            _print({"status": "valid", "provider": config.provider})
        elif args.command == "plan":
            _print(manager.plan())
        elif args.command == "generate":
            policy, plan = manager.generate(args.output)
            _print(
                {
                    "status": "generated",
                    "files": [policy.name, plan.name],
                    "mode": "private",
                }
            )
        elif args.command == "doctor":
            _print(manager.doctor().to_dict())
        elif args.command == "apply":
            _print(
                manager.apply(
                    args.state_dir,
                    approval=args.approval,
                    reviewed_policy_sha256=args.reviewed_policy_sha256,
                )
            )
        elif args.command == "rollback":
            _print(manager.rollback(args.state_dir, approval=args.approval))
    except (AccessOperationError, ConfigError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
