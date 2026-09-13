from __future__ import annotations

import hashlib
import hmac
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from cortex_platform.product import control

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _target(
    *,
    namespace: str,
    token_digest: str,
) -> control.TransportOpaqueTargetRegistration:
    return control.TransportOpaqueTargetRegistration(
        token_digest=token_digest,
        expires_at=NOW,
        target=control.TransportOpaqueTarget(
            namespace=namespace,  # type: ignore[arg-type]
            purpose="control" if namespace == "action" else "open",
            resource_kind="run",
            resource_id="run-1",
            expected_revision=3 if namespace == "action" else None,
            choice="retry" if namespace == "action" else None,
            scope_digest=(
                "hmac-sha256:" + "d" * 64 if namespace == "action" else None
            ),
            expires_at=NOW,
        ),
    )


def _projection() -> tuple[
    control.FrozenTransportDeliveryProjection,
    tuple[control.TransportOpaqueTargetRegistration, ...],
]:
    callback = "ac1.callback.signature"
    action_digest = hashlib.sha256(callback.encode()).hexdigest()
    deep_link_token = "dl1.deep-link.signature"
    text = f"Open https://cortex.example/open?token={deep_link_token}"
    encoded = text.encode("utf-8")
    token_bytes = deep_link_token.encode()
    start = encoded.index(token_bytes)
    capabilities = (
        control.TransportDeliveryCapability(
            namespace="action",
            token_digest=action_digest,
            expires_at=NOW,
        ),
        control.TransportDeliveryCapability(
            namespace="deep_link",
            token_digest=hashlib.sha256(token_bytes).hexdigest(),
            expires_at=NOW,
            start_offset=start,
            end_offset=start + len(token_bytes),
        ),
    )
    chunk = control.TransportDeliveryChunkProjection(
        chunk_index=0,
        operation_id="telegram-delivery-1:chunk:0",
        text=text,
        parse_mode="MarkdownV2",
        buttons=(
            control.TransportDeliveryButton(
                label="Retry",
                callback_data=callback,
                token_digest=action_digest,
            ),
        ),
        capabilities=capabilities,
        chunk_hash="0" * 64,
    )
    chunk = replace(
        chunk,
        chunk_hash=control.transport_delivery_chunk_hash(chunk),
    )
    projection = control.FrozenTransportDeliveryProjection(
        delivery_key=control.TransportDeliveryKey(
            transport="telegram",
            destination_digest="hmac-sha256:" + "d" * 64,
            event_id="event-1",
            projection_version=2,
        ),
        operation_id="telegram-delivery-1",
        destination_binding_digest="hmac-sha256:" + "d" * 64,
        routing="topic",
        capability_binding_digest="a" * 64,
        rpc_timeout_seconds=30,
        projection_hash="0" * 64,
        chunks=(chunk,),
    )
    targets = (
        _target(namespace="action", token_digest=action_digest),
        _target(
            namespace="deep_link",
            token_digest=hashlib.sha256(token_bytes).hexdigest(),
        ),
    )
    return (
        replace(
            projection,
            projection_hash=control.transport_delivery_projection_hash(
                projection,
                opaque_targets=targets,
            ),
        ),
        targets,
    )


def test_transport_delivery_contracts_are_frozen_and_hide_payloads() -> None:
    projection, _ = _projection()
    claim = control.TransportDeliveryChunkClaim(
        projection=projection,
        chunk=projection.chunks[0],
        status="claimed",
        revision=1,
        claim_owner="worker-1",
        claim_epoch=1,
        retry_not_before=None,
    )

    with pytest.raises(FrozenInstanceError):
        claim.revision = 2  # type: ignore[misc]
    assert projection.chunks[0].text not in repr(projection)
    assert projection.chunks[0].buttons[0].callback_data not in repr(projection)


def test_transport_delivery_operation_ids_and_receipts_are_deterministic() -> None:
    assert (
        control.transport_delivery_chunk_operation_id("telegram-delivery-1", 7)
        == "telegram-delivery-1:chunk:7"
    )
    with pytest.raises(ValueError, match="operation_id"):
        control.transport_delivery_chunk_operation_id("x" * 194, 99)

    identity_key = b"i" * 32
    provider_reference = "provider-message-42"
    expected = hmac.new(
        identity_key,
        b"telegram-provider-receipt\0" + provider_reference.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert control.telegram_provider_receipt_digest(
        identity_key, provider_reference
    ) == f"hmac-sha256:{expected}"
    with pytest.raises(ValueError, match="identity_key"):
        control.telegram_provider_receipt_digest(b"short", provider_reference)


def test_transport_delivery_hashes_bind_payload_capabilities_and_targets() -> None:
    projection, targets = _projection()
    assert control.transport_delivery_projection_hash(
        projection, opaque_targets=targets
    ) == projection.projection_hash

    changed_chunk = replace(
        projection.chunks[0],
        buttons=(replace(projection.chunks[0].buttons[0], label="Again"),),
    )
    changed_chunk = replace(
        changed_chunk,
        chunk_hash=control.transport_delivery_chunk_hash(changed_chunk),
    )
    changed = replace(projection, chunks=(changed_chunk,))
    assert control.transport_delivery_projection_hash(
        changed, opaque_targets=targets
    ) != projection.projection_hash

    changed_target = replace(
        targets[0],
        target=replace(targets[0].target, resource_id="run-2"),
    )
    assert control.transport_delivery_projection_hash(
        projection, opaque_targets=(changed_target, targets[1])
    ) != projection.projection_hash


def test_transport_delivery_chunk_rejects_text_over_utf8_byte_bound() -> None:
    chunk = control.TransportDeliveryChunkProjection(
        chunk_index=0,
        operation_id="telegram-delivery-1:chunk:0",
        text="界" * 2_000,
        parse_mode="MarkdownV2",
        buttons=(),
        capabilities=(),
        chunk_hash="0" * 64,
    )

    with pytest.raises(ValueError, match="chunk text"):
        control.transport_delivery_chunk_hash(chunk)


def test_transport_delivery_chunk_rejects_non_utf8_capability_boundaries() -> None:
    text = "界"
    token_bytes = text.encode("utf-8")[:1]
    chunk = control.TransportDeliveryChunkProjection(
        chunk_index=0,
        operation_id="telegram-delivery-1:chunk:0",
        text=text,
        parse_mode="MarkdownV2",
        buttons=(),
        capabilities=(
            control.TransportDeliveryCapability(
                namespace="deep_link",
                token_digest=hashlib.sha256(token_bytes).hexdigest(),
                expires_at=NOW,
                start_offset=0,
                end_offset=1,
            ),
        ),
        chunk_hash="0" * 64,
    )

    with pytest.raises(ValueError, match="UTF-8 boundary"):
        control.transport_delivery_chunk_hash(chunk)


def test_transport_delivery_chunk_rejects_undeclared_deep_link_token() -> None:
    projection, _ = _projection()
    chunk = replace(
        projection.chunks[0],
        text=projection.chunks[0].text + " dl1.undeclared.signature",
    )

    with pytest.raises(ValueError, match="undeclared deep_link"):
        control.transport_delivery_chunk_hash(chunk)


def test_transport_delivery_chunk_rejects_overlong_deep_link_token() -> None:
    token = "dl1." + "a" * 70 + "." + "b" * 70
    token_bytes = token.encode()
    chunk = control.TransportDeliveryChunkProjection(
        chunk_index=0,
        operation_id="telegram-delivery-1:chunk:0",
        text=token,
        parse_mode="MarkdownV2",
        buttons=(),
        capabilities=(
            control.TransportDeliveryCapability(
                namespace="deep_link",
                token_digest=hashlib.sha256(token_bytes).hexdigest(),
                expires_at=NOW,
                start_offset=0,
                end_offset=len(token_bytes),
            ),
        ),
        chunk_hash="0" * 64,
    )

    with pytest.raises(ValueError, match="128 bytes"):
        control.transport_delivery_chunk_hash(chunk)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda projection, targets: (
                replace(
                    projection,
                    chunks=(
                        replace(
                            projection.chunks[0],
                            operation_id="other:chunk:0",
                        ),
                    ),
                ),
                targets,
            ),
            "operation_id",
        ),
        (
            lambda projection, targets: (
                replace(
                    projection,
                    chunks=(
                        replace(
                            projection.chunks[0],
                            capabilities=(
                                projection.chunks[0].capabilities[0],
                                replace(
                                    projection.chunks[0].capabilities[1],
                                    start_offset=0,
                                ),
                            ),
                        ),
                    ),
                ),
                targets,
            ),
            "deep_link",
        ),
        (
            lambda projection, targets: (projection, targets[:1]),
            "opaque target",
        ),
    ],
)
def test_transport_delivery_projection_validation_fails_closed(
    mutation,
    message: str,
) -> None:
    projection, targets = _projection()
    projection, targets = mutation(projection, targets)
    with pytest.raises(ValueError, match=message):
        control.validate_transport_delivery_projection(
            projection,
            opaque_targets=targets,
        )


def test_transport_delivery_target_rejects_private_path_identity() -> None:
    projection, targets = _projection()
    changed_targets = (
        replace(
            targets[0],
            target=replace(
                targets[0].target,
                resource_id="/Users/operator/private",
            ),
        ),
        targets[1],
    )

    with pytest.raises(ValueError, match="resource_id"):
        control.transport_delivery_projection_hash(
            projection,
            opaque_targets=changed_targets,
        )
