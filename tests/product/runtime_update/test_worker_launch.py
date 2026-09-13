from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import canonical_json, digest_document
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
)
from cortex_platform.product.runtime_update.worker_launch import (
    WorkerLaunchError,
    build_active_descriptor,
    build_descriptor,
    derive_worker_state_dir,
    descriptor_document,
)
from cortex_platform.product.runtime_update.worker_protocol import PROTOCOL_V2


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


def _staged_service(tmp_path: Path, release_factory, *, protocol: str = PROTOCOL_V2):
    artifact, manifest, catalog, attestation = release_factory()
    manifest["adapter_protocol"] = protocol
    manifest_digest = digest_document(manifest)
    catalog["payload"]["entries"][0]["manifest_sha256"] = manifest_digest
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    candidate = service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda _: True)
    pin = service.pin_attempt("attempt-1")
    return service, candidate, pin, manifest


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, bytes | None], ...]:
    entries = []
    for path in sorted((root, *root.rglob("*"))):
        details = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        payload = path.read_bytes() if stat.S_ISREG(details.st_mode) else None
        entries.append((relative, stat.S_IMODE(details.st_mode), payload))
    return tuple(entries)


def test_build_descriptor_uses_verified_pin_registry_and_manifest(
    tmp_path: Path,
    release_factory,
) -> None:
    service, candidate, pin, manifest = _staged_service(tmp_path, release_factory)
    registry = json.loads(service.paths.registry.read_bytes())
    record = registry["releases"][pin.release_id]
    before = _tree_snapshot(service.paths.root)

    descriptor = build_descriptor(service, pin.attempt_id)

    assert descriptor.schema_version == 1
    assert descriptor.slot_path == candidate.slot_dir
    assert descriptor.slot_id == pin.slot_id
    assert descriptor.state_generation_id == pin.generation_id
    assert descriptor.release_id == pin.release_id
    assert descriptor.expected_artifact_digest == pin.artifact_digest
    assert descriptor.expected_manifest_sha256 == record["manifest_digest"]
    assert descriptor.expected_content_tree_sha256 == record["content_tree_digest"]
    # Derived, not supplied: `stage` expanded exactly this archive into a root
    # named by its digest, and the pin beside it holds the measured digest of the
    # interpreter binary the worker will run from.
    runtime = manifest["worker_runtime"]
    root = service.paths.interpreters / runtime["archive_sha256"]
    assert descriptor.interpreter_path == root / runtime["interpreter_relative"]
    assert descriptor.expected_interpreter_sha256 == json.loads(
        (service.paths.interpreters / f"{runtime['archive_sha256']}.pin.json").read_text()
    )["interpreter_sha256"]
    assert descriptor.worker_entrypoint == manifest["worker_entrypoint"]
    assert descriptor.state_dir == candidate.state_dir / ".cortex-worker"
    assert descriptor.worker_protocol == PROTOCOL_V2
    assert _tree_snapshot(service.paths.root) == before


def test_derive_worker_state_dir_is_validated_path_composition(
    tmp_path: Path,
    release_factory,
) -> None:
    service, _, pin, _ = _staged_service(tmp_path, release_factory)
    assert derive_worker_state_dir(service.paths, pin.release_id, pin.generation_id) == (
        service.paths.generations / pin.release_id / pin.generation_id / ".cortex-worker"
    )
    with pytest.raises(WorkerLaunchError):
        derive_worker_state_dir(service.paths, "../escape", pin.generation_id)


def test_registry_slot_digest_must_match_persisted_pin(
    tmp_path: Path,
    release_factory,
) -> None:
    service, _, pin, _ = _staged_service(tmp_path, release_factory)
    registry = json.loads(service.paths.registry.read_bytes())
    registry["releases"][pin.release_id]["slot_digest"] = "f" * 64
    service.paths.registry.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(WorkerLaunchError):
        build_descriptor(service, pin.attempt_id)


def test_manifest_digest_is_verified_before_entrypoint_is_trusted(
    tmp_path: Path,
    release_factory,
) -> None:
    service, candidate, pin, _ = _staged_service(tmp_path, release_factory)
    (candidate.slot_dir / "manifest.json").chmod(0o600)
    (candidate.slot_dir / "manifest.json").write_text(
        '{"untrusted":"document without entrypoint"}\n',
        encoding="utf-8",
    )

    with pytest.raises(WorkerLaunchError):
        build_descriptor(service, pin.attempt_id)


def test_registry_is_validated_before_v1_protocol_is_rejected(
    tmp_path: Path,
    release_factory,
) -> None:
    service, _, pin, _ = _staged_service(
        tmp_path,
        release_factory,
        protocol="cortex-worker/1",
    )
    registry = json.loads(service.paths.registry.read_bytes())
    registry["releases"][pin.release_id]["slot_digest"] = "f" * 64
    service.paths.registry.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(WorkerLaunchError, match="registry slot digest mismatch"):
        build_descriptor(service, pin.attempt_id)


def test_v1_attempt_pin_is_rejected(tmp_path: Path, release_factory) -> None:
    service, _, pin, _ = _staged_service(
        tmp_path,
        release_factory,
        protocol="cortex-worker/1",
    )

    with pytest.raises(WorkerLaunchError, match="worker_protocol_unsupported"):
        build_descriptor(service, pin.attempt_id)


def test_generation_directory_must_already_exist(tmp_path: Path, release_factory) -> None:
    service, candidate, pin, _ = _staged_service(tmp_path, release_factory)
    shutil.rmtree(candidate.state_dir)

    with pytest.raises(WorkerLaunchError):
        build_descriptor(service, pin.attempt_id)
    assert not candidate.state_dir.exists()


def test_build_active_descriptor_matches_the_attempt_pinned_one_and_writes_nothing(
    tmp_path: Path,
    release_factory,
) -> None:
    """A pre-window assertion must leave the updater's evidence untouched.

    `build_descriptor` is attempt-pinned because a run has to be answerable for
    which runtime served it. The assertion is not a run: it happens outside any
    window and must be able to answer the question without minting an attempt
    row it would then have to remember to finish on every failure path. The
    descriptor is nonetheless the same one, because the worker state dir is
    derived from the release and the generation and never from the attempt.
    """

    service, _candidate, pin, _manifest = _staged_service(tmp_path, release_factory)
    attempt_pinned = build_descriptor(service, pin.attempt_id)
    before = _tree_snapshot(service.paths.root)

    descriptor = build_active_descriptor(service)

    assert descriptor == attempt_pinned
    assert _tree_snapshot(service.paths.root) == before
    assert not (service.paths.attempts / "active.json").exists()
    assert sorted(path.name for path in service.paths.attempts.iterdir()) == [
        "attempt-1.json"
    ]


def test_build_active_descriptor_refuses_without_an_active_runtime(
    tmp_path: Path,
    release_factory,
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    catalog["payload"]["entries"][0]["manifest_sha256"] = digest_document(manifest)
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])

    with pytest.raises(WorkerLaunchError, match="active runtime is unavailable"):
        build_active_descriptor(service)


def test_descriptor_document_round_trips_through_the_loader(
    tmp_path: Path,
    release_factory,
) -> None:
    """Anything that launches a worker has to serialize a descriptor first."""

    from cortex_platform.product.runtime_update.worker_protocol import (
        SlotInterpreterDescriptor,
    )

    service, _candidate, pin, _manifest = _staged_service(tmp_path, release_factory)
    descriptor = build_descriptor(service, pin.attempt_id)
    path = tmp_path / "descriptor.json"
    path.write_text(json.dumps(descriptor_document(descriptor), sort_keys=True))

    assert SlotInterpreterDescriptor.load(path) == descriptor
