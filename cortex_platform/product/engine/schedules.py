"""D7: one Control-owned schedule table, one bounded tick, one enabled job.

F14 measured that nothing schedules anything: the LaunchAgent sets
`KeepAlive: False` and `RunAtLoad: False`, there is no `StartInterval`
anywhere, and `schedule_runtime_recovery` writes rows nothing polls. So P4.3
builds the smallest honest thing and deliberately deploys no launchd job --
that would recreate the cron jail, the copy-not-symlink rule, and an
out-of-band writer the activation gate cannot see.

The tick reads the gate first, runs at most a bounded amount of work, and
returns. Nothing detaches: the capture consumer's child is spawned and awaited
by the same call.

Exactly one job is enabled. The thirteen legacy rows beside it exist so the
inventory is auditable and are `legacy`, an operation the store refuses to arm.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from cortex_platform.product.control.errors import NotFound
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected

from .capture_consumer import CaptureConsumer, MACHINE_ACTOR, _idempotency_key

CAPTURE_DRAIN_JOB = "capture-drain"
DEFAULT_CAPTURE_INTERVAL_SECONDS = 300
# The thirteen legacy Hermes cron scripts, whose sources stay in the old
# repository. Represented so the inventory is complete and disabled so an
# unknown cadence cannot fire.
LEGACY_JOBS: tuple[str, ...] = (
    "director-sensorium",
    "eval-pipeline-health",
    "idea-incubate",
    "q-journal-commit",
    "q-pending-downgrade",
    "radar-scan",
    "research-digest",
    "review-digest",
    "spar-weekly",
    "taste-refiner",
    "xhs-auth-canary",
    "xhs-pull-scan",
    "xhs-raw-cleanup",
)
# The only two cadences that exist in this repository. Everything else lives in
# the live `~/.hermes/profiles/research/cron/jobs.json`, which
# `deploy-research-crons.sh:28` only smoke-tests, and which the contract forbids
# transcribing from documentation.
REPOSITORY_CADENCES: Mapping[str, tuple[int, str]] = {
    "director-sensorium": (300, "interval:5m"),
    "taste-refiner": (86_400, "cron:0 3 * * *"),
}
LIVE_JOBS_PATH = Path("~/.hermes/profiles/research/cron/jobs.json")
_PLACEHOLDER_INTERVAL_SECONDS = 86_400


@dataclass(frozen=True)
class MigratedJob:
    """One cadence read off a live jobs.json during a declared window."""

    job_key: str
    interval_seconds: int
    legacy_schedule: str


@dataclass(frozen=True)
class JobsFileReading:
    """What the declared window actually found, including finding nothing."""

    path: str
    present: bool
    jobs: tuple[MigratedJob, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "present": self.present,
            "jobs": [
                {
                    "job_key": job.job_key,
                    "interval_seconds": job.interval_seconds,
                    "legacy_schedule": job.legacy_schedule,
                }
                for job in self.jobs
            ],
        }


def read_live_jobs(path: Path | None = None) -> JobsFileReading:
    """Read the live cron registry, and say plainly when it is not there.

    D7 requires the times to be migrated data rather than transcription. An
    absent file is a legitimate answer -- the laptop is not the machine that
    runs the crons -- and it is recorded as `present: false` so nobody later
    mistakes a placeholder cadence for a measured one.
    """

    location = Path(path or LIVE_JOBS_PATH).expanduser()
    if not location.is_file():
        return JobsFileReading(path=str(location), present=False, jobs=())
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return JobsFileReading(path=str(location), present=False, jobs=())
    entries = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    jobs: list[MigratedJob] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        schedule = entry.get("schedule")
        if not name or not isinstance(schedule, dict):
            continue
        kind = str(schedule.get("kind") or "")
        if kind == "interval" and isinstance(schedule.get("minutes"), int):
            minutes = int(schedule["minutes"])
            # `register_research_schedule` bounds `interval_seconds` to
            # [60, 604800], and this file is the operator's, not the product's:
            # a fortnightly cadence is a legitimate thing for it to say and an
            # unbootable daemon is not a legitimate response. The clamp is
            # lossless because `legacy_schedule` keeps the migrated fact
            # verbatim and a legacy row's own interval is inert --
            # `list_due_research_schedules` selects `WHERE enabled = 1` and
            # `register_research_schedule` refuses an enabled `legacy` row.
            jobs.append(
                MigratedJob(
                    name,
                    min(604_800, max(60, minutes * 60)),
                    f"interval:{minutes}m",
                )
            )
        elif kind == "cron" and isinstance(schedule.get("expr"), str):
            # A cron expression is recorded verbatim as the migrated fact and
            # is NOT evaluated here: the row's own interval is what the tick
            # uses, because evaluating cron would need the jail P4 replaces.
            jobs.append(
                MigratedJob(name, _PLACEHOLDER_INTERVAL_SECONDS, f"cron:{schedule['expr']}")
            )
    return JobsFileReading(path=str(location), present=True, jobs=tuple(jobs))


def seed_schedules(
    store: Any,
    *,
    reading: JobsFileReading,
    capture_interval_seconds: int = DEFAULT_CAPTURE_INTERVAL_SECONDS,
) -> list[Mapping[str, Any]]:
    """Land the whole inventory: one enabled job, thirteen legacy rows.

    Idempotent by registration receipt, so a restart re-registers nothing.
    """

    migrated = {job.job_key: job for job in reading.jobs}
    plan: list[tuple[str, str, bool, int, str, str | None]] = [
        (
            CAPTURE_DRAIN_JOB,
            "capture_drain",
            True,
            capture_interval_seconds,
            "product",
            None,
        )
    ]
    for job_key in LEGACY_JOBS:
        job = migrated.get(job_key)
        if job is not None:
            plan.append((job_key, "legacy", False, job.interval_seconds, "migrated", job.legacy_schedule))
            continue
        repository = REPOSITORY_CADENCES.get(job_key)
        if repository is not None:
            # The two schedules this repository actually declares. Recorded as
            # `product` because they are read from code, not from the machine.
            plan.append((job_key, "legacy", False, repository[0], "product", repository[1]))
            continue
        plan.append(
            (job_key, "legacy", False, _PLACEHOLDER_INTERVAL_SECONDS, "unknown", None)
        )

    registered: list[Mapping[str, Any]] = []
    for job_key, operation, enabled, interval, cadence, legacy in plan:
        try:
            registered.append(store.get_research_schedule(job_key))
            continue
        except NotFound:
            pass
        try:
            result = store.register_research_schedule(
                job_key=job_key,
                operation=operation,
                enabled=enabled,
                interval_seconds=interval,
                cadence_source=cadence,
                legacy_schedule=legacy,
                actor_id=MACHINE_ACTOR,
                idempotency_key=_idempotency_key("schedule", job_key),
            )
        except Exception as error:  # noqa: BLE001 - one row is not the inventory
            # Losing one row costs an audit entry; aborting costs the daemon.
            # `capture-drain` is first in the plan and takes its interval from
            # `capture_interval_seconds`, never from the file, so the row that
            # actually runs can never be the one skipped here. The type only:
            # this line lands in the daemon log.
            print(
                f"research schedule not registered: {job_key}: "
                f"{type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            continue
        registered.append(result.value)
    return registered


@dataclass(frozen=True)
class TickReport:
    """What one bounded tick did, in terms a caller can assert on."""

    gate_enabled: bool
    due: tuple[str, ...]
    ran: tuple[str, ...]
    drained: int
    outcomes: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_enabled": self.gate_enabled,
            "due": list(self.due),
            "ran": list(self.ran),
            "drained": self.drained,
            "outcomes": dict(self.outcomes),
        }


class ResearchScheduleTick:
    """One bounded pass over the due schedules. Creates work; detaches nothing."""

    def __init__(
        self,
        *,
        store: Any,
        consumer: CaptureConsumer,
        max_captures_per_tick: int = 1,
        max_jobs_per_tick: int = 5,
    ) -> None:
        if not 1 <= max_captures_per_tick <= 20:
            raise ValueError("max_captures_per_tick must be between 1 and 20")
        self._store = store
        self._consumer = consumer
        self._max_captures = max_captures_per_tick
        self._max_jobs = max_jobs_per_tick

    def run(self) -> TickReport:
        # The gate first, before anything is read or claimed: a tick that runs
        # while dispatch is disabled is exactly the out-of-band writer D7
        # refuses to build.
        if not self._store.runtime_dispatch_enabled():
            return TickReport(
                gate_enabled=False, due=(), ran=(), drained=0, outcomes={}
            )
        due = self._store.list_due_research_schedules(limit=self._max_jobs)
        ran: list[str] = []
        outcomes: dict[str, str] = {}
        drained = 0
        for schedule in due:
            job_key = str(schedule["job_key"])
            started_at = self._store._now()
            outcome, count = self._run_job(schedule)
            drained += count
            outcomes[job_key] = outcome
            if outcome == "ran":
                ran.append(job_key)
            self._store.record_research_schedule_outcome(
                job_key=job_key,
                expected_revision=int(schedule["revision"]),
                outcome=outcome,
                started_at=started_at,
                actor_id=MACHINE_ACTOR,
                idempotency_key=_idempotency_key(
                    "tick", job_key, str(schedule["revision"])
                ),
            )
        return TickReport(
            gate_enabled=True,
            due=tuple(str(item["job_key"]) for item in due),
            ran=tuple(ran),
            drained=drained,
            outcomes=outcomes,
        )

    def _run_job(self, schedule: Mapping[str, Any]) -> tuple[str, int]:
        if str(schedule["operation"]) != "capture_drain":
            # Unreachable through the store, which refuses to enable a legacy
            # row. Kept as a refusal rather than an assertion because a row
            # that somehow armed itself must not run the wrong thing.
            return "skipped", 0
        drained = 0
        for _ in range(self._max_captures):
            try:
                outcome = self._consumer.run_once()
            except EffectPermanentlyRejected as rejection:
                if rejection.category == "runtime_activation_disabled":
                    # The window lapsed mid-tick. Not a failure of the job.
                    return "refused", drained
                raise
            except Exception:  # noqa: BLE001 - one job's failure is not the tick's
                # Deliberately BELOW the gate branch: `run_once` raises
                # `runtime_activation_disabled` before its own try and the
                # branch above owns it. Anything else reaching here is an
                # untyped escape from one job, and letting it out would skip
                # `record_research_schedule_outcome` for every remaining job in
                # the batch and leave this one's revision unadvanced.
                return "failed", drained
            if outcome is None:
                break
            drained += 1
        return ("ran" if drained else "skipped"), drained
