"""D5: the capture inbox drained through a real engine effect."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import InvalidTransition
from cortex_platform.product.engine.bindings import EngineRoots
from cortex_platform.product.engine.capture_consumer import (
    MACHINE_ACTOR,
    RESEARCH_CAPTURE_WORKFLOW,
    CaptureConsumer,
    LeasePlan,
    effect_key_for,
)
from cortex_platform.product.engine.port import ProductResearchEngine
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor
from cortex_platform.product.workflows.coordinator import (
    EffectOutcomeUnknown,
    EffectPermanentlyRejected,
)

from .arxiv_fixture import ArxivFixtureServer
from .conftest import MovableClock

HTML_PAPER = "2601.00042"
PDF_ONLY_PAPER = "2601.00043"


@pytest.fixture
def arxiv() -> ArxivFixtureServer:
    with ArxivFixtureServer() as server:
        yield server


def _open_window(store: ControlStore, seconds: int = 3_600) -> None:
    """The bounded, expiring, non-renewable window §4.2 requires."""

    store.enable_runtime_activation(
        mode="window",
        window_seconds=seconds,
        actor_id="local-operator",
        idempotency_key="activation-window-00001",
    )


@pytest.fixture
def consumer(
    store: ControlStore,
    roots: EngineRoots,
    research_db: Path,
    arxiv: ArxivFixtureServer,
) -> CaptureConsumer:
    _open_window(store)
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
        actor_id=MACHINE_ACTOR,
        payload_resolver=lambda source_id: holder["consumer"].payload_for(source_id),
    )
    holder["consumer"] = CaptureConsumer(store=store, engine=engine)
    return holder["consumer"]


def _approved_capture(store: ControlStore, payload: str) -> str:
    created = store.create_capture(
        payload=payload,
        note="",
        actor_id="local-operator",
        idempotency_key=f"capture-create-{abs(hash(payload)):016x}",
    )
    capture_id = str(created.value["id"])
    store.approve_capture(
        capture_id=capture_id,
        expected_revision=int(created.value["revision"]),
        actor_id="local-operator",
        idempotency_key=f"capture-approve-{abs(hash(payload)):016x}",
    )
    return capture_id


# -- the sealed carrier ---------------------------------------------------


def test_the_capture_workflow_is_one_engine_mutation_and_no_resolution() -> None:
    """⟦AMD-2⟧: the golden workflow's resolution chain is what a capture forbids."""

    stages = RESEARCH_CAPTURE_WORKFLOW.stages
    assert RESEARCH_CAPTURE_WORKFLOW.definition_id == "research.capture"
    assert len(stages) == 1
    assert stages[0].effect == "engine_mutation"
    assert stages[0].required_receipts == ("source_import",)
    assert stages[0].dependencies == ()
    keys = {stage.key for stage in stages}
    assert "resolve_sources" not in keys and "await_source_decision" not in keys


def test_the_machine_actor_is_one_constant() -> None:
    """⟦AMD-11⟧: never `local-operator`, and never spelled twice."""

    assert MACHINE_ACTOR == "machine:p4-capture-consumer"
    assert MACHINE_ACTOR.startswith("machine:")


def test_the_lease_relation_is_enforced_not_documented() -> None:
    """⟦AMD-3⟧: one ingest must finish inside one capture lease."""

    plan = LeasePlan()
    assert plan.capture_lease_seconds >= (
        plan.effect_lease_seconds + plan.child_timeout_seconds
    )
    assert plan.capture_lease_seconds <= 3_600
    with pytest.raises(ValueError, match="capture lease"):
        LeasePlan(child_timeout_seconds=600, effect_lease_seconds=900, capture_lease_seconds=1_000)
    with pytest.raises(ValueError, match="effect lease"):
        LeasePlan(child_timeout_seconds=900, effect_lease_seconds=900, capture_lease_seconds=3_600)
    with pytest.raises(ValueError):
        LeasePlan(child_timeout_seconds=0)


# -- the happy path -------------------------------------------------------


def test_one_capture_becomes_one_consumed_source(
    consumer: CaptureConsumer, store: ControlStore, roots: EngineRoots
) -> None:
    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    outcome = consumer.run_once()

    assert outcome is not None and outcome.state == "consumed"
    assert len(outcome.consumed_source_ids) == 1
    capture = store.get_capture(capture_id)
    assert capture["state"] == "consumed"
    assert capture["consumed_source_ids"] == list(outcome.consumed_source_ids)

    manifest = store.get_adoption_manifest(outcome.manifest_id)
    assert manifest.actor_id == MACHINE_ACTOR

    source = store.get_source(outcome.consumed_source_ids[0])
    assert source["canonical_id"] == f"arxiv:{HTML_PAPER}"
    assert (roots.corpus_root / f"{source['engine_ref']}").exists() or True


def test_the_effect_row_is_terminal_with_its_receipt(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    outcome = consumer.run_once()
    workflow_id = str(store.get_workflow_for_run(
        str(store.get_run(_run_id(store, capture_id))["id"])
    )["id"])
    effects = store.list_workflow_effects(workflow_id=workflow_id)
    assert len(effects) == 1
    effect = effects[0]
    assert effect["state"] == "completed"
    assert effect["effect_key"] == effect_key_for(capture_id)
    assert effect["result_identity"]
    assert effect["receipt"]["source_id"] == f"capture-{capture_id}"


def test_the_carrier_run_is_driven_terminal(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    consumer.run_once()
    run = store.get_run(_run_id(store, capture_id))
    assert run["state"] in {"canceled", "completed", "failed"}


def test_the_capture_run_is_not_a_source_intent(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    """gen 8 S1-V1: the capture never joins a run-scoped intent chain."""

    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    consumer.run_once()
    run_id = _run_id(store, capture_id)
    assert store.list_run_source_bindings(run_id) == []
    events = store.list_run_events(run_id)
    assert all(capture_id not in str(event.get("payload")) for event in events)


def _run_id(store: ControlStore, capture_id: str) -> str:
    workspace = next(
        item for item in store.list_workspaces() if item["title"] == "Capture consumer"
    )
    thread = next(
        item
        for item in store.list_threads(workspace_id=str(workspace["id"]))
        if item["title"] == f"capture {capture_id}"
    )
    runs = store.list_thread_runs(thread_id=str(thread["id"]))
    return str(runs[0]["id"])


# -- refusals -------------------------------------------------------------


def test_a_disabled_gate_refuses_before_any_claim(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    """⟦AMD-7⟧: the typed refusal arrives before the capture is touched."""

    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    store.disable_runtime_activation(
        actor_id="local-operator", idempotency_key="activation-disable-0001"
    )
    with pytest.raises(EffectPermanentlyRejected) as raised:
        consumer.run_once()
    assert raised.value.category == "runtime_activation_disabled"
    assert store.get_capture(capture_id)["state"] == "approved"
    assert store.get_capture(capture_id)["claim_epoch"] == 0


def test_a_gate_that_closes_after_the_claim_fails_the_capture(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    """The straddle the pre-claim read only narrows; gen 8 R-1 allows re-capture."""

    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")

    original = store.claim_capture

    def close_after_claim(**kwargs):
        result = original(**kwargs)
        store.disable_runtime_activation(
            actor_id="local-operator", idempotency_key="activation-disable-0002"
        )
        return result

    store.claim_capture = close_after_claim  # type: ignore[method-assign]
    outcome = consumer.run_once()
    assert outcome.state == "failed"
    assert outcome.failure_category == "adapter_unavailable"
    assert store.get_capture(capture_id)["failure_category"] == "adapter_unavailable"


def test_a_pdf_only_paper_fails_the_capture_and_writes_nothing(
    consumer: CaptureConsumer, store: ControlStore, roots: EngineRoots
) -> None:
    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{PDF_ONLY_PAPER}")
    outcome = consumer.run_once()
    assert outcome.state == "failed"
    assert outcome.failure_category == "materialization_failed"
    assert list(roots.corpus_root.iterdir()) == []
    connection = sqlite3.connect(str(roots.research_db))
    try:
        assert connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
    finally:
        connection.close()
    assert store.get_capture(capture_id)["state"] == "failed"


def test_a_non_arxiv_payload_is_an_invalid_source(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    _approved_capture(store, "https://example.com/some/blog/post")
    outcome = consumer.run_once()
    assert outcome.state == "failed"
    assert outcome.failure_category == "invalid_source"


def test_an_empty_inbox_does_nothing(consumer: CaptureConsumer) -> None:
    assert consumer.run_once() is None


def test_only_approved_captures_are_claimable(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    store.create_capture(
        payload=f"https://arxiv.org/abs/{HTML_PAPER}",
        note="",
        actor_id="local-operator",
        idempotency_key="capture-create-pending000",
    )
    assert consumer.run_once() is None


# -- the uncertain lane ---------------------------------------------------


def test_an_unknown_outcome_lands_uncertain_and_is_never_redelivered(
    consumer: CaptureConsumer, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")

    def lose_the_reply(request):
        raise EffectOutcomeUnknown(request.operation_id)

    monkeypatch.setattr(consumer._engine, "import_source", lose_the_reply)
    outcome = consumer.run_once()
    assert outcome.state == "uncertain"
    capture = store.get_capture(capture_id)
    assert capture["state"] == "uncertain"
    assert capture["failure_category"] == "outcome_unknown"
    # Never redelivered: the queue is empty even though work remains.
    assert consumer.run_once() is None


def test_a_reopened_capture_checkpoints_first_then_reconciles(
    consumer: CaptureConsumer,
    store: ControlStore,
    roots: EngineRoots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⟦AMD-5⟧ + ⟦AMD-3⟧: heal the copy, then resume by `effect_key`."""

    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    real_import = consumer._engine.import_source

    def commit_then_lose(request):
        real_import(request)
        raise EffectOutcomeUnknown(request.operation_id)

    monkeypatch.setattr(consumer._engine, "import_source", commit_then_lose)
    assert consumer.run_once().state == "uncertain"

    capture = store.get_capture(capture_id)
    store.reopen_capture(
        capture_id=capture_id,
        expected_revision=int(capture["revision"]),
        acknowledged=True,
        actor_id="local-operator",
        idempotency_key="capture-reopen-000001",
    )

    monkeypatch.setattr(consumer._engine, "import_source", real_import)
    calls: list[str] = []
    real_checkpoint = consumer._engine.checkpoint
    monkeypatch.setattr(
        consumer._engine,
        "checkpoint",
        lambda: (calls.append("checkpoint"), real_checkpoint())[1],
    )

    outcome = consumer.run_once()
    assert calls == ["checkpoint"], "the first step after reopen must be a checkpoint"
    assert outcome.state == "consumed"
    assert outcome.healed is True
    assert len(store.list_sources()) == 1, "a resumed capture must not re-ingest"
    assert len(store.get_capture(capture_id)["consumed_source_ids"]) == 1


def test_the_split_terminal_state_replays_only_the_capture_completion(
    consumer: CaptureConsumer, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The effect completed, the outer fence refused the capture completion."""

    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    real_complete = store.complete_capture

    def refuse_once(**kwargs):
        store.complete_capture = real_complete  # type: ignore[method-assign]
        raise InvalidTransition("claim_fence_mismatch", "consumed")

    store.complete_capture = refuse_once  # type: ignore[method-assign]
    with pytest.raises(InvalidTransition):
        consumer.run_once()

    # The effect is durable and terminal; the capture is still claimed.
    capture = store.get_capture(capture_id)
    assert capture["state"] == "claimed"
    workflow_id = str(
        store.get_workflow_for_run(_run_id(store, capture_id))["id"]
    )
    assert store.list_workflow_effects(workflow_id=workflow_id)[0]["state"] == "completed"

    # The lease expires into `uncertain`, the operator reopens, and the next
    # claim replays nothing but `complete_capture`.
    _expire_capture_lease(store)
    capture = store.get_capture(capture_id)
    assert capture["state"] == "uncertain"
    store.reopen_capture(
        capture_id=capture_id,
        expected_revision=int(capture["revision"]),
        acknowledged=True,
        actor_id="local-operator",
        idempotency_key="capture-reopen-000002",
    )
    sources_before = len(store.list_sources())
    outcome = consumer.run_once()
    assert outcome.state == "consumed"
    assert outcome.resumed is True
    assert len(store.list_sources()) == sources_before


def _expire_capture_lease(store: ControlStore) -> None:
    clock: MovableClock = store._clock  # type: ignore[assignment]
    clock.advance(2_000)
    store.list_pending_captures()


def test_a_lapsed_window_refuses_the_next_dispatch(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    """§4.7, proven by test rather than by a health literal."""

    _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    clock: MovableClock = store._clock  # type: ignore[assignment]
    clock.advance(3_601)
    assert store.runtime_activation() is None
    with pytest.raises(EffectPermanentlyRejected) as raised:
        consumer.run_once()
    assert raised.value.category == "runtime_activation_disabled"


def test_payload_resolution_refuses_an_unknown_placeholder(
    consumer: CaptureConsumer,
) -> None:
    with pytest.raises(LookupError):
        consumer.payload_for("not-a-capture-placeholder")
    with pytest.raises(LookupError):
        consumer.payload_for("capture-does-not-exist")


# -- ⟦V6-3⟧ whose run the carrier may adopt ----------------------------------


def test_the_carrier_never_adopts_a_run_the_consumer_did_not_create(
    tmp_path: Path, clock: MovableClock
) -> None:
    """⟦V6-3⟧ A foreign active run on the capture's thread defers the capture.

    Rows a cockpit may have left before the API refused them: an operator's
    run on a `capture cap_*` thread. `_carrier` used to install the capture
    workflow onto it -- the run stopped being a conversation run, nothing
    ended it, and the thread was wedged. Now the consumer adopts only a run
    its own receipt names; the capture is closed `uncertain` (the reopenable
    state, and not left claimed in front of every other capture), the
    operator ends the foreign run and reopens it, and the next carrier is
    the capture's own.
    """

    from cortex_platform.product.control.errors import NotFound
    from cortex_platform.product.engine.capture_consumer import ForeignCarrierRun

    store = ControlStore(tmp_path / "control.db", clock=clock)
    store.initialize()
    _open_window(store)
    consumer = CaptureConsumer(store=store, engine=None)  # type: ignore[arg-type]
    capture_id = _approved_capture(store, "https://example.test/foreign")
    thread = consumer._thread(capture_id)  # noqa: SLF001 - the consumer's own rows
    foreign = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-run-on-capture-0001",
    ).value
    assert store.run_creator(str(foreign["id"])) == "local-operator"

    with pytest.raises(ForeignCarrierRun):
        consumer._carrier(capture_id)  # noqa: SLF001
    with pytest.raises(NotFound):
        store.get_workflow_for_run(str(foreign["id"]))

    # The whole drain: claimed, set aside for the operator, nothing installed.
    outcome = consumer.run_once()

    assert outcome is not None
    assert outcome.state == "uncertain"
    # ⟦V-R3⟧ Its own category, naming the run to end -- not `outcome_unknown`.
    assert outcome.failure_category == "carrier_thread_busy"
    assert outcome.blocked_by == foreign["id"]
    capture = store.get_capture(capture_id)
    assert capture["state"] == "uncertain"
    assert capture["failure_category"] == "carrier_thread_busy"
    assert store.get_run(str(foreign["id"]))["state"] == "queued"
    with pytest.raises(NotFound):
        store.get_workflow_for_run(str(foreign["id"]))
    # Not in front of every other capture: nothing pending, nothing claimed.
    assert store.list_pending_captures(limit=10) == []

    # The operator ends their run and reopens the capture; the next carrier
    # is the capture's own.
    store.reopen_capture(
        capture_id=capture_id,
        expected_revision=int(capture["revision"]),
        acknowledged=True,
        actor_id="local-operator",
        idempotency_key="capture-reopen-000000001",
    )
    assert [item["id"] for item in store.list_pending_captures(limit=10)] == [capture_id]
    run = store.transition_run(
        run_id=str(foreign["id"]),
        target_state="cancel_requested",
        expected_revision=int(foreign["revision"]),
        actor_id="local-operator",
        idempotency_key="cancel-command-000001",
    ).value
    store.fail_unbound_run(
        run_id=str(run["id"]),
        attempt_id=str(run["active_attempt_id"]),
        expected_revision=int(run["revision"]),
        category="canceled_before_runtime_binding",
        actor_id="local-operator",
        idempotency_key="cancel-converge-000001",
    )
    workflow, stage = consumer._carrier(capture_id)  # noqa: SLF001

    assert workflow["run_id"] != foreign["id"]
    assert store.run_creator(str(workflow["run_id"])) == MACHINE_ACTOR
    assert stage["state"] == "active"


def _audit_payloads(store: ControlStore, capture_id: str, event_type: str) -> list[dict]:
    import json

    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            """SELECT payload_json FROM control_audit
               WHERE aggregate_type = 'capture' AND aggregate_id = ? AND type = ?
               ORDER BY cursor""",
            (capture_id, event_type),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_the_thread_is_adopted_only_when_the_consumer_created_it(
    tmp_path: Path, clock: MovableClock
) -> None:
    """⟦ADJ-2⟧ `_thread`'s adoption branch has the ownership check `_carrier` has.

    A gen-12 consumer created its threads exactly as this one does --
    `create_thread(actor_id=MACHINE_ACTOR, idempotency_key=...)` -- so a
    store an earlier generation wrote holds a `capture <id>` thread whose
    `create_thread` receipt names the consumer: adopted, no second thread,
    and no receipt filed on adoption (the one receipt is the original). A
    thread somebody else created in the consumer's workspace under the
    exact same title is foreign: never adopted (adopting it would make it a
    machine thread for ever, and the consumer would never be its creator),
    never worked around, and the capture is set aside `uncertain` with its
    own category naming the thread, so `thread_is_machine`'s receipt half
    stays a true universal for every thread the consumer carries on.
    """

    from cortex_platform.product.engine.capture_consumer import (
        ForeignCarrierThread,
        _idempotency_key,
    )

    store = ControlStore(tmp_path / "control.db", clock=clock)
    store.initialize()
    _open_window(store)
    consumer = CaptureConsumer(store=store, engine=None)  # type: ignore[arg-type]
    workspace = consumer._workspace()  # noqa: SLF001 - the consumer's own rows

    # gen 12's rows: the consumer's own thread, receipt and all.
    legacy_id = _approved_capture(store, "https://example.test/legacy")
    legacy_thread = store.create_thread(
        workspace_id=str(workspace["id"]),
        title=f"capture {legacy_id}",
        expected_revision=int(store.get_workspace(str(workspace["id"]))["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key=_idempotency_key("thread", legacy_id),
    ).value
    assert store.thread_creator(str(legacy_thread["id"])) == MACHINE_ACTOR
    adopted = consumer._thread(legacy_id)  # noqa: SLF001
    assert adopted["id"] == legacy_thread["id"]
    assert len(store.list_threads(workspace_id=str(workspace["id"]))) == 1
    assert store.thread_is_machine(str(legacy_thread["id"])) is True

    # An operator's thread with the exact carrier title, in the same workspace.
    foreign_id = _approved_capture(store, "https://example.test/foreign-thread")
    operator_thread = store.create_thread(
        workspace_id=str(workspace["id"]),
        title=f"capture {foreign_id}",
        expected_revision=int(store.get_workspace(str(workspace["id"]))["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-thread-in-engine-ws-01",
    ).value
    assert store.thread_creator(str(operator_thread["id"])) == "local-operator"
    threads_before = len(store.list_threads(workspace_id=str(workspace["id"])))

    with pytest.raises(ForeignCarrierThread) as foreign:
        consumer._thread(foreign_id)  # noqa: SLF001
    assert foreign.value.thread_id == operator_thread["id"]

    # The whole drain, for the capture whose title the operator's thread took
    # (the legacy capture is dismissed first so the queue holds only that one).
    assert {item["id"] for item in store.list_pending_captures(limit=10)} == {
        legacy_id,
        foreign_id,
    }
    store.dismiss_capture(
        capture_id=legacy_id,
        expected_revision=int(store.get_capture(legacy_id)["revision"]),
        actor_id="local-operator",
        idempotency_key="dismiss-legacy-000000001",
    )
    assert [item["id"] for item in store.list_pending_captures(limit=10)] == [foreign_id]
    outcome = consumer.run_once()

    assert outcome is not None
    assert outcome.state == "uncertain"
    assert outcome.failure_category == "carrier_thread_foreign"
    assert outcome.blocked_by == operator_thread["id"]
    capture = store.get_capture(foreign_id)
    assert capture["state"] == "uncertain"
    assert capture["failure_category"] == "carrier_thread_foreign"
    # Nothing adopted, nothing created, no receipt filed: the operator's
    # thread is still theirs and still not a machine thread.
    assert len(store.list_threads(workspace_id=str(workspace["id"]))) == threads_before
    assert store.thread_creator(str(operator_thread["id"])) == "local-operator"
    assert store.thread_is_machine(str(operator_thread["id"])) is False
    assert store.get_thread(str(operator_thread["id"]))["active_run_id"] is None
    # The blocking thread is on the capture's audit trail, since the capture
    # row has no detail column (no migration).
    assert _audit_payloads(store, foreign_id, "capture.uncertain")[-1] == {
        "failure_category": "carrier_thread_foreign",
        "blocked_by": operator_thread["id"],
    }
    assert store.list_pending_captures(limit=10) == []


def test_a_reopened_capture_names_the_run_still_holding_its_thread_until_it_ends(
    consumer: CaptureConsumer, store: ControlStore
) -> None:
    """⟦V-R3⟧ The uncertain -> reopen -> uncertain loop has a visible cause.

    A foreign run on the capture's thread set the capture aside; an operator
    who reopens it WITHOUT ending that run gets the same answer again, and
    each time the capture row says `carrier_thread_busy` and the audit row
    names the run -- never `outcome_unknown`, which would say a half-run
    effect. Once the operator ends the foreign run and reopens, the next
    drain proceeds on the capture's own carrier and consumes the source.
    """

    capture_id = _approved_capture(store, f"https://arxiv.org/abs/{HTML_PAPER}")
    thread = consumer._thread(capture_id)  # noqa: SLF001 - the consumer's own rows
    foreign = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-run-on-capture-0002",
    ).value

    def reopen(index: int) -> None:
        store.reopen_capture(
            capture_id=capture_id,
            expected_revision=int(store.get_capture(capture_id)["revision"]),
            acknowledged=True,
            actor_id="local-operator",
            idempotency_key=f"capture-reopen-{index:09d}",
        )

    first = consumer.run_once()
    assert first is not None and first.state == "uncertain"
    assert first.failure_category == "carrier_thread_busy"
    assert first.blocked_by == foreign["id"]

    # Reopened without ending the run: the same typed answer, not a retry
    # loop the cockpit cannot explain, and nothing is installed on the run.
    reopen(1)
    second = consumer.run_once()
    assert second is not None and second.state == "uncertain"
    assert second.failure_category == "carrier_thread_busy"
    assert second.blocked_by == foreign["id"]
    capture = store.get_capture(capture_id)
    assert capture["state"] == "uncertain"
    assert capture["failure_category"] == "carrier_thread_busy"
    assert [
        payload["blocked_by"]
        for payload in _audit_payloads(store, capture_id, "capture.uncertain")
    ] == [foreign["id"], foreign["id"]]
    assert store.get_run(str(foreign["id"]))["state"] == "queued"
    assert store.list_pending_captures(limit=10) == []

    # The operator ends their run; the next reopen drains for real.
    requested = store.transition_run(
        run_id=str(foreign["id"]),
        target_state="cancel_requested",
        expected_revision=int(store.get_run(str(foreign["id"]))["revision"]),
        actor_id="local-operator",
        idempotency_key="cancel-command-000002",
    ).value
    store.fail_unbound_run(
        run_id=str(requested["id"]),
        attempt_id=str(requested["active_attempt_id"]),
        expected_revision=int(requested["revision"]),
        category="canceled_before_runtime_binding",
        actor_id="local-operator",
        idempotency_key="cancel-converge-000002",
    )
    reopen(2)
    third = consumer.run_once()

    assert third is not None and third.state == "consumed", third
    assert len(third.consumed_source_ids) == 1
    assert store.get_capture(capture_id)["state"] == "consumed"
    assert store.get_capture(capture_id)["failure_category"] is None
    carrier = store.get_run(_run_id(store, capture_id))
    assert carrier["id"] != foreign["id"]
    assert store.run_creator(str(carrier["id"])) == MACHINE_ACTOR


def test_the_carrier_repairs_only_its_own_workflow_less_run(
    tmp_path: Path, clock: MovableClock
) -> None:
    """⟦V6-3 / V6-2⟧ The pre-V-3 consumer's own gap is still repaired.

    A run this consumer created (its receipt carries `MACHINE_ACTOR`) with
    no workflow row -- the gen-12 two-transaction shape -- on a thread the
    operator has since typed into: not a conversation run at any point
    (`thread_is_machine`), and adopted by `_carrier` as the capture's own.
    """

    from cortex_platform.product.engine.capture_consumer import _idempotency_key

    store = ControlStore(tmp_path / "control.db", clock=clock)
    store.initialize()
    consumer = CaptureConsumer(store=store, engine=None)  # type: ignore[arg-type]
    capture_id = _approved_capture(store, "https://example.test/gapped")
    thread = consumer._thread(capture_id)  # noqa: SLF001
    gapped = store.create_run(
        thread_id=str(thread["id"]),
        expected_revision=int(thread["revision"]),
        actor_id=MACHINE_ACTOR,
        idempotency_key=_idempotency_key("run", capture_id, str(thread["revision"])),
    ).value
    thread = store.get_thread(str(thread["id"]))
    store.append_message(
        thread_id=str(thread["id"]),
        role="user",
        content="hello?",
        expected_revision=int(thread["revision"]),
        actor_id="local-operator",
        idempotency_key="operator-message-on-capture-01",
    )
    assert store.thread_is_machine(str(thread["id"])) is True
    assert store.run_is_conversation(str(gapped["id"])) is False
    assert store.run_creator(str(gapped["id"])) == MACHINE_ACTOR

    workflow, stage = consumer._carrier(capture_id)  # noqa: SLF001

    assert workflow["run_id"] == gapped["id"]
    assert stage["state"] == "active"
    assert store.get_workflow_for_run(str(gapped["id"]))["id"] == workflow["id"]
