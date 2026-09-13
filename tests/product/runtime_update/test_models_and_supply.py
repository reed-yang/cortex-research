from __future__ import annotations

import copy
import hashlib
import zipfile
from pathlib import Path

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import (
    CatalogEnvelope,
    PatchLedger,
    ReleaseManifest,
    ValidationError,
    canonical_json,
)
from cortex_platform.product.runtime_update.service import (
    CatalogReplayError,
    DigestPinVerifier,
    RuntimeUpdateService,
    VerificationError,
)


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


def test_manifest_catalog_and_patch_ledger_are_strict(release_factory) -> None:
    _, manifest_raw, catalog_raw, _ = release_factory()
    manifest = ReleaseManifest.from_dict(manifest_raw)
    catalog = CatalogEnvelope.from_dict(catalog_raw)
    ledger = PatchLedger.from_dict(
        {
            "schema_version": 1,
            "release_id": manifest.release_id,
            "upstream_commit": manifest.upstream_commit,
            "patches": [
                {
                    "patch_id": "cortex-fable-capability",
                    "source_commit": "1" * 40,
                    "patch_sha256": "2" * 64,
                    "disposition": "required",
                }
            ],
        }
    )

    assert catalog.payload.entries[0].manifest_sha256 == manifest.digest
    assert ledger.digest != manifest.patch_set_sha256

    for raw in (manifest_raw, catalog_raw["payload"], catalog_raw):
        tampered = copy.deepcopy(raw)
        tampered["unexpected"] = True
        constructor = (
            ReleaseManifest.from_dict
            if raw is manifest_raw
            else CatalogEnvelope.from_dict
        )
        with pytest.raises(ValidationError, match="fields"):
            constructor(tampered)


def test_wrong_hash_and_provenance_fail_before_extraction(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    with pytest.raises(VerificationError, match="artifact hash"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )
    assert not service.paths.slots.exists()

    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.3", sequence=183
    )
    attestation["repository"] = "attacker/hermes-agent"
    verifier = DigestPinVerifier(hashlib.sha256(canonical_json(attestation)).hexdigest())
    service = RuntimeUpdateService(
        tmp_path / "updates-2",
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=verifier,
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )
    with pytest.raises(VerificationError, match="provenance"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )


def test_catalog_replay_is_rejected(tmp_path: Path, release_factory) -> None:
    artifact, manifest, catalog, attestation = release_factory(sequence=200)
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    older_artifact, older_manifest, older_catalog, older_attestation = release_factory(
        release_id="hermes-0.17.0", sequence=199
    )
    replay_service = _service(tmp_path / "updates", older_catalog, older_attestation)
    with pytest.raises(CatalogReplayError):
        replay_service.import_release(
            catalog=older_catalog,
            manifest=older_manifest,
            attestation=older_attestation,
            artifact=older_artifact,
        )


def test_expired_catalog_and_patch_ledger_mismatch_fail_closed(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    catalog["payload"]["expires_at"] = catalog["payload"]["issued_at"]
    service = _service(tmp_path / "expired", catalog, attestation)
    with pytest.raises((ValidationError, VerificationError), match="expir"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )

    artifact, manifest, catalog, attestation = release_factory(
        release_id="hermes-0.18.3", sequence=183
    )
    service = _service(tmp_path / "ledger", catalog, attestation)
    wrong_ledger = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "upstream_commit": manifest["upstream_commit"],
        "patches": [
            {
                "patch_id": "unexpected-patch",
                "source_commit": "1" * 40,
                "patch_sha256": "2" * 64,
                "disposition": "required",
            }
        ],
    }
    with pytest.raises(VerificationError, match="patch ledger"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
            patch_ledger=wrong_ledger,
        )
    assert not service.paths.slots.exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/../../escape"])
def test_archive_path_traversal_is_rejected(
    tmp_path: Path, release_factory, name: str
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(name, "owned")
    manifest["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    attestation["artifact_sha256"] = manifest["artifact_sha256"]
    catalog["payload"]["entries"][0]["manifest_sha256"] = hashlib.sha256(
        canonical_json(manifest)
    ).hexdigest()
    service = _service(tmp_path / "updates", catalog, attestation)

    with pytest.raises(VerificationError, match="archive path"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )
    assert not (tmp_path / "escape").exists()


def test_symlink_artifact_and_archive_member_are_rejected(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory()
    link = tmp_path / "artifact-link.zip"
    link.symlink_to(artifact)
    service = _service(tmp_path / "updates", catalog, attestation)
    with pytest.raises(VerificationError, match="regular file"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=link,
        )

    with zipfile.ZipFile(artifact, "w") as archive:
        info = zipfile.ZipInfo("runtime_worker.py")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target")
    manifest["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    attestation["artifact_sha256"] = manifest["artifact_sha256"]
    catalog["payload"]["entries"][0]["manifest_sha256"] = hashlib.sha256(
        canonical_json(manifest)
    ).hexdigest()
    service = _service(tmp_path / "updates-2", catalog, attestation)
    with pytest.raises(VerificationError, match="symlink"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )
