"""What `tools/synthetic_state.py` guarantees about the continuity fixture.

The upgrade/rollback gate compares a data directory before and after a
candidate touches it, so the tool is only evidence if a repeat run is
byte-for-byte equal semantically and a single changed row or byte is reported.
Both are pinned here, together with the one property the installed candidate
actually consumes: the generated database is a real Control store at schema 19.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.schema import SCHEMA_VERSION
from tools.synthetic_state import main


def _generate(data_dir: Path, *, seed: int = 7) -> None:
    assert main(["generate", "--data-dir", str(data_dir), "--seed", str(seed)]) == 0


def _snapshot(data_dir: Path, out: Path) -> dict:
    assert main(["snapshot", "--data-dir", str(data_dir), "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _compare(left: Path, right: Path) -> int:
    return main(["compare", str(left), str(right)])


def test_repeat_generation_at_one_path_is_semantically_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    _generate(data_dir)
    first = _snapshot(data_dir, tmp_path / "a.json")

    # The asset-root receipts hash the absolute destination, so the fixture is
    # regenerated at the same path -- which is also how the gate uses it: one
    # prefix, observed before and after an upgrade.
    for path in sorted(data_dir.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    _generate(data_dir)
    second = _snapshot(data_dir, tmp_path / "b.json")

    assert first == second
    capsys.readouterr()
    assert _compare(tmp_path / "a.json", tmp_path / "b.json") == 0
    assert capsys.readouterr().out.strip() == "identical"


def test_generated_state_covers_the_operations_the_gate_replays(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _generate(data_dir)
    snapshot = _snapshot(data_dir, tmp_path / "a.json")
    tables = snapshot["tables"]

    assert snapshot["control_schema_version"] == SCHEMA_VERSION == 19
    assert len(tables["workspaces"]) == 1
    assert len(tables["threads"]) == 2
    assert len(tables["messages"]) >= 4
    assert len(tables["runs"]) == len(tables["attempts"]) == 1
    assert len(tables["decisions"]) >= 1
    # gen9 runtime and native-session binding for the run.
    assert len(tables["runtime_bindings"]) == 1
    assert tables["runtime_bindings"][0]["adapter_id"] == "hermes"
    assert tables["attempts"][0]["runtime_release_id"] == "hermes-0.15.0-gen9"
    assert tables["attempts"][0]["runtime_worker_protocol"] == "cortex-worker/2"
    # One imported source bound to the run, one saved artifact version, one
    # adopted document version and its item/thread association.
    assert len(tables["sources"]) == len(tables["run_source_bindings"]) == 1
    assert len(tables["artifact_versions"]) == 1
    assert len(tables["research_items"]) == 1
    assert len(tables["research_document_versions"]) == 1
    assert len(tables["research_thread_items"]) == 1

    assets = snapshot["assets"]
    assert any(name.startswith("artifacts/") for name in assets)
    assert any(name.startswith("research-documents/") for name in assets)


def test_generated_database_opens_as_a_schema_19_control_store(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _generate(data_dir)

    store = ControlStore(data_dir / "control.db")
    store.initialize()
    identity = store.current_control_store_identity()

    assert identity.schema_version == 19
    workspaces = [
        store.get_workspace(row["workspace_id"])
        for row in [store.get_thread(thread) for thread in _thread_ids(store)]
    ]
    assert {workspace["title"] for workspace in workspaces} == {
        "Synthetic Research Workspace"
    }


def _thread_ids(store: ControlStore) -> list[str]:
    with store._connect() as conn:
        return [str(row["id"]) for row in conn.execute("SELECT id FROM threads")]


def test_one_changed_message_is_reported_by_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    _generate(data_dir)
    _snapshot(data_dir, tmp_path / "a.json")

    store = ControlStore(data_dir / "control.db")
    store.initialize()
    thread_id = _thread_ids(store)[0]
    store.append_message(
        thread_id=thread_id,
        role="user",
        content="An extra turn the first snapshot never saw.",
        expected_revision=store.get_thread(thread_id)["revision"],
        actor_id="local",
        idempotency_key="mutation-message-0000001",
    )
    _snapshot(data_dir, tmp_path / "b.json")

    capsys.readouterr()
    assert _compare(tmp_path / "a.json", tmp_path / "b.json") == 1
    report = capsys.readouterr().out
    assert "table messages: 0 row(s) only in A, 1 row(s) only in B" in report


def test_one_changed_artifact_byte_is_reported_by_asset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    _generate(data_dir)
    first = _snapshot(data_dir, tmp_path / "a.json")

    name = next(
        name for name in first["assets"] if name.startswith("artifacts/")
    )
    target = data_dir.joinpath(*name.split("/"))
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"tampered\n")
    _snapshot(data_dir, tmp_path / "b.json")

    capsys.readouterr()
    assert _compare(tmp_path / "a.json", tmp_path / "b.json") == 1
    assert f"asset {name}: " in capsys.readouterr().out
