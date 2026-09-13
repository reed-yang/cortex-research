"""Typed, immutable, path-free contracts for Cortex research workflows."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, ClassVar

from cortex_platform.product.sources.models import (
    CandidateObservation,
    validate_engine_ref,
)

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_OPERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_STAGE_EFFECTS = frozenset(
    {"control", "decision", "engine_query", "engine_mutation", "runtime", "artifact"}
)
_LINEAGE_STATUSES = frozenset(
    {"awaiting_human", "incubating", "dormant", "graduated", "killed", "active"}
)
_SOURCE_DISPOSITIONS = frozenset({"reused", "imported"})
_RECONCILIATION_DISPOSITIONS = frozenset({"committed", "not_found", "unknown"})
_MUTATION_DOMAINS = frozenset(
    {
        "source_binding",
        "source_import",
        "lineage_query",
        "successor_creation",
        "runtime_stage",
        "artifact_workflow",
    }
)
_ENGINE_KIND_PREFIX = {"source": "paper", "lineage": "idea"}
_ARTIFACT_EFFECT_KINDS = frozenset(
    {
        "artifact_evidence",
        "artifact_living",
        "artifact_training",
        "artifact_snapshot",
    }
)
_MAX_ARTIFACT_VERSIONS_PER_EFFECT = 500


def _closed(raw: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError(f"{name} fields do not match schema")
    return raw


def _schema_v1(raw: Mapping[str, object], name: str) -> None:
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError(f"unsupported {name} schema")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical identifier")
    return value


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or _OPERATION_RE.fullmatch(value) is None:
        raise ValueError("operation_id must be a stable identifier")
    return value


def _delivery_epoch(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("delivery_epoch must be a positive integer")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _text(value: object, name: str, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        value != normalized
        or not value
        or len(value) > maximum
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError(f"{name} is not canonical safe text")
    return value


def _identifiers(
    values: Sequence[str],
    name: str,
    *,
    allow_empty: bool,
    canonical_order: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of identifiers")
    normalized = tuple(_identifier(value, name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must not contain duplicates")
    if canonical_order and tuple(sorted(normalized)) != normalized:
        raise ValueError(f"{name} must use canonical order")
    return normalized


def _identity_hash(domain: str, payload: Mapping[str, Any]) -> str:
    document = {
        "schema_version": 1,
        "domain": _identifier(domain, "wire domain"),
        "payload": dict(payload),
    }
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("wire document is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _canonical_source_id(value: object) -> str:
    value = _text(value, "canonical_id", maximum=512)
    authority, separator, authority_id = value.partition(":")
    if not separator or authority not in {"arxiv", "doi", "sha256"}:
        raise ValueError("canonical_id is not a supported source identity")
    observation = CandidateObservation(
        claim_kind={"arxiv": "arxiv", "doi": "doi", "sha256": "local_file"}[
            authority
        ],
        authority=authority,
        authority_id=authority_id,
        official_title="Canonical source identity validation",
    )
    if observation.canonical_id != value:
        raise ValueError("canonical_id is not in canonical form")
    return value


@dataclass(frozen=True)
class StageDefinition:
    """One named stage in an extensible, versioned workflow DAG."""

    key: str
    effect: str
    dependencies: tuple[str, ...]
    required_receipts: tuple[str, ...] = ()
    required_results: tuple[str, ...] = ()
    checkpoint: bool = False

    def __post_init__(self) -> None:
        _identifier(self.key, "stage key")
        if self.effect not in _STAGE_EFFECTS:
            raise ValueError("stage effect is unsupported")
        object.__setattr__(
            self,
            "dependencies",
            _identifiers(
                self.dependencies,
                "stage dependencies",
                allow_empty=True,
                canonical_order=False,
            ),
        )
        object.__setattr__(
            self,
            "required_receipts",
            _identifiers(
                self.required_receipts,
                "required receipts",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "required_results",
            _identifiers(
                self.required_results,
                "required results",
                allow_empty=True,
            ),
        )
        if type(self.checkpoint) is not bool:
            raise ValueError("checkpoint must be boolean")
        if self.effect in {"engine_mutation", "runtime", "artifact"}:
            if not self.required_receipts or self.required_results:
                raise ValueError("mutating stages require receipts only")
        elif self.effect == "engine_query":
            if not self.required_results or self.required_receipts:
                raise ValueError("engine query stages require durable results only")
        elif self.required_receipts or self.required_results:
            raise ValueError("control and decision stages cannot require effects")


@dataclass(frozen=True)
class WorkflowDefinition:
    """A versioned workflow whose stages form a deterministic acyclic graph."""

    definition_id: str
    version: int
    stages: tuple[StageDefinition, ...]

    def __post_init__(self) -> None:
        _identifier(self.definition_id, "workflow definition_id")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("workflow version must be a positive integer")
        stages = tuple(self.stages)
        if not stages or not all(isinstance(stage, StageDefinition) for stage in stages):
            raise ValueError("workflow stages must be a non-empty typed sequence")
        object.__setattr__(self, "stages", stages)
        keys = tuple(stage.key for stage in stages)
        if len(keys) != len(set(keys)):
            raise ValueError("workflow stage keys must be unique")
        known: set[str] = set()
        for stage in stages:
            unknown = set(stage.dependencies) - known
            if unknown:
                raise ValueError(
                    f"stage {stage.key} has non-preceding dependencies: {sorted(unknown)!r}"
                )
            known.add(stage.key)

    @property
    def identity(self) -> str:
        return f"{self.definition_id}@{self.version}"

    def stage(self, key: str) -> StageDefinition:
        for stage in self.stages:
            if stage.key == key:
                return stage
        raise KeyError(key)


@dataclass(frozen=True)
class EngineReference:
    """An existing research-engine identity, preserved without remapping."""

    kind: str
    value: str

    FIELDS: ClassVar[set[str]] = {"kind", "value"}

    def __post_init__(self) -> None:
        if self.kind not in _ENGINE_KIND_PREFIX:
            raise ValueError("engine reference kind is unsupported")
        try:
            validate_engine_ref(
                self.value, namespace=_ENGINE_KIND_PREFIX[self.kind]
            )
        except ValueError as exc:
            raise ValueError(
                "engine reference namespace does not match its kind"
            ) from exc

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, raw: object) -> EngineReference:
        data = _closed(raw, cls.FIELDS, "engine reference")
        return cls(kind=data["kind"], value=data["value"])


class _MutationRequest:
    DOMAIN: ClassVar[str]
    operation_id: str
    delivery_epoch: int

    def _payload(self) -> dict[str, object]:
        raise NotImplementedError

    @property
    def request_hash(self) -> str:
        return _identity_hash(
            f"{self.DOMAIN}.request",
            {"operation_id": self.operation_id, **self._payload()},
        )

    def _wire_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "operation_id": self.operation_id,
            "delivery_epoch": self.delivery_epoch,
            "request_hash": self.request_hash,
            **self._payload(),
        }


@dataclass(frozen=True)
class SourceBindingRequest(_MutationRequest):
    operation_id: str
    delivery_epoch: int
    run_id: str
    source_id: str
    canonical_id: str
    disposition: str

    DOMAIN: ClassVar[str] = "source_binding"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "run_id",
        "source_id",
        "canonical_id",
        "disposition",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _identifier(self.run_id, "run_id")
        _identifier(self.source_id, "source_id")
        _canonical_source_id(self.canonical_id)
        if self.disposition not in _SOURCE_DISPOSITIONS:
            raise ValueError("source disposition is unsupported")

    def _payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "canonical_id": self.canonical_id,
            "disposition": self.disposition,
        }

    def to_dict(self) -> dict[str, object]:
        return self._wire_dict()

    @classmethod
    def from_dict(cls, raw: object) -> SourceBindingRequest:
        data = _closed(raw, cls.FIELDS, "source binding request")
        _schema_v1(data, "source binding request")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("source binding request domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            run_id=data["run_id"],
            source_id=data["source_id"],
            canonical_id=data["canonical_id"],
            disposition=data["disposition"],
        )
        if data["request_hash"] != value.request_hash:
            raise ValueError("source binding request hash does not match")
        return value

    def validate_result(self, result: SourceBindingResult) -> None:
        if not isinstance(result, SourceBindingResult):
            raise TypeError("source binding result must be typed")
        if (
            result.operation_id != self.operation_id
            or result.delivery_epoch != self.delivery_epoch
            or result.request_hash != self.request_hash
            or result.source_id != self.source_id
        ):
            raise ValueError("source binding result does not match its request")


@dataclass(frozen=True)
class SourceBindingResult:
    operation_id: str
    delivery_epoch: int
    request_hash: str
    source_id: str
    engine_reference: EngineReference
    replayed: bool = False

    DOMAIN: ClassVar[str] = "source_binding"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "source_id",
        "engine_reference",
        "result_identity",
        "replayed",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _sha256(self.request_hash, "request_hash")
        _identifier(self.source_id, "source_id")
        if not isinstance(self.engine_reference, EngineReference):
            raise TypeError("engine_reference must be typed")
        if self.engine_reference.kind != "source":
            raise ValueError("source binding requires a source engine reference")
        if type(self.replayed) is not bool:
            raise ValueError("replayed must be boolean")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "source_id": self.source_id,
            "engine_reference": self.engine_reference.to_dict(),
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(f"{self.DOMAIN}.result", self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "delivery_epoch": self.delivery_epoch,
            **self._identity_payload(),
            "result_identity": self.result_identity,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SourceBindingResult:
        data = _closed(raw, cls.FIELDS, "source binding result")
        _schema_v1(data, "source binding result")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("source binding result domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            request_hash=data["request_hash"],
            source_id=data["source_id"],
            engine_reference=EngineReference.from_dict(data["engine_reference"]),
            replayed=data["replayed"],
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("source binding result identity does not match")
        return value


@dataclass(frozen=True)
class SourceImportRequest(_MutationRequest):
    operation_id: str
    delivery_epoch: int
    source_id: str
    canonical_id: str

    DOMAIN: ClassVar[str] = "source_import"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "source_id",
        "canonical_id",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _identifier(self.source_id, "source_id")
        _canonical_source_id(self.canonical_id)

    def _payload(self) -> dict[str, object]:
        return {"source_id": self.source_id, "canonical_id": self.canonical_id}

    def to_dict(self) -> dict[str, object]:
        return self._wire_dict()

    @classmethod
    def from_dict(cls, raw: object) -> SourceImportRequest:
        data = _closed(raw, cls.FIELDS, "source import request")
        _schema_v1(data, "source import request")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("source import request domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            source_id=data["source_id"],
            canonical_id=data["canonical_id"],
        )
        if data["request_hash"] != value.request_hash:
            raise ValueError("source import request hash does not match")
        return value

    def validate_result(self, result: SourceImportResult) -> None:
        if not isinstance(result, SourceImportResult):
            raise TypeError("source import result must be typed")
        if (
            result.operation_id != self.operation_id
            or result.delivery_epoch != self.delivery_epoch
            or result.request_hash != self.request_hash
            or result.source_id != self.source_id
        ):
            raise ValueError("source import result does not match its request")


@dataclass(frozen=True)
class SourceImportResult:
    operation_id: str
    delivery_epoch: int
    request_hash: str
    source_id: str
    engine_reference: EngineReference
    manifest: Mapping[str, int]
    replayed: bool = False

    DOMAIN: ClassVar[str] = "source_import"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "source_id",
        "engine_reference",
        "manifest",
        "result_identity",
        "replayed",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _sha256(self.request_hash, "request_hash")
        _identifier(self.source_id, "source_id")
        if not isinstance(self.engine_reference, EngineReference):
            raise TypeError("engine_reference must be typed")
        if self.engine_reference.kind != "source":
            raise ValueError("source import requires a source engine reference")
        expected = {"source_rows", "chunks", "directories"}
        if not isinstance(self.manifest, Mapping) or set(self.manifest) != expected:
            raise ValueError("source import manifest is invalid")
        copied = {key: self.manifest[key] for key in sorted(expected)}
        if any(type(value) is not int or not 0 <= value <= 1_000_000 for value in copied.values()):
            raise ValueError("source import manifest is invalid")
        object.__setattr__(self, "manifest", MappingProxyType(copied))
        if type(self.replayed) is not bool:
            raise ValueError("replayed must be boolean")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "source_id": self.source_id,
            "engine_reference": self.engine_reference.to_dict(),
            "manifest": dict(self.manifest),
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(f"{self.DOMAIN}.result", self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "delivery_epoch": self.delivery_epoch,
            **self._identity_payload(),
            "result_identity": self.result_identity,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SourceImportResult:
        data = _closed(raw, cls.FIELDS, "source import result")
        _schema_v1(data, "source import result")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("source import result domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            request_hash=data["request_hash"],
            source_id=data["source_id"],
            engine_reference=EngineReference.from_dict(data["engine_reference"]),
            manifest=data["manifest"],
            replayed=data["replayed"],
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("source import result identity does not match")
        return value


@dataclass(frozen=True)
class LineageNode:
    node_id: str
    status: str
    title: str
    revision: int
    engine_reference: EngineReference

    FIELDS: ClassVar[set[str]] = {
        "node_id",
        "status",
        "title",
        "revision",
        "engine_reference",
    }

    def __post_init__(self) -> None:
        _identifier(self.node_id, "lineage node_id")
        if self.status not in _LINEAGE_STATUSES:
            raise ValueError("lineage status is unsupported")
        _text(self.title, "lineage title")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("lineage revision must be positive")
        if not isinstance(self.engine_reference, EngineReference):
            raise TypeError("lineage engine_reference must be typed")
        if self.engine_reference.kind != "lineage":
            raise ValueError("lineage nodes require a lineage engine reference")

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "title": self.title,
            "revision": self.revision,
            "engine_reference": self.engine_reference.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> LineageNode:
        data = _closed(raw, cls.FIELDS, "lineage node")
        return cls(
            node_id=data["node_id"],
            status=data["status"],
            title=data["title"],
            revision=data["revision"],
            engine_reference=EngineReference.from_dict(data["engine_reference"]),
        )


@dataclass(frozen=True)
class LineageQueryRequest(_MutationRequest):
    operation_id: str
    delivery_epoch: int
    run_id: str
    source_ids: tuple[str, ...]
    query: str

    DOMAIN: ClassVar[str] = "lineage_query"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "run_id",
        "source_ids",
        "query",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _identifier(self.run_id, "run_id")
        object.__setattr__(
            self,
            "source_ids",
            _identifiers(self.source_ids, "lineage source_ids", allow_empty=False),
        )
        _text(self.query, "lineage query", maximum=4_000)

    def _payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "source_ids": list(self.source_ids),
            "query": self.query,
        }

    def to_dict(self) -> dict[str, object]:
        return self._wire_dict()

    @classmethod
    def from_dict(cls, raw: object) -> LineageQueryRequest:
        data = _closed(raw, cls.FIELDS, "lineage query request")
        _schema_v1(data, "lineage query request")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("lineage query request domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            run_id=data["run_id"],
            source_ids=data["source_ids"],
            query=data["query"],
        )
        if data["request_hash"] != value.request_hash:
            raise ValueError("lineage query request hash does not match")
        return value

    def validate_result(self, result: LineageQueryResult) -> None:
        if not isinstance(result, LineageQueryResult):
            raise TypeError("lineage query result must be typed")
        if (
            result.operation_id != self.operation_id
            or result.delivery_epoch != self.delivery_epoch
            or result.request_hash != self.request_hash
        ):
            raise ValueError("lineage query result does not match its request")


@dataclass(frozen=True)
class LineageQueryResult:
    operation_id: str
    delivery_epoch: int
    request_hash: str
    nodes: tuple[LineageNode, ...]

    DOMAIN: ClassVar[str] = "lineage_query"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "nodes",
        "result_identity",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _sha256(self.request_hash, "request_hash")
        nodes = tuple(self.nodes)
        if not all(isinstance(node, LineageNode) for node in nodes):
            raise TypeError("lineage nodes must be typed")
        node_ids = tuple(node.node_id for node in nodes)
        if len(node_ids) != len(set(node_ids)) or node_ids != tuple(sorted(node_ids)):
            raise ValueError("lineage nodes must be unique and canonically ordered")
        object.__setattr__(self, "nodes", nodes)

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(f"{self.DOMAIN}.result", self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "delivery_epoch": self.delivery_epoch,
            **self._identity_payload(),
            "result_identity": self.result_identity,
        }

    @classmethod
    def from_dict(cls, raw: object) -> LineageQueryResult:
        data = _closed(raw, cls.FIELDS, "lineage query result")
        _schema_v1(data, "lineage query result")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("lineage query result domain is invalid")
        if not isinstance(data["nodes"], list):
            raise TypeError("lineage query nodes must be a list")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            request_hash=data["request_hash"],
            nodes=tuple(LineageNode.from_dict(node) for node in data["nodes"]),
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("lineage query result identity does not match")
        return value


@dataclass(frozen=True)
class LineageLink:
    link_id: str
    from_node_id: str
    to_node_id: str
    relation: str

    FIELDS: ClassVar[set[str]] = {
        "link_id",
        "from_node_id",
        "to_node_id",
        "relation",
    }

    def __post_init__(self) -> None:
        _identifier(self.link_id, "lineage link_id")
        _identifier(self.from_node_id, "lineage from_node_id")
        _identifier(self.to_node_id, "lineage to_node_id")
        _identifier(self.relation, "lineage relation")
        if self.from_node_id == self.to_node_id:
            raise ValueError("lineage links cannot self-reference")

    def to_dict(self) -> dict[str, object]:
        return {
            "link_id": self.link_id,
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "relation": self.relation,
        }

    @classmethod
    def from_dict(cls, raw: object) -> LineageLink:
        data = _closed(raw, cls.FIELDS, "lineage link")
        return cls(**data)


@dataclass(frozen=True)
class SuccessorCreationRequest(_MutationRequest):
    operation_id: str
    delivery_epoch: int
    run_id: str
    title: str
    parent_node_ids: tuple[str, ...]

    DOMAIN: ClassVar[str] = "successor_creation"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "run_id",
        "title",
        "parent_node_ids",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _identifier(self.run_id, "run_id")
        _text(self.title, "successor title")
        object.__setattr__(
            self,
            "parent_node_ids",
            _identifiers(
                self.parent_node_ids,
                "successor parent_node_ids",
                allow_empty=False,
            ),
        )

    def _payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "title": self.title,
            "parent_node_ids": list(self.parent_node_ids),
        }

    def to_dict(self) -> dict[str, object]:
        return self._wire_dict()

    @classmethod
    def from_dict(cls, raw: object) -> SuccessorCreationRequest:
        data = _closed(raw, cls.FIELDS, "successor creation request")
        _schema_v1(data, "successor creation request")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("successor creation request domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            run_id=data["run_id"],
            title=data["title"],
            parent_node_ids=data["parent_node_ids"],
        )
        if data["request_hash"] != value.request_hash:
            raise ValueError("successor creation request hash does not match")
        return value

    def validate_result(self, result: SuccessorCreationResult) -> None:
        if not isinstance(result, SuccessorCreationResult):
            raise TypeError("successor creation result must be typed")
        if (
            result.operation_id != self.operation_id
            or result.delivery_epoch != self.delivery_epoch
            or result.request_hash != self.request_hash
            or result.parent_node_ids != self.parent_node_ids
            or result.node.title != self.title
        ):
            raise ValueError("successor creation result does not match its request")


@dataclass(frozen=True)
class SuccessorCreationResult:
    operation_id: str
    delivery_epoch: int
    request_hash: str
    node: LineageNode
    parent_node_ids: tuple[str, ...]
    links: tuple[LineageLink, ...]
    replayed: bool = False

    DOMAIN: ClassVar[str] = "successor_creation"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "node",
        "parent_node_ids",
        "links",
        "result_identity",
        "replayed",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _sha256(self.request_hash, "request_hash")
        if not isinstance(self.node, LineageNode):
            raise TypeError("successor node must be typed")
        parents = _identifiers(
            self.parent_node_ids,
            "successor parent_node_ids",
            allow_empty=False,
        )
        links = tuple(self.links)
        if not links or not all(isinstance(link, LineageLink) for link in links):
            raise TypeError("successor links must be a non-empty typed sequence")
        link_ids = tuple(link.link_id for link in links)
        if len(link_ids) != len(set(link_ids)) or link_ids != tuple(sorted(link_ids)):
            raise ValueError("successor links must be unique and canonically ordered")
        if any(
            link.from_node_id != self.node.node_id
            or link.relation != "successor_reuses"
            for link in links
        ):
            raise ValueError("successor links must be exact successor reuse links")
        targets = tuple(sorted(link.to_node_id for link in links))
        if len(targets) != len(set(targets)) or targets != parents:
            raise ValueError("successor links must exactly cover the parent nodes")
        object.__setattr__(self, "parent_node_ids", parents)
        object.__setattr__(self, "links", links)
        if type(self.replayed) is not bool:
            raise ValueError("replayed must be boolean")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "node": self.node.to_dict(),
            "parent_node_ids": list(self.parent_node_ids),
            "links": [link.to_dict() for link in self.links],
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(f"{self.DOMAIN}.result", self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "delivery_epoch": self.delivery_epoch,
            **self._identity_payload(),
            "result_identity": self.result_identity,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SuccessorCreationResult:
        data = _closed(raw, cls.FIELDS, "successor creation result")
        _schema_v1(data, "successor creation result")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("successor creation result domain is invalid")
        if not isinstance(data["links"], list):
            raise TypeError("successor links must be a list")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            request_hash=data["request_hash"],
            node=LineageNode.from_dict(data["node"]),
            parent_node_ids=data["parent_node_ids"],
            links=tuple(LineageLink.from_dict(link) for link in data["links"]),
            replayed=data["replayed"],
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("successor creation result identity does not match")
        return value


@dataclass(frozen=True)
class RuntimeStageRequest(_MutationRequest):
    """A runtime mutation bound only to durable workflow identities."""

    operation_id: str
    delivery_epoch: int
    run_id: str
    attempt_id: str
    stage_key: str
    effect_kind: str
    stage_input_hash: str
    dependency_result_ids: tuple[str, ...]

    DOMAIN: ClassVar[str] = "runtime_stage"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "run_id",
        "attempt_id",
        "stage_key",
        "effect_kind",
        "stage_input_hash",
        "dependency_result_ids",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _identifier(self.run_id, "run_id")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.stage_key, "runtime stage_key")
        _identifier(self.effect_kind, "runtime effect_kind")
        if not self.effect_kind.startswith("runtime_"):
            raise ValueError("runtime effect_kind must use the runtime namespace")
        _sha256(self.stage_input_hash, "stage_input_hash")
        if isinstance(self.dependency_result_ids, (str, bytes)):
            raise TypeError("dependency_result_ids must be a sequence")
        dependencies = tuple(
            _sha256(value, "dependency_result_id")
            for value in self.dependency_result_ids
        )
        if len(dependencies) != len(set(dependencies)) or dependencies != tuple(
            sorted(dependencies)
        ):
            raise ValueError(
                "dependency_result_ids must be unique and canonically ordered"
            )
        object.__setattr__(self, "dependency_result_ids", dependencies)

    def _payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "stage_key": self.stage_key,
            "effect_kind": self.effect_kind,
            "stage_input_hash": self.stage_input_hash,
            "dependency_result_ids": list(self.dependency_result_ids),
        }

    def to_dict(self) -> dict[str, object]:
        return self._wire_dict()

    @classmethod
    def from_dict(cls, raw: object) -> RuntimeStageRequest:
        data = _closed(raw, cls.FIELDS, "runtime stage request")
        _schema_v1(data, "runtime stage request")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("runtime stage request domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            run_id=data["run_id"],
            attempt_id=data["attempt_id"],
            stage_key=data["stage_key"],
            effect_kind=data["effect_kind"],
            stage_input_hash=data["stage_input_hash"],
            dependency_result_ids=data["dependency_result_ids"],
        )
        if data["request_hash"] != value.request_hash:
            raise ValueError("runtime stage request hash does not match")
        return value

    def validate_result(self, result: RuntimeStageResult) -> None:
        if not isinstance(result, RuntimeStageResult):
            raise TypeError("runtime stage result must be typed")
        if (
            result.operation_id != self.operation_id
            or result.delivery_epoch != self.delivery_epoch
            or result.request_hash != self.request_hash
            or result.attempt_id != self.attempt_id
            or result.stage_key != self.stage_key
            or result.effect_kind != self.effect_kind
        ):
            raise ValueError("runtime stage result does not match its request")


@dataclass(frozen=True)
class RuntimeStageResult:
    """A durable runtime result identity without paths or message claims."""

    operation_id: str
    delivery_epoch: int
    request_hash: str
    attempt_id: str
    runtime_event_id: str
    stage_key: str
    effect_kind: str
    output_id: str
    output_hash: str

    DOMAIN: ClassVar[str] = "runtime_stage"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "attempt_id",
        "runtime_event_id",
        "stage_key",
        "effect_kind",
        "output_id",
        "output_hash",
        "result_identity",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _sha256(self.request_hash, "request_hash")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.runtime_event_id, "runtime_event_id")
        _identifier(self.stage_key, "runtime stage_key")
        _identifier(self.effect_kind, "runtime effect_kind")
        if not self.effect_kind.startswith("runtime_"):
            raise ValueError("runtime effect_kind must use the runtime namespace")
        _identifier(self.output_id, "runtime output_id")
        _sha256(self.output_hash, "runtime output_hash")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "attempt_id": self.attempt_id,
            "runtime_event_id": self.runtime_event_id,
            "stage_key": self.stage_key,
            "effect_kind": self.effect_kind,
            "output_id": self.output_id,
            "output_hash": self.output_hash,
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(f"{self.DOMAIN}.result", self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "delivery_epoch": self.delivery_epoch,
            **self._identity_payload(),
            "result_identity": self.result_identity,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RuntimeStageResult:
        data = _closed(raw, cls.FIELDS, "runtime stage result")
        _schema_v1(data, "runtime stage result")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("runtime stage result domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            request_hash=data["request_hash"],
            attempt_id=data["attempt_id"],
            runtime_event_id=data["runtime_event_id"],
            stage_key=data["stage_key"],
            effect_kind=data["effect_kind"],
            output_id=data["output_id"],
            output_hash=data["output_hash"],
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("runtime stage result identity does not match")
        return value


@dataclass(frozen=True)
class ArtifactWorkflowRequest(_MutationRequest):
    """One durable artifact plan bound to a workflow stage and attempt."""

    operation_id: str
    delivery_epoch: int
    run_id: str
    attempt_id: str
    stage_key: str
    effect_kind: str
    stage_input_hash: str
    dependency_result_ids: tuple[str, ...]
    plan_id: str
    plan_hash: str
    snapshot_member_version_ids: tuple[str, ...] = ()

    DOMAIN: ClassVar[str] = "artifact_workflow"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "run_id",
        "attempt_id",
        "stage_key",
        "effect_kind",
        "stage_input_hash",
        "dependency_result_ids",
        "plan_id",
        "plan_hash",
        "snapshot_member_version_ids",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _identifier(self.run_id, "run_id")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.stage_key, "artifact stage_key")
        _identifier(self.effect_kind, "artifact effect_kind")
        if self.effect_kind not in _ARTIFACT_EFFECT_KINDS:
            raise ValueError("artifact effect_kind is unsupported")
        _sha256(self.stage_input_hash, "stage_input_hash")
        if isinstance(self.dependency_result_ids, (str, bytes)):
            raise TypeError("dependency_result_ids must be a sequence")
        dependencies = tuple(
            _sha256(value, "dependency_result_id")
            for value in self.dependency_result_ids
        )
        if len(dependencies) != len(set(dependencies)) or dependencies != tuple(
            sorted(dependencies)
        ):
            raise ValueError(
                "dependency_result_ids must be unique and canonically ordered"
            )
        object.__setattr__(self, "dependency_result_ids", dependencies)
        _identifier(self.plan_id, "artifact plan_id")
        _sha256(self.plan_hash, "artifact plan_hash")
        snapshot_members = _identifiers(
            self.snapshot_member_version_ids,
            "snapshot members",
            allow_empty=self.effect_kind != "artifact_snapshot",
        )
        if self.effect_kind == "artifact_snapshot" and not snapshot_members:
            raise ValueError("snapshot members must not be empty")
        if self.effect_kind != "artifact_snapshot" and snapshot_members:
            raise ValueError("only artifact_snapshot requests may include snapshot members")
        if len(snapshot_members) > _MAX_ARTIFACT_VERSIONS_PER_EFFECT:
            raise ValueError("artifact workflow request exceeds the snapshot member limit")
        object.__setattr__(self, "snapshot_member_version_ids", snapshot_members)

    def _payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "stage_key": self.stage_key,
            "effect_kind": self.effect_kind,
            "stage_input_hash": self.stage_input_hash,
            "dependency_result_ids": list(self.dependency_result_ids),
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "snapshot_member_version_ids": list(self.snapshot_member_version_ids),
        }

    def to_dict(self) -> dict[str, object]:
        return self._wire_dict()

    @classmethod
    def from_dict(cls, raw: object) -> ArtifactWorkflowRequest:
        data = _closed(raw, cls.FIELDS, "artifact workflow request")
        _schema_v1(data, "artifact workflow request")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("artifact workflow request domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            run_id=data["run_id"],
            attempt_id=data["attempt_id"],
            stage_key=data["stage_key"],
            effect_kind=data["effect_kind"],
            stage_input_hash=data["stage_input_hash"],
            dependency_result_ids=data["dependency_result_ids"],
            plan_id=data["plan_id"],
            plan_hash=data["plan_hash"],
            snapshot_member_version_ids=data["snapshot_member_version_ids"],
        )
        if data["request_hash"] != value.request_hash:
            raise ValueError("artifact workflow request hash does not match")
        return value

    def validate_result(self, result: ArtifactWorkflowResult) -> None:
        if not isinstance(result, ArtifactWorkflowResult):
            raise TypeError("artifact workflow result must be typed")
        if (
            result.operation_id != self.operation_id
            or result.delivery_epoch != self.delivery_epoch
            or result.request_hash != self.request_hash
            or result.attempt_id != self.attempt_id
            or result.stage_key != self.stage_key
            or result.effect_kind != self.effect_kind
        ):
            raise ValueError("artifact workflow result does not match its request")
        if (
            self.effect_kind == "artifact_snapshot"
            and result.artifact_version_ids != self.snapshot_member_version_ids
        ):
            raise ValueError("artifact result does not match requested snapshot members")


@dataclass(frozen=True)
class ArtifactWorkflowResult:
    """Committed artifact identities returned by one workflow artifact plan."""

    operation_id: str
    delivery_epoch: int
    request_hash: str
    attempt_id: str
    stage_key: str
    effect_kind: str
    artifact_version_ids: tuple[str, ...]
    snapshot_id: str | None = None
    replayed: bool = False

    DOMAIN: ClassVar[str] = "artifact_workflow"
    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "delivery_epoch",
        "request_hash",
        "attempt_id",
        "stage_key",
        "effect_kind",
        "artifact_version_ids",
        "snapshot_id",
        "result_identity",
        "replayed",
    }

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        _delivery_epoch(self.delivery_epoch)
        _sha256(self.request_hash, "request_hash")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.stage_key, "artifact stage_key")
        _identifier(self.effect_kind, "artifact effect_kind")
        if self.effect_kind not in _ARTIFACT_EFFECT_KINDS:
            raise ValueError("artifact effect_kind is unsupported")
        object.__setattr__(
            self,
            "artifact_version_ids",
            _identifiers(
                self.artifact_version_ids,
                "artifact_version_ids",
                allow_empty=False,
            ),
        )
        if len(self.artifact_version_ids) > _MAX_ARTIFACT_VERSIONS_PER_EFFECT:
            raise ValueError("artifact workflow result exceeds the version limit")
        if self.effect_kind == "artifact_snapshot":
            _identifier(self.snapshot_id, "snapshot_id")
        elif self.snapshot_id is not None:
            raise ValueError("only artifact_snapshot results may include snapshot_id")
        if type(self.replayed) is not bool:
            raise ValueError("replayed must be boolean")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "attempt_id": self.attempt_id,
            "stage_key": self.stage_key,
            "effect_kind": self.effect_kind,
            "artifact_version_ids": list(self.artifact_version_ids),
            "snapshot_id": self.snapshot_id,
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(f"{self.DOMAIN}.result", self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.DOMAIN,
            "delivery_epoch": self.delivery_epoch,
            **self._identity_payload(),
            "result_identity": self.result_identity,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ArtifactWorkflowResult:
        data = _closed(raw, cls.FIELDS, "artifact workflow result")
        _schema_v1(data, "artifact workflow result")
        if data["domain"] != cls.DOMAIN:
            raise ValueError("artifact workflow result domain is invalid")
        value = cls(
            operation_id=data["operation_id"],
            delivery_epoch=data["delivery_epoch"],
            request_hash=data["request_hash"],
            attempt_id=data["attempt_id"],
            stage_key=data["stage_key"],
            effect_kind=data["effect_kind"],
            artifact_version_ids=data["artifact_version_ids"],
            snapshot_id=data["snapshot_id"],
            replayed=data["replayed"],
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("artifact workflow result identity does not match")
        return value


EffectRequest = (
    ArtifactWorkflowRequest
    | SourceBindingRequest
    | SourceImportRequest
    | LineageQueryRequest
    | SuccessorCreationRequest
    | RuntimeStageRequest
)

EffectResult = (
    ArtifactWorkflowResult
    | SourceBindingResult
    | SourceImportResult
    | LineageQueryResult
    | SuccessorCreationResult
    | RuntimeStageResult
)


@dataclass(frozen=True)
class EffectReconciliationRequest:
    effect_request: EffectRequest
    delivery_epoch: int

    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "request_hash",
        "delivery_epoch",
        "effect_request",
    }

    def __post_init__(self) -> None:
        if not isinstance(
            self.effect_request,
            (
                SourceBindingRequest,
                SourceImportRequest,
                LineageQueryRequest,
                SuccessorCreationRequest,
                RuntimeStageRequest,
                ArtifactWorkflowRequest,
            ),
        ):
            raise TypeError("reconciliation effect_request must be typed")
        _delivery_epoch(self.delivery_epoch)
        if self.delivery_epoch < self.effect_request.delivery_epoch:
            raise ValueError("reconciliation delivery_epoch cannot precede its effect")

    @property
    def domain(self) -> str:
        return self.effect_request.DOMAIN

    @property
    def operation_id(self) -> str:
        return self.effect_request.operation_id

    @property
    def request_hash(self) -> str:
        return self.effect_request.request_hash

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.domain,
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "delivery_epoch": self.delivery_epoch,
            "effect_request": self.effect_request.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> EffectReconciliationRequest:
        data = _closed(raw, cls.FIELDS, "effect reconciliation request")
        _schema_v1(data, "effect reconciliation request")
        parser = {
            "source_binding": SourceBindingRequest.from_dict,
            "source_import": SourceImportRequest.from_dict,
            "lineage_query": LineageQueryRequest.from_dict,
            "successor_creation": SuccessorCreationRequest.from_dict,
            "runtime_stage": RuntimeStageRequest.from_dict,
            "artifact_workflow": ArtifactWorkflowRequest.from_dict,
        }.get(data["domain"])
        if parser is None:
            raise ValueError("reconciliation domain is unsupported")
        value = cls(
            effect_request=parser(data["effect_request"]),
            delivery_epoch=data["delivery_epoch"],
        )
        if (
            data["operation_id"] != value.operation_id
            or data["request_hash"] != value.request_hash
        ):
            raise ValueError("reconciliation request identity does not match")
        return value

    def validate_result(self, result: EffectReconciliationResult) -> None:
        if not isinstance(result, EffectReconciliationResult):
            raise TypeError("reconciliation result must be typed")
        if (
            result.domain != self.domain
            or result.operation_id != self.operation_id
            or result.request_hash != self.request_hash
            or result.delivery_epoch != self.delivery_epoch
        ):
            raise ValueError("reconciliation result does not match its request")
        if result.disposition == "committed":
            assert result.result is not None
            effect_request = replace(
                self.effect_request,
                delivery_epoch=result.result.delivery_epoch,
            )
            effect_request.validate_result(result.result)


@dataclass(frozen=True)
class EffectReconciliationResult:
    domain: str
    operation_id: str
    request_hash: str
    delivery_epoch: int
    disposition: str
    result: EffectResult | None = None

    FIELDS: ClassVar[set[str]] = {
        "schema_version",
        "domain",
        "operation_id",
        "request_hash",
        "delivery_epoch",
        "disposition",
        "result",
        "result_identity",
    }

    def __post_init__(self) -> None:
        if self.domain not in _MUTATION_DOMAINS:
            raise ValueError("reconciliation domain is unsupported")
        _operation_id(self.operation_id)
        _sha256(self.request_hash, "request_hash")
        _delivery_epoch(self.delivery_epoch)
        if self.disposition not in _RECONCILIATION_DISPOSITIONS:
            raise ValueError("reconciliation disposition is unsupported")
        if self.disposition == "committed":
            if self.result is None or self.result.DOMAIN != self.domain:
                raise ValueError("committed reconciliation requires its typed result")
            if (
                self.result.operation_id != self.operation_id
                or self.result.request_hash != self.request_hash
                or self.result.delivery_epoch > self.delivery_epoch
            ):
                raise ValueError("reconciled result identity does not match")
        elif self.result is not None:
            raise ValueError("uncommitted reconciliation cannot carry a result")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "delivery_epoch": self.delivery_epoch,
            "disposition": self.disposition,
            "effect_result_identity": (
                self.result.result_identity if self.result is not None else None
            ),
        }

    @property
    def result_identity(self) -> str:
        return _identity_hash(
            f"{self.domain}.reconciliation", self._identity_payload()
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "domain": self.domain,
            "operation_id": self.operation_id,
            "request_hash": self.request_hash,
            "delivery_epoch": self.delivery_epoch,
            "disposition": self.disposition,
            "result": self.result.to_dict() if self.result is not None else None,
            "result_identity": self.result_identity,
        }

    @classmethod
    def from_dict(cls, raw: object) -> EffectReconciliationResult:
        data = _closed(raw, cls.FIELDS, "effect reconciliation result")
        _schema_v1(data, "effect reconciliation result")
        parser = {
            "source_binding": SourceBindingResult.from_dict,
            "source_import": SourceImportResult.from_dict,
            "lineage_query": LineageQueryResult.from_dict,
            "successor_creation": SuccessorCreationResult.from_dict,
            "runtime_stage": RuntimeStageResult.from_dict,
            "artifact_workflow": ArtifactWorkflowResult.from_dict,
        }.get(data["domain"])
        if parser is None:
            raise ValueError("reconciliation domain is unsupported")
        result = parser(data["result"]) if data["result"] is not None else None
        value = cls(
            domain=data["domain"],
            operation_id=data["operation_id"],
            request_hash=data["request_hash"],
            delivery_epoch=data["delivery_epoch"],
            disposition=data["disposition"],
            result=result,
        )
        if data["result_identity"] != value.result_identity:
            raise ValueError("reconciliation result identity does not match")
        return value


GOLDEN_RESEARCH_WORKFLOW = WorkflowDefinition(
    definition_id="research.golden",
    version=1,
    stages=(
        StageDefinition("resolve_sources", "control", ()),
        StageDefinition("await_source_decision", "decision", ("resolve_sources",)),
        StageDefinition(
            "import_sources",
            "engine_mutation",
            ("await_source_decision",),
            ("source_binding", "source_import"),
        ),
        StageDefinition(
            "retrieve_lineage",
            "engine_query",
            ("import_sources",),
            required_results=("lineage_query",),
        ),
        StageDefinition(
            "await_lineage_decision",
            "decision",
            ("retrieve_lineage",),
        ),
        StageDefinition(
            "create_successor",
            "engine_mutation",
            ("await_lineage_decision",),
            ("successor_creation",),
            checkpoint=True,
        ),
        StageDefinition(
            "research_evidence",
            "runtime",
            ("create_successor",),
            ("runtime_evidence",),
        ),
        StageDefinition(
            "research_architecture",
            "runtime",
            ("research_evidence",),
            ("runtime_architecture",),
        ),
        StageDefinition(
            "research_training_plan",
            "runtime",
            ("research_architecture",),
            ("runtime_training_plan",),
        ),
        StageDefinition(
            "publish_living_artifacts",
            "artifact",
            ("research_training_plan",),
            ("artifact_evidence", "artifact_living", "artifact_training"),
        ),
        StageDefinition(
            "freeze_snapshot",
            "artifact",
            ("publish_living_artifacts",),
            ("artifact_snapshot",),
            checkpoint=True,
        ),
    ),
)
