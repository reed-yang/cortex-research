"""The closed `turn.resolve` vocabulary, and the one place it is translated.

Two vocabularies meet here: Cortex's `approve_once | deny`, which is what the
operator is offered, and the fork's `once | session | always | deny`, two of
which grant standing permission. What is pinned is that the wire carries only
the first, that the translation happens exactly once, and that nothing anywhere
can produce the fork's standing grants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.worker_payload.cortex_worker.approval import (
    APPROVAL_CHOICES,
    CHOICE_APPROVE_ONCE,
    CHOICE_DENY,
    FORK_CHOICES,
    STANDING_CHOICES,
    ApprovalChoiceError,
    fork_choice,
    is_approval_choice,
    validate_approval_choice,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.digests import (
    DECISION_OPTIONS,
)
from cortex_platform.product.runtime_update.supervisor import WorkerProtocolError
from cortex_platform.runtime.compatibility import COMPATIBILITY_MATRIX
from cortex_platform.runtime.managed_hermes import ManagedHermesBackend

from .test_worker_turns import APPROVAL_RUNNER, _drive, _turn_request
from .test_managed_backend import _run_input
from cortex_platform.runtime import events
from cortex_platform.runtime import hermes as hermes_module
from cortex_platform.runtime.hermes import HermesSignal

REQUEST_DIGEST = "e" * 64


def test_the_wire_vocabulary_is_what_the_operator_was_offered() -> None:
    """One set, three consumers: the option list, the adapter, and this."""

    assert APPROVAL_CHOICES == (CHOICE_APPROVE_ONCE, CHOICE_DENY)
    # What the shared projection digests, so a turn's `result_digest` is taken
    # over the same option identities Control stored.
    assert set(APPROVAL_CHOICES) == set(DECISION_OPTIONS)
    # What `translate_hermes_signal` actually offers the operator, read out of
    # the source rather than restated, and what the in-process adapter refuses
    # to leave (`hermes.py` `resolve_decision`).
    events_source = Path(events.__file__).read_text(encoding="utf-8")
    assert '"options": ["approve_once", "deny"]' in events_source
    hermes_source = Path(hermes_module.__file__).read_text(encoding="utf-8")
    assert 'resolution.choice not in {"approve_once", "deny"}' in hermes_source


def test_no_standing_grant_is_reachable_from_any_input() -> None:
    """`session` and `always` are the fork's, and this product never says them."""

    for standing in STANDING_CHOICES:
        assert standing not in APPROVAL_CHOICES
        assert is_approval_choice(standing) is False
        assert fork_choice(standing) == "deny"
        with pytest.raises(ApprovalChoiceError):
            validate_approval_choice(standing)
    assert set(FORK_CHOICES).isdisjoint(STANDING_CHOICES)
    assert COMPATIBILITY_MATRIX["approval_bridge"].endswith("session|always")
    assert COMPATIBILITY_MATRIX["callback_shapes"]["approval_callback"].endswith(
        "once|deny"
    )


def test_the_mapping_is_total_and_fails_closed() -> None:
    assert fork_choice(CHOICE_APPROVE_ONCE) == "once"
    assert fork_choice(CHOICE_DENY) == "deny"
    for rubbish in (None, "", "yes", "ONCE", 1, object(), "approve", "allow"):
        assert fork_choice(rubbish) == "deny"
    assert validate_approval_choice(CHOICE_APPROVE_ONCE) == CHOICE_APPROVE_ONCE
    with pytest.raises(ApprovalChoiceError):
        validate_approval_choice("approve")


def test_an_invalid_choice_is_refused_on_the_wire_and_never_delivered(
    tmp_path: Path,
) -> None:
    """The worker's half, driven through a real turn that is parked on a decision.

    Independent of the accept case below: this turn is never resolved
    successfully, and is cancelled rather than completed.
    """

    from cortex_platform.product.runtime_update.supervisor import WorkerSupervisorV2

    descriptor_path, _state = _drive(tmp_path, APPROVAL_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        supervisor.begin_turn("attempt-vocab.0", REQUEST_DIGEST, _turn_request())
        events = supervisor.turn_events("attempt-vocab.0")
        first = next(events)
        assert first["kind"] == "decision.required"
        with pytest.raises(WorkerProtocolError, match="protocol_violation"):
            supervisor.resolve_turn(
                "attempt-vocab.0",
                {"decision_id": "decision-1", "choice": "always"},
            )
        # The turn is still parked: the refusal did not answer it.
        supervisor.cancel_turn("attempt-vocab.0")
        final = [event for event in events]
    assert final[-1]["kind"] == "operation.finish"
    assert final[-1]["payload"]["result"]["final_response"] == "chose deny"


def test_the_backend_refuses_an_invalid_choice_before_it_becomes_a_frame(
    tmp_path: Path,
) -> None:
    """The product's half. The rejection is typed and nothing is sent."""

    descriptor_path, _state = _drive(tmp_path, APPROVAL_RUNNER)
    backend = ManagedHermesBackend(descriptor_path, sandbox=False)
    rejections = []
    resolutions = []

    def emit(signal: HermesSignal) -> None:
        if signal.kind != "decision.required":
            return
        for index, choice in enumerate(("always", "session", "approve", "")):
            rejections.append(
                backend.resolve_decision(
                    "cortex_session",
                    "run-1",
                    "attempt-1",
                    "decision-1",
                    choice,
                    f"decision-op-{index}",
                    1,
                )
            )
        resolutions.append(
            backend.resolve_decision(
                "cortex_session",
                "run-1",
                "attempt-1",
                "decision-1",
                "approve_once",
                "decision-op-ok",
                1,
            )
        )

    try:
        result = backend.run(_run_input(), emit)
    finally:
        backend.close()
    assert [outcome.reason_code for outcome in rejections] == [
        "invalid_approval_choice"
    ] * 4
    assert all(outcome.status.value == "rejected" for outcome in rejections)
    assert resolutions and resolutions[0].status.value == "accepted"
    assert result.final_response == "chose approve_once"


MALFORMED_DECISION_RUNNER = APPROVAL_RUNNER


def test_a_rejected_request_is_answered_rather_than_ending_the_channel(
    tmp_path: Path,
) -> None:
    """⟦S3.4⟧ The defect the invalid-choice case exposed, pinned on its own.

    `parse_request` validates params before the serve loop has a `request_id`,
    so a params-level violation used to be answered with the literal "invalid" —
    and a reply the supervisor is not waiting for ends the channel rather than
    being ignored, because correlation is the invariant. One malformed request
    therefore killed a worker mid-turn and left the turn parked forever.
    """

    from cortex_platform.product.runtime_update.supervisor import WorkerSupervisorV2

    descriptor_path, _state = _drive(tmp_path, APPROVAL_RUNNER)
    with WorkerSupervisorV2(descriptor_path) as supervisor:
        supervisor.begin_turn("attempt-malformed.0", REQUEST_DIGEST, _turn_request())
        events_stream = supervisor.turn_events("attempt-malformed.0")
        assert next(events_stream)["kind"] == "decision.required"
        # An empty choice fails the grammar itself, before the closed-set check.
        with pytest.raises(WorkerProtocolError, match="protocol_violation"):
            supervisor.resolve_turn(
                "attempt-malformed.0", {"decision_id": "decision-1", "choice": ""}
            )
        # The channel is still the same channel, and still answers.
        assert supervisor.request("health.check", {})["healthy"] is True
        supervisor.resolve_turn(
            "attempt-malformed.0",
            {"decision_id": "decision-1", "choice": "approve_once"},
        )
        finish = [event for event in events_stream][-1]
    assert finish["kind"] == "operation.finish"
    assert finish["payload"]["result"]["final_response"] == "chose approve_once"
