"""D7 behaviour: the inventory it lands and the bounded tick that drains it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import InvalidTransition
from cortex_platform.product.engine.bindings import EngineRoots
from cortex_platform.product.engine.capture_consumer import CaptureConsumer
from cortex_platform.product.engine.port import ProductResearchEngine
from cortex_platform.product.engine.schedules import (
    CAPTURE_DRAIN_JOB,
    LEGACY_JOBS,
    ResearchScheduleTick,
    read_live_jobs,
    seed_schedules,
)
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor

from .arxiv_fixture import ArxivFixtureServer

HTML_PAPER = "2601.00042"




@pytest.fixture
def arxiv() -> ArxivFixtureServer:
    with ArxivFixtureServer() as server:
        yield server


@pytest.fixture
def consumer(
    store: ControlStore, roots: EngineRoots, research_db: Path, arxiv: ArxivFixtureServer
) -> CaptureConsumer:
    store.enable_runtime_activation(
        mode="window",
        window_seconds=3_600,
        actor_id="local-operator",
        idempotency_key="activation-window-00001",
    )
    store.register_asset_root(
        root_id="research-corpus",
        private_path=roots.corpus_root,
        max_bytes=1 << 30,
        enabled=True,
        actor_id="local-operator",
        idempotency_key="engine-corpus-root0001",
    )
    supervisor = ResearchEffectSupervisor(
        store=store,
        roots=roots,
        skip_embed=True,
        timeout_seconds=180,
        literal_overrides=arxiv.literal_overrides(),
    )
    holder: dict[str, CaptureConsumer] = {}
    engine = ProductResearchEngine(
        store=store,
        supervisor=supervisor,
        roots=roots,
        corpus_root_id="research-corpus",
        actor_id="machine:p4-capture-consumer",
        payload_resolver=lambda source_id: holder["consumer"].payload_for(source_id),
    )
    holder["consumer"] = CaptureConsumer(store=store, engine=engine)
    return holder["consumer"]


def _approve(store: ControlStore, payload: str, tag: str) -> str:
    created = store.create_capture(
        payload=payload,
        note="",
        actor_id="local-operator",
        idempotency_key=f"capture-create-{tag}0000000",
    )
    store.approve_capture(
        capture_id=str(created.value["id"]),
        expected_revision=int(created.value["revision"]),
        actor_id="local-operator",
        idempotency_key=f"capture-approve-{tag}000000",
    )
    return str(created.value["id"])


# -- the inventory --------------------------------------------------------


def test_the_live_jobs_file_is_read_not_transcribed(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "radar-scan", "schedule": {"kind": "cron", "expr": "7 8 * * *"}},
                    {"name": "idea-incubate", "schedule": {"kind": "interval", "minutes": 30}},
                ]
            }
        ),
        encoding="utf-8",
    )
    reading = read_live_jobs(path)
    assert reading.present is True
    migrated = {job.job_key: job for job in reading.jobs}
    assert migrated["radar-scan"].legacy_schedule == "cron:7 8 * * *"
    assert migrated["idea-incubate"].interval_seconds == 1_800


def test_an_absent_live_file_is_recorded_as_absent(tmp_path: Path) -> None:
    """A missing cron registry is an answer, not a licence to invent times."""

    reading = read_live_jobs(tmp_path / "not-there.json")
    assert reading.present is False
    assert reading.jobs == ()


def test_exactly_one_job_is_enabled(store: ControlStore, tmp_path: Path) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    schedules = store.list_research_schedules()
    enabled = [item for item in schedules if item["enabled"]]
    assert [item["job_key"] for item in enabled] == [CAPTURE_DRAIN_JOB]
    assert len(schedules) == len(LEGACY_JOBS) + 1
    assert all(
        item["operation"] == "legacy"
        for item in schedules
        if item["job_key"] != CAPTURE_DRAIN_JOB
    )


def test_an_unknown_cadence_is_labelled_unknown(
    store: ControlStore, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    radar = store.get_research_schedule("radar-scan")
    assert radar["cadence_source"] == "unknown"
    assert radar["legacy_schedule"] is None
    # The two cadences that DO exist in this repository are labelled honestly.
    assert store.get_research_schedule("taste-refiner")["legacy_schedule"] == (
        "cron:0 3 * * *"
    )


def test_a_legacy_job_can_never_be_armed(store: ControlStore, tmp_path: Path) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    radar = store.get_research_schedule("radar-scan")
    with pytest.raises(InvalidTransition):
        store.set_research_schedule_enabled(
            job_key="radar-scan",
            enabled=True,
            expected_revision=int(radar["revision"]),
            actor_id="local-operator",
            idempotency_key="arm-radar-scan-000001",
        )
    with pytest.raises(ValueError, match="legacy"):
        store.register_research_schedule(
            job_key="another-legacy",
            operation="legacy",
            enabled=True,
            interval_seconds=3_600,
            cadence_source="unknown",
            legacy_schedule=None,
            actor_id="local-operator",
            idempotency_key="register-legacy-000001",
        )


def test_seeding_is_idempotent(store: ControlStore, tmp_path: Path) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    before = store.list_research_schedules()
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    assert store.list_research_schedules() == before


# -- the tick -------------------------------------------------------------


def test_the_tick_reads_the_gate_before_anything_else(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    _approve(store, f"https://arxiv.org/abs/{HTML_PAPER}", "a")
    store.disable_runtime_activation(
        actor_id="local-operator", idempotency_key="activation-disable-0001"
    )
    report = ResearchScheduleTick(store=store, consumer=consumer).run()
    assert report.gate_enabled is False
    assert report.due == () and report.ran == ()
    assert store.get_capture(_only_capture(store))["state"] == "approved"


def test_one_tick_drains_one_capture_and_re_arms(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    _approve(store, f"https://arxiv.org/abs/{HTML_PAPER}", "a")
    tick = ResearchScheduleTick(store=store, consumer=consumer)
    report = tick.run()

    assert report.gate_enabled is True
    assert report.ran == (CAPTURE_DRAIN_JOB,)
    assert report.drained == 1
    schedule = store.get_research_schedule(CAPTURE_DRAIN_JOB)
    assert schedule["last_outcome"] == "ran"
    assert schedule["next_due_at"] > store._now()
    assert store.get_capture(_only_capture(store))["state"] == "consumed"


def test_a_second_tick_before_the_interval_does_nothing(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    tick = ResearchScheduleTick(store=store, consumer=consumer)
    tick.run()
    assert tick.run().due == ()


def test_the_tick_is_bounded_by_its_own_budget(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    _approve(store, f"https://arxiv.org/abs/{HTML_PAPER}", "a")
    _approve(store, "2601.00042v2", "b")
    report = ResearchScheduleTick(
        store=store, consumer=consumer, max_captures_per_tick=1
    ).run()
    assert report.drained == 1
    remaining = [
        item for item in store.list_captures(limit=50) if item["state"] == "approved"
    ]
    assert len(remaining) == 1


def test_an_empty_inbox_is_a_skip_not_a_failure(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    report = ResearchScheduleTick(store=store, consumer=consumer).run()
    assert report.outcomes[CAPTURE_DRAIN_JOB] == "skipped"
    assert store.get_research_schedule(CAPTURE_DRAIN_JOB)["last_outcome"] == "skipped"


def test_a_window_that_lapses_mid_tick_is_a_refusal(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))
    _approve(store, f"https://arxiv.org/abs/{HTML_PAPER}", "a")
    original = store.list_due_research_schedules

    def close_the_window(**kwargs):
        due = original(**kwargs)
        store.disable_runtime_activation(
            actor_id="local-operator", idempotency_key="activation-disable-0002"
        )
        return due

    store.list_due_research_schedules = close_the_window  # type: ignore[method-assign]
    report = ResearchScheduleTick(store=store, consumer=consumer).run()
    assert report.outcomes[CAPTURE_DRAIN_JOB] == "refused"
    assert report.drained == 0


def test_an_untyped_escape_from_one_job_is_recorded_not_raised(
    store: ControlStore, consumer: CaptureConsumer, tmp_path: Path
) -> None:
    """Below the gate branch: the tick records `failed` and keeps its contract.

    An untyped escape used to leave `record_research_schedule_outcome`
    unreached, so the job's revision never advanced and the batch was abandoned
    mid-flight.
    """

    seed_schedules(store, reading=read_live_jobs(tmp_path / "absent.json"))

    def explode() -> None:
        raise ValueError("adoption manifest is empty")

    consumer.run_once = explode  # type: ignore[method-assign]
    report = ResearchScheduleTick(store=store, consumer=consumer).run()
    assert report.outcomes[CAPTURE_DRAIN_JOB] == "failed"
    assert report.drained == 0
    assert store.get_research_schedule(CAPTURE_DRAIN_JOB)["last_outcome"] == "failed"


def test_the_tick_deploys_no_launchd_job() -> None:
    """D7 refuses to recreate the cron jail; nothing here writes a plist."""

    from cortex_platform.product.engine import schedules

    source = Path(schedules.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    body = code.split('"""', 2)[-1]
    for banned in ("launchctl", "LaunchAgents", "StartInterval", "start_new_session"):
        assert banned not in body


def _only_capture(store: ControlStore) -> str:
    items = store.list_captures(limit=50)
    return str(items[0]["id"])
