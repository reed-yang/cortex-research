from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from cortex_platform.product.artifacts import reader as reader_module
from cortex_platform.product.artifacts.reader import (
    ArtifactContentReference,
    ArtifactContentUnavailable,
    ArtifactReader,
)

from .test_control import CONTENT_V1, _complete, _context, _request_version


def _committed_content(tmp_path: Path, *, content: bytes = CONTENT_V1):
    store, run, source, artifact = _context(tmp_path)
    root = tmp_path / "registered-assets"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    store.register_asset_root(
        root_id="artifacts",
        private_path=root,
        max_bytes=1_048_576,
        enabled=True,
        actor_id="reader-fixture",
        idempotency_key="reader-root-register",
    )
    reservation = _request_version(store, run, source, artifact, content=content).value
    committed = _complete(store, reservation).value
    relative = reservation["materialization_action"]["relative_path"]
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(mode=0o700, parents=True)
    target.parent.chmod(0o700)
    target.write_bytes(content)
    target.chmod(0o600)
    return store, reservation, committed, root, target


def test_reader_uses_action_selected_path_and_verifies_content(tmp_path: Path) -> None:
    store, reservation, committed, _, _ = _committed_content(tmp_path)

    result = ArtifactReader(store).read(committed["id"])

    assert result.to_dict() == {
        "artifact_version_id": committed["id"],
        "media_type": "text/markdown",
        "byte_length": len(CONTENT_V1),
        "sha256": committed["sha256"],
        "content": CONTENT_V1.decode(),
    }
    assert reservation["resource_uri"] == committed["resource_uri"]
    assert (
        reservation["materialization_action"]["relative_path"]
        not in committed["resource_uri"]
    )


def test_reader_fails_closed_for_digest_and_private_directory_changes(
    tmp_path: Path,
) -> None:
    store, _, committed, _, target = _committed_content(tmp_path)
    target.write_bytes(b"tampered bytes")
    target.chmod(0o600)

    with pytest.raises(ArtifactContentUnavailable):
        ArtifactReader(store).read(committed["id"])

    target.write_bytes(CONTENT_V1)
    target.chmod(0o600)
    target.parent.chmod(0o750)
    with pytest.raises(ArtifactContentUnavailable):
        ArtifactReader(store).read(committed["id"])


def test_reader_detects_registered_root_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, committed, root, _ = _committed_content(tmp_path)
    displaced = tmp_path / "displaced-root"
    original_read = reader_module._read_regular

    def replace_root(*args, **kwargs):
        raw = original_read(*args, **kwargs)
        root.rename(displaced)
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        return raw

    monkeypatch.setattr(reader_module, "_read_regular", replace_root)
    with pytest.raises(ArtifactContentUnavailable):
        ArtifactReader(store).read(committed["id"])


def test_reader_rejects_invalid_utf8_and_route_size_limit(tmp_path: Path) -> None:
    invalid = b"\xff"
    store, _, committed, _, _ = _committed_content(tmp_path, content=invalid)
    with pytest.raises(ArtifactContentUnavailable):
        ArtifactReader(store).read(committed["id"])

    with store._transaction() as conn:
        conn.execute("UPDATE asset_roots SET max_bytes = 1 WHERE root_id = 'artifacts'")
    with pytest.raises(ArtifactContentUnavailable):
        ArtifactReader(store).read(committed["id"])


def test_reader_accepts_only_the_closed_utf8_text_media_types(tmp_path: Path) -> None:
    root = tmp_path / "media-root"
    target = root / "plain.txt"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    target.write_text("verified text")
    target.chmod(0o600)
    raw = target.read_bytes()
    reference = ArtifactContentReference(
        artifact_version_id="artifact-version-plain",
        private_root=root,
        root_max_bytes=1024,
        relative_path="plain.txt",
        media_type="text/plain; charset=utf-8",
        byte_length=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )

    class FixedStore:
        def __init__(self, value: ArtifactContentReference) -> None:
            self.value = value

        def read_artifact_content_reference(self, artifact_version_id: str):
            assert artifact_version_id == reference.artifact_version_id
            return self.value

    result = ArtifactReader(FixedStore(reference)).read(reference.artifact_version_id)
    assert result.media_type == "text/plain"
    assert result.content == "verified text"

    for media_type in (
        "application/json",
        "Text/Plain",
        "text/plain; charset=latin-1",
        "text/plain; charset=utf-8; format=flowed",
    ):
        with pytest.raises(ArtifactContentUnavailable):
            ArtifactReader(FixedStore(replace(reference, media_type=media_type))).read(
                reference.artifact_version_id
            )


def test_store_content_reference_accepts_exact_utf8_charset_metadata(
    tmp_path: Path,
) -> None:
    store, run, _, artifact = _context(tmp_path)
    root = tmp_path / "charset-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    store.register_asset_root(
        root_id="artifacts",
        private_path=root,
        max_bytes=1024,
        enabled=True,
        actor_id="reader-fixture",
        idempotency_key="charset-root-register",
    )
    content = b"plain utf8 text"
    digest = hashlib.sha256(content).hexdigest()
    version_id = "artifact-version-charset"
    operation_id = "materialize-charset-version"
    relative_path = "plain/charset.txt"
    media_type = "text/plain; charset=utf-8"
    request = {
        "schema_version": 1,
        "operation_id": operation_id,
        "root_id": "artifacts",
        "relative_path": relative_path,
        "sha256": digest,
        "byte_length": len(content),
        "media_type": media_type,
        "parents": [],
    }
    result = {**request, "replayed": False, "recovered_from": None}
    now = "2026-07-28T00:00:00Z"
    with store._transaction() as conn:
        conn.execute(
            """INSERT INTO artifact_versions
               (id, artifact_id, logical_version, resource_uri, sha256,
                byte_length, media_type, run_id, attempt_id, generator_name,
                generator_version, tool_name, tool_version, lineage_sealed,
                state, created_at)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'reader', '1',
                       'writer', '1', 0, 'pending_materialization', ?)""",
            (
                version_id,
                artifact["id"],
                f"cortex://artifacts/{artifact['id']}/{version_id}",
                digest,
                len(content),
                media_type,
                run["id"],
                run["attempt"]["id"],
                now,
            ),
        )
        conn.execute(
            "UPDATE artifact_versions SET lineage_sealed = 1 WHERE id = ?",
            (version_id,),
        )
        conn.execute(
            """INSERT INTO artifact_materialization_actions
               (id, operation_id, artifact_version_id, request_hash, root_id,
                relative_path, sha256, byte_length, media_type, advance_head,
                expected_head_revision, state, created_at)
               VALUES ('action-charset', ?, ?, ?, 'artifacts', ?, ?, ?, ?,
                       0, NULL, 'pending', ?)""",
            (
                operation_id,
                version_id,
                store._request_hash(request),
                relative_path,
                digest,
                len(content),
                media_type,
                now,
            ),
        )
        conn.execute(
            """UPDATE artifact_materialization_actions
               SET state = 'claimed', claim_owner = 'reader-fixture',
                   claim_epoch = 1, claim_expires_at = ?, attempt_count = 1
               WHERE id = 'action-charset'""",
            (now,),
        )
        conn.execute(
            """UPDATE artifact_materialization_actions
               SET state = 'materialized', result_json = ?
               WHERE id = 'action-charset'""",
            (json.dumps(result),),
        )
        conn.execute(
            """UPDATE artifact_versions
               SET state = 'committed', provenance_json = '{}', committed_at = ?
               WHERE id = ?""",
            (now, version_id),
        )
        conn.execute(
            """UPDATE artifact_materialization_actions
               SET state = 'completed', claim_owner = NULL,
                   claim_expires_at = NULL, head_advanced = 0,
                   completed_at = ? WHERE id = 'action-charset'""",
            (now,),
        )
    target = root / "plain" / "charset.txt"
    target.parent.mkdir(mode=0o700)
    target.parent.chmod(0o700)
    target.write_bytes(content)
    target.chmod(0o600)

    read = ArtifactReader(store).read(version_id)
    assert read.media_type == "text/plain"
    assert read.content == content.decode()
