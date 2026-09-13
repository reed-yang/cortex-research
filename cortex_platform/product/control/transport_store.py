"""Frozen transport delivery ledger owned by the Cortex control store."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .errors import IdempotencyConflict, InvalidTransition, NotFound
from .transport import (
    FrozenTransportDeliveryProjection,
    TransportDeliveryButton,
    TransportDeliveryCapability,
    TransportDeliveryChunkClaim,
    TransportDeliveryChunkProjection,
    TransportDeliveryChunkSendDecision,
    TransportDeliveryChunkSendPermit,
    TransportDeliveryClaim,
    TransportDeliveryFreezeResult,
    TransportDeliveryKey,
    TransportOpaqueTarget,
    TransportOpaqueTargetRegistration,
    validate_transport_delivery_projection,
)

JsonObject = dict[str, Any]

_BINDING_DIGEST_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")
_TRANSPORT_WORKER_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


class TransportDeliveryStore:
    """The frozen per-chunk delivery ledger, inherited by the sole Control writer.

    Owns seven tables and nothing else: `transport_deliveries`,
    `transport_delivery_projections`, `transport_delivery_chunks`, its
    `_buttons`, `_capabilities` and `_resolutions` children, and
    `transport_opaque_targets`. Like `ResearchItemsStore` this is a mixin with no
    `__init__` and no connection of its own -- every statement below runs inside
    the `ControlStore` transaction that `self._transaction()` opens, so a freeze
    and the capability registrations it authorizes commit or roll back together.

    Bindings, transport command receipts and the activation/window decisions stay
    in `ControlStore`; this class only reads the activation gate, through
    `self._transport_activation_in_force`, inside the transaction that issues a
    send permit.
    """

    def claim_transport_delivery(
        self,
        *,
        key: TransportDeliveryKey,
        worker_id: str,
        lease_seconds: int,
    ) -> TransportDeliveryClaim:
        key = self._transport_delivery_key(key)
        worker_id = self._transport_worker_id(worker_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now_value = self._utc_now()
        now = self._format_time(now_value)
        expires_at = self._format_time(now_value + timedelta(seconds=lease_seconds))
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            projection = self._transport_delivery_projection_row(conn, key)
            if projection is not None:
                return TransportDeliveryClaim(
                    key=key,
                    status=(
                        "delivered"
                        if str(projection["state"]) == "delivered"
                        else "in_flight"
                    ),
                    claim_owner=None,
                    claim_epoch=0,
                    claim_expires_at=None,
                )
            row = self._transport_delivery_row(conn, key)
            if row is None:
                conn.execute(
                    """INSERT INTO transport_deliveries(
                           transport, destination_digest, event_id,
                           projection_version, state, claim_owner, claim_epoch,
                           claim_expires_at, created_at
                       ) VALUES (?, ?, ?, ?, 'claimed', ?, 1, ?, ?)""",
                    (*identity, worker_id, expires_at, now),
                )
                return self._transport_delivery_claim(conn, key, status="claimed")
            if row["state"] == "delivered":
                return self._transport_delivery_claim(conn, key, status="delivered")
            if row["state"] == "claimed" and row["claim_expires_at"] > now:
                return self._transport_delivery_claim(conn, key, status="in_flight")
            conn.execute(
                """UPDATE transport_deliveries
                   SET state = 'claimed', claim_owner = ?,
                       claim_epoch = claim_epoch + 1, claim_expires_at = ?
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?""",
                (worker_id, expires_at, *identity),
            )
            return self._transport_delivery_claim(conn, key, status="claimed")

    def freeze_transport_delivery_projection(
        self,
        *,
        projection: FrozenTransportDeliveryProjection,
        opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
        request_hash: str,
    ) -> TransportDeliveryFreezeResult:
        """Atomically freeze one exact per-chunk delivery and its capabilities."""

        validate_transport_delivery_projection(
            projection,
            opaque_targets=opaque_targets,
        )
        request_hash = self._transport_digest(request_hash, "request_hash")
        key = self._transport_delivery_key(projection.delivery_key)
        identity = self._transport_delivery_identity(key)
        target_positions = {
            registration.token_digest: position
            for position, registration in enumerate(opaque_targets)
        }
        with self._transaction() as conn:
            legacy = self._transport_delivery_row(conn, key)
            if legacy is not None:
                disposition = (
                    "already_delivered"
                    if legacy["state"] == "delivered"
                    else "legacy_delivery_uncertain"
                )
                return TransportDeliveryFreezeResult(
                    disposition=disposition,  # type: ignore[arg-type]
                    projection=None,
                )

            existing = self._transport_delivery_projection_row(conn, key)
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise IdempotencyConflict()
                frozen = self._verified_frozen_transport_delivery_projection(
                    conn,
                    key,
                )
                if str(existing["projection_hash"]) != projection.projection_hash:
                    raise InvalidTransition("projection_drift", "replayed")
                return TransportDeliveryFreezeResult(
                    disposition="replayed",
                    projection=frozen,
                )

            now = self._registry_now()
            try:
                conn.execute(
                    """INSERT INTO transport_delivery_projections(
                           transport, destination_digest, event_id,
                           projection_version, operation_id, request_hash,
                           projection_hash, destination_binding_digest, routing,
                           capability_binding_digest, rpc_timeout_seconds,
                           chunk_count, state, revision, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'pending', 0, ?)""",
                    (
                        *identity,
                        projection.operation_id,
                        request_hash,
                        projection.projection_hash,
                        projection.destination_binding_digest,
                        projection.routing,
                        projection.capability_binding_digest,
                        projection.rpc_timeout_seconds,
                        len(projection.chunks),
                        now,
                    ),
                )
                for chunk in projection.chunks:
                    conn.execute(
                        """INSERT INTO transport_delivery_chunks(
                               transport, destination_digest, event_id,
                               projection_version, chunk_index, operation_id,
                               text, parse_mode, chunk_hash, state, revision,
                               claim_epoch
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0)""",
                        (
                            *identity,
                            chunk.chunk_index,
                            chunk.operation_id,
                            chunk.text,
                            chunk.parse_mode,
                            chunk.chunk_hash,
                        ),
                    )
                    for position, button in enumerate(chunk.buttons):
                        conn.execute(
                            """INSERT INTO transport_delivery_chunk_buttons(
                                   transport, destination_digest, event_id,
                                   projection_version, chunk_index, position,
                                   label, callback_data, token_digest
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                *identity,
                                chunk.chunk_index,
                                position,
                                button.label,
                                button.callback_data,
                                button.token_digest,
                            ),
                        )
                    for position, capability in enumerate(chunk.capabilities):
                        conn.execute(
                            """INSERT INTO transport_delivery_chunk_capabilities(
                                   transport, destination_digest, event_id,
                                   projection_version, chunk_index, position,
                                   target_position, namespace, token_digest,
                                   expires_at, start_offset, end_offset
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                *identity,
                                chunk.chunk_index,
                                position,
                                target_positions[capability.token_digest],
                                capability.namespace,
                                capability.token_digest,
                                self._format_registry_time(capability.expires_at),
                                capability.start_offset,
                                capability.end_offset,
                            ),
                        )
                for registration in opaque_targets:
                    if conn.execute(
                        "SELECT 1 FROM transport_opaque_targets WHERE token_digest = ?",
                        (registration.token_digest,),
                    ).fetchone() is not None:
                        raise InvalidTransition("token_collision", "frozen")
                    target = self._transport_opaque_target(registration.target)
                    conn.execute(
                        """INSERT INTO transport_opaque_targets(
                               token_digest, namespace, purpose, resource_kind,
                               resource_id, expected_revision, choice,
                               scope_digest, expires_at, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            registration.token_digest,
                            target.namespace,
                            target.purpose,
                            target.resource_kind,
                            target.resource_id,
                            target.expected_revision,
                            target.choice,
                            target.scope_digest,
                            self._format_registry_time(target.expires_at),
                            now,
                        ),
                    )
            except sqlite3.IntegrityError:
                raise InvalidTransition("projection_conflict", "frozen") from None
            return TransportDeliveryFreezeResult(
                disposition="frozen",
                projection=self._frozen_transport_delivery_projection(conn, key),
            )

    def claim_transport_delivery_chunk(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        worker_id: str,
        lease_seconds: int,
    ) -> TransportDeliveryChunkClaim:
        key = self._transport_delivery_key(delivery_key)
        chunk_index = self._transport_chunk_index(chunk_index)
        worker_id = self._transport_worker_id(worker_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now_value = self._utc_now()
        now = self._format_registry_time(now_value)
        expires_at = self._format_registry_time(
            now_value + timedelta(seconds=lease_seconds)
        )
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            row = self._transport_delivery_chunk_row(conn, key, chunk_index)
            if row["state"] in {"sending_unknown", "delivered", "failed"}:
                return self._transport_delivery_chunk_claim(conn, key, chunk_index)
            if conn.execute(
                """SELECT 1 FROM transport_delivery_chunks
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND chunk_index < ? AND state != 'delivered' LIMIT 1""",
                (*identity, chunk_index),
            ).fetchone() is not None:
                raise InvalidTransition("chunk_order", "claimed")
            if row["state"] == "claimed" and str(row["claim_expires_at"]) > now:
                return self._transport_delivery_chunk_claim(conn, key, chunk_index)
            if (
                row["state"] == "pending"
                and row["retry_not_before"] is not None
                and str(row["retry_not_before"]) > now
            ):
                return self._transport_delivery_chunk_claim(
                    conn,
                    key,
                    chunk_index,
                    status="deferred",
                )
            conn.execute(
                """UPDATE transport_delivery_chunks
                   SET state = 'claimed', revision = revision + 1,
                       claim_owner = ?, claim_epoch = claim_epoch + 1,
                       claim_expires_at = ?, retry_not_before = NULL
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND chunk_index = ?""",
                (worker_id, expires_at, *identity, chunk_index),
            )
            self._refresh_transport_delivery_parent(conn, key, now=now)
            return self._transport_delivery_chunk_claim(conn, key, chunk_index)

    def begin_transport_delivery_chunk_send(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        worker_id: str,
        claim_epoch: int,
        expected_revision: int,
        observed_capability_binding_digest: str,
    ) -> TransportDeliveryChunkSendDecision:
        key = self._transport_delivery_key(delivery_key)
        chunk_index = self._transport_chunk_index(chunk_index)
        worker_id = self._transport_worker_id(worker_id)
        claim_epoch = self._transport_claim_epoch(claim_epoch)
        expected_revision = self._transport_revision(expected_revision)
        observed_digest = self._transport_digest(
            observed_capability_binding_digest,
            "observed_capability_binding_digest",
        )
        now_value = self._utc_now()
        now = self._format_registry_time(now_value)
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            row = self._transport_delivery_chunk_row(conn, key, chunk_index)
            self._require_transport_chunk_fence(
                row,
                source="claimed",
                target="sending_unknown",
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
                now=now,
                require_live_lease=True,
            )
            projection = self._verified_frozen_transport_delivery_projection(
                conn,
                key,
            )
            if projection.capability_binding_digest != observed_digest:
                self._fail_transport_delivery_chunk_row(
                    conn,
                    key,
                    chunk_index,
                    category="capability_mismatch",
                    now=now,
                )
                return TransportDeliveryChunkSendDecision(
                    status="capability_mismatch",
                    permit=None,
                )
            deadline = self._format_registry_time(
                now_value
                + timedelta(seconds=projection.rpc_timeout_seconds)
            )
            if conn.execute(
                """SELECT 1 FROM transport_delivery_chunk_capabilities
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND chunk_index = ? AND expires_at <= ? LIMIT 1""",
                (*identity, chunk_index, deadline),
            ).fetchone() is not None:
                self._fail_transport_delivery_chunk_row(
                    conn,
                    key,
                    chunk_index,
                    category="capability_expired",
                    now=now,
                )
                return TransportDeliveryChunkSendDecision(
                    status="capability_expired",
                    permit=None,
                )
            conn.execute(
                """UPDATE transport_delivery_chunks
                   SET state = 'sending_unknown', revision = revision + 1,
                       claim_expires_at = NULL
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND chunk_index = ?""",
                (*identity, chunk_index),
            )
            self._refresh_transport_delivery_parent(conn, key, now=now)
            claim = self._transport_delivery_chunk_claim(conn, key, chunk_index)
            # Read here, inside the transaction that issues the permit: this is
            # the window that authorizes the send, and no later reading of the
            # gate can be that.
            authorizing = self._transport_activation_in_force(
                conn, key.transport
            )
            return TransportDeliveryChunkSendDecision(
                status="send_permitted",
                permit=TransportDeliveryChunkSendPermit(
                    projection=claim.projection,
                    chunk=claim.chunk,
                    revision=claim.revision,
                    claim_epoch=claim.claim_epoch,
                    transport_window_id=(
                        None
                        if authorizing is None or authorizing.scope != "window"
                        else authorizing.id
                    ),
                ),
            )

    def complete_transport_delivery_chunk(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        worker_id: str,
        claim_epoch: int,
        expected_revision: int,
        provider_receipt_digest: str,
        transport_window_id: str | None = None,
    ) -> TransportDeliveryChunkClaim:
        """Record a delivered chunk and, when one authorized it, its window.

        `transport_window_id` is the AMD-5 landing surface: a send made inside
        a transport window has to name the decision that permitted it, and
        the receipt this row carries is an HMAC digest with no room for it.
        The column is write-once, so a redelivery cannot re-attribute a chunk
        to a later authorization.
        """

        key, chunk_index, worker_id, claim_epoch, expected_revision = (
            self._validated_transport_chunk_mutation(
                delivery_key=delivery_key,
                chunk_index=chunk_index,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
            )
        )
        receipt_digest = self._binding_digest(
            provider_receipt_digest,
            "provider_receipt_digest",
        )
        if transport_window_id is not None:
            transport_window_id = self._required_text(
                transport_window_id, "transport_window_id", maximum=200
            )
        now = self._registry_now()
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            row = self._transport_delivery_chunk_row(conn, key, chunk_index)
            self._require_transport_chunk_fence(
                row,
                source="sending_unknown",
                target="delivered",
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
            )
            conn.execute(
                """UPDATE transport_delivery_chunks
                   SET state = 'delivered', revision = revision + 1,
                       claim_owner = NULL, provider_receipt_digest = ?,
                       completed_at = ?,
                       transport_window_id = COALESCE(?, transport_window_id)
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND chunk_index = ?""",
                (receipt_digest, now, transport_window_id, *identity, chunk_index),
            )
            self._refresh_transport_delivery_parent(conn, key, now=now)
            return self._transport_delivery_chunk_claim(conn, key, chunk_index)

    def release_transport_delivery_chunk(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        worker_id: str,
        claim_epoch: int,
        expected_revision: int,
        proof: Literal["rpc_not_started", "provider_proved_before_send"],
        retry_after_ms: int | None = None,
    ) -> TransportDeliveryChunkClaim:
        key, chunk_index, worker_id, claim_epoch, expected_revision = (
            self._validated_transport_chunk_mutation(
                delivery_key=delivery_key,
                chunk_index=chunk_index,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
            )
        )
        if proof not in {"rpc_not_started", "provider_proved_before_send"}:
            raise ValueError("proof is invalid")
        if retry_after_ms is not None and (
            type(retry_after_ms) is not int
            or not 1 <= retry_after_ms <= 3_600_000
        ):
            raise ValueError("retry_after_ms must be between 1 and 3600000")
        now_value = self._utc_now()
        now = self._format_registry_time(now_value)
        retry_not_before = (
            self._format_registry_time(
                now_value + timedelta(milliseconds=retry_after_ms)
            )
            if retry_after_ms is not None
            else None
        )
        source = "claimed" if proof == "rpc_not_started" else "sending_unknown"
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            row = self._transport_delivery_chunk_row(conn, key, chunk_index)
            self._require_transport_chunk_fence(
                row,
                source=source,
                target="pending",
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
                now=now,
                require_live_lease=source == "claimed",
            )
            conn.execute(
                """UPDATE transport_delivery_chunks
                   SET state = 'pending', revision = revision + 1,
                       claim_owner = NULL, claim_expires_at = NULL,
                       retry_not_before = ?
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND chunk_index = ?""",
                (retry_not_before, *identity, chunk_index),
            )
            self._refresh_transport_delivery_parent(conn, key, now=now)
            return self._transport_delivery_chunk_claim(conn, key, chunk_index)

    def fail_transport_delivery_chunk(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        worker_id: str,
        claim_epoch: int,
        expected_revision: int,
        category: str,
    ) -> TransportDeliveryChunkClaim:
        key, chunk_index, worker_id, claim_epoch, expected_revision = (
            self._validated_transport_chunk_mutation(
                delivery_key=delivery_key,
                chunk_index=chunk_index,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
            )
        )
        category = self._required_text(category, "category", maximum=100)
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", category) is None:
            raise ValueError("category is invalid")
        now = self._registry_now()
        with self._transaction() as conn:
            row = self._transport_delivery_chunk_row(conn, key, chunk_index)
            self._require_transport_chunk_fence(
                row,
                source="sending_unknown",
                target="failed",
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                expected_revision=expected_revision,
            )
            self._fail_transport_delivery_chunk_row(
                conn,
                key,
                chunk_index,
                category=category,
                now=now,
            )
            return self._transport_delivery_chunk_claim(conn, key, chunk_index)

    def resolve_transport_delivery_chunk_sending_unknown(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        resolution: Literal["assume_delivered", "retry"],
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> TransportDeliveryChunkClaim:
        key = self._transport_delivery_key(delivery_key)
        chunk_index = self._transport_chunk_index(chunk_index)
        if resolution not in {"assume_delivered", "retry"}:
            raise ValueError("resolution is invalid")
        expected_revision = self._transport_revision(expected_revision)
        actor_id = self._required_text(actor_id, "actor_id", maximum=200)
        self._validate_key(idempotency_key)
        request_hash = self._request_hash(
            {
                "delivery_key": asdict(key),
                "chunk_index": chunk_index,
                "resolution": resolution,
                "expected_revision": expected_revision,
            }
        )
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            replay = conn.execute(
                """SELECT * FROM transport_delivery_chunk_resolutions
                   WHERE actor_id = ? AND idempotency_key = ?""",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise IdempotencyConflict()
                return self._transport_delivery_resolution_claim(conn, key, replay)
            row = self._transport_delivery_chunk_row(conn, key, chunk_index)
            if row["state"] != "sending_unknown":
                raise InvalidTransition(str(row["state"]), resolution)
            if int(row["revision"]) != expected_revision:
                raise InvalidTransition("stale_revision", resolution)
            now = self._registry_now()
            resolution_id = self._id_factory("resolution")
            result_state = "delivered" if resolution == "assume_delivered" else "pending"
            result_revision = expected_revision + 1
            try:
                conn.execute(
                    """INSERT INTO transport_delivery_chunk_resolutions(
                           id, transport, destination_digest, event_id,
                           projection_version, chunk_index, actor_id,
                           idempotency_key, request_hash, expected_revision,
                           resolution, prior_state, prior_revision,
                           prior_claim_epoch, result_state, result_revision,
                           created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'sending_unknown', ?, ?, ?, ?, ?)""",
                    (
                        resolution_id,
                        *identity,
                        chunk_index,
                        actor_id,
                        idempotency_key,
                        request_hash,
                        expected_revision,
                        resolution,
                        expected_revision,
                        int(row["claim_epoch"]),
                        result_state,
                        result_revision,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise InvalidTransition(
                    "resolution_conflict", "recorded"
                ) from None
            if resolution == "assume_delivered":
                conn.execute(
                    """UPDATE transport_delivery_chunks
                       SET state = 'delivered', revision = revision + 1,
                           claim_owner = NULL, manual_resolution_id = ?,
                           completed_at = ?
                       WHERE transport = ? AND destination_digest = ?
                         AND event_id = ? AND projection_version = ?
                         AND chunk_index = ?""",
                    (resolution_id, now, *identity, chunk_index),
                )
            else:
                conn.execute(
                    """UPDATE transport_delivery_chunks
                       SET state = 'pending', revision = revision + 1,
                           claim_owner = NULL, claim_expires_at = NULL
                       WHERE transport = ? AND destination_digest = ?
                         AND event_id = ? AND projection_version = ?
                         AND chunk_index = ?""",
                    (*identity, chunk_index),
                )
            self._refresh_transport_delivery_parent(conn, key, now=now)
            return self._transport_delivery_chunk_claim(conn, key, chunk_index)

    def complete_transport_delivery(
        self,
        *,
        key: TransportDeliveryKey,
        worker_id: str,
        claim_epoch: int,
    ) -> TransportDeliveryClaim:
        key = self._transport_delivery_key(key)
        worker_id = self._transport_worker_id(worker_id)
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        now = self._now()
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE transport_deliveries
                   SET state = 'delivered', claim_owner = NULL,
                       claim_expires_at = NULL, delivered_at = ?
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND state = 'claimed' AND claim_owner = ?
                     AND claim_epoch = ? AND claim_expires_at > ?""",
                (now, *identity, worker_id, claim_epoch, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("stale_claim", "delivered")
            return self._transport_delivery_claim(conn, key, status="delivered")

    def release_transport_delivery(
        self,
        *,
        key: TransportDeliveryKey,
        worker_id: str,
        claim_epoch: int,
    ) -> None:
        key = self._transport_delivery_key(key)
        worker_id = self._transport_worker_id(worker_id)
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        identity = self._transport_delivery_identity(key)
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE transport_deliveries
                   SET state = 'pending', claim_owner = NULL,
                       claim_expires_at = NULL
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                     AND state = 'claimed' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (*identity, worker_id, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("stale_claim", "pending")

    def put_transport_opaque_target(
        self,
        *,
        token_digest: str,
        target: TransportOpaqueTarget,
    ) -> None:
        token_digest = self._transport_digest(token_digest, "token_digest")
        target = self._transport_opaque_target(target)
        with self._transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO transport_opaque_targets(
                           token_digest, namespace, purpose, resource_kind,
                           resource_id, expected_revision, choice, scope_digest,
                           expires_at, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        token_digest,
                        target.namespace,
                        target.purpose,
                        target.resource_kind,
                        target.resource_id,
                        target.expected_revision,
                        target.choice,
                        target.scope_digest,
                        self._format_time(target.expires_at),
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidTransition("token_collision", "stored") from exc

    def claim_transport_opaque_target(
        self,
        *,
        token_digest: str,
        consumer: str | None,
        consume: bool,
    ) -> TransportOpaqueTarget | None:
        token_digest = self._transport_digest(token_digest, "token_digest")
        if type(consume) is not bool:
            raise ValueError("consume must be a boolean")
        if consumer is not None:
            self._validate_key(consumer)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM transport_opaque_targets WHERE token_digest = ?",
                (token_digest,),
            ).fetchone()
            if row is None:
                return None
            target = self._transport_opaque_target_row(row)
            if not consume:
                return target
            owner = consumer or "single-use"
            if consumer is not None and row["consumed_by"] == owner:
                return target
            if row["consumed_by"] is not None:
                return None
            cursor = conn.execute(
                """UPDATE transport_opaque_targets
                   SET consumed_by = ?, consumed_at = ?
                   WHERE token_digest = ? AND consumed_by IS NULL""",
                (owner, self._now(), token_digest),
            )
            return target if cursor.rowcount == 1 else None

    def pending_transport_deliveries(
        self, *, transport: str, limit: int = 100
    ) -> list[JsonObject]:
        """Frozen deliveries that are neither finished nor abandoned.

        The identity only -- `(transport, destination_digest, event_id,
        projection_version)` -- because that is what a drain needs to re-enter
        `deliver_event`, and because the chunk text is the message body and has
        no business leaving the ledger for a listing.

        `manual_required` is deliberately NOT included. A parent reaches it when
        a chunk is `sending_unknown`, which is precisely the state in which
        nobody can say whether the provider already acted; re-entering it would
        be the re-send P5-01 forbids.
        """

        transport = self._transport_name(transport)
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT transport, destination_digest, event_id,
                          projection_version, state, created_at
                   FROM transport_delivery_projections
                   WHERE transport = ? AND state = 'pending'
                   ORDER BY created_at, operation_id LIMIT ?""",
                (transport, limit),
            ).fetchall()
        return [
            {
                "transport": str(row["transport"]),
                "destination_digest": str(row["destination_digest"]),
                "event_id": str(row["event_id"]),
                "projection_version": int(row["projection_version"]),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    @classmethod
    def _transport_delivery_key(
        cls, value: Any
    ) -> TransportDeliveryKey:
        if not isinstance(value, TransportDeliveryKey):
            raise TypeError("key must be a TransportDeliveryKey")
        transport = cls._transport_name(value.transport)
        destination_digest = cls._binding_digest(
            value.destination_digest, "destination_digest"
        )
        event_id = cls._required_text(value.event_id, "event_id", maximum=500)
        if type(value.projection_version) is not int or value.projection_version < 1:
            raise ValueError("projection_version must be a positive integer")
        return TransportDeliveryKey(
            transport=transport,
            destination_digest=destination_digest,
            event_id=event_id,
            projection_version=value.projection_version,
        )

    @staticmethod
    def _transport_delivery_identity(
        key: TransportDeliveryKey,
    ) -> tuple[str, str, str, int]:
        return (
            key.transport,
            key.destination_digest,
            key.event_id,
            key.projection_version,
        )

    @classmethod
    def _transport_opaque_target(
        cls, value: Any
    ) -> TransportOpaqueTarget:
        if not isinstance(value, TransportOpaqueTarget):
            raise TypeError("target must be a TransportOpaqueTarget")
        if value.namespace not in {"action", "deep_link"}:
            raise ValueError("namespace is invalid")
        purpose = cls._required_text(value.purpose, "purpose", maximum=100)
        resource_kind = cls._required_text(
            value.resource_kind, "resource_kind", maximum=100
        )
        resource_id = cls._required_text(value.resource_id, "resource_id", maximum=500)
        if value.expected_revision is not None and (
            type(value.expected_revision) is not int or value.expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        choice = (
            cls._required_text(value.choice, "choice", maximum=200)
            if value.choice is not None
            else None
        )
        scope_digest = (
            cls._binding_digest(value.scope_digest, "scope_digest")
            if value.scope_digest is not None
            else None
        )
        if not isinstance(value.expires_at, datetime) or value.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return TransportOpaqueTarget(
            namespace=value.namespace,
            purpose=purpose,
            resource_kind=resource_kind,
            resource_id=resource_id,
            expected_revision=value.expected_revision,
            choice=choice,
            scope_digest=scope_digest,
            expires_at=value.expires_at.astimezone(UTC),
        )

    @staticmethod
    def _binding_digest(value: Any, name: str) -> str:
        if not isinstance(value, str) or _BINDING_DIGEST_RE.fullmatch(value) is None:
            raise ValueError(f"{name} must be a Control binding digest")
        return value

    @staticmethod
    def _transport_worker_id(value: Any) -> str:
        if not isinstance(value, str) or _TRANSPORT_WORKER_RE.fullmatch(value) is None:
            raise ValueError("worker_id is invalid")
        return value

    def _transport_delivery_row(
        self, conn: sqlite3.Connection, key: TransportDeliveryKey
    ) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM transport_deliveries
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?""",
            self._transport_delivery_identity(key),
        ).fetchone()

    def _transport_delivery_projection_row(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM transport_delivery_projections
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?""",
            self._transport_delivery_identity(key),
        ).fetchone()

    def _frozen_transport_delivery_projection(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
    ) -> FrozenTransportDeliveryProjection:
        row = self._transport_delivery_projection_row(conn, key)
        if row is None:
            raise NotFound("transport_delivery_projection", key.event_id)
        identity = self._transport_delivery_identity(key)
        chunk_rows = conn.execute(
            """SELECT * FROM transport_delivery_chunks
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?
               ORDER BY chunk_index""",
            identity,
        ).fetchall()
        chunks: list[TransportDeliveryChunkProjection] = []
        for chunk_row in chunk_rows:
            chunk_index = int(chunk_row["chunk_index"])
            buttons = tuple(
                TransportDeliveryButton(
                    label=str(button["label"]),
                    callback_data=str(button["callback_data"]),
                    token_digest=str(button["token_digest"]),
                )
                for button in conn.execute(
                    """SELECT label, callback_data, token_digest
                       FROM transport_delivery_chunk_buttons
                       WHERE transport = ? AND destination_digest = ?
                         AND event_id = ? AND projection_version = ?
                         AND chunk_index = ? ORDER BY position""",
                    (*identity, chunk_index),
                )
            )
            capabilities = tuple(
                TransportDeliveryCapability(
                    namespace=str(capability["namespace"]),  # type: ignore[arg-type]
                    token_digest=str(capability["token_digest"]),
                    expires_at=self._parse_control_time(
                        str(capability["expires_at"])
                    ),
                    start_offset=(
                        int(capability["start_offset"])
                        if capability["start_offset"] is not None
                        else None
                    ),
                    end_offset=(
                        int(capability["end_offset"])
                        if capability["end_offset"] is not None
                        else None
                    ),
                )
                for capability in conn.execute(
                    """SELECT namespace, token_digest, expires_at,
                              start_offset, end_offset
                       FROM transport_delivery_chunk_capabilities
                       WHERE transport = ? AND destination_digest = ?
                         AND event_id = ? AND projection_version = ?
                         AND chunk_index = ? ORDER BY position""",
                    (*identity, chunk_index),
                )
            )
            chunks.append(
                TransportDeliveryChunkProjection(
                    chunk_index=chunk_index,
                    operation_id=str(chunk_row["operation_id"]),
                    text=str(chunk_row["text"]),
                    parse_mode=str(chunk_row["parse_mode"]),  # type: ignore[arg-type]
                    buttons=buttons,
                    capabilities=capabilities,
                    chunk_hash=str(chunk_row["chunk_hash"]),
                )
            )
        if len(chunks) != int(row["chunk_count"]):
            raise RuntimeError("transport delivery projection is incomplete")
        return FrozenTransportDeliveryProjection(
            delivery_key=key,
            operation_id=str(row["operation_id"]),
            destination_binding_digest=str(row["destination_binding_digest"]),
            routing=str(row["routing"]),  # type: ignore[arg-type]
            capability_binding_digest=str(row["capability_binding_digest"]),
            rpc_timeout_seconds=int(row["rpc_timeout_seconds"]),
            projection_hash=str(row["projection_hash"]),
            chunks=tuple(chunks),
        )

    def _verified_frozen_transport_delivery_projection(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
    ) -> FrozenTransportDeliveryProjection:
        try:
            projection = self._frozen_transport_delivery_projection(conn, key)
            registrations = self._transport_delivery_opaque_target_registrations(
                conn,
                key,
            )
            return validate_transport_delivery_projection(
                projection,
                opaque_targets=registrations,
            )
        except (NotFound, RuntimeError, TypeError, ValueError):
            raise InvalidTransition("projection_integrity", "verified") from None

    def _transport_delivery_opaque_target_registrations(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
    ) -> tuple[TransportOpaqueTargetRegistration, ...]:
        registrations: list[TransportOpaqueTargetRegistration] = []
        for capability in conn.execute(
            """SELECT token_digest, expires_at
               FROM transport_delivery_chunk_capabilities
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?
               ORDER BY target_position""",
            self._transport_delivery_identity(key),
        ):
            target = conn.execute(
                "SELECT * FROM transport_opaque_targets WHERE token_digest = ?",
                (str(capability["token_digest"]),),
            ).fetchone()
            if target is None:
                raise RuntimeError("transport delivery projection is incomplete")
            registrations.append(
                TransportOpaqueTargetRegistration(
                    token_digest=str(capability["token_digest"]),
                    expires_at=self._parse_control_time(
                        str(capability["expires_at"])
                    ),
                    target=self._transport_opaque_target_row(target),
                )
            )
        return tuple(registrations)

    def _transport_delivery_chunk_row(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
        chunk_index: int,
    ) -> sqlite3.Row:
        row = conn.execute(
            """SELECT * FROM transport_delivery_chunks
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?
                 AND chunk_index = ?""",
            (*self._transport_delivery_identity(key), chunk_index),
        ).fetchone()
        if row is None:
            raise NotFound("transport_delivery_chunk", str(chunk_index))
        return row

    def _transport_delivery_chunk_claim(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
        chunk_index: int,
        *,
        status: str | None = None,
    ) -> TransportDeliveryChunkClaim:
        row = self._transport_delivery_chunk_row(conn, key, chunk_index)
        projection = self._frozen_transport_delivery_projection(conn, key)
        return TransportDeliveryChunkClaim(
            projection=projection,
            chunk=projection.chunks[chunk_index],
            status=(status or str(row["state"])),  # type: ignore[arg-type]
            revision=int(row["revision"]),
            claim_owner=(
                str(row["claim_owner"])
                if row["claim_owner"] is not None
                else None
            ),
            claim_epoch=int(row["claim_epoch"]),
            retry_not_before=(
                self._parse_control_time(str(row["retry_not_before"]))
                if row["retry_not_before"] is not None
                else None
            ),
        )

    def _transport_delivery_resolution_claim(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
        resolution: sqlite3.Row,
    ) -> TransportDeliveryChunkClaim:
        chunk_index = int(resolution["chunk_index"])
        projection = self._frozen_transport_delivery_projection(conn, key)
        return TransportDeliveryChunkClaim(
            projection=projection,
            chunk=projection.chunks[chunk_index],
            status=str(resolution["result_state"]),  # type: ignore[arg-type]
            revision=int(resolution["result_revision"]),
            claim_owner=None,
            claim_epoch=int(resolution["prior_claim_epoch"]),
            retry_not_before=None,
        )

    def _refresh_transport_delivery_parent(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
        *,
        now: str,
    ) -> None:
        states = [
            str(row["state"])
            for row in conn.execute(
                """SELECT state FROM transport_delivery_chunks
                   WHERE transport = ? AND destination_digest = ?
                     AND event_id = ? AND projection_version = ?
                   ORDER BY chunk_index""",
                self._transport_delivery_identity(key),
            )
        ]
        if not states:
            raise RuntimeError("transport delivery projection is incomplete")
        if all(state == "delivered" for state in states):
            state = "delivered"
        elif "sending_unknown" in states:
            state = "manual_required"
        elif "failed" in states:
            state = "failed"
        else:
            state = "pending"
        completed_at = now if state in {"delivered", "failed"} else None
        conn.execute(
            """UPDATE transport_delivery_projections
               SET state = ?, revision = revision + 1, completed_at = ?
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?""",
            (state, completed_at, *self._transport_delivery_identity(key)),
        )

    def _fail_transport_delivery_chunk_row(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
        chunk_index: int,
        *,
        category: str,
        now: str,
    ) -> None:
        conn.execute(
            """UPDATE transport_delivery_chunks
               SET state = 'failed', revision = revision + 1,
                   claim_owner = NULL, claim_expires_at = NULL,
                   retry_not_before = NULL, failure_category = ?,
                   completed_at = ?
               WHERE transport = ? AND destination_digest = ?
                 AND event_id = ? AND projection_version = ?
                 AND chunk_index = ?""",
            (
                category,
                now,
                *self._transport_delivery_identity(key),
                chunk_index,
            ),
        )
        self._refresh_transport_delivery_parent(conn, key, now=now)

    def _validated_transport_chunk_mutation(
        self,
        *,
        delivery_key: TransportDeliveryKey,
        chunk_index: int,
        worker_id: str,
        claim_epoch: int,
        expected_revision: int,
    ) -> tuple[TransportDeliveryKey, int, str, int, int]:
        return (
            self._transport_delivery_key(delivery_key),
            self._transport_chunk_index(chunk_index),
            self._transport_worker_id(worker_id),
            self._transport_claim_epoch(claim_epoch),
            self._transport_revision(expected_revision),
        )

    @staticmethod
    def _require_transport_chunk_fence(
        row: sqlite3.Row,
        *,
        source: str,
        target: str,
        worker_id: str,
        claim_epoch: int,
        expected_revision: int,
        now: str | None = None,
        require_live_lease: bool = False,
    ) -> None:
        valid = (
            str(row["state"]) == source
            and row["claim_owner"] == worker_id
            and int(row["claim_epoch"]) == claim_epoch
            and int(row["revision"]) == expected_revision
        )
        if require_live_lease:
            valid = (
                valid
                and now is not None
                and row["claim_expires_at"] is not None
                and str(row["claim_expires_at"]) > now
            )
        if not valid:
            raise InvalidTransition("stale_claim", target)

    @staticmethod
    def _transport_chunk_index(value: Any) -> int:
        if type(value) is not int or not 0 <= value < 100:
            raise ValueError("chunk_index must be between 0 and 99")
        return value

    @staticmethod
    def _transport_claim_epoch(value: Any) -> int:
        if type(value) is not int or value < 1:
            raise ValueError("claim_epoch must be a positive integer")
        return value

    @staticmethod
    def _transport_revision(value: Any) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        return value

    def _transport_delivery_claim(
        self,
        conn: sqlite3.Connection,
        key: TransportDeliveryKey,
        *,
        status: str,
    ) -> TransportDeliveryClaim:
        row = self._transport_delivery_row(conn, key)
        if row is None:  # pragma: no cover - guarded by the transaction
            raise RuntimeError("transport delivery was not persisted")
        return TransportDeliveryClaim(
            key=key,
            status=status,  # type: ignore[arg-type]
            claim_owner=str(row["claim_owner"]) if row["claim_owner"] else None,
            claim_epoch=int(row["claim_epoch"]),
            claim_expires_at=(
                self._parse_control_time(str(row["claim_expires_at"]))
                if row["claim_expires_at"]
                else None
            ),
        )

    def _transport_opaque_target_row(
        self, row: sqlite3.Row
    ) -> TransportOpaqueTarget:
        return TransportOpaqueTarget(
            namespace=str(row["namespace"]),  # type: ignore[arg-type]
            purpose=str(row["purpose"]),
            resource_kind=str(row["resource_kind"]),
            resource_id=str(row["resource_id"]),
            expected_revision=(
                int(row["expected_revision"])
                if row["expected_revision"] is not None
                else None
            ),
            choice=str(row["choice"]) if row["choice"] is not None else None,
            scope_digest=(
                str(row["scope_digest"]) if row["scope_digest"] is not None else None
            ),
            expires_at=self._parse_control_time(str(row["expires_at"])),
        )
