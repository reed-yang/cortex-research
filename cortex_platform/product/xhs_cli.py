"""`cortex xhs …`: follow bloggers, arm the plugin, start scans and see status.

Every command prints one JSON object on stdout; a refusal is a non-zero exit
with a category on stderr. The commands write Control state only. Scans and
retries are queued for cortexd's drain; no command here calls a provider or
reads a credential.

`enable` and `disable` arm both schedule rows, each at the revision just
read. `[xhs] enabled` in the configuration is a separate switch, and the
plugin runs only when both are on and both asset roots are ready.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from typing import Any

from .config import XhsSettings, load_config, xhs_settings
from .control import ControlStore, ControlStoreError
from .control.xhs_store import XHS_ROLES, XHS_TASK_KINDS
from .paths import PathRegistry

_ACTOR = "local-operator"
_BLOGGER_FIELDS = (
    "user_id",
    "display_name",
    "role",
    "followed",
    "last_scan_at",
    "last_scan_outcome",
    "last_scan_error",
    "last_new_note_at",
)


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


def _refusal(settings: XhsSettings, store: ControlStore) -> str | None:
    """Why the drain would refuse to run, as `XhsDrain.refusal` says it."""

    if not settings.enabled:
        return "disabled_in_config"
    if any(state != "ready" for state in store.xhs_roots_status().values()):
        return "roots_not_ready"
    return None


def _blogger(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row.get(name) for name in _BLOGGER_FIELDS}


def _schedule_keys() -> tuple[str, str]:
    # Imported here: the engine modules stay out of every other command.
    from .engine.schedules import XHS_DRAIN_JOB, XHS_PULL_JOB

    return XHS_PULL_JOB, XHS_DRAIN_JOB


def _schedules(store: ControlStore) -> dict[str, dict[str, Any]]:
    schedules = {}
    for job_key in _schedule_keys():
        row = store.get_research_schedule(job_key)
        schedules[job_key] = {
            "enabled": bool(row["enabled"]),
            "revision": row["revision"],
            "interval_seconds": row["interval_seconds"],
            "next_due_at": row.get("next_due_at"),
            "last_outcome": row.get("last_outcome"),
        }
    return schedules


def _status(store: ControlStore, settings: XhsSettings) -> dict[str, Any]:
    usage = store.xhs_usage()
    return {
        "enabled_in_config": settings.enabled,
        "refusal": _refusal(settings, store),
        "roots": store.xhs_roots_status(),
        "schedules": _schedules(store),
        "bloggers": [
            _blogger(row) for row in store.list_xhs_bloggers(followed_only=True)
        ],
        "tasks": store.xhs_task_counts(),
        "usage": {
            provider: {"calls": usage.get(provider, 0), "cap": int(cap)}
            for provider, cap in sorted(settings.daily_calls.items())
        },
        "last_failures": store.xhs_last_failures(),
    }


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
