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
    }
