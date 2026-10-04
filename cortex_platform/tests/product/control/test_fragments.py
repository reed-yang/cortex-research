"""Idea fragments: verbatim, save-only operator text with no side effects."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    NotFound,
)
from cortex_platform.product.control import schema
from cortex_platform.product.control.research_store import research_item_id
from cortex_platform.product.control.schema import (
    IDEA_FRAGMENTS_MIGRATION,
    MIGRATION_VERSIONS,
    SCHEMA_VERSION,
)

FRAGMENT_KEYS = {
    "id",
    "text",
    "note",
    "origin",
    "thread_id",
    "context_item_id",
    "created_at",
}


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


class MovableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MovableClock) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    value.initialize()
    return value


def _key(value: str) -> str:
    return f"fragment-{value}-0000000000"


def _thread(store: ControlStore, name: str = "one") -> dict:
    workspace = store.create_workspace(
        title="Ideas", actor_id="local", idempotency_key=_key(f"ws-{name}")
    ).value
    return store.create_thread(
        workspace_id=workspace["id"],
        title="Idea thread",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=_key(f"thread-{name}"),
    ).value


def _web(store: ControlStore, text: str, *, note: str = "", key: str = "web-1"):
    return store.create_fragment(
        text=text,
        note=note,
        origin="web",
        thread_id=None,
        origin_ref=None,
        actor_id="local",
        idempotency_key=_key(key),
    )


def _telegram(store: ControlStore, text: str, *, thread_id: str, key: str = "tg-1"):
    return store.create_fragment(
        text=text,
        note="",
        origin="telegram",
        thread_id=thread_id,
        origin_ref=f"tg-{key}",
        actor_id="telegram-actor-synthetic",
        idempotency_key=_key(key),
    )


def _insert(conn: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": "fragment-raw",
        "text": "an idea",
        "note": "",
        "origin": "web",
        "origin_ref": None,
        "thread_id": None,
        "context_item_id": None,
        "actor_id": "local",
        "created_at": "2026-09-01T12:00:00Z",
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO idea_fragments ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _counts(store: ControlStore) -> dict[str, int]:
    with sqlite3.connect(store.path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("messages", "runs", "captures", "run_events", "sources")
        }


# -- migration -----------------------------------------------------------------


def test_the_fragment_migration_is_the_newest_and_creates_the_table(
    store: ControlStore,
) -> None:
    assert SCHEMA_VERSION == IDEA_FRAGMENTS_MIGRATION == max(MIGRATION_VERSIONS)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(idea_fragments)")
        }
        assert columns == FRAGMENT_KEYS | {"origin_ref", "actor_id"}
        assert [
            str(row[2])
            for row in conn.execute("PRAGMA index_info(idea_fragments_created_idx)")
        ] == ["created_at", "id"]


def test_the_fragment_table_pins_length_and_enum_checks(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        _insert(conn, id="ok-empty-note", note="")
        _insert(conn, id="ok-max-text", text="t" * 16_384)
        _insert(conn, id="ok-max-note", note="n" * 2_000)
        for index, overrides in enumerate(
            (
                {"text": ""},
                {"text": "t" * 16_385},
                {"note": "n" * 2_001},
                {"origin": "email"},
                {"actor_id": ""},
                {"actor_id": "a" * 201},
                {
                    "origin": "telegram",
                    "thread_id": "thread-x",
                    "origin_ref": "",
                },
                {
                    "origin": "telegram",
                    "thread_id": "thread-x",
                    "origin_ref": "r" * 201,
                },
            )
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(conn, id=f"bad-{index}", **overrides)


@pytest.mark.parametrize(
    ("origin", "origin_ref", "thread_id"),
    [
        ("telegram", None, "thread-x"),
        ("telegram", "tg-ref", None),
        ("telegram", None, None),
        ("web", "tg-ref", None),
        ("web", None, "thread-x"),
        ("web", "tg-ref", "thread-x"),
    ],
)
def test_the_origin_check_refuses_each_forbidden_combination(
    store: ControlStore,
    origin: str,
    origin_ref: str | None,
    thread_id: str | None,
) -> None:
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, origin=origin, origin_ref=origin_ref, thread_id=thread_id)


def test_the_origin_check_admits_exactly_the_two_allowed_shapes(
    store: ControlStore,
) -> None:
    with sqlite3.connect(store.path) as conn:
        _insert(conn, id="web-ok")
        _insert(
            conn,
            id="telegram-ok",
            origin="telegram",
            origin_ref="tg-ref",
            thread_id="thread-x",
            context_item_id="ri_x",
        )
        # A context item is provenance of a thread, never of a Web save.
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="web-with-item", context_item_id="ri_x")


def test_fragments_cannot_be_updated_or_deleted(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="idea fragments are immutable"):
            conn.execute("UPDATE idea_fragments SET note = 'x' WHERE id = 'fragment-raw'")
        with pytest.raises(sqlite3.IntegrityError, match="idea fragments are immutable"):
            conn.execute("DELETE FROM idea_fragments WHERE id = 'fragment-raw'")


def test_an_interrupted_fragment_migration_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "control.db"
    with sqlite3.connect(database) as seed:
        for version, script in schema.migration_scripts():
            if version >= IDEA_FRAGMENTS_MIGRATION:
                break
            seed.executescript(script)
            seed.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )
    original = schema._execute_script_in_transaction
    fragments_script = dict(schema.migration_scripts())[IDEA_FRAGMENTS_MIGRATION]

    def interrupt(conn: sqlite3.Connection, script: str) -> None:
        original(conn, script)
        if script == fragments_script:
            raise RuntimeError("simulated migration interruption")

    with sqlite3.connect(database, isolation_level=None) as conn:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(conn, now="2026-09-01T12:00:00.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(index,) for index in range(1, IDEA_FRAGMENTS_MIGRATION)]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'idea_fragments%'"
        ).fetchone() == (0,)

        monkeypatch.setattr(schema, "_execute_script_in_transaction", original)
        schema.apply_migrations(conn, now="2026-09-01T12:00:01.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]


# -- create ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "  leading and trailing spaces  ",
        "line one\r\nline two\r\n",
        "\n\nblank first lines",
        "多行的想法\n第二行：视频生成 🎬",
        "\t tabbed idea \t",
        "字" * 16_384,
    ],
)
def test_create_stores_the_text_exactly_as_submitted(
    store: ControlStore, text: str
) -> None:
    result = _web(store, text, note="  a note kept as is ")

    assert result.status_code == 201 and result.replayed is False
    assert set(result.value) == FRAGMENT_KEYS
    assert result.value["text"] == text
    assert result.value["note"] == "  a note kept as is "
    assert result.value["origin"] == "web"
    assert result.value["thread_id"] is None
    assert result.value["context_item_id"] is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT text FROM idea_fragments").fetchone() == (text,)
    assert store.get_fragment(result.value["id"]) == result.value


@pytest.mark.parametrize(
    ("text", "note"),
    [
        ("", ""),
        ("   \n\t ", ""),
        ("t" * 16_385, ""),
        ("\x00leading nul", ""),
        ("valid", "n" * 2_001),
        ("valid", "nul\x00inside"),
        (None, ""),
        ("valid", None),
    ],
)
def test_create_refuses_invalid_text_or_note_and_writes_nothing(
    store: ControlStore, text: object, note: object
) -> None:
    with pytest.raises(ValueError):
        _web(store, text, note=note)  # type: ignore[arg-type]
    assert store.list_fragments() == []


def test_the_same_key_replays_and_a_changed_text_conflicts(
    store: ControlStore,
) -> None:
    first = _web(store, "an idea", key="replay")
    replay = _web(store, "an idea", key="replay")

    assert replay.replayed is True and replay.status_code == 201
    assert replay.value == first.value
    assert len(store.list_fragments()) == 1
    with pytest.raises(IdempotencyConflict):
        _web(store, "a different idea", key="replay")
    assert len(store.list_fragments()) == 1


def test_identical_text_under_two_keys_is_two_fragments(store: ControlStore) -> None:
    first = _web(store, "same words", key="first")
    second = _web(store, "same words", key="second")

    assert first.value["id"] != second.value["id"]
    assert len(store.list_fragments()) == 2


def test_a_telegram_save_records_its_thread_and_selected_item(
    store: ControlStore,
) -> None:
    thread = _thread(store)
    item_id = research_item_id("idea", "synthetic-origin")
    with store._connect() as conn:  # noqa: SLF001 - one adopted row is enough
        conn.execute(
            "INSERT INTO research_items(id, kind, origin_id, title, created_at) "
            "VALUES (?, 'idea', 'synthetic-origin', 'Synthetic', 'old')",
            (item_id,),
        )
    store.select_research_item(
        thread_id=thread["id"],
        item_id=item_id,
        actor_id="local",
        idempotency_key=_key("select"),
    )

    saved = _telegram(store, "from the bot", thread_id=thread["id"])

    assert saved.value["origin"] == "telegram"
    assert saved.value["thread_id"] == thread["id"]
    assert saved.value["context_item_id"] == item_id
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT origin_ref, actor_id FROM idea_fragments"
        ).fetchone() == ("tg-tg-1", "telegram-actor-synthetic")


def test_a_thread_without_a_selected_item_records_no_context_item(
    store: ControlStore,
) -> None:
    thread = _thread(store)

    saved = _telegram(store, "no item here", thread_id=thread["id"])

    assert saved.value["thread_id"] == thread["id"]
    assert saved.value["context_item_id"] is None


def test_an_unknown_thread_is_not_found_and_writes_nothing(
    store: ControlStore,
) -> None:
    with pytest.raises(NotFound):
        _telegram(store, "orphan", thread_id="thread-missing")
    assert store.list_fragments() == []


@pytest.mark.parametrize(
    ("origin", "thread_id", "origin_ref"),
    [
        ("web", "thread-1", None),
        ("web", None, "tg-ref"),
        ("telegram", None, "tg-ref"),
        ("telegram", "thread-1", None),
        ("telegram", "thread-1", ""),
        ("email", None, None),
    ],
)
def test_origin_provenance_is_validated_before_any_write(
    store: ControlStore,
    origin: str,
    thread_id: str | None,
    origin_ref: str | None,
) -> None:
    _thread(store)
    with pytest.raises(ValueError):
        store.create_fragment(
            text="an idea",
            note="",
            origin=origin,
            thread_id=thread_id,
            origin_ref=origin_ref,
            actor_id="local",
            idempotency_key=_key("provenance"),
        )
    assert store.list_fragments() == []


def test_saving_creates_no_message_run_capture_or_source(store: ControlStore) -> None:
    thread = _thread(store)
    before = _counts(store)
    thread_before = store.get_thread(thread["id"])

    _web(store, "web idea")
    _telegram(store, "telegram idea", thread_id=thread["id"])

    assert _counts(store) == before
    assert store.get_thread(thread["id"]) == thread_before


def test_the_audit_row_names_the_save_and_never_carries_the_text(
    store: ControlStore,
) -> None:
    saved = _web(store, "secret wording of an idea", note="private note")

    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT aggregate_type, aggregate_id, type, payload_json FROM control_audit "
            "WHERE aggregate_type = 'fragment'"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("fragment", saved.value["id"], "fragment.saved")
    ]
    assert json.loads(rows[0][3]) == {"origin": "web"}


# -- read --------------------------------------------------------------------------


def test_list_is_newest_first_and_pages_by_a_proven_cursor(
    store: ControlStore, clock: MovableClock
) -> None:
    ids = []
    for index in range(3):
        ids.append(_web(store, f"idea {index}", key=f"list-{index}").value["id"])
        clock.advance(1)

    first = store.list_fragments(limit=2)
    assert [item["id"] for item in first] == [ids[2], ids[1]]
    assert first.next_cursor == ids[1]

    second = store.list_fragments(limit=2, cursor=first.next_cursor)
    assert [item["id"] for item in second] == [ids[0]]
    assert second.next_cursor is None


def test_an_exactly_full_last_page_has_no_cursor(
    store: ControlStore, clock: MovableClock
) -> None:
    for index in range(4):
        _web(store, f"idea {index}", key=f"full-{index}")
        clock.advance(1)

    first = store.list_fragments(limit=2)
    second = store.list_fragments(limit=2, cursor=first.next_cursor)

    assert len(second) == 2 and second.next_cursor is None


def test_a_later_save_in_the_same_second_lists_first(
    store: ControlStore, clock: MovableClock
) -> None:
    # A whole-second time keeps its fraction in the stored string; otherwise
    # "...12:00:00Z" would sort after "...12:00:00.400000Z" saved later.
    earlier = _web(store, "on the second", key="frac-0").value
    clock.now = clock.now + timedelta(microseconds=400_000)
    later = _web(store, "later in the same second", key="frac-1").value

    assert earlier["created_at"] == "2026-09-01T12:00:00.000000Z"
    assert later["created_at"] == "2026-09-01T12:00:00.400000Z"
    first = store.list_fragments(limit=1)
    assert [item["id"] for item in first] == [later["id"]]
    second = store.list_fragments(limit=1, cursor=first.next_cursor)
    assert [item["id"] for item in second] == [earlier["id"]]
    assert second.next_cursor is None


def test_equal_timestamps_page_by_id_without_loss(store: ControlStore) -> None:
    # The clock never moves, so every row shares created_at.
    saved = [_web(store, f"idea {index}", key=f"tie-{index}").value["id"] for index in range(5)]

    seen: list[str] = []
    cursor = None
    while True:
        page = store.list_fragments(limit=2, cursor=cursor)
        seen.extend(item["id"] for item in page)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == sorted(saved, reverse=True)


@pytest.mark.parametrize("limit", [0, 1_001, True, "2"])
def test_list_refuses_an_invalid_limit(store: ControlStore, limit: object) -> None:
    with pytest.raises(ValueError):
        store.list_fragments(limit=limit)  # type: ignore[arg-type]


def test_list_refuses_an_unknown_cursor(store: ControlStore) -> None:
    with pytest.raises(ValueError):
        store.list_fragments(cursor="fragment-missing")


def test_get_of_a_missing_fragment_is_not_found(store: ControlStore) -> None:
    with pytest.raises(NotFound):
        store.get_fragment("fragment-missing")
