"""`cortex xhs …`: follow bloggers, arm the plugin, start scans and see status.

Every command prints one JSON object on stdout; a refusal is a non-zero exit
with a category on stderr. The commands write Control state only. Scans and
retries are queued for cortexd's drain; no command here calls a provider or
reads a credential.

`enable` and `disable` arm both schedule rows, each at the revision just
read. `[xhs] enabled` in the configuration is a separate switch, and the
plugin runs only when both are on and both asset roots are ready.
`init-roots` creates and registers those roots at their default locations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import (
    XHS_ROOT_MAX_BYTES,
    XhsSettings,
    load_config,
    xhs_asset_root_paths,
    xhs_settings,
)
from .control import ControlStore, ControlStoreError, NotFound
from .control.xhs_store import XHS_ROLES, XHS_TASK_KINDS
from .paths import PathRegistry
from .xhs.status import public_blogger as _blogger
from .xhs.status import schedule_keys as _schedule_keys
from .xhs.status import xhs_refusal as _refusal
from .xhs.status import xhs_schedules as _schedules
from .xhs.status import xhs_status as _status

_ACTOR = "local-operator"


def add_xhs_parser(subparsers: Any, *, common: argparse.ArgumentParser) -> None:
    parser = subparsers.add_parser("xhs", parents=[common])
    actions = parser.add_subparsers(dest="xhs_command", required=True)
    roles = sorted(XHS_ROLES)

    def writer(name: str) -> argparse.ArgumentParser:
        command = actions.add_parser(name, parents=[common])
        command.add_argument("--actor", default=_ACTOR)
        command.add_argument("--idempotency-key")
        return command

    follow = writer("follow")
    follow.add_argument("user_id")
    follow.add_argument("--role", choices=roles, required=True)
    follow.add_argument("--name")
    writer("unfollow").add_argument("user_id")
    set_role = writer("set-role")
    set_role.add_argument("user_id")
    set_role.add_argument("role", choices=roles)
    actions.add_parser("list", parents=[common])
    actions.add_parser("status", parents=[common])
    writer("enable")
    writer("disable")
    actions.add_parser("init-roots", parents=[common]).add_argument("--actor", default=_ACTOR)
    scan = actions.add_parser("scan", parents=[common])
    scan.add_argument("--user", action="append", dest="users")
    scan.add_argument("--full", action="store_true")
    scan.add_argument("--max-pages", type=int)
    scan.add_argument("--yes", action="store_true")
    retry = writer("retry")
    retry.add_argument("--failed", action="store_true")
    retry.add_argument("--kind", choices=sorted(XHS_TASK_KINDS))


def _key(arguments: argparse.Namespace, prefix: str) -> str:
    supplied = getattr(arguments, "idempotency_key", None)
    if supplied:
        return str(supplied)
    return f"cli-xhs-{prefix}-{uuid.uuid4().hex}"[:64]


def _estimate(
    store: ControlStore, settings: XhsSettings, users: list[str] | None, max_pages: int
) -> dict[str, Any]:
    """The calls a scan may make. Only list pages are bounded in advance; each
    new image note then costs one detail, one OCR call per image and one
    identification, plus one link search per blog without a written link."""

    followed = [row["user_id"] for row in store.list_xhs_bloggers(followed_only=True)]
    selected = followed if not users else [user for user in followed if user in users]
    return {
        "bloggers": len(selected),
        "max_pages": max_pages,
        "tikhub_list_calls_at_most": len(selected) * max_pages,
        "per_new_image_note": {
            "tikhub": 1,
            "ocr": "one per image",
            "gpt": "one, plus one per blog without a written link",
        },
        "daily_calls": dict(sorted(settings.daily_calls.items())),
        "used_today": store.xhs_usage(),
    }


def run_xhs_command(arguments: argparse.Namespace, paths: PathRegistry) -> int:
    command = arguments.xhs_command
    try:
        config = load_config(paths.config_file) if paths.config_file.is_file() else {}
        settings = xhs_settings(config)
        database = paths.control_database_file
        store = ControlStore(database)
        if command in {"list", "status"}:
            # These read: no `initialize()`, so nothing is created or migrated.
            if not database.is_file():
                raise ValueError("the Control database does not exist; run `cortex init`")
        else:
            store.initialize()
        if command == "init-roots":
            payload = _init_roots(paths, store, settings, arguments.actor)
        else:
            payload = _run(command, arguments, store, settings)
        if payload is None:
            return 1
    except ControlStoreError as exc:
        print(json.dumps({"error": exc.category, "message": str(exc)}), file=sys.stderr)
        return 1
    except (ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": "invalid_request", "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _run(
    command: str,
    arguments: argparse.Namespace,
    store: ControlStore,
    settings: XhsSettings,
) -> dict[str, Any] | None:
    actor = getattr(arguments, "actor", _ACTOR)
    if command == "follow":
        return _blogger(
            store.follow_xhs_blogger(
                user_id=arguments.user_id,
                role=arguments.role,
                display_name=arguments.name,
                actor_id=actor,
                idempotency_key=_key(arguments, "follow"),
            ).value
        )
    if command == "unfollow":
        return _blogger(
            store.unfollow_xhs_blogger(
                user_id=arguments.user_id,
                actor_id=actor,
                idempotency_key=_key(arguments, "unfollow"),
            ).value
        )
    if command == "set-role":
        return _blogger(
            store.set_xhs_blogger_role(
                user_id=arguments.user_id,
                role=arguments.role,
                actor_id=actor,
                idempotency_key=_key(arguments, "role"),
            ).value
        )
    if command == "list":
        return {"bloggers": [_blogger(row) for row in store.list_xhs_bloggers()]}
    if command == "status":
        return _status(store, settings)
    if command in {"enable", "disable"}:
        enabled = command == "enable"
        key = _key(arguments, command)
        for job_key in _schedule_keys():
            row = store.get_research_schedule(job_key)
            if bool(row["enabled"]) == enabled:
                continue
            store.set_research_schedule_enabled(
                job_key=job_key,
                enabled=enabled,
                expected_revision=row["revision"],
                actor_id=actor,
                idempotency_key=f"{key}-{job_key}"[:80],
            )
        return {"schedules": _schedules(store), "refusal": _refusal(settings, store)}
    if command == "scan":
        return _scan(arguments, store, settings)
    if command == "retry":
        if not arguments.failed:
            raise ValueError("retry needs --failed")
        return store.retry_failed_xhs_tasks(
            kinds=[arguments.kind] if arguments.kind else None,
            actor_id=actor,
            idempotency_key=_key(arguments, "retry"),
        ).value
    raise ValueError(f"unknown xhs command: {command}")


def _overlaps(first: Path, second: Path) -> bool:
    first, second = Path(os.path.realpath(first)), Path(os.path.realpath(second))
    return first.is_relative_to(second) or second.is_relative_to(first)


def _init_roots(
    paths: PathRegistry, store: ControlStore, settings: XhsSettings, actor: str
) -> dict[str, Any]:
    """Create and register `xhs-notes` and `blogs` at their default locations.

    Each directory is made owner-private without following a link, and one
    that is a link, not a directory or not owned by this user is refused. A
    root already registered is reported and left as it is, so running this
    again changes nothing.
    """

    # Imported here: the materializer stays out of every other command.
    from .artifacts.materializer import MaterializerError, _open_directory, _open_secure_root

    try:
        corpus: Path | None = store.get_asset_root("research-corpus").private_path
    except NotFound:
        corpus = None
    roots: dict[str, dict[str, Any]] = {}
    for root_id, path in sorted(xhs_asset_root_paths(paths).items()):
        try:
            existing = store.get_asset_root(root_id)
        except NotFound:
            existing = None
        if existing is not None:
            roots[root_id] = {
                "action": (
                    "already_registered"
                    if existing.private_path == path
                    else "registered_elsewhere"
                ),
                "path": str(existing.private_path),
                "enabled": existing.enabled,
                "max_bytes": existing.max_bytes,
            }
            continue
        if corpus is not None and _overlaps(path, corpus):
            raise ValueError(f"the {root_id} root would overlap the research corpus")
        descriptors: list[int] = []
        try:
            descriptors.append(_open_secure_root(paths.data_dir)[0])
            for name in path.relative_to(paths.data_dir).parts:
                descriptors.append(
                    _open_directory(descriptors[-1], name, create=True, private=True)
                )
        except MaterializerError as exc:
            raise ValueError(f"the {root_id} root cannot be created safely: {exc}") from exc
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        record = store.register_asset_root(
            root_id=root_id,
            private_path=path,
            max_bytes=XHS_ROOT_MAX_BYTES,
            enabled=True,
            actor_id=actor,
            idempotency_key=(
                f"xhs-root-{root_id}-"
                + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
            ),
        )
        roots[root_id] = {
            "action": "registered",
            "path": str(record.private_path),
            "enabled": record.enabled,
            "max_bytes": record.max_bytes,
        }
    return {
        "roots": roots,
        "status": store.xhs_roots_status(),
        "refusal": _refusal(settings, store),
    }


def _scan(
    arguments: argparse.Namespace, store: ControlStore, settings: XhsSettings
) -> dict[str, Any] | None:
    """Queue page 1 of one scan per followed blogger, or per named one.

    A full scan is a backfill past seen pages: it needs `--max-pages` and,
    after the printed estimate, `--yes`.
    """

    refusal = _refusal(settings, store)
    if refusal is not None:
        raise ValueError(f"the XHS plugin will not run: {refusal}")
    if arguments.full and arguments.max_pages is None:
        raise ValueError("--full needs --max-pages")
    max_pages = arguments.max_pages or settings.max_list_pages
    if not 1 <= max_pages <= 1_000:
        raise ValueError("--max-pages must be between 1 and 1000")
    users = [user.strip().lower() for user in arguments.users] if arguments.users else None
    estimate = _estimate(store, settings, users, max_pages)
    if arguments.full and not arguments.yes:
        print(
            json.dumps(
                {
                    "estimate": estimate,
                    "enqueued": False,
                    "message": "a full scan is a backfill; repeat with --yes to queue it",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return None
    created = store.start_xhs_scans(max_pages=max_pages, user_ids=users, full=arguments.full)
    return {
        "estimate": estimate,
        "enqueued": True,
        "scans": [
            {"task_id": task["id"], "user_id": task["payload"]["user_id"], "full": arguments.full}
            for task in created
        ],
    }
