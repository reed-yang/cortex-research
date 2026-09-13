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
OPERATIONS: frozenset[str] = frozenset(
    {"ingest_arxiv", "checkpoint", "reconcile_arxiv", "self_check"}
)

# The frozen store allowlist an effect failure has to land in
# (`control/store.py:149`). Nothing else may cross this boundary.
FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "adapter_unavailable",
        "invalid_source",
        "materialization_failed",
        "outcome_unknown",
    }
)


@dataclass(frozen=True)
class EffectRequest:
    """One engine operation, and the roots the child is allowed to touch."""

    operation: str
    payload: Mapping[str, Any]
    marker: str
    result_path: str
    research_db: str
    state_dir: str
    write_roots: Sequence[str]
    watch_roots: Mapping[str, str] = field(default_factory=dict)

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
