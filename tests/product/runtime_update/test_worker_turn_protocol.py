"""The framed, request-id-correlated half of `cortex-worker/2`.

S3.2 left a channel that could carry exactly one reply per request and nothing
else. A turn has to emit events while it runs and receive a decision while it is
parked, so the wire grows a frame discriminator and four turn methods — and both
stay closed, because a closed set is the only reason the worker can be trusted to
answer questions about itself.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from cortex_platform.product.runtime_update.worker_payload.cortex_worker.turn import (
    EVENT_FINISH,
    EVENT_HEARTBEAT,
    Heartbeat,
    TurnEmitter,
)

from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    ProtocolViolation,
    WorkerError,
    WorkerEvent,
    WorkerResponse,
    encode_event,
    encode_response,
    parse_frame,
    parse_request,
)


def _line(method: str, params: dict, *, token: str = "t" * 32) -> bytes:
    return json.dumps(
        {
            "protocol": PROTOCOL_V2,
            "request_id": "r1",
            "token": token,
            "method": method,
            "params": params,
        }
    ).encode()


def test_reply_frames_carry_six_keys_and_an_operation_id() -> None:
    reply = json.loads(
        encode_response(
            WorkerResponse(request_id="r1", operation_id="a.0", result={"state": "accepted"})
        )
    )
    assert set(reply) == {
        "protocol",
        "frame",
        "request_id",
        "operation_id",
        "ok",
        "result",
    }
    assert reply["frame"] == "reply"
    assert reply["operation_id"] == "a.0"


def test_handshake_replies_carry_a_null_operation_id() -> None:
    reply = json.loads(encode_response(WorkerResponse(request_id="r1", result={"ok": 1})))
    assert reply["operation_id"] is None
    failure = json.loads(
        encode_response(
            WorkerResponse(request_id="r1", error=WorkerError("not_measured", "no"))
        )
    )
    assert set(failure) == {
        "protocol",
        "frame",
        "request_id",
        "operation_id",
        "ok",
        "error",
    }


def test_event_frames_are_unsolicited_and_carry_no_request_id() -> None:
    frame = json.loads(
        encode_event(
            WorkerEvent(operation_id="a.0", sequence=0, event={"kind": "heartbeat", "payload": {}})
        )
    )
    assert set(frame) == {"protocol", "frame", "operation_id", "sequence", "event"}
    assert frame["frame"] == "event"
    parsed = parse_frame(json.dumps(frame).encode())
    assert isinstance(parsed, WorkerEvent)
    assert parsed.sequence == 0 and parsed.event["kind"] == "heartbeat"


def test_parse_frame_round_trips_a_reply() -> None:
    parsed = parse_frame(
        encode_response(WorkerResponse(request_id="r1", operation_id=None, result=7))
    )
    assert isinstance(parsed, WorkerResponse)
    assert parsed.request_id == "r1" and parsed.result == 7 and parsed.operation_id is None


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("turn.begin", {"operation_id": "a.0", "request_digest": "0" * 64, "request": {}}),
        ("turn.resolve", {"operation_id": "a.0", "decision": {"decision_id": "d", "choice": "deny"}}),
        ("turn.cancel", {"operation_id": "a.0"}),
    ],
)
def test_the_turn_methods_are_accepted(method: str, params: dict) -> None:
    request = parse_request(_line(method, params), token="t" * 32)
    assert request.method == method
    assert request.params["operation_id"] == "a.0"


def test_turn_event_is_worker_to_supervisor_only() -> None:
    """The predecessor's else-branch treated any unmatched method as a status read."""

    with pytest.raises(ProtocolViolation):
        parse_request(
            _line("turn.event", {"operation_id": "a.0", "sequence": 0, "event": {}}),
            token="t" * 32,
        )


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("turn.begin", {"operation_id": "a.0", "request_digest": "zz", "request": {}}),
        ("turn.begin", {"operation_id": "a.0", "request_digest": "0" * 64, "request": []}),
        ("turn.begin", {"operation_id": "a.0", "request_digest": "0" * 64}),
        ("turn.resolve", {"operation_id": "a.0", "decision": {"decision_id": "d"}}),
        ("turn.resolve", {"operation_id": "a.0", "decision": {"decision_id": "", "choice": "x"}}),
        ("turn.cancel", {"operation_id": "../escape"}),
    ],
)
def test_malformed_turn_params_are_refused(method: str, params: dict) -> None:
    with pytest.raises(ProtocolViolation):
        parse_request(_line(method, params), token="t" * 32)


@pytest.mark.parametrize(
    "frame",
    [
        {"protocol": PROTOCOL_V2, "frame": "reply", "request_id": "r1", "ok": True, "result": 1},
        {"protocol": "cortex-worker/1", "frame": "reply", "request_id": "r1",
         "operation_id": None, "ok": True, "result": 1},
        {"protocol": PROTOCOL_V2, "frame": "event", "operation_id": "a.0",
         "sequence": -1, "event": {"kind": "heartbeat", "payload": {}}},
        {"protocol": PROTOCOL_V2, "frame": "event", "operation_id": "a.0",
         "sequence": 0, "event": {"kind": "", "payload": {}}},
        {"protocol": PROTOCOL_V2, "frame": "other", "request_id": "r1",
         "operation_id": None, "ok": True, "result": 1},
    ],
)
def test_malformed_frames_are_refused(frame: dict) -> None:
    with pytest.raises(ProtocolViolation):
        parse_frame(json.dumps(frame).encode())


def test_operation_finish_seals_the_turn_and_refuses_every_later_emit() -> None:
    """⟦AMD-5⟧ `operation.finish` is the turn's last frame, deterministically.

    `Heartbeat.stop()` only set a flag, and an iteration that had already
    returned from `self._stop.wait(interval)` was unstoppable — it could land
    between the ledger's fsynced `finish()` and the finish event. A bounded
    join narrows that window but cannot close it; the seal is what a regression
    test can assert.
    """

    written: list[bytes] = []
    emitter = TurnEmitter("attempt-seal.0", written.append)
    assert emitter.emit("token.delta", {"text": "x"}) is True
    assert emitter.sealed is False
    assert emitter.emit(EVENT_FINISH, {"outcome": "committed"}) is True
    assert emitter.sealed is True
    # The heartbeat timer's write, arriving after the seal.
    assert emitter.emit(EVENT_HEARTBEAT, {}) is False
    assert emitter.emit("token.delta", {"text": "late"}) is False
    kinds = [json.loads(frame)["event"]["kind"] for frame in written]
    assert kinds == ["token.delta", EVENT_FINISH]
    assert kinds[-1] == EVENT_FINISH


def test_stopping_the_heartbeat_joins_its_thread_within_a_bound() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_write(_frame: bytes) -> None:
        entered.set()
        release.wait(5)

    emitter = TurnEmitter("attempt-join.0", blocking_write)
    heartbeat = Heartbeat(emitter, interval=0.01)
    heartbeat.start()
    assert entered.wait(5)
    started = time.monotonic()
    release.set()
    heartbeat.stop()
    # `stop()` joins, so by the time it returns the timer thread is gone rather
    # than merely asked to leave. Bounded: an unbounded join lets a `_write`
    # blocked on a full pipe hang the turn thread forever.
    assert heartbeat.joined is True
    assert time.monotonic() - started < 5
