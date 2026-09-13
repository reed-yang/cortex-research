from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from cortex_platform.product.artifacts.models import (
    Artifact,
    ArtifactVersion,
    Snapshot,
    ValidationError,
)
from cortex_platform.product.artifacts import provenance as provenance_module
from cortex_platform.product.artifacts.provenance import validate_version_graph


SHA_A = "a" * 64
SHA_B = "b" * 64
COMMITTED_AT = "2026-07-23T12:34:56Z"


def _parent(version_id: str = "version-parent", sha256: str = SHA_B) -> dict[str, object]:
    return {"artifact_version_id": version_id, "sha256": sha256}


def _provenance(
    *,
    sha256: str = SHA_A,
    byte_length: int = 12,
    parents: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "source_ids": ["source-echo", "source-ttt"],
        "research_engine_refs": ["engine:brief-1"],
        "generator": {"name": "cortex-research", "version": "1.0.0"},
        "tool": {"name": "deterministic-fixture", "version": "1"},
        "parents": parents or [],
        "media_type": "text/markdown",
        "byte_length": byte_length,
        "sha256": sha256,
        "committed_at": COMMITTED_AT,
    }


def _version(
    *,
    version_id: str = "version-living-2",
    artifact_id: str = "artifact-living",
    logical_version: int = 2,
    sha256: str = SHA_A,
    parents: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    parent_inputs = parents or []
    return {
        "schema_version": 1,
        "artifact_version_id": version_id,
        "artifact_id": artifact_id,
        "logical_version": logical_version,
        "resource_uri": f"cortex://artifacts/{artifact_id}/{version_id}",
        "sha256": sha256,
        "byte_length": 12,
        "media_type": "text/markdown",
        "parents": parent_inputs,
        "provenance": _provenance(sha256=sha256, parents=parent_inputs),
        "committed_at": COMMITTED_AT,
    }


def test_artifact_v1_is_closed_and_round_trips() -> None:
    raw = {
        "schema_version": 1,
        "artifact_id": "artifact-living",
        "workspace_id": "workspace-1",
        "thread_id": "thread-evidence",
        "kind": "living-brief",
        "title": "Echo / TTT Living Brief",
    }

    artifact = Artifact.from_dict(raw)

    assert artifact.to_dict() == raw
    for mutation in (
        {**raw, "private_path": "/tmp/secret"},
        {**raw, "schema_version": 2},
        {**raw, "artifact_id": "Artifact-Living"},
        {**raw, "title": "e\u0301"},
    ):
        with pytest.raises(ValidationError):
            Artifact.from_dict(mutation)


def test_artifact_version_v1_binds_canonical_content_and_provenance() -> None:
    raw = _version(parents=[_parent()])

    version = ArtifactVersion.from_dict(raw)

    assert version.to_dict() == raw
    assert version.parents[0].sha256 == SHA_B

    attacks: list[tuple[str, object]] = [
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("byte_length", -1),
        ("byte_length", True),
        ("media_type", "Text/Markdown"),
        ("media_type", "text/markdown; charset=utf-8"),
        ("resource_uri", "cortex://artifacts/../escape.md"),
        ("resource_uri", "cortex://Artifacts/artifact/version.md"),
        ("committed_at", "2026-07-23T12:34:56+00:00"),
    ]
    for field, value in attacks:
        mutation = deepcopy(raw)
        mutation[field] = value
        with pytest.raises(ValidationError):
            ArtifactVersion.from_dict(mutation)

    extra = deepcopy(raw)
    extra["provider_payload"] = {"token": "secret"}
    with pytest.raises(ValidationError):
        ArtifactVersion.from_dict(extra)

    for invalid_uri in (
        "cortex://artifacts/artifact-living/version-living-2.md",
        "cortex://artifacts/artifact-living/version-living-2/content.md",
        "cortex://artifacts/other-artifact/version-living-2",
        "cortex://artifacts/artifact-living/other-version",
    ):
        mutation = deepcopy(raw)
        mutation["resource_uri"] = invalid_uri
        with pytest.raises(ValidationError, match="artifact.*version"):
            ArtifactVersion.from_dict(mutation)


def test_version_rejects_noncanonical_or_inconsistent_parent_inputs() -> None:
    unordered = [_parent("version-z", SHA_B), _parent("version-a", "c" * 64)]
    with pytest.raises(ValidationError, match="canonical order"):
        ArtifactVersion.from_dict(_version(parents=unordered))

    duplicated = [_parent(), _parent()]
    with pytest.raises(ValidationError, match="unique"):
        ArtifactVersion.from_dict(_version(parents=duplicated))

    self_parent = [_parent("version-living-2", SHA_B)]
    with pytest.raises(ValidationError, match="itself"):
        ArtifactVersion.from_dict(_version(parents=self_parent))

    mismatched = _version(parents=[_parent()])
    mismatched["provenance"] = _provenance(parents=[])
    with pytest.raises(ValidationError, match="provenance"):
        ArtifactVersion.from_dict(mismatched)

    mismatched = _version()
    mismatched["provenance"] = _provenance(byte_length=13)
    with pytest.raises(ValidationError, match="provenance"):
        ArtifactVersion.from_dict(mismatched)


def test_provenance_is_closed_and_rejects_noncanonical_references() -> None:
    raw = _version()
    provenance = raw["provenance"]
    assert isinstance(provenance, dict)

    for field, value in (
        ("source_ids", ["source-z", "source-a"]),
        ("source_ids", ["source-echo", "source-echo"]),
        ("research_engine_refs", ["engine:\u212b"]),
        ("generator", {"name": "cortex-research", "version": "1", "path": "/tmp"}),
    ):
        mutation = deepcopy(raw)
        assert isinstance(mutation["provenance"], dict)
        mutation["provenance"][field] = value
        with pytest.raises(ValidationError):
            ArtifactVersion.from_dict(mutation)

    mutation = deepcopy(raw)
    assert isinstance(mutation["provenance"], dict)
    mutation["provenance"]["raw_prompt"] = "hidden"
    with pytest.raises(ValidationError):
        ArtifactVersion.from_dict(mutation)


def test_snapshot_v1_freezes_sorted_exact_version_members() -> None:
    raw = {
        "schema_version": 1,
        "snapshot_id": "snapshot-1",
        "workspace_id": "workspace-1",
        "name": "Echo TTT plan snapshot",
        "members": [
            {
                "artifact_id": "artifact-evidence",
                "artifact_version_id": "version-evidence-1",
                "logical_version": 1,
                "sha256": SHA_A,
            },
            {
                "artifact_id": "artifact-training",
                "artifact_version_id": "version-training-1",
                "logical_version": 1,
                "sha256": SHA_B,
            },
        ],
        "created_at": COMMITTED_AT,
    }

    snapshot = Snapshot.from_dict(raw)

    assert snapshot.to_dict() == raw

    reversed_members = deepcopy(raw)
    assert isinstance(reversed_members["members"], list)
    reversed_members["members"].reverse()
    with pytest.raises(ValidationError, match="canonical order"):
        Snapshot.from_dict(reversed_members)

    duplicated = deepcopy(raw)
    assert isinstance(duplicated["members"], list)
    duplicated["members"].append(deepcopy(duplicated["members"][0]))
    with pytest.raises(ValidationError, match="unique"):
        Snapshot.from_dict(duplicated)

    extra = deepcopy(raw)
    extra["current_version"] = "version-living-99"
    with pytest.raises(ValidationError):
        Snapshot.from_dict(extra)


def test_closed_version_graph_verifies_hashes_and_rejects_indirect_cycles() -> None:
    parent = ArtifactVersion.from_dict(
        _version(
            version_id="version-parent",
            artifact_id="artifact-parent",
            logical_version=1,
            sha256=SHA_B,
        )
    )
    child = ArtifactVersion.from_dict(_version(parents=[_parent()]))

    validate_version_graph([parent, child])

    wrong_hash = ArtifactVersion.from_dict(
        _version(parents=[_parent("version-parent", "c" * 64)])
    )
    with pytest.raises(ValidationError, match="hash"):
        validate_version_graph([parent, wrong_hash])

    left = ArtifactVersion.from_dict(
        _version(
            version_id="version-left",
            artifact_id="artifact-left",
            parents=[_parent("version-right", SHA_B)],
        )
    )
    right = ArtifactVersion.from_dict(
        _version(
            version_id="version-right",
            artifact_id="artifact-right",
            sha256=SHA_B,
            parents=[_parent("version-left", SHA_A)],
        )
    )
    with pytest.raises(ValidationError, match="cycle"):
        validate_version_graph([left, right])

    with pytest.raises(ValidationError, match="missing"):
        validate_version_graph([child])


def test_direct_dto_construction_rejects_boolean_schema_versions() -> None:
    artifact = Artifact.from_dict(
        {
            "schema_version": 1,
            "artifact_id": "artifact-1",
            "workspace_id": "workspace-1",
            "thread_id": "thread-1",
            "kind": "living-brief",
            "title": "Living brief",
        }
    )
    version = ArtifactVersion.from_dict(_version())
    snapshot = Snapshot.from_dict(
        {
            "schema_version": 1,
            "snapshot_id": "snapshot-1",
            "workspace_id": "workspace-1",
            "name": "Snapshot",
            "members": [
                {
                    "artifact_id": version.artifact_id,
                    "artifact_version_id": version.artifact_version_id,
                    "logical_version": version.logical_version,
                    "sha256": version.sha256,
                }
            ],
            "created_at": COMMITTED_AT,
        }
    )

    for value in (artifact, version, version.provenance, snapshot):
        with pytest.raises(ValidationError, match="schema"):
            replace(value, schema_version=True)


def test_version_graph_rejects_duplicate_uri_and_logical_version() -> None:
    first = ArtifactVersion.from_dict(
        _version(version_id="version-first", logical_version=1)
    )
    same_logical = ArtifactVersion.from_dict(
        _version(version_id="version-second", logical_version=1, sha256=SHA_B)
    )
    with pytest.raises(ValidationError, match="logical version"):
        validate_version_graph([first, same_logical])

    other = ArtifactVersion.from_dict(
        _version(
            version_id="version-other",
            artifact_id="artifact-other",
            logical_version=1,
            sha256=SHA_B,
        )
    )
    object.__setattr__(other, "resource_uri", first.resource_uri)
    with pytest.raises(ValidationError, match="resource URI"):
        validate_version_graph([first, other])


def test_version_graph_bounds_nodes_and_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    first = ArtifactVersion.from_dict(
        _version(version_id="version-first", artifact_id="artifact-first")
    )
    second = ArtifactVersion.from_dict(
        _version(
            version_id="version-second",
            artifact_id="artifact-second",
            sha256=SHA_B,
        )
    )
    third = ArtifactVersion.from_dict(
        _version(
            version_id="version-third",
            artifact_id="artifact-third",
            sha256="c" * 64,
        )
    )
    monkeypatch.setattr(provenance_module, "MAX_VERSION_GRAPH_NODES", 2)
    with pytest.raises(ValidationError, match="node limit"):
        validate_version_graph([first, second, third])

    monkeypatch.setattr(provenance_module, "MAX_VERSION_GRAPH_NODES", 10)
    monkeypatch.setattr(provenance_module, "MAX_VERSION_GRAPH_EDGES", 1)
    child = ArtifactVersion.from_dict(
        _version(
            version_id="version-child",
            artifact_id="artifact-child",
            parents=[
                _parent("version-first", SHA_A),
                _parent("version-second", SHA_B),
            ],
        )
    )
    with pytest.raises(ValidationError, match="edge limit"):
        validate_version_graph([first, second, child])


def test_deep_version_graph_uses_iterative_cycle_validation() -> None:
    versions: list[ArtifactVersion] = []
    parent_id: str | None = None
    parent_sha: str | None = None
    for index in range(1_500):
        version_id = f"version-{index:04d}"
        sha256 = f"{index:064x}"
        parents = (
            [_parent(parent_id, parent_sha)]
            if parent_id is not None and parent_sha is not None
            else []
        )
        versions.append(
            ArtifactVersion.from_dict(
                _version(
                    version_id=version_id,
                    artifact_id=f"artifact-{index:04d}",
                    logical_version=1,
                    sha256=sha256,
                    parents=parents,
                )
            )
        )
        parent_id = version_id
        parent_sha = sha256

    validate_version_graph(versions)


def test_dossier_provenance_v2_preserves_document_identity_without_paper_sources():
    raw = _version()
    raw["provenance"].update(schema_version=2, source_ids=[], research_engine_refs=[],
                             document_version_ids=["rdv_document"], research_context_sha256=SHA_B)
    assert ArtifactVersion.from_dict(raw).to_dict() == raw
    for field, value in (("document_version_ids", []), ("research_context_sha256", "invalid")):
        invalid = deepcopy(raw)
        invalid["provenance"][field] = value
        with pytest.raises(ValidationError):
            ArtifactVersion.from_dict(invalid)
    historical = _version()
    historical["provenance"]["source_ids"] = []
    with pytest.raises(ValidationError):
        ArtifactVersion.from_dict(historical)
