"""Closed public projection for one durable research workflow snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..control import ControlStore
from ..workflows.models import (
    LineageQueryRequest,
    LineageQueryResult,
    SuccessorCreationRequest,
    SuccessorCreationResult,
)
from .events import _SENSITIVE_TEXT_PATTERNS, project_public_decision

JsonObject = dict[str, Any]
_MAX_COLLECTION = 200


class ResearchWorkflowProjector:
    """Build a closed DTO from one store-owned SQLite read snapshot.

    ⟦ADJ-H-2⟧ Plus the run's ownership, which ADJ-G-1 reads separately and
    outside that snapshot: ownership is immutable for a run that is visible at
    all, so the pair is still one consistent projection. Everything else here
    comes from the snapshot.
    """

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    def project(self, run_id: str) -> JsonObject:
        snapshot = self._store.read_research_workflow_snapshot(run_id)
        run = snapshot["run"]
        workflow = snapshot["workflow"]
        artifacts = self._artifacts(snapshot["artifacts"], run_id=str(run["id"]))
        run_versions = {
            str(version["id"]): version
            for artifact in artifacts
            for version in artifact["versions"]
            if version["run_id"] == run["id"]
        }
        return {
            "schema_version": 1,
            "run": self._run(run, engine_owned=self._store.run_is_machine(run_id)),
            "workflow": self._workflow(workflow),
            "source_gates": self._source_gates(snapshot["source_intents"]),
            "sources": self._sources(snapshot["source_bindings"]),
            "lineage": self._lineage(
                snapshot["effects"],
                run_id=str(run["id"]),
                workflow_id=str(workflow["id"]) if workflow is not None else None,
            ),
            "decisions": [
                project_public_decision(item)
                for item in self._bounded(snapshot["decisions"], "decisions")
            ],
            "artifacts": artifacts,
            "snapshots": self._snapshots(snapshot["snapshots"], run_versions),
        }

    @staticmethod
    def _bounded(value: object, label: str) -> Sequence[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise TypeError(f"{label} must be a sequence")
        if len(value) > _MAX_COLLECTION:
            raise ValueError(f"{label} exceeds the projection limit")
        if not all(isinstance(item, Mapping) for item in value):
            raise ValueError(f"{label} contains an invalid item")
        return value

    @staticmethod
    def _run(value: Mapping[str, Any], *, engine_owned: bool) -> JsonObject:
        """The run inside the research projection, in the Run DTO's shape.

        ⟦ADJ-G-1⟧ This is a CLOSED literal and a second producer of the same
        wire type, so a key added to the Run DTO has to be added here too or
        the cockpit's `decodeRun` throws on this route alone. `engine_owned`
        is passed in rather than read here because the projector holds a store
        but this is the one projection built from a snapshot.
        """

        return {
            "id": value["id"],
            "thread_id": value["thread_id"],
            "state": value["state"],
            "active_attempt_id": value["active_attempt_id"],
            "stage": value["stage"],
            "latest_sequence": value["latest_sequence"],
            "engine_owned": engine_owned,
            "revision": value["revision"],
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
        }

    @classmethod
    def _workflow(cls, value: object) -> JsonObject | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("workflow is invalid")
        stages = []
        for stage in cls._bounded(value.get("stages"), "workflow stages"):
            stages.append(
                {
                    "key": stage["stage_key"],
                    "position": stage["position"],
                    "effect": stage["effect"],
                    "state": stage["state"],
                    "revision": stage["revision"],
                    "attempt": stage["attempt"],
                    "checkpoint_enabled": stage["checkpoint_enabled"],
                    "started_at": stage["started_at"],
                    "completed_at": stage["completed_at"],
                }
            )
        return {
            "id": value["id"],
            "definition_id": value["definition_id"],
            "definition_version": value["definition_version"],
            "state": value["state"],
            "current_stage_key": value["current_stage_key"],
            "revision": value["revision"],
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
            "stages": stages,
        }

    @classmethod
    def _source_gates(cls, value: object) -> list[JsonObject]:
        return [
            cls.project_source_gate(item)
            for item in cls._bounded(value, "source gates")
        ]

    @classmethod
    def project_source_gate(cls, item: Mapping[str, Any]) -> JsonObject:
        """Project one Source intent without raw resolver or locator details."""

        candidates = []
        for candidate in cls._bounded(item.get("candidates"), "source candidates"):
            authority = candidate.get("authority")
            source_kind = "paper" if authority in {"arxiv", "doi"} else "project"
            candidates.append(
                {
                    "id": candidate["id"],
                    "claim_kind": candidate["claim_kind"],
                    "canonical_id": candidate["canonical_id"],
                    "official_title": cls._safe_text(
                        candidate.get("official_title"), "Untitled source"
                    ),
                    "source_kind": source_kind,
                    "version": candidate.get("version"),
                }
            )
        decision = item.get("decision")
        return {
            "id": item["id"],
            "run_id": item["run_id"],
            "attempt_id": item["attempt_id"],
            "state": item["state"],
            "revision": item["revision"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "title_observation": cls._optional_safe_text(item.get("title")),
            "locator_observation": cls._optional_safe_text(item.get("locator")),
            "candidates": candidates,
            "decision": (
                project_public_decision(decision)
                if isinstance(decision, Mapping)
                else None
            ),
        }

    @classmethod
    def _sources(cls, value: object) -> list[JsonObject]:
        result: list[JsonObject] = []
        for binding in cls._bounded(value, "source bindings"):
            source = binding.get("source")
            if not isinstance(source, Mapping):
                raise TypeError("source binding is incomplete")
            aliases = []
            for alias in cls._bounded(source.get("aliases"), "source aliases"):
                display = cls._optional_safe_text(alias.get("value"))
                if display is None:
                    continue
                aliases.append(
                    {
                        "id": alias["id"],
                        "authority": alias["authority"],
                        "value": display,
                        "created_at": alias["created_at"],
                    }
                )
            result.append(
                {
                    "id": binding["id"],
                    "disposition": binding["disposition"],
                    "created_at": binding["created_at"],
                    "source": {
                        "id": source["id"],
                        "authority": source["authority"],
                        "authority_id": source["authority_id"],
                        "canonical_id": source["canonical_id"],
                        "source_kind": source["source_kind"],
                        "official_title": cls._safe_text(
                            source.get("official_title"), "Untitled source"
                        ),
                        "import_state": source["import_state"],
                        "revision": source["revision"],
                        "aliases": aliases,
                        "created_at": source["created_at"],
                        "updated_at": source["updated_at"],
                    },
                }
            )
            selection = binding.get("research_selection")
            if selection is not None:
                result[-1]["research_selection"] = {
                    "kind": selection["kind"],
                    "run_id": selection["run_id"],
                    "message_id": selection["message_id"],
                    "authority_message_id": selection["authority_message_id"],
                    "context_sha256": selection["context_sha256"],
                    "label": selection["label"],
                }
        return result

    @classmethod
    def _lineage(
        cls,
        value: object,
        *,
        run_id: str,
        workflow_id: str | None,
    ) -> JsonObject:
        nodes: dict[str, JsonObject] = {}
        links: dict[str, JsonObject] = {}
        successor_node_id: str | None = None
        for effect in cls._bounded(value, "workflow effects"):
            if effect.get("state") != "completed":
                continue
            if workflow_id is None or effect.get("workflow_id") != workflow_id:
                raise ValueError("workflow effect identity does not match")
            kind = effect.get("effect_kind")
            receipt = effect.get("receipt")
            request = effect.get("request")
            if kind == "lineage_query":
                if (
                    not isinstance(request, LineageQueryRequest)
                    or request.run_id != run_id
                ):
                    raise ValueError("lineage query does not match the run")
                parsed = LineageQueryResult.from_dict(receipt)
                request.validate_result(parsed)
                for node in parsed.nodes:
                    cls._merge_node(nodes, node.to_dict())
            elif kind == "successor_creation":
                if (
                    not isinstance(request, SuccessorCreationRequest)
                    or request.run_id != run_id
                ):
                    raise ValueError("successor creation does not match the run")
                parsed_successor = SuccessorCreationResult.from_dict(receipt)
                request.validate_result(parsed_successor)
                cls._merge_node(nodes, parsed_successor.node.to_dict())
                successor_node_id = parsed_successor.node.node_id
                for link in parsed_successor.links:
                    public_link = {
                        "id": link.link_id,
                        "from_node_id": link.from_node_id,
                        "to_node_id": link.to_node_id,
                        "relation": link.relation,
                    }
                    previous = links.setdefault(link.link_id, public_link)
                    if previous != public_link:
                        raise ValueError("lineage link identity conflicts")
        return {
            "nodes": sorted(nodes.values(), key=lambda item: str(item["id"])),
            "links": sorted(links.values(), key=lambda item: str(item["id"])),
            "successor_node_id": successor_node_id,
        }

    @classmethod
    def _merge_node(
        cls, nodes: dict[str, JsonObject], value: Mapping[str, Any]
    ) -> None:
        public = {
            "id": value["node_id"],
            "status": value["status"],
            "title": cls._safe_text(value.get("title"), "Untitled lineage node"),
            "revision": value["revision"],
        }
        previous = nodes.setdefault(str(public["id"]), public)
        if previous != public:
            raise ValueError("lineage node identity conflicts")

    @classmethod
    def _artifacts(cls, value: object, *, run_id: str) -> list[JsonObject]:
        result = []
        projected_version_ids: set[str] = set()
        parent_edges: dict[str, tuple[str, ...]] = {}
        for artifact in cls._bounded(value, "artifacts"):
            versions = []
            owns_run_version = False
            for version in cls._bounded(artifact.get("versions"), "artifact versions"):
                if version.get("state") != "committed":
                    raise ValueError("artifact version is not committed")
                owns_run_version = owns_run_version or version.get("run_id") == run_id
                version_id = str(version["id"])
                if version_id in projected_version_ids:
                    raise ValueError("artifact version is duplicated")
                projected_version_ids.add(version_id)
                parents = cls._bounded(version.get("parents"), "artifact parents")
                parent_ids = tuple(
                    str(parent["artifact_version_id"]) for parent in parents
                )
                if len(parent_ids) != len(set(parent_ids)):
                    raise ValueError("artifact parents are duplicated")
                parent_edges[version_id] = parent_ids
                versions.append(cls._artifact_version(version))
            if not owns_run_version:
                raise ValueError("artifact is outside the projected run")
            result.append(
                {
                    "id": artifact["id"],
                    "workspace_id": artifact["workspace_id"],
                    "thread_id": artifact["thread_id"],
                    "kind": artifact["kind"],
                    "title": cls._safe_text(artifact.get("title"), "Untitled artifact"),
                    "head_artifact_version_id": artifact["head_artifact_version_id"],
                    "head_revision": artifact["head_revision"],
                    "created_at": artifact["created_at"],
                    "updated_at": artifact["updated_at"],
                    "versions": versions,
                }
            )
        cls._require_acyclic_parents(parent_edges)
        return result

    @staticmethod
    def _artifact_version(value: Mapping[str, Any]) -> JsonObject:
        provenance = value.get("provenance")
        public_provenance = None
        if isinstance(provenance, Mapping):
            public_provenance = {
                "schema_version": provenance.get("schema_version"),
                "run_id": provenance.get("run_id"),
                "attempt_id": provenance.get("attempt_id"),
                "source_ids": provenance.get("source_ids"),
                "generator": provenance.get("generator"),
                "tool": provenance.get("tool"),
                "parents": provenance.get("parents"),
                "media_type": provenance.get("media_type"),
                "byte_length": provenance.get("byte_length"),
                "sha256": provenance.get("sha256"),
                "committed_at": provenance.get("committed_at"),
            }
            if provenance.get("schema_version") == 2:
                public_provenance.update(
                    document_version_ids=provenance["document_version_ids"],
                    research_context_sha256=provenance["research_context_sha256"],
                )
        return {
            "id": value["id"],
            "artifact_id": value["artifact_id"],
            "logical_version": value["logical_version"],
            "resource_uri": value["resource_uri"],
            "sha256": value["sha256"],
            "byte_length": value["byte_length"],
            "media_type": value["media_type"],
            "run_id": value["run_id"],
            "attempt_id": value["attempt_id"],
            "parents": value["parents"],
            "source_ids": value["source_ids"],
            "generator": value["generator"],
            "tool": value["tool"],
            "state": value["state"],
            "provenance": public_provenance,
            "created_at": value["created_at"],
            "committed_at": value["committed_at"],
        }

    @staticmethod
    def _require_acyclic_parents(edges: Mapping[str, tuple[str, ...]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(version_id: str) -> None:
            if version_id in visiting:
                raise ValueError("artifact parent graph contains a cycle")
            if version_id in visited:
                return
            visiting.add(version_id)
            for parent_id in edges.get(version_id, ()):
                if parent_id in edges:
                    visit(parent_id)
            visiting.remove(version_id)
            visited.add(version_id)

        for version_id in edges:
            visit(version_id)

    @classmethod
    def _snapshots(
        cls,
        value: object,
        run_versions: Mapping[str, Mapping[str, Any]],
    ) -> list[JsonObject]:
        result = []
        for snapshot in cls._bounded(value, "artifact snapshots"):
            members = cls._bounded(snapshot.get("members"), "snapshot members")
            if not all(
                str(member["artifact_version_id"]) in run_versions for member in members
            ):
                continue
            for member in members:
                version = run_versions[str(member["artifact_version_id"])]
                if (
                    member["artifact_id"] != version["artifact_id"]
                    or member["logical_version"] != version["logical_version"]
                    or member["sha256"] != version["sha256"]
                ):
                    raise ValueError("snapshot member does not match artifact version")
            result.append(
                {
                    "id": snapshot["id"],
                    "workspace_id": snapshot["workspace_id"],
                    "name": cls._safe_text(snapshot.get("name"), "Unnamed snapshot"),
                    "members": [
                        {
                            "artifact_id": member["artifact_id"],
                            "artifact_version_id": member["artifact_version_id"],
                            "logical_version": member["logical_version"],
                            "sha256": member["sha256"],
                        }
                        for member in members
                    ],
                    "created_at": snapshot["created_at"],
                }
            )
        return result

    @staticmethod
    def _optional_safe_text(value: object) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 2_000:
            return None
        if any(ord(character) < 32 for character in value):
            return None
        if any(pattern.search(value) for pattern in _SENSITIVE_TEXT_PATTERNS):
            return None
        return value

    @classmethod
    def _safe_text(cls, value: object, fallback: str) -> str:
        return cls._optional_safe_text(value) or fallback
