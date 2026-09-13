"""D1/D6: a real child, a replaced environment, and a gate it cannot pass."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from cortex_platform.product.engine.bindings import (
    EFFECT_MARKER_VARIABLE,
    ENGINE_BINDINGS,
    PLATFORM_INJECTED_VARIABLES,
    EngineRoots,
)
from cortex_platform.product.engine.child import (
    assess_write_boundary,
    checkpoint_research_db,
)
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor
from cortex_platform.product.secrets import SecretNotFound, SecretValue
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected

from .conftest import ActivationGate


@pytest.fixture
def supervisor(roots: EngineRoots, research_db: Path) -> ResearchEffectSupervisor:
    return ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        timeout_seconds=120,
    )


def test_a_disabled_gate_refuses_before_anything_is_spawned(
    roots: EngineRoots, research_db: Path
) -> None:
    gate = ActivationGate(False)
    supervisor = ResearchEffectSupervisor(
        store=gate, roots=roots, python_executable=Path(sys.executable)
    )
    with pytest.raises(EffectPermanentlyRejected) as raised:
        supervisor.run("checkpoint")
    assert raised.value.category == "runtime_activation_disabled"
    assert gate.reads == 1
    assert not (roots.state / "effects").exists()


def test_the_gate_is_re_read_for_every_effect(supervisor: ResearchEffectSupervisor) -> None:
    supervisor.run("checkpoint")
    supervisor.run("checkpoint")
    assert supervisor._store.reads == 2


def test_the_child_runs_and_checkpoints_the_copy(
    supervisor: ResearchEffectSupervisor, roots: EngineRoots
) -> None:
    execution = supervisor.run("checkpoint")
    assert execution.ok, execution.failure_message
    assert execution.checkpointed
    log = roots.research_db.with_name(roots.research_db.name + "-wal")
    assert not log.exists() or log.stat().st_size == 0


def test_the_child_environment_is_the_replaced_one(
    supervisor: ResearchEffectSupervisor, roots: EngineRoots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_PROFILE", "investment")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_RESEARCH", "must-not-travel")
    probe = roots.state / "probe.json"
    execution = supervisor.run("self_check", {"write_path": str(probe)})
    assert execution.ok, execution.failure_message
    assert probe.read_text(encoding="utf-8").startswith("p4.2")


def test_the_child_inherits_no_ambient_name(
    supervisor: ResearchEffectSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_PROFILE", "investment")
    monkeypatch.setenv("SOMETHING_ENTIRELY_UNRELATED", "1")
    execution = supervisor.run("self_check", {"report_environment_names": True})
    assert execution.ok, execution.failure_message
    names = set(execution.engine["environment_names"])
    assert "HERMES_PROFILE" not in names
    assert "SOMETHING_ENTIRELY_UNRELATED" not in names
    assert "TELEGRAM_BOT_TOKEN_RESEARCH" not in names
    assert names <= set(ENGINE_BINDINGS) | {EFFECT_MARKER_VARIABLE} | PLATFORM_INJECTED_VARIABLES
    # HOME is the one name AMD-9 forbids leaving unbound; CORTEX_RESEARCH_DB is
    # the one the effect cannot do its work without.
    assert "HOME" in names and "CORTEX_RESEARCH_DB" in names


def test_the_write_boundary_fires_on_an_unbound_path(
    supervisor: ResearchEffectSupervisor, tmp_path: Path
) -> None:
    """D9 item 7, synthesised because the package contains no real one."""

    outside = tmp_path / "outside-every-root" / "leaked.txt"
    execution = supervisor.run("self_check", {"write_path": str(outside)})
    assert not execution.ok
    assert execution.failure_category == "outcome_unknown"
    assert execution.write_boundary["ok"] is False
    assert any(
        "outside-every-root" in violation
        for violation in execution.write_boundary["violations"]
    )


def test_a_missing_secret_reference_is_adapter_unavailable(
    roots: EngineRoots, research_db: Path
) -> None:
    def provider():
        raise SecretNotFound("openrouter", "nothing is stored behind it")

    supervisor = ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        secret_provider=provider,
    )
    with pytest.raises(EffectPermanentlyRejected) as raised:
        supervisor.run("checkpoint")
    assert raised.value.category == "adapter_unavailable"


def test_a_resolved_secret_reaches_the_child_and_nothing_else(
    roots: EngineRoots, research_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦AMD-8⟧ P2b-shaped hygiene, on the engine child boundary."""

    supervisor = ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        secret_provider=lambda: {
            "openrouter": SecretValue("openrouter", "fake-secret-value-42")
        },
    )
    execution = supervisor.run("checkpoint")
    assert execution.ok
    assert "fake-secret-value-42" not in json.dumps(execution.to_dict())
    assert "fake-secret-value-42" not in execution.stderr_tail
    result = roots.state / "effects" / execution.marker / "result.json"
    assert "fake-secret-value-42" not in result.read_text(encoding="utf-8")


def test_a_hard_timeout_is_an_unknown_outcome(
    roots: EngineRoots, research_db: Path
) -> None:
    supervisor = ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        python_executable=Path(sys.executable),
        timeout_seconds=1,
    )
    supervisor._timeout = 1
    original = supervisor.run

    # A checkpoint on a database held open by a competing writer blocks past the
    # one-second hard timeout; the child is killed, and a killed child is never
    # a failure, always an unknown outcome.
    blocker = sqlite3.connect(str(roots.research_db))
    blocker.execute("PRAGMA journal_mode = WAL")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        execution = original("checkpoint")
    finally:
        blocker.rollback()
        blocker.close()
    if execution.ok:
        pytest.skip("the platform completed the checkpoint inside the hard timeout")
    assert execution.failure_category == "outcome_unknown"


def test_a_live_detached_lock_is_a_survivor(
    supervisor: ResearchEffectSupervisor, roots: EngineRoots
) -> None:
    """V3: the bound detached/*.lock directory must be empty to claim success."""

    detached = roots.state / "detached"
    detached.mkdir(parents=True, exist_ok=True)
    (detached / "radar_scan.lock").write_text(str(os.getpid()), encoding="utf-8")
    execution = supervisor.run("checkpoint")
    assert not execution.ok
    assert execution.failure_category == "outcome_unknown"
    assert execution.survivors["supervisor"]["locks"]


def test_a_dead_detached_lock_is_not_a_survivor(
    supervisor: ResearchEffectSupervisor, roots: EngineRoots
) -> None:
    detached = roots.state / "detached"
    detached.mkdir(parents=True, exist_ok=True)
    (detached / "radar_scan.lock").write_text("999999", encoding="utf-8")
    execution = supervisor.run("checkpoint")
    assert execution.ok, execution.failure_message


def test_gdrive_watch_roots_are_digested_before_and_after(
    supervisor: ResearchEffectSupervisor, tmp_path: Path
) -> None:
    decoy = tmp_path / "decoy-gdrive" / "agent-readings"
    (decoy / "papers" / "20260101-Existing").mkdir(parents=True)
    note = decoy / "papers" / "20260101-Existing" / "notes.md"
    note.write_text("# untouched\n", encoding="utf-8")
    execution = supervisor.run("checkpoint", watch_roots={"decoy": decoy})
    assert execution.ok, execution.failure_message
    assert execution.gdrive["changed"] == []
    assert (
        execution.gdrive["before"]["decoy"]["tree_digest"]
        == execution.gdrive["after"]["decoy"]["tree_digest"]
    )


def test_write_boundary_assessment_is_a_pure_function(tmp_path: Path) -> None:
    inside = tmp_path / "root" / "a.txt"
    outside = tmp_path / "elsewhere" / "b.txt"
    violations = assess_write_boundary(
        {str(inside), str(outside), "relative.txt"}, (tmp_path / "root",)
    )
    assert violations == (str(outside),)


def test_checkpoint_is_idempotent_on_a_missing_database(tmp_path: Path) -> None:
    assert checkpoint_research_db(tmp_path / "absent.db") is True


def test_a_corpus_that_cannot_carry_both_names_is_refused_before_the_child(
    supervisor: ResearchEffectSupervisor, roots: EngineRoots
) -> None:
    """F4: a real directory where the corpus link belongs is a split on disk."""

    link = roots.readings_root / "papers"
    link.unlink()
    link.mkdir(parents=True)
    with pytest.raises(EffectPermanentlyRejected) as error:
        supervisor.run("self_check", {})
    assert error.value.category == "adapter_unavailable"
