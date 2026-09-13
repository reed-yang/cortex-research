"""D6 enforced: the four doors that make a release run refuse without approval.

Contract §7: every refusal test is independent of its accept test. Each case
below builds its own runtime-update root and its own control store, so a refusal
that passed because it inherited the accept case's state is not expressible
here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cortex_platform.product.control.store import ControlStore
from cortex_platform.product.runtime_update.approval import (
    ControlReleaseApprovals,
    ReleaseApprovalError,
    require_release_approval,
)
from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import canonical_json
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
)

from .fake_python_stager import FakePythonStager

ACTOR = "operator"


def _service(root: Path, catalog: dict, attestation: dict, approvals):
    return RuntimeUpdateService(
        root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=FakePythonStager(),
        approvals=approvals,
    )


def _imported(tmp_path: Path, release_factory, approvals, *, name: str = "root"):
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / name, catalog, attestation, approvals)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])
    return service, manifest


def _control(tmp_path: Path, name: str = "control") -> ControlStore:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = ControlStore(root / "control.db")
    store.initialize()
    return store


def _digest(service, manifest) -> str:
    registry = service._registry()
    return registry["releases"][manifest["release_id"]]["manifest_digest"]


def test_activate_refuses_a_release_with_no_approval(
    tmp_path: Path, release_factory
) -> None:
    store = _control(tmp_path)
    service, manifest = _imported(
        tmp_path, release_factory, ControlReleaseApprovals(store)
    )
    with pytest.raises(ReleaseApprovalError) as raised:
        service.activate(manifest["release_id"], probe=lambda _candidate: True)
    assert raised.value.reason_code == "release_not_approved"
    assert raised.value.release_id == manifest["release_id"]
    assert service.status()["active"] is None


def test_activate_accepts_the_release_the_operator_approved(
    tmp_path: Path, release_factory
) -> None:
    store = _control(tmp_path)
    service, manifest = _imported(
        tmp_path, release_factory, ControlReleaseApprovals(store)
    )
    store.approve_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=_digest(service, manifest),
        actor_id=ACTOR,
        idempotency_key="approve-command-0001",
    )
    candidate = service.activate(
        manifest["release_id"], probe=lambda _candidate: True
    )
    assert candidate.release_id == manifest["release_id"]
    assert service.status()["active"]["release_id"] == manifest["release_id"]


def test_activate_refuses_after_the_approval_is_revoked(
    tmp_path: Path, release_factory
) -> None:
    store = _control(tmp_path)
    service, manifest = _imported(
        tmp_path, release_factory, ControlReleaseApprovals(store)
    )
    digest = _digest(service, manifest)
    store.approve_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=digest,
        actor_id=ACTOR,
        idempotency_key="approve-command-0002",
    )
    store.revoke_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=digest,
        actor_id=ACTOR,
        idempotency_key="revoke-command-0002",
    )
    with pytest.raises(ReleaseApprovalError) as raised:
        service.activate(manifest["release_id"], probe=lambda _candidate: True)
    assert raised.value.reason_code == "release_not_approved"


def test_an_approval_of_other_bytes_does_not_admit_this_release(
    tmp_path: Path, release_factory
) -> None:
    """The digest is the identity, so a same-named approval must not count."""

    store = _control(tmp_path)
    service, manifest = _imported(
        tmp_path, release_factory, ControlReleaseApprovals(store)
    )
    store.approve_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256="f" * 64,
        actor_id=ACTOR,
        idempotency_key="approve-command-0003",
    )
    with pytest.raises(ReleaseApprovalError):
        service.activate(manifest["release_id"], probe=lambda _candidate: True)


def test_dispatch_refuses_before_approval_and_pins_after(
    tmp_path: Path, release_factory
) -> None:
    store = _control(tmp_path)
    service, manifest = _imported(
        tmp_path, release_factory, ControlReleaseApprovals(store)
    )
    digest = _digest(service, manifest)
    store.approve_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=digest,
        actor_id=ACTOR,
        idempotency_key="approve-command-0004",
    )
    service.activate(manifest["release_id"], probe=lambda _candidate: True)
    pin = service.pin_attempt("attempt-approved")
    assert pin.release_id == manifest["release_id"]
    assert service.preview_attempt_pin("attempt-preview").release_id == (
        manifest["release_id"]
    )

    store.revoke_runtime_release(
        release_id=manifest["release_id"],
        manifest_sha256=digest,
        actor_id=ACTOR,
        idempotency_key="revoke-command-0004",
    )
    # An already-active slot is not a licence to dispatch on it: the pin is
    # dispatch's door and it re-asks.
    with pytest.raises(ReleaseApprovalError) as raised:
        service.pin_attempt("attempt-after-revoke")
    assert raised.value.reason_code == "release_not_approved"
    with pytest.raises(ReleaseApprovalError):
        service.preview_attempt_pin("attempt-after-revoke")


def test_no_gate_configured_refuses_rather_than_permits(
    tmp_path: Path, release_factory
) -> None:
    """A deployment that forgot to wire D6 must lose the release, not the rule."""

    service, manifest = _imported(tmp_path, release_factory, None)
    with pytest.raises(ReleaseApprovalError) as raised:
        service.activate(manifest["release_id"], probe=lambda _candidate: True)
    assert raised.value.reason_code == "release_approval_unavailable"


def test_import_and_stage_stay_ungated(tmp_path: Path, release_factory) -> None:
    """An operator has to get the bytes far enough to read their digest.

    Neither runs slot code, and gating them would make the approval decision
    unmakeable: its subject is the manifest digest `import` records.
    """

    service, manifest = _imported(tmp_path, release_factory, None)
    status = service.status()
    assert status["releases"][manifest["release_id"]]["status"] == "staged"


def test_require_release_approval_is_the_single_decision_point() -> None:
    with pytest.raises(ReleaseApprovalError) as missing:
        require_release_approval(None, "release", "a" * 64)
    assert missing.value.reason_code == "release_approval_unavailable"

    class Refusing:
        def approved(self, release_id: str, manifest_sha256: str) -> bool:
            return False

    with pytest.raises(ReleaseApprovalError) as refused:
        require_release_approval(Refusing(), "release", "a" * 64)
    assert refused.value.reason_code == "release_not_approved"
    require_release_approval(AllowUnapprovedReleases(), "release", "a" * 64)
