"""Closed public projections for durable Cortex control events."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..resources import parse_resource_uri


JsonObject = dict[str, Any]

_PUBLIC_EVENT_TYPES = frozenset(
    {
        "run.queued",
        "run.starting",
        "run.running",
        "run.waiting_for_decision",
        "run.pause_requested",
        "run.paused",
        "run.resuming",
        "run.retrying",
        "run.cancel_requested",
        "run.canceled",
        "run.completed",
        "run.failed",
        "runtime.bound",
        "runtime.tool.started",
        "runtime.tool.completed",
        "runtime.session_rebound",
        "runtime.message.completed",
        "runtime.event.discarded",
        "runtime.action.acked",
        "runtime.action.failed",
        "checkpoint.committed",
        "decision.required",
        "decision.resolved",
        "source.intent_received",
        "source.conflict_detected",
        "source.reused",
        "source.import_requested",
        "source.import_waiting",
        "source.imported",
        "artifact.created",
        "artifact.materialization_requested",
        "artifact.version_committed",
        "artifact.head_advanced",
        "artifact.snapshot_created",
    }
)
_RUN_STATES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "waiting_for_decision",
        "pause_requested",
        "paused",
        "resuming",
        "retrying",
        "cancel_requested",
        "canceled",
        "completed",
        "failed",
    }
)
_CONTROL_EVENT_PAYLOAD_FIELDS = {
    "run.queued": ("state",),
    "run.starting": ("from", "state", "stage"),
    "run.running": ("from", "state", "stage"),
    "run.waiting_for_decision": ("from", "state", "stage"),
    "run.pause_requested": ("from", "state", "stage"),
    "run.paused": ("from", "state", "stage"),
    "run.resuming": ("from", "state", "stage"),
    "run.retrying": ("from", "state", "stage"),
    "run.cancel_requested": ("from", "state", "stage"),
    "run.canceled": ("from", "state", "stage"),
    "run.completed": ("from", "state", "stage"),
    "run.failed": ("from", "state", "stage", "category", "retryable"),
    "runtime.bound": ("runtime_release_id",),
    "runtime.tool.started": ("tool_name",),
    "runtime.tool.completed": ("tool_name", "is_error", "duration_ms"),
    "runtime.session_rebound": (),
    "runtime.message.completed": ("message_id", "role"),
    "runtime.event.discarded": ("event_type",),
    "runtime.action.acked": ("kind",),
    "runtime.action.failed": ("kind", "category"),
    "checkpoint.committed": ("checkpoint_uri",),
    "decision.resolved": ("decision_id", "choice"),
    "source.intent_received": ("source_intent_id", "canonical_ids"),
    "source.conflict_detected": ("source_intent_id", "canonical_ids"),
    "source.reused": ("source_id", "binding_id", "canonical_id"),
    "source.import_requested": ("source_id", "canonical_id"),
    "source.import_waiting": ("source_id", "canonical_id"),
    "source.imported": ("source_id", "binding_id", "canonical_id"),
    "artifact.created": ("artifact_id", "kind"),
    "artifact.materialization_requested": (
        "artifact_id",
        "artifact_version_id",
        "logical_version",
    ),
    "artifact.version_committed": (
        "artifact_id",
        "artifact_version_id",
        "logical_version",
        "resource_uri",
    ),
    "artifact.head_advanced": (
        "artifact_id",
        "artifact_version_id",
        "head_revision",
    ),
    "artifact.snapshot_created": (
        "snapshot_id",
        "workspace_id",
        "member_count",
    ),
}
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_CANONICAL_SOURCE_RE = re.compile(
    r"(?:arxiv:[0-9]{4}\.[0-9]{4,5}"
    r"|doi:10\.[0-9]{4,9}/[-._;()/:a-z0-9]+"
    r"|sha256:[0-9a-f]{64})\Z",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        (
            r"(?:/Users/|/home/|/private/|/tmp/|/etc/|/var/|/opt/|"
            r"/usr/|/root/|/Volumes/|/workspace/|[A-Za-z]:\\)"
        ),
        r"\bfile://",
        r"\b(?:token|api[_ -]?key|secret|credential|password|authorization)\s*[:=]",
        r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}",
        r"\bsk-[A-Za-z0-9_-]{8,}",
        r"\bgh[opsu]_[A-Za-z0-9]{8,}",
        r"\bAKIA[A-Z0-9]{12,}",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"<thinking>|hidden reasoning|internal reasoning|chain[- ]of[- ]thought",
    )
)
_REDACTED_DECISION_PROMPT = "Decision details are unavailable in this client."


def project_public_event(
    event: Mapping[str, Any], *, encode_cursor: Callable[[int], str]
) -> JsonObject:
    """Return a closed event envelope without forwarding arbitrary payload data."""

    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in _PUBLIC_EVENT_TYPES:
        public_type = "event.redacted"
        payload: JsonObject = {"category": "unsupported_event_type"}
    else:
        public_type = event_type
        payload = _project_payload(event_type, event.get("payload"))

    cursor = event.get("cursor")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("event cursor is invalid")
    sequence = event.get("sequence")
    schema_version = event.get("schema_version")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("event sequence is invalid")
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("event schema version is invalid")

    return {
        "cursor": encode_cursor(cursor),
        "schema_version": schema_version,
        "id": _required_identifier(event.get("id"), "event id"),
        "run_id": _required_identifier(event.get("run_id"), "run id"),
        "attempt_id": _optional_identifier(event.get("attempt_id")),
        "sequence": sequence,
        "type": public_type,
        "occurred_at": _safe_timestamp(event.get("occurred_at")),
        "causation_id": _optional_identifier(event.get("causation_id")),
        "durability": "durable",
        "payload": payload,
    }


def project_public_decision(decision: Mapping[str, Any]) -> JsonObject:
    """Return the minimum public Decision DTO with sanitized interactive text."""

    prompt, options = project_decision_prompt_options(
        decision.get("prompt"), decision.get("options")
    )
    resolution = decision.get("resolution")
    public_resolution: JsonObject | None = None
    if isinstance(resolution, Mapping):
        choice = _safe_identifier(resolution.get("choice"))
        if choice is not None:
            public_resolution = {"choice": choice}
    revision = decision.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("decision revision is invalid")
    state = decision.get("state")
    if state not in {"pending", "resolved", "expired"}:
        raise ValueError("decision state is invalid")
    kind = _safe_category(decision.get("kind")) or "unavailable"
    return {
        "id": _required_identifier(decision.get("id"), "decision id"),
        "run_id": _required_identifier(decision.get("run_id"), "run id"),
        "attempt_id": _required_identifier(
            decision.get("attempt_id"), "attempt id"
        ),
        "kind": kind,
        "prompt": prompt,
        "options": options,
        "state": state,
        "resolution": public_resolution,
        "revision": revision,
        "created_at": _safe_timestamp(decision.get("created_at")),
        "resolved_at": _optional_timestamp(decision.get("resolved_at")),
    }


def project_decision_prompt_options(
    prompt: Any, options: Any
) -> tuple[str, list[JsonObject]]:
    """Sanitize the only runtime-authored prose exposed by decision events."""

    safe_prompt = _safe_human_text(prompt, maximum=20_000)
    safe_options: list[JsonObject] = []
    if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
        for option in options[:100]:
            projected = _project_decision_option(option)
            if projected is not None:
                safe_options.append(projected)
    if safe_prompt is None or not safe_options:
        safe_prompt = _REDACTED_DECISION_PROMPT
    return safe_prompt, safe_options


def _project_payload(event_type: str, value: Any) -> JsonObject:
    payload = value if isinstance(value, Mapping) else {}
    if event_type == "decision.required":
        prompt, options = project_decision_prompt_options(
            payload.get("prompt"), payload.get("options")
        )
        result: JsonObject = {
            "prompt": prompt,
            "options": options,
        }
        decision_id = _safe_identifier(payload.get("decision_id"))
        kind = _safe_category(payload.get("kind"))
        if decision_id is not None:
            result["decision_id"] = decision_id
        if kind is not None:
            result["kind"] = kind
        return result

    result = {}
    for field in _CONTROL_EVENT_PAYLOAD_FIELDS[event_type]:
        projected = _project_scalar(field, payload.get(field))
        if projected is not None:
            result[field] = projected
    return result


def _project_scalar(field: str, value: Any) -> Any:
    if field in {"from", "state"}:
        return value if value in _RUN_STATES else None
    if field in {"is_error", "retryable"}:
        return value if type(value) is bool else None
    if field == "duration_ms":
        return value if type(value) is int and 0 <= value <= 86_400_000 else None
    if field in {"logical_version", "member_count"}:
        return value if type(value) is int and 1 <= value <= 1_000_000 else None
    if field == "head_revision":
        return value if type(value) is int and 0 <= value <= 1_000_000_000 else None
    if field == "resource_uri":
        if not isinstance(value, str) or len(value) > 4_000:
            return None
        try:
            resource = parse_resource_uri(value)
        except ValueError:
            return None
        return resource.value if resource.root == "artifacts" else None
    if field == "checkpoint_uri":
        if not isinstance(value, str) or len(value) > 4_000:
            return None
        try:
            resource = parse_resource_uri(value)
        except ValueError:
            return None
        if resource.root == "artifacts":
            return resource.value
        return None
    if field == "canonical_id":
        return (
            value
            if isinstance(value, str)
            and _CANONICAL_SOURCE_RE.fullmatch(value) is not None
            else None
        )
    if field == "canonical_ids":
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return None
        result = [
            item
            for item in value[:20]
            if isinstance(item, str)
            and _CANONICAL_SOURCE_RE.fullmatch(item) is not None
        ]
        return result or None
    if field in {"category", "kind"}:
        return _safe_category(value)
    if field == "role":
        return value if value == "assistant" else None
    if field == "stage":
        return _safe_human_text(value, maximum=200)
    if field == "tool_name":
        return _safe_identifier(value)
    return _safe_identifier(value)


def _project_decision_option(value: Any) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    option_id = _safe_identifier(value.get("id"))
    if option_id is None:
        return None
    result: JsonObject = {"id": option_id}
    label = _safe_human_text(value.get("label"), maximum=500)
    description = _safe_human_text(value.get("description"), maximum=2_000)
    tone = value.get("tone")
    if label is not None:
        result["label"] = label
    if description is not None:
        result["description"] = description
    if tone in {"primary", "danger", "neutral"}:
        result["tone"] = tone
    return result


def _safe_human_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        return None
    if any(pattern.search(value) for pattern in _SENSITIVE_TEXT_PATTERNS):
        return None
    return value


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        return None
    if any(pattern.search(value) for pattern in _SENSITIVE_TEXT_PATTERNS):
        return None
    return value


def _required_identifier(value: Any, field: str) -> str:
    projected = _safe_identifier(value)
    if projected is None:
        raise ValueError(f"{field} is invalid")
    return projected


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_identifier(value)


def _safe_category(value: Any) -> str | None:
    if not isinstance(value, str) or not _CATEGORY_RE.fullmatch(value):
        return None
    return value


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("event timestamp is invalid")
    return value


def _optional_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_timestamp(value)
