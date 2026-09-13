"""The daemon-facing wiring: what `cortexd` actually starts and stops."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.engine import service as service_module
from cortex_platform.product.engine.bindings import EngineRoots
from cortex_platform.product.engine.schedules import (
    CAPTURE_DRAIN_JOB,
    LEGACY_JOBS,
    JobsFileReading,
    read_live_jobs,
    seed_schedules,
)
from cortex_platform.product.engine.service import (
    EngineService,
    ResearchScheduleRunner,
    build_engine_service,
)
from cortex_platform.product.paths import resolve_paths

from .arxiv_fixture import ArxivFixtureServer

HTML_PAPER = "2601.00042"


class _Boom:
    """A tick that fails the way an untyped escape used to fail: silently."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self) -> None:
        self.runs += 1
        raise ValueError("adoption manifest is empty")


class _Service:
    def __init__(self, tick: object) -> None:
        self.tick = tick


@pytest.fixture
def arxiv() -> ArxivFixtureServer:
    with ArxivFixtureServer() as server:
        yield server


@pytest.fixture
def paths(tmp_path: Path):
    return resolve_paths(
        environ={
            "HOME": str(tmp_path / "home"),
            "CORTEX_DATA_DIR": str(tmp_path / "data"),
            "CORTEX_STATE_DIR": str(tmp_path / "state"),
        },
        platform="darwin",
    )


@pytest.fixture
def service(
    store: ControlStore,
    roots: EngineRoots,
    research_db: Path,
    arxiv: ArxivFixtureServer,
    paths,
) -> EngineService:
    """The production assembly: `build_engine_service`, nothing hand-wired."""

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
    value = build_engine_service(
        store=store,
        paths=paths,
        config={},
        literal_overrides=arxiv.literal_overrides(),
    )
    assert value is not None
    # Embedding needs a provider key; nothing else about the wiring is touched.
    value.supervisor._skip_embed = True
    return value


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


def test_a_failing_tick_is_reported_by_type_and_never_by_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tick that fails every interval must not do so silently.

    Only the exception's type is emitted: the message carries whatever the
    engine put in it and this line reaches the daemon's stderr.
    """

    tick = _Boom()
    runner = ResearchScheduleRunner(
        service=_Service(tick), store=object(), interval_seconds=0.01
    )
    thread = threading.Thread(target=runner._loop, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while tick.runs < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    runner._stop.set()
    thread.join(timeout=5.0)

    assert runner.failures >= 1
    assert runner.last_failure == "ValueError"
    captured = capsys.readouterr()
    assert "research-schedule-tick failed: ValueError" in captured.err
    assert "adoption manifest is empty" not in captured.err


def test_stop_kills_the_in_flight_effect_child_instead_of_orphaning_it(
    store: ControlStore,
    service: EngineService,
    arxiv: ArxivFixtureServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cortexd stop` must not leave a child writing the copied research DB.

    Joining the tick thread with a timeout shorter than the child's own hard
    timeout returned from `stop()` while a real ingest was still running, so the
    child outlived the daemon that spawned it. Terminating it first converts the
    orphan into the killed-child path V3 and ⟦AMD-5⟧ already model.
    """

    arxiv.stall_seconds = 60.0
    _approve(store, f"https://arxiv.org/abs/{HTML_PAPER}", "stall")
    # `start()` seeds from the live cron registry; this test owns neither.
    monkeypatch.setattr(
        service_module,
        "read_live_jobs",
        lambda *_, **__: JobsFileReading(path="absent", present=False, jobs=()),
    )
    runner = ResearchScheduleRunner(service=service, store=store, interval_seconds=0.05)
    runner.start()
    try:
        deadline = time.monotonic() + 30.0
        while service.supervisor.in_flight is None and time.monotonic() < deadline:
            time.sleep(0.02)
        process = service.supervisor.in_flight
        assert process is not None, "no effect child was ever spawned"
        assert process.poll() is None, "the child had already exited"
        pid = process.pid

        runner.stop(timeout=10.0)
    finally:
        runner.stop(timeout=10.0)

    assert process.poll() is not None, "an engine child survived stop()"
    assert process.returncode < 0, "the child exited on its own, not on a signal"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_production_shaped_drain_computes_and_lands_the_gdrive_digest_pair(
    store: ControlStore,
    service: EngineService,
    roots: EngineRoots,
) -> None:
    """⟦AMD-1⟧/⟦AMD-12⟧ nothing hand-sets `_watch_roots`, and the pair persists.

    `build_engine_service` never passed `watch_roots`, so the child's gdrive
    block was skipped and `execution.gdrive` was None on every production drain
    -- the pair was not merely unpersisted, it was never computed. And nothing
    carried it into `ControlStore`, so ⟦AMD-1⟧'s landing surface did not exist.
    """

    decoy = roots.home / "gdrive" / "agent-readings" / "papers" / "20260101-Untouched"
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "notes.md").write_text("an existing note\n", encoding="utf-8")
    (decoy / "link.md").symlink_to("notes.md")

    capture_id = _approve(store, f"https://arxiv.org/abs/{HTML_PAPER}", "watch")
    outcome = service.consumer.run_once()
    assert outcome is not None and outcome.state == "consumed"

    execution = service.engine.outcomes[f"capture.import.{capture_id}"].execution
    pair = execution.gdrive
    assert pair is not None, "the digest pair was never computed"
    assert list(pair["changed"]) == []
    name = next(iter(pair["before"]))
    assert pair["before"][name]["tree_digest"] == pair["after"][name]["tree_digest"]
    assert int(pair["before"][name]["entry_count"]) >= 2

    landed = store.effect_watch_digests(effect_id=str(outcome.effect_id))
    assert len(landed) == 1
    assert landed[0]["before"] == pair["before"]
    assert landed[0]["after"] == pair["after"]
    assert landed[0]["changed"] == []


def _jobs_file(path: Path, minutes: int) -> Path:
    path.write_text(
        json.dumps(
            {"jobs": [{"name": "radar-scan", "schedule": {"kind": "interval", "minutes": minutes}}]}
        ),
        encoding="utf-8",
    )
    return path


def test_an_out_of_range_legacy_cadence_does_not_stop_cortexd(
    store: ControlStore,
    service: EngineService,
    paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad cadence in the live cron registry is not a reason not to boot.

    `register_research_schedule` bounds `interval_seconds` to [60, 604800], so a
    fortnightly `{"kind": "interval", "minutes": 20160}` raised `ValueError` out
    of `seed_schedules` -> `runner.start()` -> `start_research_schedules`, and
    `cortexd` died on a file the product does not own and cannot fix.
    """

    jobs = _jobs_file(tmp_path / "jobs.json", 20_160)
    monkeypatch.setattr(
        service_module, "read_live_jobs", lambda *_, **__: read_live_jobs(jobs)
    )
    runner = service_module.start_research_schedules(
        store=store, paths=paths, config={}
    )
    try:
        assert runner is not None
        rows = {str(item["job_key"]) for item in store.list_research_schedules()}
        assert rows == {CAPTURE_DRAIN_JOB, *LEGACY_JOBS}
        assert len(rows) == 14
        radar = store.get_research_schedule("radar-scan")
        # Clamped for the row, verbatim for the migrated fact.
        assert int(radar["interval_seconds"]) == 604_800
        assert radar["legacy_schedule"] == "interval:20160m"
        assert not radar["enabled"]
    finally:
        if runner is not None:
            runner.stop(timeout=10.0)


def test_a_row_that_cannot_register_is_skipped_not_fatal(
    store: ControlStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The inventory is auditable; one unregistrable row must not cost the rest."""

    real = store.register_research_schedule

    def refuse_one(*, job_key: str, **kwargs: object):
        if job_key == "radar-scan":
            raise ValueError("interval_seconds must be between 60 and 604800")
        return real(job_key=job_key, **kwargs)

    store.register_research_schedule = refuse_one  # type: ignore[method-assign]
    seeded = seed_schedules(
        store, reading=JobsFileReading(path="absent", present=False, jobs=())
    )
    keys = {str(item["job_key"]) for item in seeded}
    assert CAPTURE_DRAIN_JOB in keys
    assert "radar-scan" not in keys
    assert len(keys) == 13
    captured = capsys.readouterr()
    assert "radar-scan" in captured.err
    assert "ValueError" in captured.err
    assert "604800" not in captured.err


def test_engine_wiring_that_fails_reports_a_type_and_leaves_cortexd_running(
    store: ControlStore,
    paths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`cortex start` reports only a readiness timeout, so the log line matters."""

    def explode(**_: object):
        raise RuntimeError("a corpus root that names a secret /tmp/xyz")

    monkeypatch.setattr(service_module, "build_engine_service", explode)
    assert service_module.start_research_schedules(store=store, paths=paths) is None
    captured = capsys.readouterr()
    assert "engine wiring failed: RuntimeError" in captured.err
    assert "/tmp/xyz" not in captured.err


def test_an_env_reference_the_daemon_cannot_use_is_reported_not_silently_dropped(
    store: ControlStore,
    roots: EngineRoots,
    research_db: Path,
    paths,
) -> None:
    """⟦AMD-8⟧'s rule is right; its silence was the defect.

    `keychain_only` is never passed by any caller in the repository, so the
    "deliberate foreground path" the module describes has no production call
    site and every `env://` reference is dropped unconditionally -- while
    `validate_config` accepts one and `config.py` advertises the syntax.
    """

    store.register_asset_root(
        root_id="research-corpus",
        private_path=roots.corpus_root,
        max_bytes=1 << 30,
        enabled=True,
        actor_id="local-operator",
        idempotency_key="engine-corpus-root0002",
    )
    value = build_engine_service(
        store=store,
        paths=paths,
        config={
            "secret_refs": {
                "openrouter": "env://P4_FAKE_OPENROUTER",
                "anthropic": "keychain://cortex/anthropic",
            }
        },
    )
    assert value is not None
    assert value.dropped_secret_aliases == ("openrouter",)


def test_doctor_names_the_unusable_aliases_and_still_exits_healthy(
    tmp_path: Path,
) -> None:
    from cortex_platform.product.diagnostics import doctor
    from cortex_platform.product.paths import resolve_paths as _resolve

    registry = _resolve(environ={"HOME": str(tmp_path / "home")}, platform="darwin")
    for directory in registry.directories():
        directory.mkdir(parents=True, exist_ok=True)
    registry.config_file.write_text(
        'config_version = 1\n\n[secret_refs]\n'
        'openrouter = "env://P4_FAKE_OPENROUTER"\n',
        encoding="utf-8",
    )
    report = doctor(registry, environ={})
    rendered = report.render()
    assert "secret_refs: 1 unusable under the supervised daemon" in rendered
    assert "env:// resolves only on the foreground path" in rendered
    assert "openrouter" in rendered
    # The alias only -- never the variable it names, and never a value.
    assert "P4_FAKE_OPENROUTER" not in rendered
    assert report.healthy is True

    # A configuration the daemon can honour in full says nothing here.
    registry.config_file.write_text(
        'config_version = 1\n\n[secret_refs]\n'
        'openrouter = "keychain://cortex/openrouter"\n',
        encoding="utf-8",
    )
    assert "secret_refs" not in doctor(registry, environ={}).render()
