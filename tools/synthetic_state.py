#!/usr/bin/env python3
"""Generate, snapshot and compare a synthetic Control schema-19 state.

The continuity gate of the auto-research extraction milestone
(`docs/plans/parallel-results/AR-MAIN-plan.md`, section 6 item 7) needs an
old-state fixture that an accepted bundle can install, an upgraded candidate
can read back, and a rollback can be measured against. No such fixture exists
(`logs/auto-research-extraction/recon/R6-build-supply-and-lifecycle.md`,
section 7 and gap 5), so this tool composes one.

Three rules make the result usable as evidence rather than decoration:

* Every row is written through the product's own store APIs, so the schema is
  created by `ControlStore.initialize()` and the rows obey the same
  transactions, receipts and invariants a real installation obeys. The tool
  contains no DDL and no direct INSERT.
* Generation is deterministic for a given `--seed`: the store is given a fixed
  clock and a hash-derived id factory, so two generates produce the same
  semantic snapshot and a real difference is therefore a real difference.
* Nothing but `--data-dir` is written, and every value is synthetic
  (example.com, lorem text, fabricated identifiers).

`snapshot` never writes: it opens the control database read-only, so it can be
run against an installed candidate's data directory before and after an
upgrade. It reports the rows that carry stable identity and the digests of the
files under the data directory, and it normalizes timestamps it did not choose
to "set"/null, because wall-clock values differ between two real installations
while their presence carries meaning (an archived thread, a resolved decision).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cortex_platform.product.artifacts import (
    ArtifactMaterializationService,
    AssetRoot,
    FilesystemMaterializer,
)
from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.research_store import research_item_id
from cortex_platform.product.research.documents import (
    ROOT_ID as RESEARCH_DOCUMENT_ROOT_ID,
    ResearchDocumentAdopter,
)
from cortex_platform.product.research.service import ROOT_ID as RESEARCH_ARTIFACT_ROOT_ID
from cortex_platform.product.sources import CandidateObservation

SNAPSHOT_FORMAT_VERSION = 1

#: The one gen9 runtime identity this product admits; the digests and refs
#: below are fabricated, the shape is the shipped one.
RUNTIME_RELEASE_ID = "hermes-0.15.0-gen9"
RUNTIME_WORKER_PROTOCOL = "cortex-worker/2"
RUNTIME_ADAPTER_ID = "hermes"
RUNTIME_ADAPTER_VERSION = "0.15.0-gen9"

#: A fabricated arXiv identity. The locator grammar admits only arxiv.org and
#: doi.org (`sources/identity.py:141-147`), so the authority is the real public
#: one and the work id is invented; no such paper exists.
SYNTHETIC_ARXIV_ID = "2601.00001"
SYNTHETIC_ARXIV_URL = "https://arxiv.org/abs/2601.00001"
SYNTHETIC_PAPER_TITLE = "Lorem Ipsum: A Synthetic Study"

_CLOCK_ORIGIN = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_CLOCK_STEP = timedelta(seconds=1)

#: Text longer than this is recorded as a digest instead of a literal, so a
#: snapshot of a real installation stays readable and diffable.
_MAX_INLINE_TEXT = 120


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


class _FixedClock:
    """Advance one second per call from a fixed origin."""

    def __init__(self) -> None:
        self._ticks = 0

    def __call__(self) -> datetime:
        value = _CLOCK_ORIGIN + self._ticks * _CLOCK_STEP
        self._ticks += 1
        return value


class _SeededIds:
    """Hash-derived identifiers: stable per seed, no uuid, no counter drift.

    The shape is `<kind>-<hex>`: `_ARTIFACT_IDENTIFIER_RE` in the Control store
    rejects underscores in the identifiers that reach artifact provenance, so
    the separator is a dash rather than the production factory's underscore.
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._counts: dict[str, int] = {}

    def __call__(self, kind: str) -> str:
        count = self._counts.get(kind, 0) + 1
        self._counts[kind] = count
        material = f"{self._seed}\0{kind}\0{count}".encode()
        return f"{kind.replace('_', '-')}-{hashlib.sha256(material).hexdigest()[:24]}"


def _key(seed: int, label: str) -> str:
    """Build an idempotency key; `_KEY_RE` demands 16..128 of [A-Za-z0-9_-]."""

    digest = hashlib.sha256(f"{seed}\0{label}".encode()).hexdigest()[:24]
    return f"{label}-{digest}"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# --------------------------------------------------------------------------
# Synthetic content
# --------------------------------------------------------------------------


def _dossier_markdown(seed: int) -> str:
    return (
        "# Lorem Ipsum Dossier\n"
        "\n"
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do\n"
        "eiusmod tempor incididunt ut labore et dolore magna aliqua.\n"
        "\n"
        "- Reference: <https://example.com/lorem/ipsum>\n"
        f"- Synthetic seed: {seed}\n"
        "- A formula: $x^2 + y^2 = z^2$\n"
    )


def _artifact_markdown(seed: int) -> bytes:
    return (
        "# Synthetic Saved Output\n"
        "\n"
        "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris\n"
        "nisi ut aliquip ex ea commodo consequat [D1].\n"
        "\n"
        f"Generated by tools/synthetic_state.py with seed {seed}.\n"
    ).encode()


class _SyntheticCatalog:
    """The two-method catalog contract `ResearchDocumentAdopter` consumes.

    The shipped `ResearchCatalog` is a query-only reader over the legacy
    `research.db`, which lives outside `--data-dir` and whose schema belongs to
    `cortex_research`. Standing one up here would mean hand-written legacy DDL
    and a second database this tool has no business creating; the catalog also
    writes no Control row. So the adoption input is supplied directly, and the
    Control-side path -- `ResearchDocumentAdopter.apply` and
    `ControlStore.register_research_documents` -- is the real one.
    """

    def __init__(self, document: Path, *, kind: str, origin_id: str, title: str) -> None:
        self._document = document
        self.item = {
            "id": research_item_id(kind, origin_id),
            "kind": kind,
            "origin_id": origin_id,
            "title": title,
        }

    def list_items(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        items = [self.item][offset : offset + limit]
        return {"items": items, "total": 1}

    def document_candidates(self, item_id: str) -> list[dict[str, str]]:
        if item_id != self.item["id"]:
            return []
        return [{"kind": self.item["kind"], "registered_path": str(self._document)}]


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def generate(data_dir: Path, *, seed: int) -> dict[str, Any]:
    """Write a complete synthetic schema-19 state below `data_dir`."""

    # Resolved, not merely absolute: the materializer refuses an asset root
    # with a symlink ancestor, and `/tmp` is a symlink on Darwin.
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    store = ControlStore(
        data_dir / "control.db", clock=_FixedClock(), id_factory=_SeededIds(seed)
    )
    store.initialize()

    workspace = store.create_workspace(
        title="Synthetic Research Workspace",
        actor_id="local",
        idempotency_key=_key(seed, "workspace"),
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Lorem ipsum conversation",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key(seed, "thread"),
    ).value

    thread = _append_messages(
        store,
        thread,
        seed=seed,
        label="conversation",
        exchanges=(
            ("user", "Summarize the lorem ipsum dossier."),
            ("assistant", "The dossier records synthetic evidence only [D1]."),
            ("user", "Save the summary as an output."),
        ),
    )

    run = store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=_key(seed, "run"),
    ).value

    source, run = _bind_source(store, run, seed=seed)
    run, runtime = _start_run(store, run, seed=seed)
    run = _decide(store, run, runtime, seed=seed)
    artifact = _save_artifact(
        store, data_dir, run=run, source=source, workspace=workspace, seed=seed
    )
    item, document = _adopt_document(store, data_dir, seed=seed)
    research_thread = store.open_research_thread(
        item_id=item["id"],
        workspace_id=workspace["id"],
        expected_revision=store.get_workspace(workspace["id"])["revision"],
        actor_id="local",
        idempotency_key=_key(seed, "research-thread"),
    ).value
    _append_messages(
        store,
        research_thread,
        seed=seed,
        label="research",
        exchanges=(
            ("user", "Open the adopted dossier."),
            ("assistant", "Reading the retained document version [D1]."),
        ),
    )

    identity = store.current_control_store_identity()
    return {
        "data_dir": str(data_dir),
        "seed": seed,
        "control_schema_version": identity.schema_version,
        "workspace_id": workspace["id"],
        "thread_ids": [thread["id"], research_thread["id"]],
        "run_id": run["id"],
        "source_id": source["id"],
        "artifact_version_id": artifact["artifact_version_id"],
        "research_item_id": item["id"],
        "research_document_version_id": document["id"],
    }


def _append_messages(
    store: ControlStore,
    thread: Mapping[str, Any],
    *,
    seed: int,
    label: str,
    exchanges: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    current = dict(thread)
    for index, (role, content) in enumerate(exchanges, start=1):
        store.append_message(
            thread_id=current["id"],
            role=role,
            content=content,
            expected_revision=current["revision"],
            actor_id="local",
            idempotency_key=_key(seed, f"{label}-message-{index}"),
        )
        current = store.get_thread(current["id"])
    return current


def _bind_source(
    store: ControlStore, run: Mapping[str, Any], *, seed: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Register a paper, declare an intent for the run and resolve it."""

    source = store.register_source(
        authority="arxiv",
        authority_id=SYNTHETIC_ARXIV_ID,
        source_kind="paper",
        official_title=SYNTHETIC_PAPER_TITLE,
        engine_ref="paper:lorem-ipsum-synthetic",
        aliases=({"authority": "project", "value": "Lorem-Ipsum"},),
        actor_id="local",
        idempotency_key=_key(seed, "source-register"),
    ).value
    intent = store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title=SYNTHETIC_PAPER_TITLE,
        locator=SYNTHETIC_ARXIV_URL,
        candidates=tuple(
            observation.to_record()
            for observation in (
                CandidateObservation(
                    claim_kind="title",
                    authority="arxiv",
                    authority_id=SYNTHETIC_ARXIV_ID,
                    official_title=SYNTHETIC_PAPER_TITLE,
                    locator=SYNTHETIC_ARXIV_URL,
                ),
                CandidateObservation(
                    claim_kind="url",
                    authority="arxiv",
                    authority_id=SYNTHETIC_ARXIV_ID,
                    official_title=SYNTHETIC_PAPER_TITLE,
                    locator=SYNTHETIC_ARXIV_URL,
                ),
            )
        ),
        actor_id="local",
        idempotency_key=_key(seed, "source-intent"),
    ).value
    store.resolve_source_intent(
        intent_id=intent["id"],
        choice="use_source",
        expected_revision=intent["revision"],
        actor_id="local",
        idempotency_key=_key(seed, "source-resolve"),
    )
    return source, {**store.get_run(run["id"]), "attempt": run["attempt"]}


def _start_run(
    store: ControlStore, run: Mapping[str, Any], *, seed: int
) -> tuple[dict[str, Any], dict[str, str]]:
    """Reserve, bind and pin one gen9 runtime, then start the attempt."""

    attempt_id = str(run["attempt"]["id"])
    state_generation_id = f"sg-{_digest(f'{seed}:state-generation'.encode())[:16]}"
    artifact_digest = _digest(f"{seed}:runtime-artifact".encode())
    current = store.reserve_attempt_dispatch(
        run_id=run["id"],
        attempt_id=attempt_id,
        dispatch_owner="synthetic-worker",
        runtime_release_id=RUNTIME_RELEASE_ID,
        state_generation_id=state_generation_id,
        runtime_slot_id="slot-a",
        runtime_artifact_digest=artifact_digest,
        runtime_worker_protocol=RUNTIME_WORKER_PROTOCOL,
        expected_revision=run["revision"],
        actor_id="runtime",
        idempotency_key=_key(seed, "runtime-reserve"),
    ).value
    binding = store.create_runtime_binding(
        thread_id=run["thread_id"],
        adapter_id=RUNTIME_ADAPTER_ID,
        runtime_session_ref=f"synthetic-session-{seed}",
        generation=1,
        adapter_version=RUNTIME_ADAPTER_VERSION,
        actor_id="runtime",
        idempotency_key=_key(seed, "runtime-binding"),
    ).value
    runtime = {
        "attempt_id": attempt_id,
        "runtime_binding_id": binding["id"],
        "runtime_release_id": RUNTIME_RELEASE_ID,
        "state_generation_id": state_generation_id,
    }
    current = store.pin_attempt_runtime(
        run_id=run["id"],
        attempt_id=attempt_id,
        runtime_binding_id=binding["id"],
        runtime_release_id=RUNTIME_RELEASE_ID,
        state_generation_id=state_generation_id,
        dispatch_owner="synthetic-worker",
        expected_revision=current["revision"],
        actor_id="runtime",
        idempotency_key=_key(seed, "runtime-pin"),
    ).value
    for state in ("starting", "running"):
        current = store.apply_runtime_transition(
            run_id=run["id"],
            target_state=state,
            expected_revision=current["revision"],
            actor_id="runtime",
            idempotency_key=_key(seed, f"runtime-{state}"),
            **runtime,
        ).value
    return {**current, "attempt": {"id": attempt_id}}, runtime


def _decide(
    store: ControlStore,
    run: Mapping[str, Any],
    runtime: Mapping[str, str],
    *,
    seed: int,
) -> dict[str, Any]:
    """Record and resolve one ordinary (non-source) decision."""

    decision = store.create_decision(
        run_id=run["id"],
        attempt_id=runtime["attempt_id"],
        runtime_binding_id=runtime["runtime_binding_id"],
        runtime_release_id=runtime["runtime_release_id"],
        state_generation_id=runtime["state_generation_id"],
        expected_revision=run["revision"],
        kind="approval",
        prompt="Save the synthetic summary as an output?",
        options=(
            {"id": "approve", "label": "Approve"},
            {"id": "deny", "label": "Deny"},
        ),
        actor_id="runtime",
        idempotency_key=_key(seed, "decision-create"),
    ).value
    store.resolve_decision(
        decision_id=decision["id"],
        choice="approve",
        expected_revision=decision["revision"],
        actor_id="local",
        idempotency_key=_key(seed, "decision-resolve"),
    )
    return {**store.get_run(run["id"]), "attempt": run["attempt"]}


def _save_artifact(
    store: ControlStore,
    data_dir: Path,
    *,
    run: Mapping[str, Any],
    source: Mapping[str, Any],
    workspace: Mapping[str, Any],
    seed: int,
) -> dict[str, Any]:
    """Create an artifact version and materialize its bytes under the root."""

    root_path = data_dir / "artifacts" / RESEARCH_ARTIFACT_ROOT_ID
    root_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_path.chmod(0o700)
    root = store.register_asset_root(
        root_id=RESEARCH_ARTIFACT_ROOT_ID,
        private_path=root_path,
        max_bytes=1_048_576,
        enabled=True,
        actor_id="local",
        idempotency_key=_key(seed, "artifact-root"),
    )
    artifact = store.create_artifact(
        workspace_id=workspace["id"],
        thread_id=run["thread_id"],
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        kind="research-answer",
        title="Synthetic saved output",
        actor_id="local",
        idempotency_key=_key(seed, "artifact-create"),
    ).value
    content = _artifact_markdown(seed)
    reservation = store.request_artifact_version(
        artifact_id=artifact["id"],
        logical_version=1,
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        source_ids=(source["id"],),
        research_engine_refs=(source["engine_ref"],),
        generator={"name": "synthetic-state", "version": "1"},
        tool={"name": "synthetic-state", "version": "1"},
        parents=(),
        root_id=RESEARCH_ARTIFACT_ROOT_ID,
        relative_path=f"{artifact['id']}-v1.md",
        sha256=_digest(content),
        byte_length=len(content),
        media_type="text/markdown",
        advance_head=True,
        expected_head_revision=0,
        actor_id="local",
        idempotency_key=_key(seed, "artifact-version"),
    ).value
    materializer = FilesystemMaterializer(
        (AssetRoot(RESEARCH_ARTIFACT_ROOT_ID, root.private_path, root.max_bytes),)
    )
    saved = ArtifactMaterializationService(store, materializer).materialize_action(
        action_id=reservation["materialization_action"]["id"],
        content=content,
        worker_id="synthetic-worker",
    )
    return {"artifact_id": artifact["id"], "artifact_version_id": saved["id"]}


def _adopt_document(
    store: ControlStore, data_dir: Path, *, seed: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adopt one dossier document into an immutable, versioned copy."""

    originals = data_dir / "synthetic-originals" / "idea"
    originals.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = originals / "lorem-ipsum.md"
    document.write_text(_dossier_markdown(seed), encoding="utf-8")
    catalog = _SyntheticCatalog(
        document,
        kind="idea",
        origin_id=f"synthetic-idea-{seed:04d}",
        title="Lorem ipsum idea",
    )
    adopter = ResearchDocumentAdopter(store, catalog)
    preview = adopter.preview({"idea": originals})
    result = adopter.apply(
        preview, destination=data_dir / RESEARCH_DOCUMENT_ROOT_ID, actor_id="local"
    )
    return catalog.item, result["items"][0]["documents"][0]


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def _is_timestamp(column: str) -> bool:
    return column.endswith("_at")


#: Stands in for the data directory wherever a stored value repeats it.
_DATA_DIR_TOKEN = "<data-dir>"


def _scalar(column: str, value: Any, *, data_dir: str) -> Any:
    if _is_timestamp(column):
        return None if value is None else "<set>"
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"sha256": _digest(raw), "byte_length": len(raw)}
    if isinstance(value, str):
        # Two installations hold the same state at different prefixes; the
        # location of a root relative to the data directory is the identity,
        # the absolute prefix is not. Receipts embed the same path inside
        # their JSON, so the substitution is on the whole text.
        value = value.replace(data_dir, _DATA_DIR_TOKEN)
        if len(value) > _MAX_INLINE_TEXT:
            return {"sha256": _digest(value.encode("utf-8")), "byte_length": len(value)}
    return value


def _control_database(data_dir: Path) -> Path:
    return data_dir / "control.db"


def _read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_rows(
    conn: sqlite3.Connection, table: str, *, data_dir: str
) -> list[dict[str, Any]]:
    columns = [str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
    rows = [
        {column: _scalar(column, row[column], data_dir=data_dir) for column in columns}
        for row in conn.execute(f'SELECT * FROM "{table}"')
    ]
    # Sorted by rendered content: the physical rowid order is not identity, and
    # no table selected here exposes its rowid as a public column.
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    return rows


def _is_control_sidecar(path: Path, database: Path) -> bool:
    """True for the control database, its WAL/SHM files and its dot sidecars.

    The binding key is 32 random bytes minted at `initialize()`; it is both a
    secret and a file that differs between two otherwise identical generates,
    so it is excluded by name rather than digested. The initialization lock is
    empty bookkeeping.
    """

    if path.name.startswith(database.name):
        return True
    # `.control.db.transport.key` and `.control.db.initialize.lock`.
    return path.name.startswith(f".{database.name}.")


#: The materializer's private idempotency bookkeeping. Its file names embed a
#: root capability derived from the root directory's device and inode
#: (`artifacts/materializer.py:724-731`), so they differ between any two
#: directories -- including the same path recreated -- while carrying no
#: product state. Artifact and document bytes live outside it.
_MATERIALIZER_STATE_DIR = ".cortex-artifacts-v1"


def _asset_files(data_dir: Path, database: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if _is_control_sidecar(path, database):
            continue
        relative = path.relative_to(data_dir)
        if _MATERIALIZER_STATE_DIR in relative.parts:
            continue
        yield relative.as_posix(), path


def snapshot(data_dir: Path) -> dict[str, Any]:
    """Read one data directory without writing to it."""

    data_dir = data_dir.expanduser().resolve()
    database = _control_database(data_dir)
    if not database.is_file():
        raise SystemExit(f"no control database at {database}")
    conn = _read_only(database)
    try:
        version_row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        tables = sorted(
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
            if not str(row["name"]).startswith("sqlite_")
        )
        table_rows = {
            table: _table_rows(conn, table, data_dir=str(data_dir))
            for table in tables
        }
    finally:
        conn.close()
    assets = {
        relative: {
            "sha256": _digest(path.read_bytes()),
            "byte_length": path.stat().st_size,
        }
        for relative, path in _asset_files(data_dir, database)
    }
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "control_schema_version": None if version_row is None else version_row[0],
        # Empty tables are kept: a table that loses all its rows is a
        # difference, and a table that disappears is a different difference.
        "tables": table_rows,
        "assets": assets,
    }


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------


def _row_multiset(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        rendered = json.dumps(row, sort_keys=True, ensure_ascii=False)
        counts[rendered] = counts.get(rendered, 0) + 1
    return counts


def _table_difference(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    left_counts, right_counts = _row_multiset(left), _row_multiset(right)
    only_left = [
        rendered
        for rendered, count in sorted(left_counts.items())
        for _ in range(max(0, count - right_counts.get(rendered, 0)))
    ]
    only_right = [
        rendered
        for rendered, count in sorted(right_counts.items())
        for _ in range(max(0, count - left_counts.get(rendered, 0)))
    ]
    return only_left, only_right


def compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    """Return one compact line per difference; empty means identical."""

    lines: list[str] = []
    if left.get("control_schema_version") != right.get("control_schema_version"):
        lines.append(
            "control schema version: "
            f"{left.get('control_schema_version')} != {right.get('control_schema_version')}"
        )
    left_tables: Mapping[str, Any] = left.get("tables", {})
    right_tables: Mapping[str, Any] = right.get("tables", {})
    for table in sorted(set(left_tables) - set(right_tables)):
        lines.append(f"table {table}: only in A ({len(left_tables[table])} rows)")
    for table in sorted(set(right_tables) - set(left_tables)):
        lines.append(f"table {table}: only in B ({len(right_tables[table])} rows)")
    for table in sorted(set(left_tables) & set(right_tables)):
        only_left, only_right = _table_difference(
            left_tables[table], right_tables[table]
        )
        if not only_left and not only_right:
            continue
        lines.append(
            f"table {table}: {len(only_left)} row(s) only in A, "
            f"{len(only_right)} row(s) only in B"
        )
        for rendered in only_left[:3]:
            lines.append(f"  A only: {rendered}")
        for rendered in only_right[:3]:
            lines.append(f"  B only: {rendered}")
    left_assets: Mapping[str, Any] = left.get("assets", {})
    right_assets: Mapping[str, Any] = right.get("assets", {})
    for name in sorted(set(left_assets) - set(right_assets)):
        lines.append(f"asset {name}: only in A")
    for name in sorted(set(right_assets) - set(left_assets)):
        lines.append(f"asset {name}: only in B")
    for name in sorted(set(left_assets) & set(right_assets)):
        if left_assets[name] != right_assets[name]:
            lines.append(
                f"asset {name}: {left_assets[name]['sha256'][:12]} != "
                f"{right_assets[name]['sha256'][:12]}"
            )
    return lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synthetic_state",
        description="Synthetic Control schema-19 state for the continuity gate.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("generate", help="write synthetic state")
    create.add_argument("--data-dir", required=True, type=Path)
    create.add_argument("--seed", default=0, type=int)

    read = commands.add_parser("snapshot", help="emit a semantic snapshot")
    read.add_argument("--data-dir", required=True, type=Path)
    read.add_argument("--out", required=True, type=Path)

    diff = commands.add_parser("compare", help="compare two snapshots")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "generate":
        summary = generate(arguments.data_dir, seed=arguments.seed)
        print(json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "snapshot":
        payload = snapshot(arguments.data_dir)
        _write_json(arguments.out, payload)
        print(
            f"schema {payload['control_schema_version']}, "
            f"{sum(len(rows) for rows in payload['tables'].values())} rows, "
            f"{len(payload['assets'])} asset file(s) -> {arguments.out}"
        )
        return 0
    left = json.loads(arguments.left.read_text(encoding="utf-8"))
    right = json.loads(arguments.right.read_text(encoding="utf-8"))
    differences = compare(left, right)
    if not differences:
        print("identical")
        return 0
    for line in differences:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
