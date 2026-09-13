"""Pure provenance graph checks for immutable artifact versions."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .models import ArtifactVersion, ValidationError


MAX_VERSION_GRAPH_NODES = 4_096
MAX_VERSION_GRAPH_EDGES = 65_536


def validate_version_graph(versions: Iterable[ArtifactVersion]) -> None:
    """Validate one supplied closed graph without resolving mutable pointers."""

    by_id: dict[str, ArtifactVersion] = {}
    resource_uris: set[str] = set()
    logical_versions: set[tuple[str, int]] = set()
    edge_count = 0
    for version in versions:
        if len(by_id) >= MAX_VERSION_GRAPH_NODES:
            raise ValidationError("version graph exceeds the node limit")
        if not isinstance(version, ArtifactVersion):
            raise ValidationError("version graph contains a non-version value")
        if version.artifact_version_id in by_id:
            raise ValidationError("version graph identities must be unique")
        if version.resource_uri in resource_uris:
            raise ValidationError("version graph resource URIs must be unique")
        logical_identity = (version.artifact_id, version.logical_version)
        if logical_identity in logical_versions:
            raise ValidationError(
                "version graph logical versions must be unique per artifact"
            )
        edge_count += len(version.parents)
        if edge_count > MAX_VERSION_GRAPH_EDGES:
            raise ValidationError("version graph exceeds the edge limit")
        by_id[version.artifact_version_id] = version
        resource_uris.add(version.resource_uri)
        logical_versions.add(logical_identity)

    dependents: dict[str, list[str]] = {version_id: [] for version_id in by_id}
    remaining_parents: dict[str, int] = {}
    for version in by_id.values():
        remaining_parents[version.artifact_version_id] = len(version.parents)
        for parent in version.parents:
            resolved = by_id.get(parent.artifact_version_id)
            if resolved is None:
                raise ValidationError("version graph has a missing parent")
            if resolved.sha256 != parent.sha256:
                raise ValidationError("version graph parent hash does not match")
            dependents[parent.artifact_version_id].append(
                version.artifact_version_id
            )

    ready = deque(
        sorted(
            version_id
            for version_id, parent_count in remaining_parents.items()
            if parent_count == 0
        )
    )
    visited = 0
    while ready:
        version_id = ready.popleft()
        visited += 1
        for dependent in dependents[version_id]:
            remaining_parents[dependent] -= 1
            if remaining_parents[dependent] == 0:
                ready.append(dependent)
    if visited != len(by_id):
        raise ValidationError("version graph contains a parent cycle")
