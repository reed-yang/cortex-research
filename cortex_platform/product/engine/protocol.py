"""The one-request / one-result contract between cortexd and an effect child.

D1 refuses a new RPC protocol: S3.3's framed channel exists because a Hermes
turn emits events and awaits decisions mid-flight, and an ingest does neither.
So the child reads one JSON request from stdin, writes one JSON result to a
file the request names, and exits. The result goes to a file rather than stdout
because the engine writes to both streams itself -- a shipped `print` would
otherwise corrupt the reply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

PROTOCOL_VERSION = 1

# The operations the engine boundary exposes. P4.2 wires the non-agentic ingest
# subset; `radar` and `audit` are named by A3 but are not implemented here.
ARXIV_OPERATIONS: frozenset[str] = frozenset(
    {"ingest_arxiv", "checkpoint", "reconcile_arxiv", "self_check"}
)
# ⟦XHS⟧ First-party provider calls. Each is one bounded request (or one short
# chain, for OCR's fallback and link verification), imports its research
# profile client inside its handler, never opens `research.db`, and receives
# only its own credentials (`bindings.OPERATION_SECRET_SCOPES`). The weekly
# fallback's model call and its page-title check are two operations, so a
# verification retry never calls the model again.
PROVIDER_OPERATIONS: frozenset[str] = frozenset(
    {
        "xhs_list_page",
        "xhs_note_detail",
        "xhs_download_image",
        "xhs_ocr_image",
        "xhs_identify",
        "xhs_resolve_link",
        "blog_fetch",
        "xhs_fallback_decide",
        "xhs_fallback_verify",
    }
)
OPERATIONS: frozenset[str] = ARXIV_OPERATIONS | PROVIDER_OPERATIONS

# The asset root each writing provider operation writes under, by root ID. The
# caller binds that root as the child's only write root; every other provider
# operation may write nothing outside its own effect directory.
PROVIDER_WRITE_ROOTS: Mapping[str, str] = {
    "xhs_download_image": "xhs-notes",
    "blog_fetch": "blogs",
}

# What a provider operation may fail with: the XHS task allowlist
# (`control/xhs_store.py`). The Capture allowlist below does not apply to them.
PROVIDER_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "auth",
        "payment",
        "rate_limited",
        "transient",
        "outcome_unknown",
        "upstream_error",
        "not_found",
        "invalid_response",
        "url_expired",
    }
)

# The frozen store allowlist an effect failure has to land in
# (`control/store.py:161`). Nothing else may cross this boundary.
FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "adapter_unavailable",
        "capability_unavailable",
        "invalid_source",
        "materialization_failed",
        "outcome_unknown",
    }
)


@dataclass(frozen=True)
class EffectRequest:
    """One engine operation, and the roots the child is allowed to touch.

    `capabilities` is what the supervisor found for each operator skill slot
    (`{"ocr": {"state": ..., "reason": ...}}`), so a refusal can say why a
    capability was absent rather than only that it was.
    """

    operation: str
    payload: Mapping[str, Any]
    marker: str
    result_path: str
    research_db: str
    state_dir: str
    write_roots: Sequence[str]
    watch_roots: Mapping[str, str] = field(default_factory=dict)
    capabilities: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS:
            raise ValueError(f"unsupported engine operation: {self.operation}")
        if not self.marker or not self.write_roots or not self.result_path:
            raise ValueError("effect request is incomplete")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROTOCOL_VERSION,
            "operation": self.operation,
            "payload": dict(self.payload),
            "marker": self.marker,
            "result_path": self.result_path,
            "research_db": self.research_db,
            "state_dir": self.state_dir,
            "write_roots": list(self.write_roots),
            "watch_roots": dict(self.watch_roots),
            "capabilities": {
                name: dict(status) for name, status in self.capabilities.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EffectRequest:
        if raw.get("schema_version") != PROTOCOL_VERSION:
            raise ValueError("unsupported effect request schema")
        return cls(
            operation=raw["operation"],
            payload=raw["payload"],
            marker=raw["marker"],
            result_path=raw["result_path"],
            research_db=raw["research_db"],
            state_dir=raw["state_dir"],
            write_roots=tuple(raw["write_roots"]),
            watch_roots=dict(raw.get("watch_roots") or {}),
            capabilities={
                str(name): {str(key): str(value) for key, value in dict(status).items()}
                for name, status in dict(raw.get("capabilities") or {}).items()
            },
        )


def write_result(path: Path, value: Mapping[str, Any]) -> None:
    """Write the result atomically: a torn file is an unknown outcome."""

    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


def read_result(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
