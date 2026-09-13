from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    ProtocolViolation,
    SlotInterpreterDescriptor,
    WorkerError,
    WorkerResponse,
    encode_response,
    parse_request,
)


TOKEN = "private-token"
DIGEST = "a" * 64


def _request(method: str, params: object, **updates: object) -> bytes:
    value: dict[str, object] = {
        "protocol": PROTOCOL_V2,
        "request_id": "request-1",
        "token": TOKEN,
        "method": method,
        "params": params,
    }
    value.update(updates)
    return json.dumps(value).encode("utf-8")


def _descriptor(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    slot = tmp_path / DIGEST
    state = tmp_path / "state"
    slot.mkdir()
    state.mkdir()
    value: dict[str, object] = {
        "schema_version": 1,
        "slot_path": str(slot),
        "slot_id": "primary",
        "state_generation_id": "generation-1",
        "release_id": "release-1",
        "expected_artifact_digest": DIGEST,
        "expected_manifest_sha256": "b" * 64,
        "expected_content_tree_sha256": "c" * 64,
        "expected_interpreter_sha256": "d" * 64,
        "interpreter_path": sys.executable,
        "worker_entrypoint": "runtime_worker.py",
        "state_dir": str(state),
        "worker_protocol": PROTOCOL_V2,
    }
    path = tmp_path / "descriptor.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (b"not-json", "invalid JSON"),
        (
            b'{"protocol":"cortex-worker/2","protocol":"cortex-worker/2"}',
            "duplicate keys",
        ),
        (_request("health.check", {}, extra=True), "closed request fields"),
        (_request("health.check", {}, protocol="cortex-worker/1"), "protocol"),
        (_request("health.check", {}, token="wrong"), "token"),
        (_request("health.check", {}, request_id=""), "request_id"),
        (_request("arbitrary.call", {}), "method"),
        (_request("health.check", {"unexpected": True}), "params"),
        (_request("operation.status", {"operation_id": "bad/id"}), "operation_id"),
        (
            _request(
                "operation.begin",
                {"operation_id": "op-1", "kind": "run", "request_digest": "xyz"},
            ),
            "digest",
        ),
        (
            _request(
                "operation.finish",
                {"operation_id": "op-1", "outcome": "unknown", "result_digest": DIGEST},
            ),
            "outcome",
        ),
    ],
)
def test_parse_request_rejects_protocol_deviations(line: bytes, reason: str) -> None:
    with pytest.raises(ProtocolViolation, match=reason):
        parse_request(line, token=TOKEN)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("identity.measure", {}),
        ("health.check", {}),
        (
            "operation.begin",
            {"operation_id": "op_1", "kind": "run", "request_digest": DIGEST},
        ),
        (
            "operation.finish",
            {"operation_id": "op_1", "outcome": "committed", "result_digest": DIGEST},
        ),
        ("operation.status", {"operation_id": "op_1"}),
        ("shutdown", {}),
    ],
)
def test_parse_request_accepts_each_closed_method(method: str, params: dict[str, object]) -> None:
    request = parse_request(_request(method, params), token=TOKEN)
    assert request.method == method
    assert request.params == params
    assert request.request_id == "request-1"


def test_encode_response_emits_exact_success_or_error_schema() -> None:
    success = json.loads(
        encode_response(WorkerResponse(request_id="r1", result={"healthy": True}))
    )
    failure = json.loads(
        encode_response(
            WorkerResponse(
                request_id="r2",
                error=WorkerError("protocol_violation", "request rejected"),
            )
        )
    )
    assert success == {
        "protocol": PROTOCOL_V2,
        "frame": "reply",
        "request_id": "r1",
        "operation_id": None,
        "ok": True,
        "result": {"healthy": True},
    }
    assert failure == {
        "protocol": PROTOCOL_V2,
        "frame": "reply",
        "request_id": "r2",
        "operation_id": None,
        "ok": False,
        "error": {"category": "protocol_violation", "message": "request rejected"},
    }


def test_descriptor_loads_all_frozen_fields(tmp_path: Path) -> None:
    path, value = _descriptor(tmp_path)
    descriptor = SlotInterpreterDescriptor.load(path)
    assert descriptor.slot_path == Path(value["slot_path"])
    assert descriptor.expected_content_tree_sha256 == "c" * 64
    assert descriptor.expected_interpreter_sha256 == "d" * 64
    assert descriptor.worker_protocol == PROTOCOL_V2


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"unexpected": True}, "fields"),
        ({"expected_artifact_digest": "not-hex"}, "digest"),
        ({"expected_manifest_sha256": "D" * 64}, "digest"),
        ({"expected_content_tree_sha256": "e" * 63}, "digest"),
        ({"expected_interpreter_sha256": "not-hex"}, "digest"),
        ({"expected_interpreter_sha256": ""}, "non-empty"),
        ({"slot_path": "relative/slot"}, "absolute"),
        ({"interpreter_path": "python"}, "absolute"),
        ({"state_dir": "relative/state"}, "absolute"),
        ({"worker_protocol": "cortex-worker/1"}, "protocol"),
    ],
)
def test_descriptor_rejects_invalid_closed_document(
    tmp_path: Path, mutation: dict[str, object], reason: str
) -> None:
    path, value = _descriptor(tmp_path)
    value.update(mutation)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProtocolViolation, match=reason):
        SlotInterpreterDescriptor.load(path)


def test_descriptor_rejects_state_directory_inside_slot(tmp_path: Path) -> None:
    path, value = _descriptor(tmp_path)
    inside = Path(value["slot_path"]) / "state"
    value["state_dir"] = str(inside)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProtocolViolation, match="state_dir"):
        SlotInterpreterDescriptor.load(path)
