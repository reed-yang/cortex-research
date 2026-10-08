"""The XHS plugin's status, read for `cortex xhs status` and `GET /api/v1/xhs/status`.

Everything here reads Control state and settings only. No path, task payload,
lease or credential is part of the answer.
"""

from __future__ import annotations

from typing import Any

from ..config import XhsSettings

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
_FALLBACK_RUN_FIELDS = (
    "id",
    "state",
    "trigger",
    "started_at",
    "finished_at",
    "item_cap",
    "model",
    "effort",
    "prompt_version",
    "summary",
    "digest_state",
    "digest_reason",
)


def xhs_refusal(settings: XhsSettings, store: Any) -> str | None:
    """Why the drain would refuse to run, as `XhsDrain.refusal` says it."""

    if not settings.enabled:
        return "disabled_in_config"
    if any(state != "ready" for state in store.xhs_roots_status().values()):
        return "roots_not_ready"
    return None


def public_blogger(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row.get(name) for name in _BLOGGER_FIELDS}


def schedule_keys() -> tuple[str, str]:
    # Imported here: the engine modules stay out of every other command.
    from ..engine.schedules import XHS_DRAIN_JOB, XHS_PULL_JOB

    return XHS_PULL_JOB, XHS_DRAIN_JOB


def xhs_schedules(store: Any) -> dict[str, dict[str, Any]]:
    schedules = {}
    for job_key in schedule_keys():
        row = store.get_research_schedule(job_key)
        schedules[job_key] = {
            "enabled": bool(row["enabled"]),
            "revision": row["revision"],
            "interval_seconds": row["interval_seconds"],
            "next_due_at": row.get("next_due_at"),
            "last_outcome": row.get("last_outcome"),
        }
    return schedules


def public_fallback_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if run is None else {name: run.get(name) for name in _FALLBACK_RUN_FIELDS}


def xhs_fallback_status(store: Any, settings: XhsSettings) -> dict[str, Any]:
    """The weekly fallback: its switch, the running and the last run, when the
    next may start, the backlog it would take, and what waits for the operator.

    `backlog` counts the recommendations a run could still take once the rules
    applied; an input an earlier run already took is not counted. Read-only.
    """

    state = store.xhs_fallback_state()
    running = public_fallback_run(state["running"])
    if running is not None:
        items = store.list_xhs_fallback_items(running["id"])
        running["items"] = len(items)
        running["remaining"] = sum(
            1 for item in items if item["state"] in {"pending", "deciding", "verifying"}
        )
    selection = store.preview_xhs_fallback_run(item_cap=settings.fallback_weekly_cap)[
        "selection"
    ]
    return {
        "enabled": settings.fallback_enabled,
        "running": running,
        "last": public_fallback_run(state["last"]),
        "next_start_at": state["next_start_at"],
        "backlog": selection["eligible"] - selection["already_reviewed"],
        "needs_operator": store.xhs_needs_operator_count(),
    }


def xhs_status(store: Any, settings: XhsSettings) -> dict[str, Any]:
    usage = store.xhs_usage()
    return {
        "enabled_in_config": settings.enabled,
        "refusal": xhs_refusal(settings, store),
        "roots": store.xhs_roots_status(),
        "schedules": xhs_schedules(store),
        "bloggers": [
            public_blogger(row) for row in store.list_xhs_bloggers(followed_only=True)
        ],
        "tasks": store.xhs_task_counts(),
        "usage": {
            provider: {"calls": usage.get(provider, 0), "cap": int(cap)}
            for provider, cap in sorted(settings.daily_calls.items())
        },
        "last_failures": store.xhs_last_failures(),
        "fallback": xhs_fallback_status(store, settings),
    }
