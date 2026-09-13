from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update.models import host_platform
from cortex_platform.product.runtime_update.worker_payload import (
    ENTRYPOINT_SOURCE,
    module_sources,
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


RUNTIME_ARCHIVE_RELATIVE = "runtime/cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz"
# Not a real CPython archive. Every test that reaches expansion injects a
# staging kernel; the one test that stages for real supplies the vendored
# archive through `runtime_archive`.
SYNTHETIC_RUNTIME_ARCHIVE = b"synthetic-cp311-runtime\n"


@pytest.fixture
def release_factory(tmp_path: Path):
    def build(
        *,
        release_id: str = "hermes-0.18.2",
        sequence: int = 182,
        worker_entrypoint: str = "runtime_worker.py",
        platform: dict | None = None,
        runtime_archive: bytes | None = None,
        runtime_relative: str = RUNTIME_ARCHIVE_RELATIVE,
        interpreter_relative: str = "bin/python3.11",
        python_version: str = "3.11.15",
        worker_modules: dict[str, str] | None = None,
        real_worker_payload: bool = False,
    ):
        artifact = tmp_path / f"{release_id}.zip"
        worker = f"# Synthetic release: {release_id}\n" + """\
def handle(method, params):
    if method == "health":
        return {"status": "healthy", "release_id": params.get("release_id")}
    if method == "crash":
        raise RuntimeError("synthetic crash")
    return {"method": method, "params": params}
"""
        runtime_payload = (
            SYNTHETIC_RUNTIME_ARCHIVE if runtime_archive is None else runtime_archive
        )
        modules: dict[str, bytes] = {}
        if real_worker_payload:
            # What `package_hermes_release.py` places: the product's own attested
            # entrypoint and the stdlib-only worker package beside it.
            worker = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
            modules = {
                relative: source.read_bytes()
                for relative, source in module_sources().items()
            }
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(worker_entrypoint, worker)
            archive.writestr("runtime.lock", "synthetic-lock\n")
            for relative, payload in modules.items():
                archive.writestr(relative, payload)
            # The interpreter rides inside `content/` as one ordinary payload
            # file, so the slot's content-tree digest binds it for free.
            archive.writestr(runtime_relative, runtime_payload)
        artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lock_digest = hashlib.sha256(b"synthetic-lock\n").hexdigest()
        patch_ledger = {
            "schema_version": 1,
            "release_id": release_id,
            "upstream_commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
            "patches": [],
        }
        patch_digest = hashlib.sha256(canonical(patch_ledger)).hexdigest()
        manifest = {
            "schema_version": 3,
            "release_id": release_id,
            "release_sequence": sequence,
            "distribution_name": "hermes-agent",
            "distribution_version": release_id.removeprefix("hermes-"),
            "upstream_repository": "NousResearch/hermes-agent",
            "upstream_tag": "v2026.7.7.2",
            "upstream_commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
            "artifact_filename": artifact.name,
            "artifact_sha256": artifact_digest,
            "publisher": "pypi:NousResearch",
            "workflow": "release.yml",
            "python_range": ">=3.11,<3.14",
            "dependency_lock_sha256": lock_digest,
            "adapter_protocol": "0.1",
            "session_schema": 13,
            "patch_set_sha256": patch_digest,
            "evidence_sha256": "b" * 64,
            "worker_entrypoint": worker_entrypoint,
            "worker_runtime": {
                "archive": runtime_relative,
                "archive_sha256": hashlib.sha256(runtime_payload).hexdigest(),
                "interpreter_relative": interpreter_relative,
                "python_version": python_version,
            },
            # Empty until S3.3 packages the worker-side protocol modules.
            "worker_modules": (
                worker_modules
                if worker_modules is not None
                else {
                    relative: hashlib.sha256(payload).hexdigest()
                    for relative, payload in modules.items()
                }
            ),
            # Default to this host so a release built here imports here; the
            # refusal tests pass a deliberately foreign target.
            "platform": platform if platform is not None else host_platform(),
        }
        manifest_digest = hashlib.sha256(canonical(manifest)).hexdigest()
        now = datetime.now(timezone.utc)
        payload = {
            "schema_version": 1,
            "sequence": sequence,
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(days=1)).isoformat().replace(
                "+00:00", "Z"
            ),
            "entries": [
                {
                    "release_id": release_id,
                    "manifest_sha256": manifest_digest,
                    "status": "certified",
                }
            ],
        }
        catalog = {
            "payload": payload,
            "key_id": "test-key",
            "signature": hashlib.sha256(canonical(payload)).hexdigest(),
        }
        attestation = {
            "schema_version": 1,
            "artifact_sha256": artifact_digest,
            "repository": manifest["upstream_repository"],
            "tag": manifest["upstream_tag"],
            "commit": manifest["upstream_commit"],
            "publisher": manifest["publisher"],
            "workflow": manifest["workflow"],
        }
        return artifact, manifest, catalog, attestation

    return build
