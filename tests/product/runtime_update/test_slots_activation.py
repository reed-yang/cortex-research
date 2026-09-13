from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import canonical_json
from cortex_platform.product.runtime_update.service import (
    ActivationError,
    CrashInjected,
    DigestPinVerifier,
    RuntimeUpdateService,
)
from cortex_platform.product.runtime_update.supervisor import WorkerSupervisor


def _service(
    root: Path,
    catalog: dict,
    attestation: dict,
    stager: FakePythonStager | None = None,
) -> RuntimeUpdateService:
    return RuntimeUpdateService(
        root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=stager or FakePythonStager(),
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )


def _import_stage(
    service,
    release_factory,
    release_id: str,
    sequence: int,
    state=None,
    *,
    catalog_sequence: int | None = None,
):
    artifact, manifest, catalog, attestation = release_factory(
        release_id=release_id, sequence=sequence
    )
    if catalog_sequence is not None:
        catalog["payload"]["sequence"] = catalog_sequence
    service = _service(service.paths.root, catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    return service, service.stage(release_id, source_state=state)


def test_candidate_slot_is_immutable_and_state_generation_is_copy_only(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    source = tmp_path / "source-state"
    source.mkdir()
    (source / "state.db").write_bytes(b"old-state")
    staged = service.stage(manifest["release_id"], source_state=source)

    assert staged.slot_dir.name == manifest["artifact_sha256"]
    assert not (staged.slot_dir.stat().st_mode & 0o222)
    assert (staged.state_dir / "state.db").read_bytes() == b"old-state"
    (staged.state_dir / "state.db").write_bytes(b"candidate-write")
    assert (source / "state.db").read_bytes() == b"old-state"


def test_state_copy_rejects_symlinks(tmp_path: Path, release_factory) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    source = tmp_path / "state"
    source.mkdir()
    (source / "escape").symlink_to(tmp_path)
    with pytest.raises(ActivationError, match="symlink"):
        service.stage(manifest["release_id"], source_state=source)


def test_idle_only_activation_pins_attempt_to_release_and_generation(
    tmp_path: Path, release_factory
) -> None:
    first_artifact, first_manifest, first_catalog, first_attestation = release_factory(
        release_id="hermes-0.18.1", sequence=181
    )
    service = _service(tmp_path / "updates", first_catalog, first_attestation)
    service.import_release(
        catalog=first_catalog,
        manifest=first_manifest,
        attestation=first_attestation,
        artifact=first_artifact,
    )
    service.stage(first_manifest["release_id"])
    service.activate(first_manifest["release_id"], probe=lambda candidate: True)
    preview = service.preview_attempt_pin("attempt-1")
    assert service.status()["active_attempts"] == 0
    pin = service.pin_attempt("attempt-1", preview)
    assert pin.slot_id == first_manifest["artifact_sha256"]
    assert pin.artifact_digest == first_manifest["artifact_sha256"]
    assert pin.worker_protocol == first_manifest["adapter_protocol"]

    service, _ = _import_stage(
        service, release_factory, "hermes-0.18.2", 182
    )
    with pytest.raises(ActivationError, match="active attempts"):
        service.activate("hermes-0.18.2", probe=lambda candidate: True)
    assert service.attempt_pin("attempt-1") == pin
    service.finish_attempt("attempt-1", pin)
    service.finish_attempt("attempt-1", pin)
    service.activate("hermes-0.18.2", probe=lambda candidate: True)
    assert service.status()["active"]["release_id"] == "hermes-0.18.2"
    assert service.status()["last_known_good"]["release_id"] == "hermes-0.18.1"


def test_resume_style_pin_handoff_releases_both_attempts_before_activation(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.1", sequence=181
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda candidate: True)
    paused_pin = service.pin_attempt("paused-attempt")
    resumed_preview = service.preview_attempt_pin("resumed-attempt")
    resumed_pin = service.pin_attempt("resumed-attempt", resumed_preview)
    service.finish_attempt("paused-attempt", paused_pin)

    service, _ = _import_stage(
        service, release_factory, "hermes-0.18.2", 182
    )
    with pytest.raises(ActivationError, match="active attempts"):
        service.activate("hermes-0.18.2", probe=lambda candidate: True)
    service.finish_attempt("resumed-attempt", resumed_pin)
    service.activate("hermes-0.18.2", probe=lambda candidate: True)

    assert service.status()["active_attempts"] == 0
    assert service.status()["active"]["release_id"] == "hermes-0.18.2"


def test_legacy_attempt_pin_is_enriched_from_immutable_manifest(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda candidate: True)
    pin = service.pin_attempt("legacy-attempt")
    pin_path = service.paths.attempts / "legacy-attempt.json"
    pin_path.write_text(
        json.dumps(
            {
                "attempt_id": pin.attempt_id,
                "release_id": pin.release_id,
                "slot_digest": pin.slot_digest,
                "generation_id": pin.generation_id,
            }
        ),
        encoding="utf-8",
    )

    assert service.attempt_pin("legacy-attempt") == pin
    assert service.pin_attempt("legacy-attempt") == pin
    persisted = json.loads(pin_path.read_text(encoding="utf-8"))
    assert persisted["worker_protocol"] == manifest["adapter_protocol"]


@pytest.mark.parametrize(
    "crash_point",
    ["journal_prepared", "candidate_healthy", "active_pointer_switched", "post_health"],
)
def test_activation_crash_recovers_exact_old_pointer(
    tmp_path: Path, release_factory, crash_point: str
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.1", sequence=181
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda candidate: True)
    old = service.status()["active"]
    service, _ = _import_stage(service, release_factory, "hermes-0.18.2", 182)

    with pytest.raises(CrashInjected):
        service.activate(
            "hermes-0.18.2",
            probe=lambda candidate: True,
            crash_at=crash_point,
        )
    service.recover_activation()
    assert service.status()["active"] == old


def test_candidate_probe_failure_rolls_back_and_quarantines(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    service.stage(manifest["release_id"])
    with pytest.raises(ActivationError, match="health"):
        service.activate(manifest["release_id"], probe=lambda candidate: False)
    status = service.status()
    assert status["active"] is None
    assert status["releases"][manifest["release_id"]]["status"] == "quarantined"


def test_freeze_no_downgrade_and_concurrent_activation(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.2", sequence=182
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    service.stage(manifest["release_id"])
    service.freeze()
    with pytest.raises(ActivationError, match="frozen"):
        service.activate(manifest["release_id"], probe=lambda candidate: True)
    service.thaw()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.activate(
                    manifest["release_id"], probe=lambda candidate: True
                ),
                range(2),
            )
        )
    assert all(result.release_id == manifest["release_id"] for result in results)
    pointers = [path for path in service.paths.pointers.glob("active*.json")]
    assert [path.name for path in pointers] == ["active.json"]

    service, _ = _import_stage(
        service,
        release_factory,
        "hermes-0.17.0",
        170,
        catalog_sequence=183,
    )
    with pytest.raises(ActivationError, match="downgrade"):
        service.activate("hermes-0.17.0", probe=lambda candidate: True)


def test_explicit_rollback_uses_preserved_generation_without_downgrade_write(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.1", sequence=181
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    first = service.stage(manifest["release_id"])
    (first.state_dir / "schema.txt").write_text("old", encoding="utf-8")
    service.activate(manifest["release_id"], probe=lambda candidate: True)
    service, second = _import_stage(service, release_factory, "hermes-0.18.2", 182, first.state_dir)
    service.activate("hermes-0.18.2", probe=lambda candidate: True)
    (second.state_dir / "schema.txt").write_text("new", encoding="utf-8")

    restored = service.rollback(probe=lambda candidate: True)
    assert restored.release_id == "hermes-0.18.1"
    assert (restored.state_dir / "schema.txt").read_text(encoding="utf-8") == "old"
    assert (second.state_dir / "schema.txt").read_text(encoding="utf-8") == "new"


def test_real_synthetic_worker_crash_quarantines_without_active_pointer(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    candidate = service.stage(manifest["release_id"])

    reached_worker = False

    def crashing_probe(staged) -> bool:
        nonlocal reached_worker
        with WorkerSupervisor(
            python_executable=Path(__import__("sys").executable),
            candidate_root=staged.slot_dir / "content",
            state_root=staged.state_dir,
            worker_entrypoint=manifest["worker_entrypoint"],
        ) as worker:
            reached_worker = True
            worker.request("crash", {}, timeout=2)
        return True

    with pytest.raises(ActivationError, match="health"):
        service.activate(candidate.release_id, probe=crashing_probe)
    # `activate` funnels every probe exception into the same ActivationError, so
    # this test once passed on a TypeError raised before the worker ever
    # started - proving nothing about crash handling while staying green.
    assert reached_worker, "the probe never reached a running worker"
    assert service.status()["active"] is None


def test_managed_slot_parent_symlink_is_rejected(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    outside = tmp_path / "outside"
    outside.mkdir()
    (service.paths.root / "slots").parent.mkdir(parents=True)
    (service.paths.root / "slots").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ActivationError, match="unsafe"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )
    assert list(outside.iterdir()) == []


def test_retention_preserves_active_lkg_and_latest_candidate(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.1", sequence=181
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda candidate: True)
    for release_id, sequence in (
        ("hermes-0.18.2", 182),
        ("hermes-0.18.3", 183),
        ("hermes-0.18.4", 184),
    ):
        service, _ = _import_stage(service, release_factory, release_id, sequence)
        if sequence == 182:
            service.activate(release_id, probe=lambda candidate: True)

    assert service.prune(retain_candidates=1) == ("hermes-0.18.3",)
    releases = service.status()["releases"]
    assert set(releases) == {
        "hermes-0.18.1",
        "hermes-0.18.2",
        "hermes-0.18.4",
    }


def test_rollback_rejects_tampered_lkg_slot(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.1", sequence=181
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(catalog=catalog, manifest=manifest, attestation=attestation, artifact=artifact)
    first = service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda candidate: True)
    service, _ = _import_stage(service, release_factory, "hermes-0.18.2", 182)
    service.activate("hermes-0.18.2", probe=lambda candidate: True)
    worker = first.slot_dir / "content" / "runtime_worker.py"
    worker.chmod(0o600)
    worker.write_text("def handle(method, params): return {}\n", encoding="utf-8")

    with pytest.raises(ActivationError, match="content verification"):
        service.rollback(probe=lambda candidate: True)
    assert service.status()["active"]["release_id"] == "hermes-0.18.2"
