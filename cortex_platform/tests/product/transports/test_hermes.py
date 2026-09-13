from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    FrozenTransportDeliveryProjection,
    TransportDeliveryChunkProjection,
    TransportDeliveryChunkSendPermit,
    TransportDeliveryKey,
)
from cortex_platform.product.transports.hermes import (
    HermesTelegramClient,
    _HermesCapabilityUnavailable,
    _HermesFrozenSendRequest,
    _HermesOutcomeUnknown,
    _HermesSendResult,
    _HermesTelegramCapability,
    _HermesTelegramCapabilityProbe,
    _parse_send_result,
    _serialize_send_request,
)
from cortex_platform.product.transports.models import TelegramDestination
from cortex_platform.product.transports.ports import ControlTransportChunkPort


class StubRPC:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, Mapping[str, object], float]] = []

    def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> object:
        self.calls.append((method, params, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _capability() -> dict[str, object]:
    return {
        "protocol": "cortex.telegram.transport/1",
        "send_message": True,
        "topics": True,
        "inline_buttons": True,
        "markdown_v2": True,
        "provider_idempotency": False,
        "outcome_query": False,
    }


def test_capability_probe_accepts_only_the_closed_contract_and_caches_it() -> None:
    rpc = StubRPC(_capability())
    probe = _HermesTelegramCapabilityProbe(rpc, timeout=2.5)

    expected = _HermesTelegramCapability(
        protocol="cortex.telegram.transport/1",
        send_message=True,
        topics=True,
        inline_buttons=True,
        markdown_v2=True,
        provider_idempotency=False,
        outcome_query=False,
    )
    assert probe.probe() == expected
    assert probe.probe() is probe.probe()
    assert rpc.calls == [("telegram.capabilities", {}, 2.5)]


def _missing_field(value: dict[str, object]) -> None:
    value.pop("outcome_query")


def _extra_field(value: dict[str, object]) -> None:
    value["raw_provider"] = "must-not-cross"


def _protocol_drift(value: dict[str, object]) -> None:
    value["protocol"] = "cortex.telegram.transport/2"


def _wrong_field_type(value: dict[str, object]) -> None:
    value["provider_idempotency"] = 0


@pytest.mark.parametrize(
    "mutate",
    [_missing_field, _extra_field, _protocol_drift, _wrong_field_type],
)
def test_capability_probe_rejects_malformed_or_drifted_contracts(mutate) -> None:
    raw = deepcopy(_capability())
    mutate(raw)

    with pytest.raises(
        _HermesCapabilityUnavailable,
        match="^hermes_telegram_capability_unavailable$",
    ):
        _HermesTelegramCapabilityProbe(StubRPC(raw)).probe()


@pytest.mark.parametrize(
    "unsupported",
    ["send_message", "topics", "inline_buttons", "markdown_v2"],
)
def test_capability_probe_rejects_missing_required_support(unsupported: str) -> None:
    raw = _capability()
    raw[unsupported] = False

    with pytest.raises(
        _HermesCapabilityUnavailable,
        match="^hermes_telegram_capability_unavailable$",
    ):
        _HermesTelegramCapabilityProbe(StubRPC(raw)).probe()


def test_capability_probe_sanitizes_and_re_asks_after_an_rpc_failure() -> None:
    """⟦F-B2⟧ A failed probe is sanitized, and it is not the final answer.

    `_attempted` was set BEFORE the RPC, so one lost race -- a worker still
    starting, a relaunch, a single timed-out frame -- made this raise for the
    life of the client. `send_chunk` calls `capability_binding_digest()` too, so
    that one race became a daemon-lifetime outbound outage with nothing on any
    surface naming it.
    """

    rpc = StubRPC(RuntimeError("raw worker secret and destination"))
    probe = _HermesTelegramCapabilityProbe(rpc)

    for _ in range(2):
        with pytest.raises(_HermesCapabilityUnavailable) as captured:
            probe.probe()
        assert str(captured.value) == "hermes_telegram_capability_unavailable"
        assert "secret" not in repr(captured.value)
        assert "destination" not in repr(captured.value)
    assert len(rpc.calls) == 2


def test_a_capability_probe_that_finally_answers_is_retained() -> None:
    """The worker came up; the digest is asked for once and then remembered."""

    rpc = StubRPC(RuntimeError("not ready"))
    probe = _HermesTelegramCapabilityProbe(rpc)
    with pytest.raises(_HermesCapabilityUnavailable):
        probe.probe()

    rpc.response = _capability()
    first = probe.probe()
    second = probe.probe()

    assert first is second
    assert len(rpc.calls) == 2


def test_hermes_client_requires_the_control_chunk_port() -> None:
    rpc = StubRPC(_capability())

    with pytest.raises(TypeError, match="ControlTransportChunkPort"):
        HermesTelegramClient(
            rpc=rpc,
            state=object(),  # type: ignore[arg-type]
            bot_identity="research-bot",
        )
    assert rpc.calls == []


def test_forged_public_permit_cannot_reach_rpc(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "control.db")
    store.initialize()
    state = ControlTransportChunkPort(store=store, worker_id="permit-test-worker")
    rpc = StubRPC(_capability())
    client = HermesTelegramClient(
        rpc=rpc,
        state=state,
        bot_identity="research-bot",
    )
    chunk = TransportDeliveryChunkProjection(
        chunk_index=0,
        operation_id="telegram.delivery.forged:chunk:0",
        text="Forged",
        chunk_hash="0" * 64,
    )
    projection = FrozenTransportDeliveryProjection(
        delivery_key=TransportDeliveryKey(
            transport="telegram",
            destination_digest="hmac-sha256:" + "d" * 64,
            event_id="forged-event",
            projection_version=1,
        ),
        operation_id="telegram.delivery.forged",
        destination_binding_digest="hmac-sha256:" + "d" * 64,
        routing="topic",
        capability_binding_digest="0" * 64,
        rpc_timeout_seconds=30,
        projection_hash="0" * 64,
        chunks=(chunk,),
    )
    forged = TransportDeliveryChunkSendPermit(
        projection=projection,
        chunk=chunk,
        revision=1,
        claim_epoch=1,
    )

    with pytest.raises(_HermesOutcomeUnknown):
        client.send_chunk(
            permit=forged,
            destination=TelegramDestination(chat_id=-100, topic_id=41),
        )
    assert rpc.calls == []


def test_send_request_serializes_exact_topic_and_final_chunk_buttons() -> None:
    request = _HermesFrozenSendRequest(
        operation_id="delivery:event-1:chunk-2",
        chat_id=-100123456789,
        topic_id=41,
        text="Research completed\\.",
        buttons=(("Open", "opaque-callback-token"),),
    )

    assert _serialize_send_request(request) == {
        "schema_version": 1,
        "operation_id": "delivery:event-1:chunk-2",
        "chat_id": -100123456789,
        "topic_id": 41,
        "text": "Research completed\\.",
        "parse_mode": "MarkdownV2",
        "buttons": [
            {"label": "Open", "callback_data": "opaque-callback-token"}
        ],
    }
    rendered = repr(request)
    assert "-100123456789" not in rendered
    assert "opaque-callback-token" not in rendered
    assert "Research completed" not in rendered


def test_send_request_serializes_root_routing_without_buttons() -> None:
    request = _HermesFrozenSendRequest(
        operation_id="delivery:event-2:chunk-0",
        chat_id=12345,
        topic_id=None,
        text="Ready",
        buttons=(),
    )

    assert _serialize_send_request(request) == {
        "schema_version": 1,
        "operation_id": "delivery:event-2:chunk-0",
        "chat_id": 12345,
        "topic_id": None,
        "text": "Ready",
        "parse_mode": "MarkdownV2",
        "buttons": [],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"chat_id": True},
        {"topic_id": 0},
        {"text": ""},
        {"parse_mode": "HTML"},
        {"buttons": (("Open", "x" * 65),)},
        {"buttons": (("", "opaque-callback-token"),)},
    ],
)
def test_send_request_rejects_invalid_fields_without_disclosure(overrides) -> None:
    values = {
        "operation_id": "delivery:event-secret:chunk-0",
        "chat_id": -100987654321,
        "topic_id": 41,
        "text": "Safe text",
        "parse_mode": "MarkdownV2",
        "buttons": (("Open", "opaque-callback-token"),),
    }
    values.update(overrides)

    with pytest.raises(ValueError) as captured:
        _HermesFrozenSendRequest(**values)
    rendered = repr(captured.value)
    assert "opaque-callback-token" not in rendered
    assert "-100987654321" not in rendered
    assert "event-secret" not in rendered


def test_send_result_reduces_accepted_reference_without_disclosure() -> None:
    result = _parse_send_result(
        {"status": "accepted", "provider_message_ref": "provider-secret-42"}
    )

    assert result == _HermesSendResult(
        status="accepted", provider_message_ref="provider-secret-42"
    )
    assert "provider-secret-42" not in repr(result)


def test_send_result_reduces_rate_limit_with_bounded_delay() -> None:
    result = _parse_send_result(
        {"status": "rate_limited", "retry_after_ms": 2_500}
    )

    assert result == _HermesSendResult(
        status="rate_limited", retry_after_ms=2_500
    )


def test_send_result_reduces_proved_before_send_retry() -> None:
    assert _parse_send_result({"status": "retryable_before_send"}) == (
        _HermesSendResult(status="retryable_before_send")
    )


def test_send_result_reduces_permanent_sanitized_rejection() -> None:
    assert _parse_send_result(
        {"status": "rejected", "category": "destination_unavailable"}
    ) == _HermesSendResult(status="rejected", category="destination_unavailable")


def _missing_send_field(value: dict[str, object]) -> None:
    value.pop("provider_message_ref")


def _extra_send_field(value: dict[str, object]) -> None:
    value["provider_payload"] = "raw-provider-secret"


def _invalid_send_status(value: dict[str, object]) -> None:
    value["status"] = "temporarily_failed"


def _invalid_retry_delay(value: dict[str, object]) -> None:
    value.clear()
    value.update({"status": "rate_limited", "retry_after_ms": 3_600_001})


@pytest.mark.parametrize(
    "raw,mutate",
    [
        (
            {"status": "accepted", "provider_message_ref": "provider-secret-42"},
            _missing_send_field,
        ),
        (
            {"status": "accepted", "provider_message_ref": "provider-secret-42"},
            _extra_send_field,
        ),
        (
            {"status": "accepted", "provider_message_ref": "provider-secret-42"},
            _invalid_send_status,
        ),
        (
            {"status": "rate_limited", "retry_after_ms": 100},
            _invalid_retry_delay,
        ),
    ],
)
def test_send_result_malformed_payload_is_sanitized_outcome_unknown(
    raw: dict[str, object], mutate
) -> None:
    mutate(raw)

    with pytest.raises(_HermesOutcomeUnknown) as captured:
        _parse_send_result(raw)
    rendered = repr(captured.value)
    assert str(captured.value) == "hermes_telegram_outcome_unknown"
    assert "provider-secret-42" not in rendered
    assert "raw-provider-secret" not in rendered


def test_send_result_explicit_unknown_remains_manual_required() -> None:
    with pytest.raises(
        _HermesOutcomeUnknown,
        match="^hermes_telegram_outcome_unknown$",
    ):
        _parse_send_result({"status": "outcome_unknown"})
