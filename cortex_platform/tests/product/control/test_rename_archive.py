from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    RevisionConflict,
    ThreadActiveRun,
    ThreadArchived,
)
from cortex_platform.product.control.schema import MIGRATION_VERSIONS, THREAD_ARCHIVE_MIGRATION


def _key(name: str) -> str:
    """The store demands 16-128 URL-safe characters; keep the readable name."""

    return f"{re.sub(r'[^A-Za-z0-9_-]', '-', name)}-000000000000000"[:128]


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(tmp_path / "control.db")
    value.initialize()
    return value


def _workspace(store: ControlStore, title: str = "Echo memory") -> dict:
    return store.create_workspace(title=title, actor_id="local", idempotency_key=_key(f"ws-{title}")).value


def _thread(store: ControlStore, workspace: dict, title: str = "First question") -> dict:
    return store.create_thread(
        workspace_id=workspace["id"], title=title, expected_revision=workspace["revision"],
        actor_id="local", idempotency_key=_key(f"thread-{title}"),
    ).value


def test_schema_18_adds_archived_at_and_is_recorded(store: ControlStore) -> None:
    assert THREAD_ARCHIVE_MIGRATION == 18
    assert THREAD_ARCHIVE_MIGRATION in MIGRATION_VERSIONS
    with sqlite3.connect(store.path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        assert "archived_at" in columns
        assert (18,) in conn.execute("SELECT version FROM schema_migrations").fetchall()


def test_rename_workspace_bumps_revision_audits_and_replays(store: ControlStore) -> None:
    workspace = _workspace(store)
    first = store.rename_workspace(workspace_id=workspace["id"], title="Echo v2", expected_revision=0, actor_id="local", idempotency_key=_key("rename-1"))
    assert first.status_code == 200 and first.value["title"] == "Echo v2" and first.value["revision"] == 1
    replay = store.rename_workspace(workspace_id=workspace["id"], title="Echo v2", expected_revision=0, actor_id="local", idempotency_key=_key("rename-1"))
    assert replay.replayed and replay.value == first.value
    with pytest.raises(RevisionConflict):
        store.rename_workspace(workspace_id=workspace["id"], title="Stale", expected_revision=0, actor_id="local", idempotency_key=_key("rename-2"))
    with pytest.raises(ValueError):
        store.rename_workspace(workspace_id=workspace["id"], title="   ", expected_revision=1, actor_id="local", idempotency_key=_key("rename-3"))
    with sqlite3.connect(store.path) as conn:
        kinds = [row[0] for row in conn.execute("SELECT type FROM control_audit WHERE aggregate_id = ? ORDER BY rowid", (workspace["id"],))]
    assert kinds[-1] == "workspace.renamed"


def test_rename_thread_touches_only_the_thread(store: ControlStore) -> None:
    workspace = _workspace(store)
    thread = _thread(store, workspace)
    result = store.rename_thread(thread_id=thread["id"], title="Sharper question", expected_revision=0, actor_id="local", idempotency_key=_key("tr-1"))
    assert result.value["title"] == "Sharper question" and result.value["revision"] == 1
    assert store.get_workspace(workspace["id"])["revision"] == 1  # unchanged by the rename


def test_archive_hides_unarchive_restores_and_both_are_idempotent(store: ControlStore) -> None:
    workspace = _workspace(store)
    thread = _thread(store, workspace)
    archived = store.archive_thread(thread_id=thread["id"], expected_revision=0, actor_id="local", idempotency_key=_key("a-1")).value
    assert archived["archived_at"] is not None and archived["revision"] == 1
    assert [t["id"] for t in store.list_threads(workspace_id=workspace["id"])] == []
    assert [t["id"] for t in store.list_threads(workspace_id=workspace["id"], include_archived=True)] == [thread["id"]]
    again = store.archive_thread(thread_id=thread["id"], expected_revision=1, actor_id="local", idempotency_key=_key("a-2")).value
    assert again["revision"] == 1  # no-op: no bump
    restored = store.unarchive_thread(thread_id=thread["id"], expected_revision=1, actor_id="local", idempotency_key=_key("u-1")).value
    assert restored["archived_at"] is None and restored["revision"] == 2
    assert [t["id"] for t in store.list_threads(workspace_id=workspace["id"])] == [thread["id"]]


def test_archive_refuses_a_thread_with_an_active_run(store: ControlStore) -> None:
    workspace = _workspace(store)
    thread = _thread(store, workspace)
    store.append_message(thread_id=thread["id"], role="user", content="hello", expected_revision=0, actor_id="local", idempotency_key=_key("m-1"))
    run = store.create_run(thread_id=thread["id"], expected_revision=1, actor_id="local", idempotency_key=_key("r-1")).value
    current = store.get_thread(thread["id"])
    assert current["active_run_id"] == run["id"]
    with pytest.raises(ThreadActiveRun) as excinfo:
        store.archive_thread(thread_id=thread["id"], expected_revision=current["revision"], actor_id="local", idempotency_key=_key("a-1"))
    assert excinfo.value.category == "thread_active_run"


def test_an_archived_thread_takes_no_message_and_starts_no_run(store: ControlStore) -> None:
    """The archived set holds nothing that can start moving again.

    Archiving refuses a thread whose run is still active, which only settles
    the instant of archiving. Telegram and a direct API call reach the same
    two writers afterwards, so the refusal lives on them too.
    """

    workspace = _workspace(store)
    thread = _thread(store, workspace)
    archived = store.archive_thread(thread_id=thread["id"], expected_revision=0, actor_id="local", idempotency_key=_key("a-1")).value
    with pytest.raises(ThreadArchived) as refused_message:
        store.append_message(thread_id=thread["id"], role="user", content="still there?", expected_revision=archived["revision"], actor_id="local", idempotency_key=_key("m-1"))
    assert refused_message.value.category == "thread_archived"
    assert refused_message.value.current["id"] == thread["id"]
    with pytest.raises(ThreadArchived):
        store.create_run(thread_id=thread["id"], expected_revision=archived["revision"], actor_id="local", idempotency_key=_key("r-1"))
    # Nothing durable was written by either refusal.
    assert store.list_messages(thread["id"]) == []
    assert store.get_thread(thread["id"])["active_run_id"] is None

    restored = store.unarchive_thread(thread_id=thread["id"], expected_revision=archived["revision"], actor_id="local", idempotency_key=_key("u-1")).value
    saved = store.append_message(thread_id=thread["id"], role="user", content="still there?", expected_revision=restored["revision"], actor_id="local", idempotency_key=_key("m-2")).value
    run = store.create_run(thread_id=thread["id"], expected_revision=restored["revision"] + 1, actor_id="local", idempotency_key=_key("r-2")).value
    assert saved["content"] == "still there?" and run["state"] == "queued"
