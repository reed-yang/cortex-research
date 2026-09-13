"""Replaceable ports for Telegram delivery, receipts, and opaque targets."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, Protocol

from ..control import (
    ControlStore,
    FrozenTransportDeliveryProjection,
    IdempotencyConflict,
    InvalidTransition,
    TransportCommandResponse,
    TransportDeliveryChunkClaim,
    TransportDeliveryChunkSendDecision,
    TransportDeliveryChunkSendPermit,
    TransportDeliveryFreezeResult,
    TransportDeliveryKey,
    TransportOpaqueTarget,
    TransportOpaqueTargetRegistration,
)
from .models import (
    AdapterResult,
    OutboundMessage,
    TelegramChunkSendOutcome,
    TelegramDestination,
    TelegramScope,
)


class TelegramClient(Protocol):
    def send(self, message: OutboundMessage) -> None: ...


class TelegramChunkClient(Protocol):
    def capability_binding_digest(self) -> str: ...

    def send_chunk(
        self,
        *,
        permit: TransportDeliveryChunkSendPermit,
        destination: TelegramDestination,
    ) -> TelegramChunkSendOutcome: ...


class TelegramClientFailure(Exception):
    """A classified client failure without a raw SDK exception payload."""

    retryable = False
    retry_after_ms: int | None = None


class TelegramRateLimited(TelegramClientFailure):
    retryable = True

    def __init__(self, retry_after_ms: int) -> None:
        super().__init__("rate_limited")
        self.retry_after_ms = max(1, retry_after_ms)


class TelegramTemporaryFailure(TelegramClientFailure):
    retryable = True

    def __init__(self) -> None:
        super().__init__("transport_unavailable")


class TelegramPermanentFailure(TelegramClientFailure):
    def __init__(self) -> None:
        super().__init__("delivery_rejected")


@dataclass(frozen=True)
class DeliveryKey:
    transport: str
    destination_digest: str
    event_id: str
    projection_version: int

    def canonical(self) -> str:
        return ":".join(
            (
                self.transport,
                self.destination_digest,
                self.event_id,
                str(self.projection_version),
            )
        )


@dataclass(frozen=True)
class OpaqueTarget:
    namespace: Literal["action", "deep_link"]
    purpose: str
    resource_kind: str
    resource_id: str
    expected_revision: int | None
    choice: str | None
    scope_digest: str | None
    expires_at: datetime


@dataclass
class StoredOpaqueTarget:
    target: OpaqueTarget
    consumed_by: str | None = None


class OpaqueTargetPort(Protocol):
    def put(self, token_digest: str, target: OpaqueTarget) -> None: ...

    def claim(
        self,
        token_digest: str,
        *,
        consumer: str | None,
        consume: bool,
    ) -> OpaqueTarget | None: ...


class TransportReceiptPort(Protocol):
    def command_result(self, key: str, request_digest: str) -> AdapterResult | None: ...

    def record_command(
        self, key: str, request_digest: str, result: AdapterResult
    ) -> None: ...

    def reserve_delivery(
        self, key: DeliveryKey
    ) -> Literal["reserved", "delivered", "in_flight"]: ...

    def complete_delivery(self, key: DeliveryKey) -> None: ...

    def release_delivery(self, key: DeliveryKey) -> None: ...


class InMemoryOpaqueTargetPort:
    """Thread-safe synthetic token state; production must inject durable state."""

    def __init__(self) -> None:
        self._items: dict[str, StoredOpaqueTarget] = {}
        self._lock = threading.Lock()

    def put(self, token_digest: str, target: OpaqueTarget) -> None:
        with self._lock:
            if token_digest in self._items:
                raise RuntimeError("opaque token collision")
            self._items[token_digest] = StoredOpaqueTarget(target=target)

    def claim(
        self,
        token_digest: str,
        *,
        consumer: str | None,
        consume: bool,
    ) -> OpaqueTarget | None:
        with self._lock:
            stored = self._items.get(token_digest)
            if stored is None:
                return None
            if not consume:
                return stored.target
            if stored.consumed_by is None:
                stored.consumed_by = consumer or "single-use"
                return stored.target
            if consumer is not None and stored.consumed_by == consumer:
                return stored.target
            return None


class InMemoryTransportReceiptPort:
    """Thread-safe synthetic receipts reusable across adapter reconstruction."""

    def __init__(self) -> None:
        self._commands: dict[str, tuple[str, AdapterResult]] = {}
        self._deliveries: dict[DeliveryKey, Literal["reserved", "delivered"]] = {}
        self._lock = threading.Lock()

    def command_result(self, key: str, request_digest: str) -> AdapterResult | None:
        with self._lock:
            stored = self._commands.get(key)
            if stored is None:
                return None
            if stored[0] != request_digest:
                raise ValueError("idempotency_conflict")
            value = stored[1]
            return AdapterResult(**{**value.__dict__, "replayed": True})

    def record_command(
        self, key: str, request_digest: str, result: AdapterResult
    ) -> None:
        with self._lock:
            stored = self._commands.get(key)
            if stored is not None and stored[0] != request_digest:
                raise ValueError("idempotency_conflict")
            self._commands.setdefault(key, (request_digest, result))

    def reserve_delivery(
        self, key: DeliveryKey
    ) -> Literal["reserved", "delivered", "in_flight"]:
        with self._lock:
            state = self._deliveries.get(key)
            if state == "delivered":
                return "delivered"
            if state == "reserved":
                return "in_flight"
            self._deliveries[key] = "reserved"
            return "reserved"

    def complete_delivery(self, key: DeliveryKey) -> None:
        with self._lock:
            if self._deliveries.get(key) != "reserved":
                raise RuntimeError("delivery was not reserved")
            self._deliveries[key] = "delivered"

    def release_delivery(self, key: DeliveryKey) -> None:
        with self._lock:
            if self._deliveries.get(key) == "reserved":
                self._deliveries.pop(key, None)


class ControlTransportStatePort:
    """Adapter port backed exclusively by the Control-owned transport tables."""

    def __init__(
        self,
        *,
        store: ControlStore,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> None:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id.strip()) <= 200:
            raise ValueError("worker_id is invalid")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self._store = store
        self._worker_id = worker_id.strip()
        self._lease_seconds = lease_seconds
        self._claims: dict[DeliveryKey, int] = {}
        self._lock = threading.Lock()

    def command_result(self, key: str, request_digest: str) -> AdapterResult | None:
        try:
            receipt = self._store.get_transport_command(
                transport="telegram",
                command_key=key,
                request_hash=request_digest,
            )
        except IdempotencyConflict as exc:
            raise ValueError("idempotency_conflict") from exc
        if receipt is None:
            return None
        return AdapterResult(**{**asdict(receipt.response), "replayed": True})

    def record_command(
        self, key: str, request_digest: str, result: AdapterResult
    ) -> None:
        try:
            self._store.record_transport_command(
                transport="telegram",
                command_key=key,
                request_hash=request_digest,
                response=TransportCommandResponse(
                    ok=result.ok,
                    category=result.category,
                    action=result.action,
                    response_text=result.response_text,
                    mutated=result.mutated,
                    replayed=result.replayed,
                    retryable=result.retryable,
                    retry_after_ms=result.retry_after_ms,
                    state=result.state,
                    revision=result.revision,
                ),
            )
        except IdempotencyConflict as exc:
            raise ValueError("idempotency_conflict") from exc

    def reserve_delivery(
        self, key: DeliveryKey
    ) -> Literal["reserved", "delivered", "in_flight"]:
        with self._lock:
            if key in self._claims:
                return "in_flight"
            claim = self._store.claim_transport_delivery(
                key=self._control_delivery_key(key),
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
            if claim.status == "delivered":
                return "delivered"
            if claim.status == "in_flight":
                return "in_flight"
            self._claims[key] = claim.claim_epoch
        return "reserved"

    def complete_delivery(self, key: DeliveryKey) -> None:
        with self._lock:
            claim_epoch = self._claims.get(key)
        if claim_epoch is None:
            raise RuntimeError("delivery was not reserved by this worker")
        self._store.complete_transport_delivery(
            key=self._control_delivery_key(key),
            worker_id=self._worker_id,
            claim_epoch=claim_epoch,
        )
        with self._lock:
            self._claims.pop(key, None)

    def release_delivery(self, key: DeliveryKey) -> None:
        with self._lock:
            claim_epoch = self._claims.pop(key, None)
        if claim_epoch is None:
            return
        try:
            self._store.release_transport_delivery(
                key=self._control_delivery_key(key),
                worker_id=self._worker_id,
                claim_epoch=claim_epoch,
            )
        except InvalidTransition:
            return

    def put(self, token_digest: str, target: OpaqueTarget) -> None:
        self._store.put_transport_opaque_target(
            token_digest=token_digest,
            target=TransportOpaqueTarget(
                namespace=target.namespace,
                purpose=target.purpose,
                resource_kind=target.resource_kind,
                resource_id=target.resource_id,
                expected_revision=target.expected_revision,
                choice=target.choice,
                scope_digest=target.scope_digest,
                expires_at=target.expires_at,
            ),
        )

    def claim(
        self,
        token_digest: str,
        *,
        consumer: str | None,
        consume: bool,
    ) -> OpaqueTarget | None:
        target = self._store.claim_transport_opaque_target(
            token_digest=token_digest,
            consumer=consumer,
            consume=consume,
        )
        if target is None:
            return None
        return OpaqueTarget(
            namespace=target.namespace,
            purpose=target.purpose,
            resource_kind=target.resource_kind,
            resource_id=target.resource_id,
            expected_revision=target.expected_revision,
            choice=target.choice,
            scope_digest=target.scope_digest,
            expires_at=target.expires_at,
        )

    @staticmethod
    def _control_delivery_key(key: DeliveryKey) -> TransportDeliveryKey:
        return TransportDeliveryKey(
            transport=key.transport,
            destination_digest=key.destination_digest,
            event_id=key.event_id,
            projection_version=key.projection_version,
        )


class ControlTransportChunkPort:
    """Control-backed coordination for frozen per-chunk Telegram delivery."""

    def __init__(
        self,
        *,
        store: ControlStore,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> None:
        if type(store) is not ControlStore:
            raise TypeError("ControlStore is required")
        if not isinstance(worker_id, str) or not 1 <= len(worker_id.strip()) <= 200:
            raise ValueError("worker_id is invalid")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self._store = store
        self._worker_id = worker_id.strip()
        self._lease_seconds = lease_seconds
        self._permit_lock = threading.Lock()
        self._send_permits: dict[int, TransportDeliveryChunkSendPermit] = {}

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def freeze(
        self,
        *,
        projection: FrozenTransportDeliveryProjection,
        opaque_targets: tuple[TransportOpaqueTargetRegistration, ...],
        request_hash: str,
    ) -> TransportDeliveryFreezeResult:
        return self._store.freeze_transport_delivery_projection(
            projection=projection,
            opaque_targets=opaque_targets,
            request_hash=request_hash,
        )

    def replay(
        self,
        *,
        projection: FrozenTransportDeliveryProjection,
        request_hash: str,
        opaque_targets: tuple[TransportOpaqueTargetRegistration, ...] | None = None,
    ) -> TransportDeliveryFreezeResult:
        registrations = (
            self.registered_targets(projection)
            if opaque_targets is None
            else opaque_targets
        )
        return self.freeze(
            projection=projection,
            opaque_targets=registrations,
            request_hash=request_hash,
        )

    def registered_targets(
        self,
        projection: FrozenTransportDeliveryProjection,
    ) -> tuple[TransportOpaqueTargetRegistration, ...]:
        registrations: list[TransportOpaqueTargetRegistration] = []
        for chunk in projection.chunks:
            for capability in chunk.capabilities:
                target = self._store.claim_transport_opaque_target(
                    token_digest=capability.token_digest,
                    consumer=None,
                    consume=False,
                )
                if target is None:
                    raise InvalidTransition("projection_integrity", "replayed")
                registrations.append(
                    TransportOpaqueTargetRegistration(
                        token_digest=capability.token_digest,
                        expires_at=capability.expires_at,
                        target=target,
                    )
                )
        return tuple(registrations)

    def claim(
        self, delivery_key: TransportDeliveryKey, chunk_index: int
    ) -> TransportDeliveryChunkClaim:
        return self._store.claim_transport_delivery_chunk(
            delivery_key=delivery_key,
            chunk_index=chunk_index,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )

    def begin(
        self,
        claim: TransportDeliveryChunkClaim,
        *,
        observed_capability_binding_digest: str,
    ) -> TransportDeliveryChunkSendDecision:
        decision = self._store.begin_transport_delivery_chunk_send(
            delivery_key=claim.projection.delivery_key,
            chunk_index=claim.chunk.chunk_index,
            worker_id=self._worker_id,
            claim_epoch=claim.claim_epoch,
            expected_revision=claim.revision,
            observed_capability_binding_digest=observed_capability_binding_digest,
        )
        if decision.permit is not None:
            with self._permit_lock:
                self._send_permits[id(decision.permit)] = decision.permit
        return decision

    def consume_send_permit(self, permit: TransportDeliveryChunkSendPermit) -> bool:
        """Consume the exact in-process permit returned by a committed begin."""

        if type(permit) is not TransportDeliveryChunkSendPermit:
            return False
        with self._permit_lock:
            issued = self._send_permits.pop(id(permit), None)
        return issued is permit

    def complete(
        self,
        permit: TransportDeliveryChunkSendPermit,
        *,
        provider_receipt_digest: str,
    ) -> TransportDeliveryChunkClaim:
        return self._store.complete_transport_delivery_chunk(
            delivery_key=permit.projection.delivery_key,
            chunk_index=permit.chunk.chunk_index,
            worker_id=self._worker_id,
            claim_epoch=permit.claim_epoch,
            expected_revision=permit.revision,
            provider_receipt_digest=provider_receipt_digest,
            # ⟦AMD-5⟧: the window that AUTHORIZED the send, carried on the
            # permit from the transaction that issued it. Re-reading the gate
            # here made a disable between permit and receipt lose the id, and a
            # NEW window between them steal it -- and `transport_window_id` is
            # write-once, so the wrong answer would have been permanent.
            transport_window_id=permit.transport_window_id,
        )

    def release(
        self,
        *,
        claim: TransportDeliveryChunkClaim | None = None,
        permit: TransportDeliveryChunkSendPermit | None = None,
        proof: Literal["rpc_not_started", "provider_proved_before_send"],
        retry_after_ms: int | None = None,
    ) -> TransportDeliveryChunkClaim:
        value = permit or claim
        if value is None or (permit is None) == (claim is None):
            raise ValueError("exactly one claim or permit is required")
        projection = value.projection
        chunk = value.chunk
        return self._store.release_transport_delivery_chunk(
            delivery_key=projection.delivery_key,
            chunk_index=chunk.chunk_index,
            worker_id=self._worker_id,
            claim_epoch=value.claim_epoch,
            expected_revision=value.revision,
            proof=proof,
            retry_after_ms=retry_after_ms,
        )

    def fail(
        self,
        permit: TransportDeliveryChunkSendPermit,
        *,
        category: str,
    ) -> TransportDeliveryChunkClaim:
        return self._store.fail_transport_delivery_chunk(
            delivery_key=permit.projection.delivery_key,
            chunk_index=permit.chunk.chunk_index,
            worker_id=self._worker_id,
            claim_epoch=permit.claim_epoch,
            expected_revision=permit.revision,
            category=category,
        )

    def matches_destination(
        self,
        *,
        projection: FrozenTransportDeliveryProjection,
        destination: TelegramDestination,
        bot_identity: str,
    ) -> bool:
        scope = TelegramScope(
            bot_identity=bot_identity,
            chat_id=destination.chat_id,
            topic_id=destination.topic_id,
        )
        binding = self._store.resolve_transport(
            transport="telegram",
            external_scope=scope.canonical(),
        )
        expected_routing = "topic" if destination.topic_id is not None else "root"
        return bool(
            binding is not None
            and binding["external_scope"] == projection.destination_binding_digest
            and projection.routing == expected_routing
        )


class SyntheticTelegramClient:
    """A network-free client with deterministic idempotent sends and failures."""

    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []
        self._sent_keys: set[str] = set()
        self._failures: list[TelegramClientFailure] = []
        self._lock = threading.Lock()

    def queue_failure(self, failure: TelegramClientFailure) -> None:
        with self._lock:
            self._failures.append(failure)

    def send(self, message: OutboundMessage) -> None:
        with self._lock:
            if message.idempotency_key in self._sent_keys:
                return
            if self._failures:
                raise self._failures.pop(0)
            self.messages.append(message)
            self._sent_keys.add(message.idempotency_key)
