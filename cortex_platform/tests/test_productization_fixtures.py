from __future__ import annotations

import hashlib
import asyncio
import json
import re
import socket
import unicodedata
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

import pytest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "productization"
SCHEMA_NAME = "golden-case-v0.1.schema.json"
G0_NAME = "g0-source-conflict.json"
G1_NAME = "g1-keep-both-successor.json"

SECONDARY_TITLE = "Scaling Mixture-of-Experts Video Pretraining for Embodied Intelligence"
G0_EVENT_TYPES = [
    "run.created",
    "run.started",
    "source.intent_received",
    "source.conflict_detected",
    "decision.requested",
    "run.waiting_for_decision",
]
G1_EVENT_TYPES = [
    *G0_EVENT_TYPES,
    "decision.resolved",
    "source.reused",
    "source.imported",
    "lineage.candidates_retrieved",
    "decision.requested",
    "run.waiting_for_decision",
    "decision.resolved",
    "lineage.successor_created",
    "checkpoint.committed",
    "run.recovered",
    "thread.created",
    "thread.created",
    "thread.created",
    "artifact.version_committed",
    "artifact.version_committed",
    "artifact.version_committed",
    "artifact.version_committed",
    "artifact.snapshot_committed",
    "run.completed",
]


def _blocked_external_io(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("productization fixtures must not access the network")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _blocked_external_io)
    monkeypatch.setattr(socket.socket, "connect", _blocked_external_io)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_external_io)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_external_io)
    monkeypatch.setattr(socket, "gethostbyname", _blocked_external_io)
    monkeypatch.setattr(socket, "gethostbyname_ex", _blocked_external_io)
    monkeypatch.setattr(asyncio, "open_connection", _blocked_external_io)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_external_io)


def _fixture_path(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        raise ValueError("fixture paths must be relative")
    resolved = (FIXTURE_ROOT / candidate).resolve()
    if not resolved.is_relative_to(FIXTURE_ROOT.resolve()):
        raise ValueError("fixture path escapes the fixture root")
    return resolved


def _load_json(name: str) -> dict[str, Any]:
    return json.loads(_fixture_path(name).read_text(encoding="utf-8"))


def _resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    assert reference.startswith("#/"), f"external schema reference is forbidden: {reference}"
    node: Any = root_schema
    for segment in reference[2:].split("/"):
        node = node[segment.replace("~1", "/").replace("~0", "~")]
    return node


SCHEMA_KEYWORDS = {
    "$schema", "$id", "$defs", "$ref", "title", "type", "additionalProperties",
    "required", "properties", "const", "enum", "pattern", "minItems", "maxItems",
    "maxProperties", "items", "oneOf",
}


def _assert_supported_schema_keywords(schema: dict[str, Any], path: str = "$schema") -> None:
    assert not (set(schema) - SCHEMA_KEYWORDS), f"{path}: unsupported schema keywords {set(schema) - SCHEMA_KEYWORDS}"
    for map_key in ("properties", "$defs"):
        for key, child in schema.get(map_key, {}).items():
            _assert_supported_schema_keywords(child, f"{path}.{map_key}.{key}")
    if isinstance(schema.get("items"), dict):
        _assert_supported_schema_keywords(schema["items"], f"{path}.items")
    for index, child in enumerate(schema.get("oneOf", [])):
        _assert_supported_schema_keywords(child, f"{path}.oneOf[{index}]")
    if isinstance(schema.get("additionalProperties"), dict):
        _assert_supported_schema_keywords(schema["additionalProperties"], f"{path}.additionalProperties")


def _assert_schema(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str = "$",
) -> None:
    unknown_keywords = set(schema) - SCHEMA_KEYWORDS
    assert not unknown_keywords, f"{path}: unsupported schema keywords {unknown_keywords}"
    if "$ref" in schema:
        _assert_schema(value, _resolve_ref(root_schema, schema["$ref"]), root_schema, path)
        return

    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _assert_schema(value, branch, root_schema, path)
            except AssertionError:
                continue
            matches += 1
        assert matches == 1, f"{path}: expected exactly one stage schema match, got {matches}"

    if "const" in schema:
        assert value == schema["const"], f"{path}: expected {schema['const']!r}, got {value!r}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} is not in {schema['enum']!r}"

    type_names = schema.get("type")
    if isinstance(type_names, str):
        type_names = [type_names]
    if type_names:
        matches = {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        assert any(matches.get(name, False) for name in type_names), (
            f"{path}: expected type {type_names!r}, got {type(value).__name__}"
        )

    if isinstance(value, str) and "pattern" in schema:
        assert re.search(schema["pattern"], value), f"{path}: {value!r} does not match {schema['pattern']!r}"
    if isinstance(value, list):
        assert len(value) >= schema.get("minItems", 0), f"{path}: list is too short"
        assert len(value) <= schema.get("maxItems", len(value)), f"{path}: list is too long"
        if "items" in schema:
            for index, item in enumerate(value):
                _assert_schema(item, schema["items"], root_schema, f"{path}[{index}]")
    if isinstance(value, dict):
        assert len(value) <= schema.get("maxProperties", len(value)), f"{path}: object has too many keys"
        for required in schema.get("required", []):
            assert required in value, f"{path}: missing required key {required!r}"
        properties = schema.get("properties", {})
        extra_keys = set(value) - set(properties)
        additional_schema = schema.get("additionalProperties")
        if additional_schema is False:
            assert not extra_keys, f"{path}: unexpected keys {extra_keys}"
        elif isinstance(additional_schema, dict):
            for key in extra_keys:
                _assert_schema(value[key], additional_schema, root_schema, f"{path}.{key}")
        for key, child_schema in properties.items():
            if key in value:
                _assert_schema(value[key], child_schema, root_schema, f"{path}.{key}")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _sources_by_canonical(fixture: dict[str, Any], phase: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for source in fixture["sources"][phase]:
        result.setdefault(source["canonical_id"], []).append(source)
    return result


@pytest.mark.parametrize("fixture_name", [G0_NAME, G1_NAME])
def test_fixtures_match_the_offline_v01_schema(fixture_name: str) -> None:
    schema = _load_json(SCHEMA_NAME)
    fixture = _load_json(fixture_name)

    _assert_supported_schema_keywords(schema)
    _assert_schema(fixture, schema, schema)
    assert fixture["schema_version"] == "0.1"
    assert fixture["clock"]["tick_seconds"] == 1
    assert fixture["run"]["run_id"] == fixture["identities"]["run_id"]
    assert fixture["workspace"]["workspace_id"] == fixture["identities"]["workspace_id"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_types"),
    [(G0_NAME, G0_EVENT_TYPES), (G1_NAME, G1_EVENT_TYPES)],
)
def test_durable_events_have_fixed_strict_sequences(fixture_name: str, expected_types: list[str]) -> None:
    fixture = _load_json(fixture_name)
    events = fixture["events"]

    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events] == expected_types
    assert len({event["id"] for event in events}) == len(events)
    assert {event["run_id"] for event in events} == {fixture["identities"]["run_id"]}
    assert {event["durability"] for event in events} == {"durable"}

    started_at = datetime.fromisoformat(fixture["clock"]["frozen_at"].replace("Z", "+00:00"))
    assert started_at.tzinfo == timezone.utc
    assert [datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")) for event in events] == [
        started_at + timedelta(seconds=index) for index in range(len(events))
    ]


def test_g0_blocks_the_mismatched_source_before_mutation() -> None:
    fixture = _load_json(G0_NAME)
    decision = fixture["decisions"][0]
    candidates = _sources_by_canonical(fixture, "candidates")

    assert fixture["run"]["state"] == "waiting_for_decision"
    assert fixture["source_intent"]["title_canonical_id"] == "arxiv:2606.04527"
    assert fixture["source_intent"]["url_canonical_id"] == "arxiv:2607.07675"
    assert decision == {
        "decision_id": "decision-source-001",
        "kind": "source_conflict",
        "status": "pending",
        "revision": 1,
        "options": ["keep_both", "replace_url_with_echo", "cancel"],
        "selected": None,
        "created_at": "2026-07-15T20:00:04Z",
        "resolved_at": None,
    }
    assert fixture["sources"]["initial"] == fixture["sources"]["final"]
    assert fixture["mutation_manifest"] == {
        "scope": "research_import_materialization_side_effects",
        "excludes_control_entities": ["run", "attempt", "decision", "event", "checkpoint"],
        "transactions": [],
        "materializations": [],
    }
    assert fixture["replay"] == {
        "idempotency_key": "golden-g0-source-conflict-001",
        "expected_additional_events": [],
        "expected_additional_mutations": [],
    }
    assert fixture["expected"]["echo_import_delta"] == {
        "source_rows": 0, "chunks": 0, "directories": 0, "materializations": 0,
    }
    assert candidates["arxiv:2607.07675"][0]["official_title"] == SECONDARY_TITLE
    assert candidates["arxiv:2607.07675"][0]["aliases"] == [
        {"alias_id": "alias-lingbot-video", "kind": "model", "value": "LingBot-Video"}
    ]


def test_g1_keeps_both_without_duplicating_echo_and_reuses_lineage() -> None:
    fixture = _load_json(G1_NAME)
    initial_sources = _sources_by_canonical(fixture, "initial")
    final_sources = _sources_by_canonical(fixture, "final")
    decisions = {decision["kind"]: decision for decision in fixture["decisions"]}

    assert decisions["source_conflict"]["selected"] == "keep_both"
    assert decisions["prior_lineage_disposition"]["selected"] == "create_successor"
    assert len(initial_sources["arxiv:2606.04527"]) == len(final_sources["arxiv:2606.04527"]) == 1
    assert initial_sources["arxiv:2606.04527"] == final_sources["arxiv:2606.04527"]
    assert "arxiv:2607.07675" not in initial_sources
    assert len(final_sources["arxiv:2607.07675"]) == 1
    assert final_sources["arxiv:2607.07675"][0]["official_title"] == SECONDARY_TITLE
    assert final_sources["arxiv:2607.07675"][0]["official_title"] != "LingBot-Video"
    assert final_sources["arxiv:2607.07675"][0]["aliases"][0]["value"] == "LingBot-Video"
    assert fixture["sources"]["run_bindings"] == [
        {"binding_id": "binding-run-echo", "run_id": "run-golden-001", "source_id": "source-echo", "disposition": "reused", "bound_at_sequence": 8},
        {"binding_id": "binding-run-lingbot", "run_id": "run-golden-001", "source_id": "source-lingbot", "disposition": "imported", "bound_at_sequence": 9},
    ]
    reused_event = next(event for event in fixture["events"] if event["type"] == "source.reused")
    assert reused_event["payload"] == {
        "source_id": "source-echo", "binding_id": "binding-run-echo", "canonical_id": "arxiv:2606.04527",
    }

    initial_lineage = {node["node_id"]: node for node in fixture["lineage"]["initial_nodes"]}
    final_lineage = {node["node_id"]: node for node in fixture["lineage"]["final_nodes"]}
    assert {node["status"] for node in initial_lineage.values()} == {"dormant", "graduated"}
    assert all(final_lineage[node_id] == node for node_id, node in initial_lineage.items())
    assert final_lineage["lineage-helios-echo-successor"]["status"] == "active"
    assert {link["to_node_id"] for link in fixture["lineage"]["links"]} == set(initial_lineage)
    assert {link["relation"] for link in fixture["lineage"]["links"]} == {"successor_reuses"}
    retrieval = fixture["lineage"]["retrieval"]
    assert retrieval["strategy"] == "independent_lineage_retrieval"
    assert {item["status"] for item in retrieval["retrieved"]} == {"dormant", "graduated"}
    assert all(item["in_live_seed_dedup_domain"] is False for item in retrieval["retrieved"])
    assert "graduated" not in retrieval["live_seed_dedup_statuses"]


def test_g1_has_the_exact_post_resolution_mutation_manifest() -> None:
    fixture = _load_json(G1_NAME)
    transactions = fixture["mutation_manifest"]["transactions"]
    source_resolution_sequence = next(
        event["sequence"]
        for event in fixture["events"]
        if event["type"] == "decision.resolved" and event["payload"]["decision_id"] == "decision-source-001"
    )
    flattened = [
        (transaction["transaction_id"], mutation["op"], mutation["entity"], mutation["entity_id"], mutation["count"])
        for transaction in transactions
        for mutation in transaction["mutations"]
    ]

    assert source_resolution_sequence == 7
    assert all(transaction["committed_at_sequence"] > source_resolution_sequence for transaction in transactions)
    assert fixture["mutation_manifest"]["scope"] == "research_import_materialization_side_effects"
    assert flattened == [
        ("transaction-source-reuse-001", "insert", "run_source_binding", "binding-run-echo", 1),
        ("transaction-source-import-001", "insert", "source", "source-lingbot", 1),
        ("transaction-source-import-001", "insert", "source_alias", "alias-lingbot-video", 1),
        ("transaction-source-import-001", "insert", "run_source_binding", "binding-run-lingbot", 1),
        ("transaction-lineage-001", "insert", "lineage_node", "lineage-helios-echo-successor", 1),
        ("transaction-lineage-001", "insert", "lineage_link", "link-successor-dormant", 1),
        ("transaction-lineage-001", "insert", "lineage_link", "link-successor-graduated", 1),
        ("transaction-thread-evidence-001", "insert", "thread", "thread-evidence-001", 1),
        ("transaction-thread-architecture-001", "insert", "thread", "thread-architecture-001", 1),
        ("transaction-thread-training-001", "insert", "thread", "thread-training-001", 1),
        ("transaction-artifact-living-001", "insert", "evidence", "evidence-echo-001", 1),
        ("transaction-artifact-living-001", "insert", "evidence", "evidence-lingbot-001", 1),
        ("transaction-artifact-living-001", "insert", "artifact", "artifact-living-brief-001", 1),
        ("transaction-artifact-living-001", "insert", "artifact_version", "version-living-001", 1),
        ("transaction-artifact-living-002", "update", "artifact", "artifact-living-brief-001", 1),
        ("transaction-artifact-living-002", "insert", "artifact_version", "version-living-002", 1),
        ("transaction-artifact-evidence-001", "insert", "artifact", "artifact-evidence-matrix-001", 1),
        ("transaction-artifact-evidence-001", "insert", "artifact_version", "version-evidence-001", 1),
        ("transaction-artifact-training-001", "insert", "artifact", "artifact-training-plan-001", 1),
        ("transaction-artifact-training-001", "insert", "artifact_version", "version-training-001", 1),
        ("transaction-artifact-snapshot-001", "insert", "artifact", "artifact-snapshot-001", 1),
        ("transaction-artifact-snapshot-001", "insert", "artifact_version", "version-snapshot-001", 1),
    ]
    assert not any(mutation[3] == "source-echo" for mutation in flattened)
    assert fixture["expected"]["echo_import_delta"] == {
        "source_rows": 0, "chunks": 0, "directories": 0, "materializations": 0,
    }
    assert not any(item["resource_uri"].startswith("cortex://readings/echo") for item in fixture["mutation_manifest"]["materializations"])


def test_g1_references_are_closed_and_snapshot_is_immutable() -> None:
    fixture = _load_json(G1_NAME)
    workspace_id = fixture["workspace"]["workspace_id"]
    thread_ids = {thread["thread_id"] for thread in fixture["threads"]}
    source_ids = {source["source_id"] for source in fixture["sources"]["final"]}
    lineage_ids = {node["node_id"] for node in fixture["lineage"]["final_nodes"]}
    evidence_ids = {evidence["evidence_id"] for evidence in fixture["evidence"]}
    version_ids = {
        version["version_id"]
        for artifact in fixture["artifacts"]
        for version in artifact["versions"]
    }

    assert len(fixture["threads"]) == 3
    assert {thread["kind"] for thread in fixture["threads"]} == {"evidence", "architecture", "training_plan"}
    assert {thread["workspace_id"] for thread in fixture["threads"]} == {workspace_id}
    assert thread_ids == set(fixture["identities"]["thread_ids"])
    assert fixture["expected"]["memory_variants"] == [
        "token_kv",
        "ttt_weight_space",
        "hybrid",
        "cache_no_new_memory",
    ]
    assert {artifact["kind"] for artifact in fixture["artifacts"]} == {
        "living_brief",
        "evidence_matrix",
        "training_plan",
        "snapshot",
    }
    for evidence in fixture["evidence"]:
        assert evidence["thread_id"] in thread_ids
        assert evidence["source_id"] in source_ids
    for artifact in fixture["artifacts"]:
        assert artifact["thread_id"] in thread_ids
        assert set(artifact["references"]["source_ids"]) <= source_ids
        assert set(artifact["references"]["evidence_ids"]) <= evidence_ids
        assert set(artifact["references"]["lineage_node_ids"]) <= lineage_ids
        assert set(artifact["references"]["artifact_version_ids"]) <= version_ids

    snapshot = next(artifact for artifact in fixture["artifacts"] if artifact["kind"] == "snapshot")
    assert snapshot["mutable"] is False
    assert snapshot["references"]["artifact_version_ids"] == [
        "version-living-002",
        "version-evidence-001",
        "version-training-001",
    ]
    assert snapshot["versions"] == [
        {
            "version_id": "version-snapshot-001",
            "revision": 1,
            "resource_uri": "cortex://artifacts/golden/snapshot.md",
            "sha256": "1c6148bfba7d0301a468d32174d28ed2481159d53fa6ffbc1d95176ceee16c5d",
            "created_at": "2026-07-15T20:10:23Z",
            "immutable": True,
        }
    ]


def test_g1_artifact_hashes_and_materializations_are_exact() -> None:
    fixture = _load_json(G1_NAME)
    version_uris = [
        version["resource_uri"]
        for artifact in fixture["artifacts"]
        for version in artifact["versions"]
    ]
    materialization_uris = [
        item["resource_uri"] for item in fixture["mutation_manifest"]["materializations"]
    ]
    assert len(version_uris) == len(set(version_uris))
    assert len(materialization_uris) == len(set(materialization_uris))
    versions = {
        version["resource_uri"]: version
        for artifact in fixture["artifacts"]
        for version in artifact["versions"]
    }
    materializations = {
        item["resource_uri"]: item for item in fixture["mutation_manifest"]["materializations"]
    }

    assert set(fixture["blobs"]) == set(versions) == set(materializations)
    for resource_uri, relative_path in fixture["blobs"].items():
        content = _fixture_path(relative_path).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        assert digest == versions[resource_uri]["sha256"]
        assert digest == materializations[resource_uri]["sha256"]
        assert versions[resource_uri]["immutable"] is True


def test_g1_idempotent_replay_and_restart_expect_no_duplicates() -> None:
    fixture = _load_json(G1_NAME)
    replay = fixture["replay"]
    transaction_ids = [item["transaction_id"] for item in fixture["mutation_manifest"]["transactions"]]
    restart = replay["restart_recovery"]

    assert replay["idempotency"] == {
        "key": "golden-g1-keep-both-successor-001",
        "first_transaction_ids": transaction_ids,
        "repeated_response_matches_first": True,
        "expected_additional_events": [],
        "expected_additional_mutations": [],
        "expected_additional_materializations": [],
    }
    assert restart["checkpoint_at_sequence"] == restart["after_sequence"] == 15
    assert restart["expected_replayed_sequences"] == list(range(16, 26))
    assert restart["expected_duplicate_event_ids"] == []
    assert restart["expected_duplicate_entity_ids"] == []
    assert {event["attempt_id"] for event in fixture["events"][:15]} == {restart["source_attempt_id"]}
    assert {event["attempt_id"] for event in fixture["events"][15:]} == {restart["recovered_attempt_id"]}


def test_checkpoint_state_is_committed_before_recovery() -> None:
    fixture = _load_json(G1_NAME)
    checkpoint = fixture["checkpoints"][0]
    canonical_state = json.dumps(
        checkpoint["state"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    checkpoint_event = next(event for event in fixture["events"] if event["type"] == "checkpoint.committed")
    recovery_event = next(event for event in fixture["events"] if event["type"] == "run.recovered")
    checkpoint_transaction = next(
        transaction
        for transaction in fixture["control_state_manifest"]["transactions"]
        if transaction["transaction_id"] == "transaction-checkpoint-001"
    )

    assert hashlib.sha256(canonical_state).hexdigest() == checkpoint["state_sha256"]
    assert checkpoint["committed_at_sequence"] == checkpoint_event["sequence"] == 15
    assert checkpoint_event["payload"]["state_sha256"] == checkpoint["state_sha256"]
    assert checkpoint_transaction["committed_at_sequence"] == checkpoint_event["sequence"]
    assert checkpoint_transaction["mutations"] == [
        {"op": "insert", "entity": "checkpoint", "entity_id": checkpoint["checkpoint_id"], "count": 1}
    ]
    assert recovery_event["sequence"] > checkpoint_event["sequence"]
    assert recovery_event["payload"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert recovery_event["payload"]["state_sha256"] == checkpoint["state_sha256"]
    assert "checkpoint" in fixture["mutation_manifest"]["excludes_control_entities"]


@pytest.mark.parametrize("fixture_name", [G0_NAME, G1_NAME])
def test_state_events_share_their_declared_commit_boundary(fixture_name: str) -> None:
    fixture = _load_json(fixture_name)
    transactions = [
        *fixture["mutation_manifest"]["transactions"],
        *fixture["control_state_manifest"]["transactions"],
    ]
    commits_by_entity: dict[str, list[int]] = {}
    for transaction in transactions:
        for mutation in transaction["mutations"]:
            commits_by_entity.setdefault(mutation["entity_id"], []).append(transaction["committed_at_sequence"])

    for event in fixture["events"]:
        target_ids: list[str] = []
        if event["type"] in {
            "run.created", "run.started", "run.waiting_for_decision",
            "run.recovered", "run.completed",
        }:
            target_ids.append(event["run_id"])
        if event["type"] in {"run.created", "run.started", "run.recovered", "run.completed"}:
            target_ids.append(event["attempt_id"])
        if event["type"] in {"decision.requested", "decision.resolved"}:
            target_ids.append(event["payload"]["decision_id"])
        if event["type"] in {"source.intent_received", "source.conflict_detected"}:
            target_ids.append(event["payload"]["source_intent_id"])
        for key in {
            "source.reused": ("binding_id",),
            "source.imported": ("source_id", "binding_id"),
            "lineage.successor_created": ("node_id",),
            "checkpoint.committed": ("checkpoint_id",),
            "thread.created": ("thread_id",),
            "artifact.version_committed": ("artifact_id", "version_id"),
            "artifact.snapshot_committed": ("artifact_id", "version_id"),
        }.get(event["type"], ()):
            target_ids.append(event["payload"][key])
        for target_id in target_ids:
            assert event["sequence"] in commits_by_entity[target_id]

    if fixture_name == G0_NAME:
        return

    version_uri = {
        version["version_id"]: version["resource_uri"]
        for artifact in fixture["artifacts"]
        for version in artifact["versions"]
    }
    materialization_sequence = {
        item["resource_uri"]: item["committed_at_sequence"]
        for item in fixture["mutation_manifest"]["materializations"]
    }
    for event in fixture["events"]:
        if event["type"] in {"artifact.version_committed", "artifact.snapshot_committed"}:
            assert materialization_sequence[version_uri[event["payload"]["version_id"]]] == event["sequence"]


@pytest.mark.parametrize("fixture_name", [G0_NAME, G1_NAME])
def test_global_identity_registry_and_all_references_are_closed(fixture_name: str) -> None:
    fixture = _load_json(fixture_name)
    registry: dict[str, str] = {}

    def define(kind: str, identity: str) -> None:
        assert identity not in registry, f"duplicate identity definition: {identity}"
        registry[identity] = kind

    define("workspace", fixture["workspace"]["workspace_id"])
    for run_id in fixture["control_state_manifest"]["persisted_run_ids"]:
        define("run", run_id)
    for attempt_id in fixture["control_state_manifest"]["persisted_attempt_ids"]:
        define("attempt", attempt_id)
    define("source_intent", fixture["source_intent"]["source_intent_id"])
    for decision in fixture["decisions"]:
        define("decision", decision["decision_id"])
    candidate_sources = fixture["sources"]["candidates"]
    assert len({source["canonical_id"] for source in candidate_sources}) == len(candidate_sources)
    for source in candidate_sources:
        define("source", source["source_id"])
        for alias in source["aliases"]:
            define("source_alias", alias["alias_id"])
    for binding in fixture["sources"]["run_bindings"]:
        define("run_source_binding", binding["binding_id"])
    for node in fixture["lineage"]["final_nodes"]:
        define("lineage_node", node["node_id"])
    for link in fixture["lineage"]["links"]:
        define("lineage_link", link["link_id"])
    for checkpoint in fixture["checkpoints"]:
        define("checkpoint", checkpoint["checkpoint_id"])
    for thread in fixture["threads"]:
        define("thread", thread["thread_id"])
    for evidence in fixture["evidence"]:
        define("evidence", evidence["evidence_id"])
    uris: set[str] = set()
    for artifact in fixture["artifacts"]:
        define("artifact", artifact["artifact_id"])
        for version in artifact["versions"]:
            define("artifact_version", version["version_id"])
            assert version["resource_uri"] not in uris, f"duplicate URI definition: {version['resource_uri']}"
            uris.add(version["resource_uri"])
    for event in fixture["events"]:
        define("event", event["id"])
    all_transactions = [*fixture["mutation_manifest"]["transactions"], *fixture["control_state_manifest"]["transactions"]]
    for transaction in all_transactions:
        define("transaction", transaction["transaction_id"])

    assert fixture["identities"]["workspace_id"] == fixture["workspace"]["workspace_id"]
    assert fixture["identities"]["run_id"] == fixture["run"]["run_id"]
    assert fixture["identities"]["attempt_id"] == fixture["run"]["attempt_id"]
    assert set(fixture["identities"]["thread_ids"]) == {
        thread["thread_id"] for thread in fixture["threads"]
    }
    assert registry[fixture["run"]["run_id"]] == "run"
    assert registry[fixture["run"]["attempt_id"]] == "attempt"
    assert set(fixture["control_state_manifest"]["persisted_run_ids"]) == {
        fixture["run"]["run_id"]
    }
    assert set(fixture["control_state_manifest"]["persisted_attempt_ids"]) == {
        event["attempt_id"] for event in fixture["events"]
    }
    assert fixture["control_state_manifest"]["persisted_source_intent_ids"] == [
        fixture["source_intent"]["source_intent_id"]
    ]
    assert set(fixture["control_state_manifest"]["persisted_event_ids"]) == {
        event["id"] for event in fixture["events"]
    }
    assert set(fixture["control_state_manifest"]["persisted_decision_ids"]) == {
        decision["decision_id"] for decision in fixture["decisions"]
    }
    for phase in ("initial", "final"):
        for source in fixture["sources"][phase]:
            assert registry[source["source_id"]] == "source"
            for alias in source["aliases"]:
                assert registry[alias["alias_id"]] == "source_alias"
    for binding in fixture["sources"]["run_bindings"]:
        assert registry[binding["run_id"]] == "run"
        assert registry[binding["source_id"]] == "source"
    for node in fixture["lineage"]["initial_nodes"]:
        assert registry[node["node_id"]] == "lineage_node"
    for link in fixture["lineage"]["links"]:
        assert registry[link["from_node_id"]] == registry[link["to_node_id"]] == "lineage_node"
    retrieval = fixture["lineage"]["retrieval"]
    if retrieval is not None:
        for item in retrieval["retrieved"]:
            assert registry[item["node_id"]] == "lineage_node"
    for thread in fixture["threads"]:
        assert registry[thread["workspace_id"]] == "workspace"
    for evidence in fixture["evidence"]:
        assert registry[evidence["thread_id"]] == "thread"
        assert registry[evidence["source_id"]] == "source"
    for artifact in fixture["artifacts"]:
        assert registry[artifact["thread_id"]] == "thread"
        for source_id in artifact["references"]["source_ids"]:
            assert registry[source_id] == "source"
        for evidence_id in artifact["references"]["evidence_ids"]:
            assert registry[evidence_id] == "evidence"
        for node_id in artifact["references"]["lineage_node_ids"]:
            assert registry[node_id] == "lineage_node"
        for version_id in artifact["references"]["artifact_version_ids"]:
            assert registry[version_id] == "artifact_version"
    for transaction in all_transactions:
        assert transaction["triggered_by"] in registry
        for mutation in transaction["mutations"]:
            assert mutation["entity_id"] in registry
    for materialization in fixture["mutation_manifest"]["materializations"]:
        assert materialization["resource_uri"] in uris
    assert set(fixture["blobs"]) == uris

    decisions = {item["decision_id"]: item for item in fixture["decisions"]}
    event_sequences = {event["id"]: event["sequence"] for event in fixture["events"]}
    for event in fixture["events"]:
        assert registry[event["run_id"]] == "run"
        assert registry[event["attempt_id"]] == "attempt"
        if "causation_id" in event:
            assert registry[event["causation_id"]] == "event"
            assert event_sequences[event["causation_id"]] < event["sequence"]
        payload = event["payload"]
        for key, expected_kind in {
            "decision_id": "decision", "thread_id": "thread", "artifact_id": "artifact",
            "checkpoint_id": "checkpoint", "source_id": "source", "binding_id": "run_source_binding",
            "node_id": "lineage_node", "version_id": "artifact_version",
            "snapshot_version_id": "artifact_version", "workspace_id": "workspace",
            "source_attempt_id": "attempt", "source_intent_id": "source_intent",
        }.items():
            if key in payload:
                assert registry[payload[key]] == expected_kind
        if "decision_id" in payload and "revision" in payload:
            final_revision = decisions[payload["decision_id"]]["revision"]
            assert payload["revision"] <= final_revision
            if event["type"] == "decision.resolved":
                assert payload["revision"] == final_revision
        for key, expected_kind in {
            "candidate_source_ids": "source", "node_ids": "lineage_node",
        }.items():
            for identity in payload.get(key, []):
                assert registry[identity] == expected_kind

    for checkpoint in fixture["checkpoints"]:
        assert registry[checkpoint["run_id"]] == "run"
        assert registry[checkpoint["attempt_id"]] == "attempt"
        state = checkpoint["state"]
        for decision_id in state["decision_revisions"]:
            assert registry[decision_id] == "decision"
        for node_id in state["lineage_node_ids"]:
            assert registry[node_id] == "lineage_node"
        for binding_id in state["run_source_binding_ids"]:
            assert registry[binding_id] == "run_source_binding"

    replay = fixture["replay"]
    if fixture_name == G1_NAME:
        restart = replay["restart_recovery"]
        assert registry[restart["checkpoint_id"]] == "checkpoint"
        assert registry[restart["source_attempt_id"]] == "attempt"
        assert registry[restart["recovered_attempt_id"]] == "attempt"
        for transaction_id in replay["idempotency"]["first_transaction_ids"]:
            assert registry[transaction_id] == "transaction"


def _assert_valid_cortex_uri(uri: str) -> None:
    assert uri.startswith("cortex://"), f"scheme or case mismatch: {uri}"
    assert not re.search(r"%(?:2f|5c)", uri, re.IGNORECASE), f"encoded separator: {uri}"
    raw_authority = uri[len("cortex://"):].split("/", 1)[0]
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,62}", raw_authority), (
        f"invalid root authority: {uri}"
    )
    parsed = urlsplit(uri)
    assert parsed.scheme == "cortex"
    assert parsed.username is None and parsed.password is None
    assert parsed.port is None
    assert parsed.query == "" and parsed.fragment == ""
    assert parsed.hostname == raw_authority
    segments = parsed.path.split("/")[1:]
    assert segments and all(segments)
    for segment in segments:
        assert not re.search(r"%(?![0-9A-Fa-f]{2})", segment), f"invalid percent escape: {uri}"
        decoded = unquote_to_bytes(segment).decode("utf-8", errors="strict")
        assert unicodedata.normalize("NFC", decoded) == decoded, f"non-NFC path: {uri}"
        assert decoded not in {".", ".."}
        assert "/" not in decoded and "\\" not in decoded and "\x00" not in decoded


def test_all_cortex_resource_uris_follow_the_v01_grammar() -> None:
    for fixture_name in (SCHEMA_NAME, G0_NAME, G1_NAME):
        for value in _walk_strings(_load_json(fixture_name)):
            if value.startswith("cortex://"):
                _assert_valid_cortex_uri(value)


@pytest.mark.parametrize("uri", [
    "cortex://Artifacts/file", "cortex://artifacts", "cortex://artifacts//file",
    "cortex://artifacts/./file", "cortex://artifacts/%2e%2e/file",
    "cortex://artifacts/a%2Fb", "cortex://artifacts/a%5Cb",
    "cortex://user@artifacts/file", "cortex://artifacts:9/file",
    "cortex://artifacts/file?q=1", "cortex://artifacts/file#fragment",
    "cortex://artifacts/%ZZ", "cortex://artifacts/%C0%AF",
    "cortex://artifacts/e\u0301.md",
])
def test_invalid_cortex_resource_uris_fail_closed(uri: str) -> None:
    with pytest.raises((AssertionError, ValueError)):
        _assert_valid_cortex_uri(uri)


def test_fixture_tree_contains_no_personal_or_production_paths() -> None:
    forbidden_fragments = (
        "/Users/",
        "/home/",
        "~/.local/",
        "file://",
        "research.db",
        "agent-readings",
        "C:\\",
    )
    text_files = [path for path in FIXTURE_ROOT.rglob("*") if path.is_file()]
    assert text_files
    for path in text_files:
        content = path.read_text(encoding="utf-8")
        assert not any(fragment in content for fragment in forbidden_fragments), path
    for fixture_name in (G0_NAME, G1_NAME):
        fixture = _load_json(fixture_name)
        assert all(not Path(relative_path).is_absolute() for relative_path in fixture["blobs"].values())


def test_fixture_loader_fails_closed_on_external_access() -> None:
    with pytest.raises(ValueError, match="must be relative"):
        _load_json("/Users/example/production.json")
    with pytest.raises(ValueError, match="escapes"):
        _load_json("../../production.json")
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.gethostbyname("example.invalid")
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.socket().connect_ex(("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="must not access the network"):
        asyncio.open_connection("example.invalid", 443)
    with pytest.raises(AssertionError, match="must not access the network"):
        urllib.request.urlopen("https://example.invalid")


def test_living_brief_evolves_without_mutating_the_snapshot() -> None:
    fixture = _load_json(G1_NAME)
    living = next(artifact for artifact in fixture["artifacts"] if artifact["kind"] == "living_brief")
    snapshot = next(artifact for artifact in fixture["artifacts"] if artifact["kind"] == "snapshot")
    living_versions = living["versions"]

    assert living["mutable"] is True
    assert [version["revision"] for version in living_versions] == [1, 2]
    assert [version["version_id"] for version in living_versions] == [
        "version-living-001",
        "version-living-002",
    ]
    assert len({version["sha256"] for version in living_versions}) == 2
    assert snapshot["mutable"] is False
    assert snapshot["references"]["artifact_version_ids"][0] == "version-living-002"

    snapshot_mutations = [
        mutation
        for transaction in fixture["mutation_manifest"]["transactions"]
        for mutation in transaction["mutations"]
        if mutation["entity_id"] in {snapshot["artifact_id"], "version-snapshot-001"}
    ]
    assert snapshot_mutations == [
        {"op": "insert", "entity": "artifact", "entity_id": "artifact-snapshot-001", "count": 1},
        {"op": "insert", "entity": "artifact_version", "entity_id": "version-snapshot-001", "count": 1},
    ]


def test_fixture_copy_cannot_mutate_the_canonical_snapshot() -> None:
    fixture = _load_json(G1_NAME)
    original = _load_json(G1_NAME)
    mutated = deepcopy(fixture)
    snapshot = next(artifact for artifact in mutated["artifacts"] if artifact["kind"] == "snapshot")
    snapshot["versions"][0]["sha256"] = "0" * 64

    assert mutated != original
    assert _load_json(G1_NAME) == original
