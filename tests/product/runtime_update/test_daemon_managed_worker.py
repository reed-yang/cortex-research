"""The daemon's binding, against a real service and a real worker process.

Everything below the binding is real: a release carrying the product's own
worker payload and the vendored cp311, imported/staged/activated through
`RuntimeUpdateService` under the REAL D6 gate, the attempt-free descriptor, the
seatbelt-wrapped `Popen` on the slot's own `bin/python3.11`, and a frame that
crosses its stdin. What that buys over the policy tests beside it is the two
answers a fake cannot give: that a closed gate leaves no process behind at all,
and that the release proof is derived from a pid that really existed and really
went away.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.paths import PathRegistry
from cortex_platform.product.runtime_update.approval import ControlReleaseApprovals
from cortex_platform.product.runtime_update.models import (
    canonical_json,
    digest_document,
)
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
)
from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    SlotInterpreterDescriptor,
)
from cortex_platform.product.transports.managed_worker import (
    REFUSED_GATE_CLOSED,
    UNBOUND_NOT_APPROVED,
    ManagedTransportWorker,
    ManagedWorkerUnavailable,
)
from cortex_platform.product.transports.worker_rpc import (
    TELEGRAM_CREDENTIAL_KEY,
    TELEGRAM_SECRET_ALIAS,
    TELEGRAM_TRANSPORT_PROTOCOL,
)

#: Obviously fake. No real credential exists anywhere in this repository.
FAKE_TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"
_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _paths(root: Path) -> PathRegistry:
    for name in ("config", "data", "state", "cache", "log"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    return PathRegistry(
        config_dir=root / "config",
        config_file=root / "config" / "config.toml",
        data_dir=root / "data",
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "log",
    )


def _store(paths: PathRegistry) -> ControlStore:
    store = ControlStore(paths.control_database_file, clock=lambda: _NOW)
    store.initialize()
    return store


def _release(
    paths: PathRegistry, store: ControlStore, release_factory, vendored_worker_runtime
):
    """Import a real release under the real approval gate, without activating."""

    archive, _pin = vendored_worker_runtime
    artifact, manifest, catalog, attestation = release_factory(
        runtime_archive=archive.read_bytes(), real_worker_payload=True
    )
    manifest["adapter_protocol"] = PROTOCOL_V2
    catalog["payload"]["entries"][0]["manifest_sha256"] = digest_document(manifest)
    service = RuntimeUpdateService(
        paths.runtime_update_root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        # The real gate. `import` and `stage` are ungated by design; `activate`
        # is not, which is why the approval below has to be recorded first.
        approvals=ControlReleaseApprovals(store),
    )
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])
    return service, manifest


def _approve(store: ControlStore, manifest) -> None:
    store.approve_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=digest_document(manifest),
        actor_id="operator",
        idempotency_key="p54-approve-0000000001",
    )


def _worker(paths: PathRegistry, store: ControlStore, service) -> ManagedTransportWorker:
    return ManagedTransportWorker(
        store=store,
        paths=paths,
        config={"secret_refs": {TELEGRAM_SECRET_ALIAS: "env://CORTEX_P54_FAKE_TOKEN"}},
        environ={"CORTEX_P54_FAKE_TOKEN": FAKE_TOKEN},
        service=service,
    )


def _open_window(store: ControlStore):
    return store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=1800,
        actor_id="operator",
        idempotency_key="p54-window-00000000001",
    )


def test_a_revoked_active_release_leaves_the_daemon_unbound(
    tmp_path: Path, release_factory, vendored_worker_runtime
) -> None:
    """The bytes were approved once, activated, then revoked. Nothing may launch."""

    paths = _paths(tmp_path)
    store = _store(paths)
    service, manifest = _release(paths, store, release_factory, vendored_worker_runtime)
    _approve(store, manifest)
    service.activate(manifest["release_id"], probe=lambda _candidate: True)
    store.revoke_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=digest_document(manifest),
        actor_id="operator",
        idempotency_key="p54-revoke-00000000001",
    )
    worker = _worker(paths, store, service)
    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_NOT_APPROVED
    assert health.release_id == manifest["release_id"]
    _open_window(store)
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.capabilities", {}, timeout=5.0)
    assert refusal.value.reason == UNBOUND_NOT_APPROVED


def test_the_daemon_binds_the_active_release_and_launches_nothing(
    tmp_path: Path, release_factory, vendored_worker_runtime
) -> None:
    paths = _paths(tmp_path)
    store = _store(paths)
    service, manifest = _release(paths, store, release_factory, vendored_worker_runtime)
    _approve(store, manifest)
    service.activate(manifest["release_id"], probe=lambda _candidate: True)
    worker = _worker(paths, store, service)
    health = worker.bind()
    assert health.bound is True
    assert health.reason is None
    assert health.release_id == manifest["release_id"]
    assert health.launched is False
    # The attempt-free path: binding writes no attempt row into the updater.
    assert not service.paths.attempts.exists()
    # The descriptor is daemon-owned state, and it is the document a supervisor
    # would actually load.
    document = paths.state_dir / "managed-worker" / "descriptor.json"
    assert document.is_file()
    descriptor = SlotInterpreterDescriptor.load(document)
    assert descriptor.release_id == manifest["release_id"]
    assert descriptor.interpreter_path.name == "python3.11"
    assert descriptor.interpreter_path.is_file()

    # Gate closed: refused before anything is launched, and no worker exists.
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.capabilities", {}, timeout=5.0)
    assert refusal.value.reason == REFUSED_GATE_CLOSED
    assert worker.health().launched is False
    assert not (descriptor.state_dir / "worker.stderr.log").exists()


def test_a_window_launches_a_real_sandboxed_worker_and_the_release_proves_it(
    tmp_path: Path, release_factory, vendored_worker_runtime
) -> None:
    """One window, end to end: a real process, a real frame, a real derivation."""

    paths = _paths(tmp_path)
    store = _store(paths)
    service, manifest = _release(paths, store, release_factory, vendored_worker_runtime)
    _approve(store, manifest)
    service.activate(manifest["release_id"], probe=lambda _candidate: True)
    worker = _worker(paths, store, service)
    assert worker.bind().bound is True
    _open_window(store)
    try:
        answer = worker.request("telegram.capabilities", {}, timeout=30.0)
        assert isinstance(answer, dict)
        assert answer["protocol"] == TELEGRAM_TRANSPORT_PROTOCOL
        backend = worker._backend
        assert backend is not None
        # The real seatbelt, not a bypass: the profile was generated, sealed and
        # probed before this process existed, and it names one egress port.
        launch = backend.sandbox_launch
        assert launch is not None
        assert launch.policy.egress_port == 443
        assert launch.profile_path.is_file()
        process = worker._process
        assert process is not None and process.poll() is None
        pid = process.pid
        os.kill(pid, 0)
        # The credential really is in the environment of the launched process
        # while the window is open, and in exactly one key.
        environment = backend._launch_environment()
        assert [
            key for key, value in environment.items() if value == FAKE_TOKEN
        ] == [TELEGRAM_CREDENTIAL_KEY]
    finally:
        proof = worker.release()

    assert proof.worker_launched is True
    assert proof.worker_pid == pid
    assert proof.exit_status is not None
    assert proof.established_sockets == ()
    assert proof.poller_stopped is True
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert f"pid {pid}" in proof.proof

    # ⟦AMD-4⟧ The next environment the same backend factory would build, with
    # the window closed, carries no token at all.
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="p54-disable-0000000001",
    )
    reopened = _worker(paths, store, service)
    assert reopened.bind().bound is True
    assert TELEGRAM_CREDENTIAL_KEY not in reopened._environment()
    assert json.loads(
        (paths.state_dir / "managed-worker" / "descriptor.json").read_text()
    )["release_id"] == manifest["release_id"]
