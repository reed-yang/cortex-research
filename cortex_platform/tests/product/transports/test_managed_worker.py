"""P5.4: the daemon's binding to the managed worker, and the window that owns it.

The worker process itself is real in `tests/product/runtime_update/
test_daemon_managed_worker.py`; what is exercised here is the policy around it,
which is where the refusals live: the D6 approval on the launch path, the gate
before any launch at all, the credential the window scopes, and the derivation
`poller_stopped` is forbidden to be a literal.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import InvalidTransition, NotFound
from cortex_platform.product.paths import PathRegistry
from cortex_platform.product.transports.managed_worker import (
    REFUSED_GATE_CLOSED,
    REFUSED_NO_CREDENTIAL,
    REFUSED_NOT_APPROVED,
    REFUSED_PROVIDER_CREDENTIAL,
    UNBOUND_EGRESS_CONFLICT,
    UNBOUND_EGRESS_INVALID,
    UNBOUND_LEDGER_HELD,
    UNBOUND_NO_ACTIVE_RELEASE,
    UNBOUND_NOT_APPROVED,
    UNBOUND_PROVIDER_CREDENTIAL,
    UNBOUND_PROVIDER_ENDPOINT,
    EgressEndpointInvalid,
    ManagedTransportWorker,
    ManagedWorkerUnavailable,
    TransportWindowSupervisor,
    _egress_port,
    established_sockets,
)
from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    SlotInterpreterDescriptor,
)
from cortex_platform.product.transports.worker_rpc import (
    TELEGRAM_CREDENTIAL_KEY,
    TELEGRAM_SECRET_ALIAS,
)

#: Obviously fake. No real credential exists anywhere in this repository.
FAKE_TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"
_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
RELEASE = "hermes-0.15.0-test"
MANIFEST = "a" * 64
SLOT = "b" * 64


@dataclass
class _FakeProcess:
    pid: int
    exit_status: int | None = None

    def poll(self) -> int | None:
        return self.exit_status


class _FakeSupervisor:
    def __init__(self, pid: int) -> None:
        self.process = _FakeProcess(pid)
        self.calls: list[tuple[str, dict, float]] = []

    def request(self, method: str, params, *, timeout: float) -> object:
        self.calls.append((method, dict(params), timeout))
        return {"status": "ok", "updates": []}


class _FakeBackend:
    """Stands in for `ManagedHermesBackend`, with its two public verbs."""

    instances: list["_FakeBackend"] = []

    def __init__(
        self,
        descriptor_path,
        *,
        environment_factory,
        egress_port,
        agent_options_factory=None,
    ) -> None:
        self.descriptor_path = descriptor_path
        self.environment_factory = environment_factory
        self.egress_port = egress_port
        self.agent_options_factory = agent_options_factory
        self.environments: list[dict[str, str]] = []
        self.supervisor: _FakeSupervisor | None = None
        self.closed = 0
        _FakeBackend.instances.append(self)

    def worker(self) -> _FakeSupervisor:
        if self.supervisor is None:
            self.environments.append(dict(self.environment_factory()))
            self.supervisor = _FakeSupervisor(pid=424_242 + len(self.environments))
        return self.supervisor

    def close(self) -> None:
        self.closed += 1
        if self.supervisor is not None:
            self.supervisor.process.exit_status = -15
        self.supervisor = None


def _paths(root: Path, *, updater: bool = True) -> PathRegistry:
    for name in ("config", "data", "state", "cache", "log"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    if updater:
        # An installation that has imported at least one release. The absence
        # of this root is its own answer, and `bind` must not create it.
        (root / "state" / "runtime-update").mkdir(exist_ok=True, mode=0o700)
    return PathRegistry(
        config_dir=root / "config",
        config_file=root / "config" / "config.toml",
        data_dir=root / "data",
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "log",
    )


class _FakeService:
    """`RuntimeUpdateService.status()`, in the shape the binding reads."""

    def __init__(self, *, active: bool = True) -> None:
        self._active = active

    def status(self) -> dict:
        if not self._active:
            return {"active": None, "releases": {}}
        return {
            "active": {
                "release_id": RELEASE,
                "slot_digest": SLOT,
                "generation_id": "generation-1",
            },
            "releases": {
                RELEASE: {
                    "status": "active",
                    "manifest_digest": MANIFEST,
                    "slot_digest": SLOT,
                }
            },
        }


@pytest.fixture(autouse=True)
def _reset_backends() -> None:
    _FakeBackend.instances = []


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    value = ControlStore(tmp_path / "data" / "control.db", clock=lambda: _NOW)
    value.initialize()
    return value


def _approve(store: ControlStore) -> None:
    store.approve_runtime_release(
        release_id=RELEASE,
        manifest_sha256=MANIFEST,
        actor_id="operator",
        idempotency_key="approve-000000000000001",
    )


def _open_window(store: ControlStore, key: str = "window-000000000000001"):
    return store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=1800,
        actor_id="operator",
        idempotency_key=key,
    )


def _descriptor(root: Path) -> SlotInterpreterDescriptor:
    """A descriptor no worker is launched from, so nothing here has to exist."""

    return SlotInterpreterDescriptor(
        schema_version=1,
        slot_path=root / "slots" / SLOT,
        slot_id=SLOT,
        state_generation_id="generation-1",
        release_id=RELEASE,
        expected_artifact_digest=SLOT,
        expected_manifest_sha256=MANIFEST,
        expected_content_tree_sha256="c" * 64,
        expected_interpreter_sha256="d" * 64,
        interpreter_path=root / "interpreters" / "bin" / "python3.11",
        worker_entrypoint="runtime_worker.py",
        state_dir=root / "worker-state",
        worker_protocol=PROTOCOL_V2,
    )


def _worker(
    tmp_path: Path,
    store: ControlStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: bool = True,
    references: dict | None = None,
    environ: dict | None = None,
    runtime: dict | None = None,
) -> ManagedTransportWorker:
    # The attempt-free derivation itself is exercised against a real
    # `RuntimeUpdateService`, a real slot and a real worker process in
    # `tests/product/runtime_update/test_daemon_managed_worker.py`. Here it is
    # replaced so these tests are about the policy around the launch.
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.build_active_descriptor",
        lambda service: _descriptor(tmp_path),
    )
    return ManagedTransportWorker(
        store=store,
        paths=_paths(tmp_path),
        config={
            "secret_refs": (
                {TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}"}
                if references is None
                else references
            ),
            **({} if runtime is None else {"runtime": runtime}),
        },
        environ={TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN} if environ is None else environ,
        backend_factory=_FakeBackend,
        service=_FakeService(active=active),
    )


def test_a_product_that_never_imported_a_release_creates_no_updater_root(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status()` takes the updater's lock, which creates its root. Ask first."""

    paths = _paths(tmp_path, updater=False)
    worker = ManagedTransportWorker(
        store=store,
        paths=paths,
        config={},
        environ={},
        backend_factory=_FakeBackend,
        service=_FakeService(),
    )
    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_NO_ACTIVE_RELEASE
    assert not paths.runtime_update_root.exists()


def test_an_installation_with_no_active_release_stays_unbound(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(tmp_path, store, monkeypatch, active=False)
    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_NO_ACTIVE_RELEASE
    assert health.release_id is None
    assert worker.health().to_dict()["state"] == "unbound"


def test_an_unapproved_active_release_is_bound_to_nothing(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦S3.4/D6⟧ on the launch path: no approval, no worker, and health says so."""

    worker = _worker(tmp_path, store, monkeypatch)
    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_NOT_APPROVED
    # The identity is still reported: an operator has to be able to read the
    # digest they are being asked to approve.
    assert health.release_id == RELEASE
    assert health.slot_digest == SLOT
    _open_window(store)
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.poll", {}, timeout=1.0)
    assert refusal.value.reason == UNBOUND_NOT_APPROVED
    assert _FakeBackend.instances == []


def test_a_closed_gate_refuses_before_any_process_is_launched(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    assert worker.bind().bound is True
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.poll", {}, timeout=1.0)
    assert refusal.value.reason == REFUSED_GATE_CLOSED
    assert _FakeBackend.instances == []
    assert worker.health().launched is False


def test_the_first_frame_of_a_window_launches_the_worker_with_the_token(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    assert worker.request("telegram.poll", {"offset": None}, timeout=5.0) == {
        "status": "ok",
        "updates": [],
    }
    backend = _FakeBackend.instances[0]
    assert backend.egress_port == 443
    assert len(backend.environments) == 1
    assert backend.environments[0][TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN
    assert worker.health().launched is True
    # A second frame reuses the process rather than launching another.
    worker.request("telegram.poll", {"offset": 1}, timeout=5.0)
    assert len(_FakeBackend.instances) == 1
    assert len(backend.environments) == 1


def test_a_relaunch_after_the_window_carries_no_token(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦AMD-4⟧ The credential is scoped to the window, not to the process."""

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    worker.release()
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000001",
    )
    _open_window(store, key="window-000000000000002")
    worker.request("telegram.poll", {}, timeout=5.0)
    second = _FakeBackend.instances[1]
    assert second.environments[0][TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN
    # And the environment the factory would build with the gate shut has no key
    # at all -- the same callable, a different answer.
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000002",
    )
    assert TELEGRAM_CREDENTIAL_KEY not in second.environment_factory()


def _provider_worker(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> ManagedTransportWorker:
    """A worker whose turns have a provider, so a launch has something to bind."""

    return _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}",
            "openai": "env://CORTEX_TEST_PROVIDER_KEY",
        },
        environ={
            TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN,
            "CORTEX_TEST_PROVIDER_KEY": "sk-obviously-fake",
            "OPENAI_BASE_URL": "https://provider.invalid/v1",
        },
    )


def test_a_turn_launches_the_worker_outside_a_window_without_the_token(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P8⟧ A cockpit turn needs the provider key and no window.

    The window is ⟦AMD-4⟧'s decision about who may speak as the bot; a run
    created through the control API answers into its thread and speaks as
    nobody. The environment the launch binds is the one the shut gate builds:
    provider key present, bot token absent -- and a transport frame on the
    same worker is still refused, because the window is what a frame needs.
    """

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    assert worker.bind().bound is True

    backend = worker.backend(window_required=False)

    assert backend is _FakeBackend.instances[0]
    assert len(backend.environments) == 1
    assert backend.environments[0]["OPENAI_API_KEY"] == "sk-obviously-fake"
    assert TELEGRAM_CREDENTIAL_KEY not in backend.environments[0]
    assert worker.health().launched is True
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.poll", {}, timeout=1.0)
    assert refusal.value.reason == REFUSED_GATE_CLOSED
    # Refused, not relaunched: the turn's worker is still the only one.
    assert len(_FakeBackend.instances) == 1


def test_the_recovery_form_of_backend_still_requires_the_window(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The c4 decision stands for recovery: no launch outside a window."""

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.backend()
    assert refusal.value.reason == REFUSED_GATE_CLOSED
    assert _FakeBackend.instances == []


def test_a_turn_outside_a_window_still_needs_the_approval(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _provider_worker(tmp_path, store, monkeypatch)
    assert worker.bind().bound is False
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.backend(window_required=False)
    assert refusal.value.reason == REFUSED_NOT_APPROVED
    assert _FakeBackend.instances == []


def test_the_first_frame_of_a_window_relaunches_a_worker_a_turn_launched(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window must not inherit a process that holds no bot token.

    `TransportWindowSupervisor.reconcile` starts the poller on the tick that
    sees the window, and the poller's first frame is the acquisition that
    finds the turn's worker. It is closed and launched again from an
    environment that binds the token; a turn arriving afterwards reuses the
    window's worker, which has the provider key too.
    """

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    first = worker.backend(window_required=False)
    assert TELEGRAM_CREDENTIAL_KEY not in first.environments[0]

    _open_window(store)
    worker.begin_window()
    worker.request("telegram.poll", {"offset": None}, timeout=5.0)

    assert first.closed == 1
    assert len(_FakeBackend.instances) == 2
    second = _FakeBackend.instances[1]
    assert second.environments[0][TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN
    assert second.environments[0]["OPENAI_API_KEY"] == "sk-obviously-fake"
    # The turn's form now reuses the window's worker rather than launching.
    assert worker.backend(window_required=False) is second
    assert len(_FakeBackend.instances) == 2
    # The pre-window close is not this window's release proof: the release
    # derives from the window's worker, which is the one it closes.
    proof = worker.release()
    assert proof.worker_launched is True
    assert second.closed == 1
    assert worker.health().launched is False


def test_a_window_opening_closes_a_worker_a_turn_launched_and_says_so(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """⟦Batch G P8-5/P8-03⟧ `begin_window` handles the pre-window backend itself.

    The window must not start with a token-less worker attached, and a turn
    running on it ends typed -- which is explainable only if the close is in
    `cortexd.log`.
    """

    import logging

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    first = worker.backend(window_required=False)
    _open_window(store)

    with caplog.at_level(logging.WARNING):
        worker.begin_window()

    assert first.closed == 1
    assert worker.health().launched is False
    assert any("closed at window open" in record.getMessage() for record in caplog.records)
    worker.request("telegram.poll", {"offset": None}, timeout=5.0)
    second = _FakeBackend.instances[1]
    assert second.environments[0][TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN


def test_the_window_release_leaves_a_turn_worker_and_keeps_the_window_proof(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦Batch G P8-03 / G-R2⟧ The proof is about the token holder; the close is not.

    A window whose worker was released, then a cockpit turn launching a
    worker of its own, then the operator's `close-window`: the derivation
    recorded is the window's worker's -- and the turn's worker is closed all
    the same, because `close-window` must leave no `runtime_worker.py`
    behind (cutover step 5), whoever launched it.
    """

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.begin_window()
    worker.request("telegram.poll", {"offset": None}, timeout=5.0)
    windowed = _FakeBackend.instances[0]
    window_proof = worker.release()
    assert windowed.closed == 1
    assert window_proof.worker_launched is True
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000001",
    )
    turn = worker.backend(window_required=False)
    assert turn is not windowed

    recorded = worker.release()

    assert recorded == window_proof
    assert turn.closed == 1
    assert worker.health().launched is False


def test_a_window_with_only_a_turn_worker_proves_nothing_held_the_token(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    turn = worker.backend(window_required=False)

    proof = worker.release()

    assert proof.worker_launched is False
    assert proof.poller_stopped is True
    assert "nothing held the token" in proof.proof
    # ⟦G-R2⟧ The proof describes the window; the turn's worker is gone anyway.
    assert turn.closed == 1
    assert worker.health().launched is False


def test_the_window_release_observes_the_turn_worker_it_closes(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """⟦V-4⟧ The proof stays the window's; the close is observed all the same."""

    import logging

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    turn = worker.backend(window_required=False)
    pid = turn.worker().process.pid
    seen: dict[str, list[str]] = {}

    def _lsof(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.subprocess.run", _lsof
    )
    with caplog.at_level(logging.WARNING):
        proof = worker.release()

    assert proof.worker_launched is False
    assert "nothing held the token" in proof.proof
    assert turn.closed == 1
    assert worker.health().launched is False
    # Derived from the process this code just ended, not assumed.
    assert seen["argv"][seen["argv"].index("-p") + 1] == str(pid)
    assert any(
        f"pid={pid}" in record.getMessage() and "exit_status=-15" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_a_raising_close_at_window_release_keeps_the_turn_worker_owned(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦V-4⟧ The fields are cleared after `close()` returned, never before.

    `runtime_update.supervisor` ends `kill()` with an unguarded `wait`, so a
    close can raise `TimeoutExpired`. Clearing first left an orphan
    `health()` reported as absent; now the worker stays owned and the next
    release reaches it.
    """

    import subprocess

    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    turn = worker.backend(window_required=False)
    turn.worker()
    real_close = turn.close

    def raising_close() -> None:
        raise subprocess.TimeoutExpired(cmd="runtime_worker.py", timeout=2)

    turn.close = raising_close  # type: ignore[method-assign]
    with pytest.raises(subprocess.TimeoutExpired):
        worker.release()

    assert worker.health().launched is True
    assert turn.closed == 0

    turn.close = real_close  # type: ignore[method-assign]
    worker.release()

    assert turn.closed == 1
    assert worker.health().launched is False


def test_a_turn_inside_a_window_reuses_the_windowed_worker(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _provider_worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {"offset": None}, timeout=5.0)
    windowed = _FakeBackend.instances[0]

    assert worker.backend(window_required=False) is windowed
    assert len(_FakeBackend.instances) == 1
    assert len(windowed.environments) == 1
    assert windowed.closed == 0


def test_an_unusable_credential_reference_refuses_rather_than_launches(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch, references={})
    worker.bind()
    _open_window(store)
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.send", {}, timeout=5.0)
    assert refusal.value.reason == REFUSED_NO_CREDENTIAL


def test_a_revoked_release_refuses_the_next_frame_and_closes_the_worker(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    backend = _FakeBackend.instances[0]
    assert backend.closed == 0
    store.revoke_runtime_release(
        release_id=RELEASE,
        manifest_sha256=MANIFEST,
        actor_id="operator",
        idempotency_key="revoke-0000000000000001",
    )
    with pytest.raises(ManagedWorkerUnavailable) as refusal:
        worker.request("telegram.poll", {}, timeout=5.0)
    assert refusal.value.reason == REFUSED_NOT_APPROVED
    # The revocation reached the process, not merely the ledger.
    assert backend.closed == 1
    assert worker.health().launched is False


def test_a_window_with_no_launch_derives_a_true_proof_that_says_why(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    proof = worker.release()
    assert proof.poller_stopped is True
    assert proof.worker_launched is False
    assert "no managed worker was launched" in proof.proof
    assert len(proof.proof) <= 200


def test_the_release_proof_is_derived_from_the_process_and_its_sockets(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-P5-5: three observations, none of them a literal."""

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    pid = _FakeBackend.instances[0].supervisor.process.pid
    seen: dict[str, list[str]] = {}

    def _lsof(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.subprocess.run", _lsof
    )
    proof = worker.release()
    assert proof.poller_stopped is True
    assert proof.worker_pid == pid
    assert proof.exit_status == -15
    assert proof.established_sockets == ()
    assert seen["argv"][:2] == ["/usr/sbin/lsof", "-nP"]
    # `-a` is the whole predicate: without it lsof ORs its selection options.
    assert "-a" in seen["argv"]
    assert seen["argv"][seen["argv"].index("-p") + 1] == str(pid)
    assert "TCP:ESTABLISHED" in seen["argv"]
    assert f"pid {pid}" in proof.proof


def test_a_remaining_provider_connection_makes_the_proof_false(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout="p1234\nn10.0.0.2:52001->149.154.167.220:443\n", stderr=""
        ),
    )
    proof = worker.release()
    assert proof.poller_stopped is False
    assert proof.established_sockets == ("10.0.0.2:52001->149.154.167.220:443",)


def test_an_unanswerable_lsof_is_not_the_same_as_an_empty_one() -> None:
    """The contract's own residual, closed on this side: None, never ()."""

    assert (
        established_sockets(
            1,
            run=lambda argv, **kwargs: SimpleNamespace(
                returncode=1, stdout="", stderr="lsof: WARNING: no pwd entry"
            ),
        )
        is None
    )
    assert (
        established_sockets(
            1,
            run=lambda argv, **kwargs: SimpleNamespace(
                returncode=77, stdout="", stderr=""
            ),
        )
        is None
    )


def test_the_egress_port_follows_the_endpoint_the_transport_will_reach() -> None:
    assert _egress_port(None) == 443
    assert _egress_port("https://api.telegram.org") == 443
    assert _egress_port("http://127.0.0.1:53211") == 53_211


def test_the_window_supervisor_starts_and_stops_the_poller_with_the_gate(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()

    class _Poller:
        def __init__(self, **kwargs) -> None:
            self.stopped = False

        def run(self) -> str:
            while not self.stopped:
                pass
            return "stopped"

        def stop(self) -> None:
            self.stopped = True

    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=_Poller,
    )
    supervisor.reconcile()
    assert supervisor.window_id is None
    assert supervisor.polling is False
    window = _open_window(store)
    supervisor.reconcile()
    assert supervisor.window_id == window.id
    assert supervisor.polling is True
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000003",
    )
    supervisor.reconcile()
    assert supervisor.window_id is None
    assert supervisor.polling is False
    assert supervisor.outcomes == ["stopped"]
    supervisor.stop()


def test_close_window_refuses_while_the_window_still_authorizes_a_send(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    supervisor = TransportWindowSupervisor(
        store=store, worker=worker, rpc=object(), handle_update=lambda update: None
    )
    window = store.transport_activation("telegram")
    assert window is not None
    with pytest.raises(InvalidTransition):
        supervisor.close_window(window_id=window.id, actor_id="operator")
    # Refused before anything was touched: the worker is still serving.
    assert _FakeBackend.instances[0].closed == 0


def test_close_window_derives_the_value_it_records(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    window = _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000004",
    )
    supervisor = TransportWindowSupervisor(
        store=store, worker=worker, rpc=object(), handle_update=lambda update: None
    )
    result = supervisor.close_window(window_id=window.id, actor_id="operator")
    assert result["recorded"] == "transport_window_closed"
    assert result["derivation"]["poller_stopped"] is True
    assert result["derivation"]["worker_launched"] is True
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT aggregate_id, type, payload_json FROM control_audit "
            "WHERE aggregate_type = 'transport_window'"
        ).fetchall()
    assert len(rows) == 1
    assert (rows[0][0], rows[0][1]) == (window.id, "transport_window_closed")
    payload = json.loads(rows[0][2])
    assert payload["poller_stopped"] is True
    assert "close() returned" in payload["proof"]


def test_health_reports_a_revoke_that_landed_after_the_binding(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding is made once per process; the decision is not.

    A daemon that bound an approved release and then had it withdrawn would
    otherwise report `bound` for the rest of its life while refusing every
    frame -- health saying one thing and the launch path another.
    """

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    assert worker.bind().bound is True
    store.revoke_runtime_release(
        release_id=RELEASE,
        manifest_sha256=MANIFEST,
        actor_id="operator",
        idempotency_key="revoke-0000000000000002",
    )
    health = worker.health()
    assert health.bound is False
    assert health.reason == UNBOUND_NOT_APPROVED
    assert worker.bound is False
    # And the identity is still reported, because it is what the operator has
    # to quote to approve it again.
    assert health.release_id == RELEASE


def test_a_revocation_close_is_the_proof_the_window_close_then_records(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the real run: every close derives, or the audit row lies.

    A revoke closed the worker without observing anything, so the `close-window`
    that followed found no backend and recorded "no managed worker was launched"
    for a window in which one demonstrably had been -- with `poller_stopped`
    true for a reason that was false.
    """

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    window = _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    pid = _FakeBackend.instances[0].supervisor.process.pid
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    store.revoke_runtime_release(
        release_id=RELEASE,
        manifest_sha256=MANIFEST,
        actor_id="operator",
        idempotency_key="revoke-0000000000000003",
    )
    with pytest.raises(ManagedWorkerUnavailable):
        worker.request("telegram.poll", {}, timeout=5.0)

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000009",
    )
    supervisor = TransportWindowSupervisor(
        store=store, worker=worker, rpc=object(), handle_update=lambda update: None
    )
    result = supervisor.close_window(window_id=window.id, actor_id="operator")
    derivation = result["derivation"]
    assert derivation["worker_launched"] is True
    assert derivation["worker_pid"] == pid
    assert derivation["poller_stopped"] is True
    assert derivation["observed_at"].endswith("Z")
    assert "close() returned" in derivation["proof"]


class _EndingPoller:
    """A poller whose `run` returns a scripted terminal outcome immediately."""

    scripted: list[str] = []
    built: list["_EndingPoller"] = []

    def __init__(self, **kwargs) -> None:
        self.polls = 0
        self.handled = 0
        self.failures = 3
        self.last_error = "WorkerProtocolError"
        self._stop = False
        _EndingPoller.built.append(self)

    def run(self) -> str:
        return (
            _EndingPoller.scripted.pop(0) if _EndingPoller.scripted else "failed"
        )

    def stop(self) -> None:
        self._stop = True


def _supervisor(store: ControlStore, worker, **kwargs) -> TransportWindowSupervisor:
    return TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=_EndingPoller,
        **kwargs,
    )


def test_a_poller_that_fails_inside_a_live_window_is_restarted_with_backoff(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦F5⟧ found by the P5.4a real run: the loop gave up and nothing said so.

    `TelegramInboundPoller.run` returns `failed` after five consecutive
    refusals, and the supervisor only ever started a poller on a gate
    TRANSITION -- so a burst inside a live window left the window open, the
    worker bound, and no inbound loop at all.
    """

    _EndingPoller.built = []
    _EndingPoller.scripted = []
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.time.monotonic",
        lambda: clock["now"],
    )
    supervisor = _supervisor(store, worker)
    _open_window(store)
    supervisor.reconcile()
    assert len(_EndingPoller.built) == 1

    # The loop ended `failed` while the window is still in force.
    time.sleep(0.05)
    supervisor.reconcile()
    assert len(_EndingPoller.built) == 2
    assert supervisor.status()["poller_restarts"] == 1

    # Immediately again: the backoff holds it rather than spinning.
    time.sleep(0.05)
    supervisor.reconcile()
    assert len(_EndingPoller.built) == 2
    assert supervisor.status()["poller_state"] == "backoff"

    clock["now"] += 60.0
    time.sleep(0.05)
    supervisor.reconcile()
    assert len(_EndingPoller.built) == 3
    supervisor.stop()


def test_the_restart_budget_is_bounded_and_its_exhaustion_is_readable(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _EndingPoller.built = []
    _EndingPoller.scripted = []
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.time.monotonic",
        lambda: clock["now"],
    )
    supervisor = _supervisor(store, worker)
    _open_window(store)
    supervisor.reconcile()
    for _ in range(TransportWindowSupervisor.MAX_POLLER_RESTARTS + 3):
        clock["now"] += 120.0
        time.sleep(0.05)
        supervisor.reconcile()
    status = supervisor.status()
    assert status["poller_restarts"] == TransportWindowSupervisor.MAX_POLLER_RESTARTS
    assert status["poller_state"] == "exhausted"
    assert "failed after 5 restarts" in status["poller_terminal_reason"]
    assert "WorkerProtocolError" in status["poller_terminal_reason"]
    assert len(_EndingPoller.built) == TransportWindowSupervisor.MAX_POLLER_RESTARTS + 1
    supervisor.stop()


def test_a_poller_that_stopped_on_purpose_is_not_restarted(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gate_closed` and `stopped` are answers, not faults."""

    _EndingPoller.built = []
    _EndingPoller.scripted = ["gate_closed", "stopped"]
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    supervisor = _supervisor(store, worker)
    _open_window(store)
    supervisor.reconcile()
    for _ in range(3):
        time.sleep(0.05)
        supervisor.reconcile()
    assert len(_EndingPoller.built) == 1
    assert supervisor.status()["poller_state"] == "idle"
    assert supervisor.status()["poller_restarts"] == 0
    supervisor.stop()


def test_a_new_window_resets_the_restart_budget(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _EndingPoller.built = []
    _EndingPoller.scripted = []
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    supervisor = _supervisor(store, worker)
    _open_window(store)
    supervisor.reconcile()
    time.sleep(0.05)
    supervisor.reconcile()
    assert supervisor.status()["poller_restarts"] == 1
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000021",
    )
    supervisor.reconcile()
    _open_window(store, key="window-000000000000021")
    supervisor.reconcile()
    assert supervisor.status()["poller_restarts"] == 0
    assert supervisor.status()["window_id"] is not None
    supervisor.stop()


def test_binding_refuses_when_another_process_owns_the_slot_ledger(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.4c⟧ Two daemons on one slot, named rather than suffered.

    The P5.4b real run reached this state by purging a live `cortexd`'s state
    tree: the lifetime lock is a file in that tree, so the next `cortex start`
    took a fresh one while the first daemon kept its fd. The second daemon's
    worker then could not open the ledger the first one's holds under `flock`
    and EVERY frame came back `WorkerProtocolError` -- which reads like a
    broken worker and is actually two products.
    """

    import fcntl

    _approve(store)
    state_dir = tmp_path / "worker-state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = state_dir / "worker.lock"
    lock.touch(mode=0o600)
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        worker = _worker(tmp_path, store, monkeypatch)
        health = worker.bind()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert health.bound is False
    assert health.reason == UNBOUND_LEDGER_HELD
    # Nothing was written on the way to the refusal.
    assert not (tmp_path / "state" / "managed-worker" / "descriptor.json").exists()

    # ⟦P54A-9⟧ And the lock is asked again rather than once. The cutover's own
    # preconditions run `assert-transport-capability`, whose worker takes this
    # same lock, immediately before `cortex start` -- so a lock held for a few
    # hundred milliseconds used to cost the daemon its worker for the whole
    # window, with a reason code the runbook does not cover.
    assert worker.health().to_dict()["reason"] is None
    assert worker.bound is True


def test_a_free_ledger_is_not_mistaken_for_a_held_one(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe takes the lock and drops it; a stale file is not an owner."""

    _approve(store)
    state_dir = tmp_path / "worker-state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state_dir / "worker.lock").touch(mode=0o600)

    assert _worker(tmp_path, store, monkeypatch).bind().bound is True


def test_two_endpoints_on_two_ports_are_refused_rather_than_denied_later(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.4c⟧ The seatbelt permits one port, so the configuration must agree.

    A provider on a different port than the transport launches fine and then
    fails every model call with a sandbox denial that names neither endpoint.
    The refusal happens where the two ports are visible together.
    """

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        environ={
            TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN,
            "TELEGRAM_API_BASE_URL": "http://127.0.0.1:9001",
            "OPENAI_BASE_URL": "http://127.0.0.1:9002/v1",
        },
    )

    health = worker.bind()

    assert health.bound is False
    assert health.reason == UNBOUND_EGRESS_CONFLICT


def test_the_provider_credential_reaches_the_worker_and_the_bot_token_still_gates(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn needs a model provider; the window is not what authorizes one.

    ⟦AMD-4⟧ scopes the BOT TOKEN to the window, because the window is the
    decision about who may speak as the bot. Whether a turn may run is
    migration 12's decision, taken in Control before any runtime call -- so the
    provider key is bound from `secret_refs` whenever the worker launches, and
    the transport credential is still absent until a window is open.
    """

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}",
            "openai": "env://CORTEX_TEST_PROVIDER_KEY",
        },
        environ={
            TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN,
            "CORTEX_TEST_PROVIDER_KEY": "sk-obviously-fake",
            "OPENAI_BASE_URL": "https://provider.invalid/v1",
        },
    )
    assert worker.bind().bound is True

    closed = worker._environment()  # noqa: SLF001 - the launch environment
    assert closed["OPENAI_API_KEY"] == "sk-obviously-fake"
    assert closed["OPENAI_BASE_URL"] == "https://provider.invalid/v1"
    assert TELEGRAM_CREDENTIAL_KEY not in closed

    _open_window(store)
    opened = worker._environment()  # noqa: SLF001
    assert opened[TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN
    assert opened["OPENAI_API_KEY"] == "sk-obviously-fake"


def test_a_turn_is_told_which_provider_to_use(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.4c⟧ The fork cannot work this out inside the seatbelt.

    Observed on the real gen 9 slot: with `OPENAI_BASE_URL` and
    `OPENAI_API_KEY` both present in the worker's environment, every leg of the
    fork's auto-detect chain still came back empty -- `config.yaml` lives under
    a HERMES_HOME whose `hooks` directory is deny-write, `ensure_hermes_home()`
    fails, and `AIAgent.__init__` raises "No LLM provider configured" before a
    request leaves the sandbox. So the product says it.
    """

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}",
            "openai": "env://CORTEX_TEST_PROVIDER_KEY",
        },
        environ={
            TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN,
            "CORTEX_TEST_PROVIDER_KEY": "sk-obviously-fake",
            "OPENAI_BASE_URL": "https://provider.invalid/v1",
        },
    )
    assert worker.bind().bound is True

    options = worker._agent_options()  # noqa: SLF001 - the turn's provider

    assert options["provider"] == "custom"
    assert options["base_url"] == "https://provider.invalid/v1"
    assert options["api_key"] == "sk-obviously-fake"
    # `quiet_mode` is the backend's, not negotiable from here: fd 1 is the
    # frame stream.
    assert "quiet_mode" not in options


def test_an_installation_that_names_no_provider_is_told_nothing(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A way to answer, never a default answer."""

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    assert worker.bind().bound is True

    assert worker._agent_options() == {}  # noqa: SLF001


# -- ⟦BLOCK-1⟧ the provider a SUPERVISED daemon can name ----------------------


def _control_environment(home: Path) -> dict[str, str]:
    """The literal seven keys `distribution.lifecycle` spawns `cortexd` with.

    Copied rather than imported so a change to the supervisor's environment
    breaks this test loudly instead of silently redefining what it proves. The
    source is `LifecycleSpec.control_environment`, and the point of the whole
    BLOCK-1 fix is that this dict REPLACES the environment: there is no shell
    export and no LaunchAgent `EnvironmentVariables` entry that can add an
    eighth key to it.
    """

    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/opt/cortex/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
    }


def test_the_supervised_daemon_names_a_provider_from_configuration_alone(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦BLOCK-1⟧ The production shape: no `*_BASE_URL` anywhere in sight.

    This is the exact configuration the mini runs and the exact environment the
    distribution supervisor gives the daemon. Before the fix `_base_urls` was
    built from `self._environ` only, so `_agent_options` skipped every alias --
    it has no endpoint -- and returned `{}`; the worker then reached
    `AIAgent.__init__` with nothing to go on and every managed turn on every
    deployed start path ended `runtime_execution_failed`.
    """

    _approve(store)
    environ = _control_environment(tmp_path / "home")
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: "keychain://cortex-telegram/bot",
            "anthropic": "env://CORTEX_TEST_PROVIDER_KEY",
        },
        environ=environ | {"CORTEX_TEST_PROVIDER_KEY": "sk-obviously-fake"},
        runtime={
            "model": "claude-fable-5",
            "provider": "anthropic",
            "base_url": "https://provider.invalid",
        },
    )

    assert worker.bind().bound is True
    options = worker._agent_options()  # noqa: SLF001 - the turn's provider

    assert options == {
        "provider": "anthropic",
        "base_url": "https://provider.invalid",
        "api_key": "sk-obviously-fake",
        "model": "claude-fable-5",
    }
    # The endpoint agrees with the transport's, so the one port the seatbelt
    # permits is still 443 and nothing conflicts.
    assert worker._egress_port == 443  # noqa: SLF001
    # And the environment really was the closed seven-key dict: no allowlisted
    # base URL variable was consulted, because none of them is there.
    assert set(environ) - {"CORTEX_TEST_PROVIDER_KEY"} == {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
    }


def test_the_loopback_override_still_wins_for_an_acceptance(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver that starts `cortexd` itself may still aim it at a stand-in."""

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}",
            "openai": "env://CORTEX_TEST_PROVIDER_KEY",
        },
        environ={
            TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN,
            "CORTEX_TEST_PROVIDER_KEY": "sk-obviously-fake",
            "TELEGRAM_API_BASE_URL": "http://127.0.0.1:9001",
            "OPENAI_BASE_URL": "http://127.0.0.1:9001/v1",
        },
        runtime={"model": "gpt-4o-mini", "base_url": "https://provider.invalid"},
    )

    assert worker.bind().bound is True
    assert worker._agent_options()["base_url"] == "http://127.0.0.1:9001/v1"  # noqa: SLF001


def test_a_credential_alias_with_no_endpoint_refuses_at_bind(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a provider is not a provider, and the daemon says so before a window."""

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: "keychain://cortex-telegram/bot",
            "anthropic": "keychain://cortex-provider/anthropic",
        },
        environ=_control_environment(tmp_path / "home"),
        runtime={"model": "claude-fable-5"},
    )

    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_PROVIDER_ENDPOINT


def test_an_endpoint_with_no_credential_alias_refuses_at_bind(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={TELEGRAM_SECRET_ALIAS: "keychain://cortex-telegram/bot"},
        environ=_control_environment(tmp_path / "home"),
        runtime={"model": "claude-fable-5", "base_url": "https://provider.invalid"},
    )

    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_PROVIDER_CREDENTIAL


def test_an_unresolvable_provider_reference_is_a_named_refusal(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent `{}` is gone: which credential failed is now sayable.

    `_agent_options` used to return `{}` for an unresolvable reference, which is
    indistinguishable on every surface from naming no provider at all.
    """

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={
            TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}",
            "openai": "env://CORTEX_TEST_PROVIDER_KEY_THAT_IS_UNSET",
        },
        environ={TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN},
        runtime={"base_url": "https://provider.invalid/v1"},
    )
    assert worker.bind().bound is True

    with pytest.raises(ManagedWorkerUnavailable) as options_error:
        worker._agent_options()  # noqa: SLF001
    assert options_error.value.reason == REFUSED_PROVIDER_CREDENTIAL

    _open_window(store)
    store.enable_runtime_activation(
        mode="window",
        window_seconds=1800,
        actor_id="operator",
        idempotency_key="dispatch-00000000000001",
    )
    with pytest.raises(ManagedWorkerUnavailable) as launch_error:
        worker.acquire()
    # The launch resolves the provider key too, and the refusal names the
    # provider rather than the bot token.
    assert launch_error.value.reason == REFUSED_PROVIDER_CREDENTIAL


def test_a_malformed_endpoint_is_a_reason_rather_than_a_daemon_that_will_not_start(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P54A-8⟧ An operator typo must not take `doctor` and `backup` with it."""

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        environ={
            TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN,
            "TELEGRAM_API_BASE_URL": "http://127.0.0.1:not-a-port",
        },
    )

    health = worker.bind()
    assert health.bound is False
    assert health.reason == UNBOUND_EGRESS_INVALID


def test_a_scheme_less_endpoint_is_not_quietly_treated_as_https(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(EgressEndpointInvalid):
        _egress_port("provider.invalid/v1")


def test_an_installation_that_names_nothing_still_binds(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal for an answer that cannot be delivered, not a requirement."""

    _approve(store)
    worker = _worker(
        tmp_path,
        store,
        monkeypatch,
        references={TELEGRAM_SECRET_ALIAS: "keychain://cortex-telegram/bot"},
        environ=_control_environment(tmp_path / "home"),
    )

    assert worker.bind().bound is True
    assert worker._agent_options() == {}  # noqa: SLF001


# -- ⟦P54A-2⟧ a window that ends releases the worker --------------------------


def test_a_disabled_window_releases_the_worker_on_the_next_tick(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot token stops being held the moment the window stops authorizing it.

    Before this, only `close-window` released. A `transport disable` -- step 5
    of the cutover, and the thing an expiry does with nobody present -- stopped
    the poller and left the worker process alive with the token in its
    environment. `rollback.sh` never calls `close-window` at all, so an
    abandoned ceremony left one running indefinitely.
    """

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    window = _open_window(store)
    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=lambda **kwargs: SimpleNamespace(
            run=lambda: "stopped", stop=lambda: None, last_error=None, polls=0,
            handled=0, failures=0,
        ),
    )
    supervisor.reconcile()
    assert supervisor.window_id == window.id
    # A turn is in flight: the worker is launched and holding the token.
    worker.request("telegram.poll", {}, timeout=5.0)
    backend = _FakeBackend.instances[0]
    assert backend.closed == 0

    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.subprocess.run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000009",
    )
    supervisor.reconcile()

    assert supervisor.window_id is None
    assert backend.closed == 1

    # And the derivation made at that instant is what the operator's later
    # `close-window` records -- not "no managed worker was launched", which is
    # what an unreleased-then-released worker used to write into the audit.
    result = supervisor.close_window(window_id=window.id, actor_id="operator")
    proof = result["derivation"]
    assert proof["worker_launched"] is True
    assert proof["poller_stopped"] is True
    assert proof["observed_at"]
    with sqlite3.connect(store.path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM control_audit "
                "WHERE type = 'transport_window_closed'"
            ).fetchone()[0]
        )
    assert payload["poller_stopped"] is True
    assert "close() returned" in payload["proof"]


def test_a_release_that_cannot_be_derived_does_not_stop_the_window_loop(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_watch` swallows a raising tick, and the next one has moved on."""

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)

    class _Exploding:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def release(self):
            raise RuntimeError("lsof is wedged")

    supervisor = TransportWindowSupervisor(
        store=store,
        worker=_Exploding(worker),
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=lambda **kwargs: SimpleNamespace(
            run=lambda: "stopped", stop=lambda: None, last_error=None, polls=0,
            handled=0, failures=0,
        ),
    )
    supervisor.reconcile()
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000010",
    )

    supervisor.reconcile()
    assert supervisor.window_id is None


def test_established_sockets_treats_a_wedged_lsof_as_unanswered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⟦P54A-2⟧ The call moved onto the 1 s reconcile thread, so it is bounded."""

    import subprocess as _subprocess

    def _timeout(argv, **kwargs):
        assert kwargs["timeout"] == 10
        raise _subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert established_sockets(4242, run=_timeout) is None


# -- the batch C minors -------------------------------------------------------


def test_close_window_refuses_a_superseded_id_without_touching_the_live_one(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P54A-4⟧ A stale `$WINDOW` used to kill the LIVE window's worker.

    The refusal came from the store, and the store was the LAST thing
    `close_window` did: the poller was already stopped and the worker already
    released by the time it answered, and `transport status` afterwards
    reported only `idle`.
    """

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    superseded = _open_window(store)
    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000011",
    )
    live = _open_window(store, "window-000000000000012")
    worker.request("telegram.poll", {}, timeout=5.0)
    backend = _FakeBackend.instances[0]
    supervisor = TransportWindowSupervisor(
        store=store, worker=worker, rpc=object(), handle_update=lambda update: None
    )

    with pytest.raises(InvalidTransition):
        supervisor.close_window(window_id=superseded.id, actor_id="operator")

    # The live window is untouched: its worker is still serving.
    assert backend.closed == 0
    assert store.transport_activation("telegram").id == live.id


def test_close_window_refuses_an_id_that_names_no_decision(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    supervisor = TransportWindowSupervisor(
        store=store, worker=worker, rpc=object(), handle_update=lambda update: None
    )

    with pytest.raises(NotFound):
        supervisor.close_window(window_id="window-that-never-was", actor_id="op")


def test_an_approval_that_lands_after_start_reaches_the_binding(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P54A-5⟧ The re-read was one-way, so `release_not_approved` was sticky.

    `cortex-product-deploy` exempts revocation from the restart requirement,
    which invites the symmetric and wrong inference -- during an
    operator-present window, which is where it costs the most.
    """

    worker = _worker(tmp_path, store, monkeypatch)
    assert worker.bind().reason == UNBOUND_NOT_APPROVED

    _approve(store)

    assert worker.health().bound is True
    assert worker.bound is True
    # The binding really completed: the descriptor the launch needs is written.
    assert (tmp_path / "state" / "managed-worker" / "descriptor.json").is_file()


def test_an_unanswered_lsof_is_not_a_proof_that_nothing_remains(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P54A-6⟧ `clear = sockets is None or not sockets` spent None as true.

    The audit row could read `poller_stopped: true` beside a proof sentence
    ending "unanswered", which is two different pieces of evidence wearing one
    answer.
    """

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    _open_window(store)
    worker.request("telegram.poll", {}, timeout=5.0)
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.established_sockets",
        lambda pid, run=None: None,
    )

    proof = worker.release().to_dict()

    assert proof["poller_stopped"] is False
    assert proof["sockets_observed"] is False
    assert proof["proof"].endswith("unanswered")


class _DetailPoller:
    """A poller that fails with a message, through the supervisor's sink."""

    built: list["_DetailPoller"] = []
    messages: list[str] = []

    def __init__(self, *, on_failure=None, **kwargs) -> None:
        self.polls = 0
        self.handled = 0
        self.failures = 0
        self.last_error = None
        self.last_error_detail = None
        self.line_busy = 2
        self.worker_busy = 3
        self.worker_busy_seconds = 7.0
        self._on_failure = on_failure
        _DetailPoller.built.append(self)

    def run(self) -> str:
        for message in _DetailPoller.messages:
            self.failures += 1
            self.last_error = "WorkerProtocolError"
            self.last_error_detail = message
            if self._on_failure is not None:
                self._on_failure(
                    {
                        "at": "2026-09-04T00:05:00Z",
                        "error": "WorkerProtocolError",
                        "detail": message,
                        "poll_seconds": 30,
                        "elapsed_ms": 45_000,
                    }
                )
        return "failed"

    def stop(self) -> None:
        return None


def test_the_window_status_carries_the_error_text_and_a_ring_across_restarts(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.6⟧ The sixth window: `poller_last_error: WorkerProtocolError`, four
    restarts, and no way to tell "worker response timed out" from
    `operation_conflict`. The ring is the supervisor's, so a restart does not
    take the previous poller's failures with it."""

    _DetailPoller.built = []
    _DetailPoller.messages = ["worker response timed out"] + ["operation_conflict"] * 5
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(
        "cortex_platform.product.transports.managed_worker.time.monotonic",
        lambda: clock["now"],
    )
    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=_DetailPoller,
    )
    _open_window(store)
    supervisor.reconcile()
    time.sleep(0.05)
    status = supervisor.status()
    assert status["poller_last_error"] == "WorkerProtocolError"
    assert status["poller_last_error_detail"] == "operation_conflict"
    assert status["poller_line_busy"] == 2
    assert status["poller_worker_busy"] == 3
    assert status["poller_worker_busy_seconds"] == 7.0
    api = ControlAPI(store, access_token="x" * 48, transport_windows=supervisor)
    health = api.handle(method="GET", target="/api/v1/health", headers={})
    assert health.status == 200
    assert health.payload["transport_window"]["poller_worker_busy"] == 3
    assert health.payload["transport_window"]["poller_worker_busy_seconds"] == 7.0
    log = status["poller_failure_log"]
    assert [entry["detail"] for entry in log] == _DetailPoller.messages
    assert set(log[0]) == {"at", "error", "detail", "poll_seconds", "elapsed_ms"}

    # A restart replaces the poller; the ring keeps the first poller's entries
    # and is bounded at ten (twelve failures were recorded across the two).
    clock["now"] += 60.0
    supervisor.reconcile()
    time.sleep(0.05)
    status = supervisor.status()
    assert status["poller_restarts"] == 1
    assert len(_DetailPoller.built) == 2
    assert len(status["poller_failure_log"]) == 10
    assert status["poller_failure_log"][0]["detail"] == "operation_conflict"
    assert status["poller_failure_log"][4]["detail"] == "worker response timed out"
    supervisor.stop()


def test_a_new_window_starts_with_an_empty_ring_and_no_error_text(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦Batch F P56-OBS-6⟧ The ring, the error text and the drain's failure
    count are the window's story: a verification window's failures must not
    read as the live window's at cutover step 4b. Cleared at window open."""

    _DetailPoller.built = []
    _DetailPoller.messages = ["worker response timed out"]
    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()

    class _Drain:
        def __init__(self) -> None:
            self.raising = True

        def drain(self):
            if self.raising:
                raise RuntimeError("control.db is locked")
            return []

        def status(self):
            return {}

    drain = _Drain()
    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=_DetailPoller,
        drain=drain,
    )
    _open_window(store)
    supervisor.reconcile()
    time.sleep(0.05)
    status = supervisor.status()
    assert [entry["detail"] for entry in status["poller_failure_log"]] == [
        "worker response timed out"
    ]
    assert status["poller_last_error_detail"] == "worker response timed out"
    assert status["drain_failures"] == 1
    assert status["drain_last_failure"]["error"] == "RuntimeError"

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="disable-00000000000021",
    )
    supervisor.reconcile()
    _DetailPoller.messages = []
    drain.raising = False
    _open_window(store, key="window-000000000000021")
    supervisor.reconcile()
    time.sleep(0.05)
    status = supervisor.status()
    assert status["window_id"] is not None
    assert status["poller_failure_log"] == []
    assert status["poller_last_error_detail"] is None
    assert status["drain_failures"] == 0
    assert status["drain_last_failure"] is None
    supervisor.stop()


def test_a_drain_pass_that_raises_is_counted_with_its_text(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_drain_locked` swallowed the exception and nothing said which."""

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()

    class _Drain:
        def drain(self):
            raise RuntimeError("control.db is locked?token=abc")

        def status(self):
            return {}

    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=object(),
        handle_update=lambda update: None,
        poller_factory=lambda **kwargs: SimpleNamespace(
            run=lambda: "stopped", stop=lambda: None, last_error=None, polls=0,
            handled=0, failures=0,
        ),
        drain=_Drain(),
    )
    _open_window(store)
    supervisor.reconcile()
    status = supervisor.status()
    assert status["drain_failures"] == 1
    assert status["drain_last_failure"]["error"] == "RuntimeError"
    assert status["drain_last_failure"]["detail"] == "control.db is locked?[redacted]"
    assert status["drain_last_failure"]["at"].endswith("Z")
    supervisor.stop()


def test_outside_a_window_the_line_is_told_nothing_is_outstanding(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.6⟧ Batch D D-3: the early return skipped the report and left the
    line's last answer standing."""

    from cortex_platform.product.transports.worker_rpc import TransportCallSerializer

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    serializer = TransportCallSerializer()
    passes = {"count": 0}

    class _Drain:
        def drain(self):
            passes["count"] += 1
            return []

        def status(self):
            return {}

    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=SimpleNamespace(serializer=serializer),
        handle_update=lambda update: None,
        poller_factory=lambda **kwargs: SimpleNamespace(
            run=lambda: "stopped", stop=lambda: None, last_error=None, polls=0,
            handled=0, failures=0,
        ),
        drain=_Drain(),
    )
    serializer.outbound_pending(True)
    supervisor.reconcile()
    assert passes["count"] == 0
    assert serializer.status()["outbound_pending"] is False
    supervisor.stop()


def test_a_new_window_starts_the_transport_line_counters_at_zero(
    tmp_path: Path, store: ControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⟦P5.6⟧ Batch D D-4: the serializer outlives every window, the drain does
    not, and step 4b read the two side by side."""

    from cortex_platform.product.transports.worker_rpc import TransportCallSerializer

    _approve(store)
    worker = _worker(tmp_path, store, monkeypatch)
    worker.bind()
    serializer = TransportCallSerializer()
    serializer.sends_refused = 7
    serializer.polls_shortened = 40
    serializer.expect_send()
    serializer.outbound_pending(True)
    supervisor = TransportWindowSupervisor(
        store=store,
        worker=worker,
        rpc=SimpleNamespace(serializer=serializer),
        handle_update=lambda update: None,
        poller_factory=lambda **kwargs: SimpleNamespace(
            run=lambda: "stopped", stop=lambda: None, last_error=None, polls=0,
            handled=0, failures=0,
        ),
    )
    _open_window(store)
    supervisor.reconcile()
    line = supervisor.status()["transport_line"]
    assert line["sends_refused"] == 0
    assert line["polls_shortened"] == 0
    assert line["outbound_pending"] is False
    assert serializer.poll_seconds(30) == 30
    supervisor.stop()
