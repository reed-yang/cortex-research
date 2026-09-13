"""The canonical document encoding, in the worker's own copy.

The worker measures its slot's identity by replicating the updater's canonical
digest exactly, which is what lets it self-measure without importing
`cortex_platform`. `tests/product/runtime_update/test_worker_payload.py` pins
these two functions against `models.canonical_json` and `models.digest_document`
over a corpus that includes the shapes a real manifest carries, so the
replication is checked rather than asserted.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_document(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


# ---------------------------------------------------------------------------
# ⟦AMD-1⟧ The shared, versioned result projection.
#
# The ledger records digests, not payloads, so a worker-side result store would
# be a second durable truth beside `control.db`'s `run_events`. It is not one:
# the durable event stream in Control IS the result, and this is the function
# that names which part of it the two sides are allowed to agree about.
#
# Control mints `id`/`sequence`/`occurred_at` itself, re-mints `decision_id` and
# `pin_release_action_id`, and drops privacy-excluded fields (tool arguments and
# raw tool returns) before anything is written. A digest over the raw payloads
# could therefore never be reproduced from the stored rows. So the digest is
# taken over a projection that contains only fields both sides can see, and the
# projection is versioned, because changing it changes every digest.
#
# This is a replay/harness verification, never a per-turn gate: a mismatch fails
# S3.5's harness or a replay check and never raises `runtime_operation_uncertain`.
# ---------------------------------------------------------------------------

PROJECTION_VERSION = 1

# Which Hermes callback signals become durable events. Everything absent from
# this map is transient — token deltas, progress, reasoning availability, step
# completions — and transient events are dropped before they reach `run_events`,
# so including them would make the digest unreproducible from the stored stream.
_DURABLE_SIGNALS = {
    "tool.started": "runtime.tool.started",
    "tool.completed": "runtime.tool.completed",
    "decision.required": "runtime.decision.required",
}

_TERMINAL_TYPES = {
    "runtime.run.completed": "runtime_completed",
    "runtime.run.failed": "runtime_failed",
    "runtime.run.canceled": "runtime_canceled",
}

#: What the adapter offers an operator for an approval. Stated here because the
#: worker has to predict the *event* payload, not its own signal payload: the
#: adapter adds these on translation, Control normalizes and stores them, and a
#: worker that digested its own payload instead would compute a digest nothing
#: could reproduce. The real acceptance is what caught that.
DECISION_OPTIONS = ("approve_once", "deny")
DEFAULT_DECISION_PROMPT = "Runtime approval required"


class ProjectionError(ValueError):
    """An event outside the projection's closed domain reached the digest."""


def _text(value: object, *, limit: int | None = None) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit] if limit is not None else text


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _options(value: object) -> list[str]:
    """Decision options, reduced to the identities Control stores.

    Control normalizes a bare option into `{"id": option}` before persisting it,
    so a projection over the raw list and one over the stored list would differ
    for exactly the shape the native backend emits (`["approve_once", "deny"]`).
    """

    if not isinstance(value, (list, tuple)):
        return []
    identities: list[str] = []
    for option in value:
        if isinstance(option, dict):
            identities.append(_text(option.get("id")))
        else:
            identities.append(_text(option))
    return identities


def project_event(event_type: str, payload: object) -> dict[str, object]:
    """Reduce one durable runtime event to the fields both sides can see."""

    fields = payload if isinstance(payload, dict) else {}
    if event_type == "runtime.run.started":
        # `binding_generation` is the adapter's bookkeeping about the binding,
        # not about the turn. The event's presence is the whole signal.
        return {}
    if event_type == "runtime.tool.started":
        # `arguments` is privacy-excluded: Control's `allowed_fields` drops it,
        # so it is not in the stored row and cannot be in the digest.
        return {
            "tool_call_id": _text(fields.get("tool_call_id")),
            "tool_name": _text(fields.get("tool_name")),
        }
    if event_type == "runtime.tool.completed":
        return {
            "tool_call_id": _text(fields.get("tool_call_id")),
            "tool_name": _text(fields.get("tool_name")),
            "is_error": bool(fields.get("is_error", False)),
            "duration_ms": _optional_int(fields.get("duration_ms")),
        }
    if event_type == "runtime.decision.required":
        # `decision_id` is re-minted by Control, so the runtime's own id cannot
        # appear here; what survives is what the operator is actually asked.
        return {
            "kind": _text(fields.get("kind") or fields.get("decision_kind") or "approval"),
            "prompt": _text(fields.get("prompt") or "Runtime approval required"),
            "options": _options(fields.get("options")),
        }
    if event_type == "runtime.session_rebound":
        # `generation` is minted by the adapter from the binding it holds.
        return {
            "runtime_session_ref": _text(fields.get("runtime_session_ref")),
            "parent_runtime_session_ref": _optional_text(
                fields.get("parent_runtime_session_ref")
            ),
        }
    if event_type == "runtime.message.completed":
        return {"content": _text(fields.get("content"))}
    default_category = _TERMINAL_TYPES.get(event_type)
    if default_category is not None:
        return {
            "category": _text(fields.get("category") or default_category, limit=100),
            "retryable": bool(fields.get("retryable", False)),
        }
    raise ProjectionError("event type is outside the projection domain")


def signal_event_type(kind: str) -> str | None:
    """The durable event a Hermes callback signal becomes, or None if transient."""

    return _DURABLE_SIGNALS.get(kind)


def signal_event_payload(kind: str, payload) -> dict[str, object]:
    """Translate one signal payload into the durable event payload it becomes.

    The adapter's `translate_hermes_signal` is the specification; this is the
    part of it the digest depends on. Only `decision.required` actually changes
    shape — the adapter supplies the option set and the default prompt that
    Control then normalizes and stores — but stating all three keeps the
    correspondence checkable rather than remembered.
    """

    fields = dict(payload) if isinstance(payload, dict) else {}
    if kind == "decision.required":
        return {
            "decision_id": fields.get("decision_id"),
            "kind": fields.get("decision_kind", "approval"),
            "prompt": fields.get("prompt", DEFAULT_DECISION_PROMPT),
            "command": fields.get("command"),
            "description": fields.get("description"),
            "options": list(DECISION_OPTIONS),
            "revision": 0,
        }
    return fields


def turn_durable_stream(
    signals,
    *,
    result,
    session_ref: str,
) -> list[tuple[str, dict[str, object]]]:
    """The durable stream one turn causes, in the order the adapter emits it.

    Written here rather than in the adapter because the worker has to be able to
    predict it: the worker is the side that computes the digest the ledger
    records, and it can only do that if it knows exactly which durable events its
    turn will produce. The adapter's `execute` is the specification; this is that
    specification stated once, where both sides read it.
    """

    stream: list[tuple[str, dict[str, object]]] = [("runtime.run.started", {})]
    for kind, payload in signals:
        event_type = signal_event_type(kind)
        if event_type is not None:
            stream.append((event_type, signal_event_payload(kind, payload)))
    final_ref = _text(result.get("session_ref")) or session_ref
    canceled = bool(result.get("canceled", False))
    failed = bool(result.get("failed", False))
    final_response = _optional_text(result.get("final_response"))
    if final_ref != session_ref:
        stream.append(
            (
                "runtime.session_rebound",
                {
                    "runtime_session_ref": final_ref,
                    "parent_runtime_session_ref": session_ref,
                },
            )
        )
    if final_response is not None and not canceled and not failed:
        stream.append(("runtime.message.completed", {"content": final_response}))
    if canceled:
        stream.append(("runtime.run.canceled", {}))
    elif failed:
        stream.append(
            ("runtime.run.failed", {"category": "runtime_execution_failed", "retryable": True})
        )
    else:
        stream.append(("runtime.run.completed", {}))
    return stream


def result_digest(events) -> str:
    """Digest one durable event stream through the versioned projection."""

    return digest_document(
        {
            "projection_version": PROJECTION_VERSION,
            "events": [
                {"type": event_type, "fields": project_event(event_type, payload)}
                for event_type, payload in events
            ],
        }
    )
