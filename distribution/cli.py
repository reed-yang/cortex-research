"""Standalone command surface for developer distribution artifacts."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path

from .bundle import BundleVerificationError, build_repository_bundle, verify_bundle
from .install import DistributionInstaller, InstallError
from .lifecycle import LifecycleError, LifecycleManager, LifecycleStatus, stage_launch_agent
from .product_paths import allowed_product_path_environment
from .state_safety import (
    DEFAULT_KEEP_BACKUP_SETS,
    BackupProofError,
    prune_backup_sets,
    record_backup_proof,
)


def _positive_int(value: str) -> int:
    """`--keep` below one is refused by the parser, BEFORE `record_backup_proof`
    copies the set: the pruner raises for it too, but only after ~12 GB."""

    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex-dist")
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build")
    build.add_argument("--repository", type=Path, default=Path.cwd())
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--release-id", required=True)
    build.add_argument("--release-sequence", type=int, required=True)
    build.add_argument("--web-payload-root", type=Path)
    build.add_argument("--web-payload-ledger", type=Path)
    build.add_argument("--web-build-id")
    build.add_argument("--web-lock-sha256")
    build.add_argument("--node-adapter-version", type=int)
    build.add_argument("--node-executable", type=Path)
    # The interpreter travels inside the bundle, so verify/install/upgrade get
    # no matching flag: there is deliberately no way to point an install at a
    # different Python than the one the bundle carries.
    build.add_argument("--python-runtime", type=Path)
    build.add_argument("--python-runtime-pin", type=Path)
    build.add_argument("--requirements", type=Path)
    build.add_argument("--wheelhouse", type=Path)
    # Defaults to `<wheelhouse>/built-from-sdist.json`, which is where
    # tools/vendor_wheelhouse.py writes it; pass this only to override.
    build.add_argument("--sdist-builds", type=Path)

    verify = subcommands.add_parser("verify")
    verify.add_argument("--bundle", type=Path, default=Path.cwd())
    verify.add_argument("--node-executable", type=Path)

    install = subcommands.add_parser("install")
    install.add_argument("--bundle", type=Path, default=Path.cwd())
    install.add_argument("--prefix", type=Path)
    install.add_argument("--node-executable", type=Path)
    install.add_argument("--allow-unsigned-developer", action="store_true")

    upgrade = subcommands.add_parser("upgrade")
    upgrade.add_argument("--bundle", type=Path, default=Path.cwd())
    upgrade.add_argument("--prefix", type=Path)
    upgrade.add_argument("--runtime-root", type=Path, required=True)
    upgrade.add_argument("--home", type=Path, default=Path.home())
    upgrade.add_argument("--node-executable", type=Path)
    upgrade.add_argument("--allow-unsigned-developer", action="store_true")

    doctor = subcommands.add_parser("doctor")
    doctor.add_argument("--prefix", type=Path)
    doctor.add_argument("--runtime-root", type=Path)
    doctor.add_argument("--home", type=Path, default=Path.home())

    recover = subcommands.add_parser("recover")
    recover.add_argument("--prefix", type=Path)

    for name in ("rollback", "uninstall"):
        command = subcommands.add_parser(name)
        command.add_argument("--prefix", type=Path)
        command.add_argument("--runtime-root", type=Path, required=True)
        command.add_argument("--home", type=Path, default=Path.home())

    for name in ("start", "stop", "status"):
        command = subcommands.add_parser(name)
        command.add_argument("--prefix", type=Path)
        command.add_argument("--generation", type=Path)
        command.add_argument("--runtime-root", type=Path)
        command.add_argument("--home", type=Path, default=Path.home())
        if name in {"start", "stop"}:
            command.add_argument("--timeout", type=float, default=10)

    record_proof = subcommands.add_parser("record-proof")
    record_proof.add_argument("--prefix", type=Path)
    record_proof.add_argument("--runtime-root", type=Path, required=True)
    record_proof.add_argument("--home", type=Path, default=Path.home())
    record_proof.add_argument(
        "--backup-dir",
        type=Path,
        help="where the paired backup set is written; defaults to <state>/backups",
    )
    record_proof.add_argument("--actor", default="operator")
    record_proof.add_argument("--proof-id")
    record_proof.add_argument(
        "--keep",
        type=_positive_int,
        default=DEFAULT_KEEP_BACKUP_SETS,
        help=(
            "how many backup sets to leave under the backup root after this "
            "proof is recorded, newest first and always including this one "
            "(default %(default)s); older sets are deleted"
        ),
    )

    stage_service = subcommands.add_parser("stage-service")
    stage_service.add_argument("--generation", type=Path, required=True)
    stage_service.add_argument("--runtime-root", type=Path, required=True)
    stage_service.add_argument("--staging-root", type=Path, required=True)
    stage_service.add_argument("--home", type=Path, default=Path.home())
    return parser


def _lifecycle_payload(status: LifecycleStatus) -> dict[str, object]:
    payload: dict[str, object] = {"state": status.state}
    if status.generation_identity is not None:
        payload["generation_identity"] = status.generation_identity
    if status.supervisor_pid is not None:
        payload["supervisor_pid"] = status.supervisor_pid
    if status.control_port is not None:
        payload["control_listener"] = {
            "host": "127.0.0.1",
            "port": status.control_port,
        }
    if status.web_port is not None:
        payload["web_listener"] = {
            "host": "127.0.0.1",
            "port": status.web_port,
        }
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_repository_bundle(
                args.repository,
                args.output,
                release_id=args.release_id,
                release_sequence=args.release_sequence,
                web_payload_root=args.web_payload_root,
                web_payload_ledger=args.web_payload_ledger,
                web_build_id=args.web_build_id,
                web_lock_sha256=args.web_lock_sha256,
                node_adapter_version=args.node_adapter_version,
                node_executable=args.node_executable,
                python_runtime_archive=args.python_runtime,
                python_runtime_pin=args.python_runtime_pin,
                requirements=args.requirements,
                wheelhouse=args.wheelhouse,
                sdist_builds=args.sdist_builds,
            )
            payload: object = {"path": str(result.path), "bundle_digest": result.digest}
        elif args.command == "verify":
            bundle = verify_bundle(args.bundle, node_executable=args.node_executable)
            payload = {
                "release_id": bundle.manifest["release_id"],
                "bundle_digest": bundle.digest,
                "channel": bundle.manifest["channel"],
            }
        elif args.command in {"start", "stop", "status"}:
            if args.prefix is not None and args.generation is not None:
                raise InstallError(
                    "installed lifecycle cannot override the current generation"
                )
            if args.generation is None:
                generation_binding = DistributionInstaller(
                    args.prefix
                ).current_generation_binding()
            else:
                generation_binding = contextlib.nullcontext(args.generation)
            with generation_binding as generation:
                lifecycle = LifecycleManager(
                    generation,
                    args.runtime_root,
                    home=args.home,
                    environment=allowed_product_path_environment(os.environ),
                )
                if args.command == "start":
                    payload = _lifecycle_payload(lifecycle.start(timeout=args.timeout))
                elif args.command == "stop":
                    payload = _lifecycle_payload(lifecycle.stop(timeout=args.timeout))
                else:
                    payload = _lifecycle_payload(lifecycle.status())
        elif args.command == "record-proof":
            with DistributionInstaller(args.prefix).current_generation_binding() as generation:
                # Same exemption, same reason as `doctor`: the binding above
                # already bound this generation to the pointer, so the tools
                # pin would only re-measure a PREVIOUS generation against this
                # verifier's own tools. This command exists solely for the
                # cross-generation moment -- the release being installed runs
                # it against the generation already on disk -- so leaving the
                # pin on makes it unable to succeed on the release that
                # introduces it, with no earlier `record-proof` to fall back
                # to. `start|stop|status` keep the pin: they run through the
                # installed prefix's own launcher, which execs that
                # generation's `bundle/tools`, so there the pin is
                # self-consistent (755b7e1).
                lifecycle = LifecycleManager(
                    generation,
                    args.runtime_root,
                    home=args.home,
                    environment=allowed_product_path_environment(os.environ),
                    pin_tools=False,
                )
                # R0-C's own quiescence requirement, at the producing end: a
                # running daemon writes to the database while it is being
                # copied, so the copy would describe a state that never
                # existed. The upgrade the proof unlocks refuses for the same
                # reason, with the same words.
                state = lifecycle.status().state
                if state != "stopped":
                    raise LifecycleError("operation requires the lifecycle to be stopped")
                database = lifecycle.product_paths.control_database_file
                backup_dir = (
                    args.backup_dir
                    if args.backup_dir is not None
                    else lifecycle.product_paths.state_dir / "backups"
                )
                backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                recorded = record_backup_proof(
                    database=database,
                    companion=database.with_name(f".{database.name}.transport.key"),
                    backup_root=backup_dir,
                    python_executable=lifecycle.supervisor_python,
                    environment=lifecycle.generation.control_environment(
                        home=args.home
                    ),
                    actor_id=args.actor,
                    proof_id=args.proof_id,
                )
            # ⟦P5.6⟧ Only after the proof is recorded, and never the set it
            # just wrote: an older set holds nothing the newest-wins gate
            # reads, and five of them filled the mini's disk in one day.
            # A prune that cannot run (a symlinked backup root, a set whose
            # stat fails) must not discard the receipt of a proof that IS
            # recorded: the receipt prints, and the refusal rides along.
            prune_error: dict[str, str] | None = None
            try:
                pruning = prune_backup_sets(
                    backup_dir, keep=args.keep, protect=recorded.backup_root
                ).to_dict()
            except (BackupProofError, OSError) as exc:
                pruning = {"kept": [], "pruned": [], "failed": []}
                prune_error = {"error": type(exc).__name__, "detail": str(exc)}
            payload = {
                "proof_id": recorded.proof_id,
                "source_digest": recorded.source_digest,
                "backup_set_digest": recorded.backup_set_digest,
                "backup_root": str(recorded.backup_root),
                "restore_completed_at": recorded.restore_completed_at,
                "copied_snapshot_count": recorded.copied_snapshot_count,
                "restored_database_count": recorded.restored_database_count,
                "verified_sample_count": recorded.verified_sample_count,
                # Newest-wins: the coordinator selects the newest proof by
                # `restore_completed_at`, so this one is now the one that
                # decides, and it stops deciding after this many seconds.
                "authorizes_for_seconds": recorded.freshness_seconds,
                "kept_backup_sets": pruning["kept"],
                "pruned_backup_sets": pruning["pruned"],
                "prune_failures": pruning["failed"],
                "prune_error": prune_error,
            }
        elif args.command == "stage-service":
            staged = stage_launch_agent(
                args.generation,
                args.runtime_root,
                args.staging_root,
                home=args.home,
                environment=allowed_product_path_environment(os.environ),
            )
            payload = {
                "path": str(staged.path),
                "sha256": staged.sha256,
                "generation_identity": staged.generation_identity,
                "launchctl_invoked": staged.launchctl_invoked,
            }
        else:
            installer = DistributionInstaller(args.prefix)
            if args.command == "install":
                payload = installer.install(
                    args.bundle,
                    node_executable=args.node_executable,
                    allow_unsigned_developer=args.allow_unsigned_developer,
                ).__dict__
            elif args.command == "upgrade":
                payload = installer.upgrade(
                    args.bundle,
                    runtime_root=args.runtime_root,
                    home=args.home,
                    environment=allowed_product_path_environment(os.environ),
                    node_executable=args.node_executable,
                    allow_unsigned_developer=args.allow_unsigned_developer,
                ).__dict__
            elif args.command == "doctor":
                payload = installer.doctor(
                    runtime_root=args.runtime_root,
                    home=args.home,
                    environment=allowed_product_path_environment(os.environ),
                )
            elif args.command == "recover":
                payload = {"action": installer.recover()}
            elif args.command == "rollback":
                payload = installer.rollback(
                    runtime_root=args.runtime_root,
                    home=args.home,
                    environment=allowed_product_path_environment(os.environ),
                ).__dict__
            else:
                installer.uninstall(
                    runtime_root=args.runtime_root,
                    home=args.home,
                    environment=allowed_product_path_environment(os.environ),
                )
                payload = {"uninstalled": True}
    except (
        BackupProofError,
        BundleVerificationError,
        InstallError,
        LifecycleError,
        OSError,
    ) as exc:
        failure = {"ok": False, "error": str(exc)}
        if (
            args.command in {"upgrade", "rollback", "uninstall", "record-proof"}
            and str(exc) == "operation requires the lifecycle to be stopped"
        ):
            # `record-proof` is the only hyphenated member, and a category is
            # read by shell, so the separator stays one character.
            failure["category"] = f"{args.command.replace('-', '_')}_requires_stop"
        print(json.dumps(failure, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "result": payload}, sort_keys=True))
    return 0
