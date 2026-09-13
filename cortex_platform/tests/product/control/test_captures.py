"""The capture inbox: raw operator staging with an explicit approval gate."""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control.schema import SCHEMA_VERSION

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.schema import MIGRATION_VERSIONS


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


class MovableClock:
    """Leases are the only capture behaviour that depends on time passing."""

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
        tmp_path / "control.db",
        clock=clock,
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value


def _insert_source(store: ControlStore, source_id: str) -> str:
    """Adopt one real source row so a completion can name it."""

    with store._connect() as conn:
        conn.execute(
            """INSERT INTO sources
               (id, authority, authority_id, canonical_id, source_kind,
                official_title, engine_ref, import_state, revision,
                created_at, updated_at)
               VALUES (?, 'arxiv', ?, ?, 'paper', 'A Paper', ?, 'imported',
                       0, '2026-09-01T12:00:00+00:00',
                       '2026-09-01T12:00:00+00:00')""",
            (source_id, source_id, f"canonical:{source_id}", f"paper/{source_id}"),
        )
        conn.commit()
    return source_id


def _insert(conn: sqlite3.Connection, **overrides: object) -> None:
    row = {
        "id": "capture-1",
        "capture_key": "https://example.com",
        "payload": "https://example.com",
        "kind": "url",
        "note": "",
        "state": "pending",
        "claim_owner": None,
        "claim_epoch": 0,
        "claim_expires_at": None,
        "known_source_id": None,
        "consumed_source_ids": None,
        "failure_category": None,
        "revision": 0,
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:00:00Z",
    }
    row.update(overrides)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO captures({columns}) VALUES ({marks})", tuple(row.values())
    )


def test_migration_13_creates_the_capture_inbox_table(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
        columns = {
            str(row[1]): row for row in conn.execute("PRAGMA table_info(captures)")
        }
        assert set(columns) == {
            "id",
            "capture_key",
            "payload",
            "kind",
            "note",
            "state",
            "claim_owner",
            "claim_epoch",
            "claim_expires_at",
            "known_source_id",
            "consumed_source_ids",
            "failure_category",
            "revision",
            "created_at",
            "updated_at",
        }
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "captures_key_state_idx" in indexes
        assert [
            str(row[2])
            for row in conn.execute("PRAGMA index_info(captures_key_state_idx)")
        ] == ["capture_key", "state"]


def test_migration_13_pins_the_capture_state_and_length_checks(
    store: ControlStore,
) -> None:
    with sqlite3.connect(store.path) as conn:
        # The approved state is the explicit operator decision the product
        # shell invariant requires before any import may run.
        for state in (
            "pending",
            "approved",
            "claimed",
            "uncertain",
            "consumed",
            "dismissed",
            "failed",
        ):
            _insert(conn, id=f"capture-{state}", state=state)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="capture-bad-state", state="staged")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="capture-bad-kind", kind="pdf")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="capture-empty-payload", payload="")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="capture-long-payload", payload="a" * 16_385)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="capture-long-note", note="n" * 2_001)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(
                conn,
                id="capture-long-category",
                failure_category="c" * 101,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, id="capture-empty-category", failure_category="")
        # An empty note is legal: the schema bounds it at <= 2000, not 1..2000.
        _insert(conn, id="capture-empty-note", note="")


def test_migration_13_refuses_to_delete_a_capture(store: ControlStore) -> None:
    with sqlite3.connect(store.path) as conn:
        _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="capture"):
            conn.execute("DELETE FROM captures WHERE id = 'capture-1'")


def test_interrupted_migration_13_leaves_no_capture_table_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every migration boundary in this store is pinned this way.

    A migration that half-applies is the worst failure mode a control store
    has, because the next start sees tables it cannot account for.
    """

    from cortex_platform.product.control import schema

    database = tmp_path / "control.db"
    with sqlite3.connect(database) as seed:
        # Walked off `migration_scripts()` rather than `_MIGRATION_{n}`: the
        # scripts are named after what they create, not after the number they
        # were given, and a renumber moves the number only.
        for version, script in schema.migration_scripts():
            if version >= schema.CAPTURES_MIGRATION:
                break
            seed.executescript(script)
            seed.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )
    original = schema._execute_script_in_transaction
    # The script this test interrupts, indexed by the constant rather than
    # named by its number, for the same reason the replay above is.
    captures_script = dict(schema.migration_scripts())[schema.CAPTURES_MIGRATION]

    def interrupt(conn: sqlite3.Connection, script: str) -> None:
        if script == captures_script:
            conn.execute("CREATE TABLE interrupted_capture_migration(id TEXT)")
            raise RuntimeError("simulated migration interruption")
        original(conn, script)

    with sqlite3.connect(database, isolation_level=None) as conn:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(conn, now="2026-09-01T12:00:00.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(index,) for index in range(1, 13)]
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type = 'table' AND name IN (
                   'captures',
                   'interrupted_capture_migration'
               )"""
        ).fetchone() == (0,)

        monkeypatch.setattr(schema, "_execute_script_in_transaction", original)
        schema.apply_migrations(conn, now="2026-09-01T12:00:01.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]


def _capture(
    store: ControlStore,
    payload: str,
    *,
    note: str = "",
    key: str = "capture-key-00001",
):
    return store.create_capture(
        payload=payload,
        note=note,
        actor_id="local-operator",
        idempotency_key=key,
    )


def test_capture_stores_the_payload_raw_and_starts_pending(
    store: ControlStore,
) -> None:
    result = _capture(store, "  https://example.com/post  ", note="read later")

    assert result.status_code == 201
    assert result.replayed is False
    value = result.value
    # The submission is stored exactly as submitted; only the dedup key is
    # derived from the trimmed string.
    assert value["payload"] == "  https://example.com/post  "
    assert value["capture_key"] == "https://example.com/post"
    assert value["note"] == "read later"
    assert value["state"] == "pending"
    assert value["revision"] == 0
    assert value["known_source_id"] is None
    assert value["consumed_source_ids"] is None
    assert value["failure_category"] is None
    assert store.get_capture(value["id"]) == value


@pytest.mark.parametrize(
    ("payload", "note", "capture_key"),
    [
        (
            "https://example.com/post\u00a0",
            "arXiv note\u00a0",
            "https://example.com/post",
        ),
        (
            "    def f():\n        return 1\n",
            "  spaced note  ",
            "def f():\n        return 1",
        ),
    ],
)
def test_capture_preserves_the_submission_byte_for_byte(
    store: ControlStore, payload: str, note: str, capture_key: str
) -> None:
    """Trimming is how the key is derived, not something done to the payload.

    `str.strip()` removes Unicode whitespace, so an NBSP-terminated paste and
    an indented multi-line snippet both lose characters the operator typed if
    the validator's return value is stored.
    """

    value = _capture(store, payload, note=note).value

    assert value["payload"] == payload
    assert value["note"] == note
    assert value["capture_key"] == capture_key
    assert store.get_capture(value["id"])["payload"] == payload


@pytest.mark.parametrize(
    ("payload", "kind", "capture_key"),
    [
        ("HTTPS://Example.com", "url", "https://example.com"),
        ("example.com/path", "text", "example.com/path"),
        (
            "https://example.com\nsecond line",
            "text",
            "https://example.com\nsecond line",
        ),
        # A non-empty host is what URL-shaped means, so an authority made only
        # of userinfo or only of a port is text.
        ("https://user@", "text", "https://user@"),
        ("https://:443", "text", "https://:443"),
        ("https://@", "text", "https://@"),
        # urlsplit deletes every C0 control before parsing, so a payload
        # carrying one is not the URL that would be left behind.
        ("\x01https://example.com/x", "text", "\x01https://example.com/x"),
        # Lowering happens in place, so an explicit empty query or fragment
        # survives and the path keeps its case.
        ("https://example.com?", "url", "https://example.com?"),
        ("https://example.com#", "url", "https://example.com#"),
        ("HTTPS://Example.com/Path", "url", "https://example.com/Path"),
    ],
)
def test_capture_key_is_string_hygiene_only(
    store: ControlStore, payload: str, kind: str, capture_key: str
) -> None:
    """The three contract vectors for the one URL-shape decision.

    Canonicalization is resolution, so `canonicalize_source_locator` is
    deliberately not reachable from here: it refuses exactly the blog and
    social hosts the operator pastes most.
    """

    value = _capture(store, payload).value

    assert value["kind"] == kind
    assert value["capture_key"] == capture_key


def test_capture_preserves_case_and_path_outside_scheme_and_host(
    store: ControlStore,
) -> None:
    value = _capture(store, "HTTP://User:Pw@Example.COM:8080/Path?Q=A#Frag").value

    assert value["kind"] == "url"
    assert (
        value["capture_key"] == "http://User:Pw@example.com:8080/Path?Q=A#Frag"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "https://example.com",
        "https://example.com?",
        "https://example.com#",
        "https://example.com?#",
        "HTTPS://Example.com/Path?Q=A#Frag",
        "HTTP://User:Pw@Example.COM:8080/Path",
        "https://example.com/%2F%2f",
    ],
)
def test_capture_key_only_changes_letter_case(
    store: ControlStore, payload: str
) -> None:
    """The key is the trimmed payload with two spans lowered, nothing else.

    Rebuilding the key from a parsed URL silently drops an explicit empty
    query or fragment, which collides submissions the operator wrote
    differently. Lowering in place cannot lose a character.
    """

    value = _capture(store, payload).value

    assert value["kind"] == "url"
    assert value["capture_key"].lower() == payload.lower()


def test_capture_replays_the_same_idempotency_key(store: ControlStore) -> None:
    first = _capture(store, "https://example.com/post")
    second = _capture(store, "https://example.com/post")

    assert second.replayed is True
    assert second.value == first.value
    assert len(store.list_captures()) == 1


def test_capture_refuses_an_open_duplicate_with_the_current_row(
    store: ControlStore,
) -> None:
    from cortex_platform.product.control import CaptureConflict

    first = _capture(store, "https://example.com/post", key="capture-key-00001").value
    with pytest.raises(CaptureConflict) as excinfo:
        _capture(store, "HTTPS://EXAMPLE.com/post", key="capture-key-00002")

    assert excinfo.value.category == "already_captured"
    assert excinfo.value.current == first
    assert len(store.list_captures()) == 1


def test_capture_is_allowed_again_after_a_terminal_state(
    store: ControlStore,
) -> None:
    first = _capture(store, "https://example.com/post", key="capture-key-00001").value
    store.dismiss_capture(
        capture_id=first["id"],
        expected_revision=first["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00002",
    )

    second = _capture(store, "https://example.com/post", key="capture-key-00003").value

    assert second["id"] != first["id"]
    assert second["state"] == "pending"


def test_capture_hints_a_known_source_from_an_alias(store: ControlStore) -> None:
    """Exact string equality against an alias, never canonicalization.

    Alias values structurally cannot be URLs, so this hits bare-identifier
    pastes only; a URL-form paste of an adopted source stays undetectable.
    """

    source = store.register_source(
        authority="arxiv",
        authority_id="2401.12345",
        source_kind="paper",
        official_title="Speculative Decoding",
        engine_ref="paper:20260618-Speculative_Decoding",
        actor_id="operator",
        idempotency_key="capture-source-0001",
    ).value

    hinted = _capture(store, " 2401.12345 ", key="capture-key-00001").value
    missed = _capture(store, "2401.99999", key="capture-key-00002").value

    assert hinted["known_source_id"] == source["id"]
    assert hinted["state"] == "pending"
    assert missed["known_source_id"] is None


def test_capture_enforces_the_length_bounds_at_the_store_boundary(
    store: ControlStore,
) -> None:
    with pytest.raises(ValueError, match="payload"):
        _capture(store, "   ")
    with pytest.raises(ValueError, match="payload"):
        _capture(store, "a" * 16_385)
    with pytest.raises(ValueError, match="note"):
        _capture(store, "https://example.com", note="n" * 2_001)
    with pytest.raises(ValueError, match="note"):
        _capture(store, "https://example.com", note=None)

    # An empty note is legal, and a whitespace-only one is stored as it was
    # submitted rather than collapsed.
    blank_note = _capture(store, "https://example.com", note="  ").value

    assert blank_note["note"] == "  "


def test_list_captures_pages_by_an_id_cursor(store: ControlStore) -> None:
    """A capture row can never be deleted, so the list has to be bounded."""

    created = [
        _capture(store, f"https://example.com/{index}", key=f"capture-key-{index:05d}").value
        for index in range(5)
    ]
    ordered = [item["id"] for item in created]

    first = store.list_captures(limit=2)
    assert [item["id"] for item in first] == ordered[:2]

    second = store.list_captures(limit=2, cursor=first[-1]["id"])
    assert [item["id"] for item in second] == ordered[2:4]

    last = store.list_captures(limit=2, cursor=second[-1]["id"])
    assert [item["id"] for item in last] == ordered[4:]
    assert store.list_captures(limit=2, cursor=last[-1]["id"]) == []

    # The filter and the cursor compose.
    store.approve_capture(
        capture_id=ordered[0],
        expected_revision=created[0]["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-0001-appr00",
    )
    assert [
        item["id"] for item in store.list_captures(state="pending", limit=2)
    ] == ordered[1:3]


def test_list_captures_validates_the_limit_and_the_cursor(
    store: ControlStore,
) -> None:
    for bad_limit in (0, 1_001, -1, True, 2.0, "10"):
        with pytest.raises(ValueError, match="limit"):
            store.list_captures(limit=bad_limit)
    with pytest.raises(ValueError, match="cursor"):
        store.list_captures(cursor="capture-does-not-exist")


def test_list_captures_filters_by_state(store: ControlStore) -> None:
    pending = _capture(store, "https://example.com/a", key="capture-key-00001").value
    other = _capture(store, "https://example.com/b", key="capture-key-00002").value
    store.approve_capture(
        capture_id=other["id"],
        expected_revision=other["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00003",
    )

    assert [item["id"] for item in store.list_captures()] == [
        pending["id"],
        other["id"],
    ]
    assert [item["id"] for item in store.list_captures(state="pending")] == [
        pending["id"]
    ]
    assert [item["id"] for item in store.list_captures(state="approved")] == [
        other["id"]
    ]
    assert store.list_captures(state="consumed") == []
    with pytest.raises(ValueError, match="state"):
        store.list_captures(state="staged")


def test_get_capture_reports_a_missing_capture(store: ControlStore) -> None:
    from cortex_platform.product.control import NotFound

    with pytest.raises(NotFound):
        store.get_capture("capture-missing")


def test_public_projection_strips_exactly_the_capture_lease_columns(
    store: ControlStore,
) -> None:
    """The denylist is by name, so the lease columns must reuse those names.

    A capture row that invented its own fence column names would leak the
    consumer's lease into the public DTO the moment C8.2 adds the route.
    """

    from cortex_platform.product.api.app import ControlAPI

    value = _capture(store, "https://example.com/post").value
    public = ControlAPI._public_value(dict(value))

    assert set(value) - set(public) == {
        "claim_owner",
        "claim_epoch",
        "claim_expires_at",
    }


def test_approve_records_the_decision_under_cas(store: ControlStore) -> None:
    from cortex_platform.product.control import InvalidTransition, RevisionConflict

    capture = _capture(store, "https://example.com/post").value
    approved = store.approve_capture(
        capture_id=capture["id"],
        expected_revision=capture["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00002",
    )

    assert approved.status_code == 200
    assert approved.value["state"] == "approved"
    assert approved.value["revision"] == capture["revision"] + 1

    replay = store.approve_capture(
        capture_id=capture["id"],
        expected_revision=capture["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00002",
    )
    assert replay.replayed is True
    assert replay.value == approved.value

    with pytest.raises(RevisionConflict) as conflict:
        store.approve_capture(
            capture_id=capture["id"],
            expected_revision=capture["revision"],
            actor_id="local-operator",
            idempotency_key="capture-key-00003",
        )
    assert conflict.value.current["state"] == "approved"

    with pytest.raises(InvalidTransition) as invalid:
        store.approve_capture(
            capture_id=capture["id"],
            expected_revision=approved.value["revision"],
            actor_id="local-operator",
            idempotency_key="capture-key-00004",
        )
    assert (invalid.value.source, invalid.value.target) == ("approved", "approved")


def test_dismiss_is_legal_from_pending_and_approved(store: ControlStore) -> None:
    from cortex_platform.product.control import InvalidTransition

    first = _capture(store, "https://example.com/a", key="capture-key-00001").value
    second = _capture(store, "https://example.com/b", key="capture-key-00002").value
    approved = store.approve_capture(
        capture_id=second["id"],
        expected_revision=second["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00003",
    ).value

    dismissed = store.dismiss_capture(
        capture_id=first["id"],
        expected_revision=first["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00004",
    ).value
    assert dismissed["state"] == "dismissed"

    assert store.dismiss_capture(
        capture_id=approved["id"],
        expected_revision=approved["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00005",
    ).value["state"] == "dismissed"

    with pytest.raises(InvalidTransition) as invalid:
        store.dismiss_capture(
            capture_id=first["id"],
            expected_revision=dismissed["revision"],
            actor_id="local-operator",
            idempotency_key="capture-key-00006",
        )
    assert (invalid.value.source, invalid.value.target) == ("dismissed", "dismissed")


def test_capture_decisions_leave_an_audit_trail(store: ControlStore) -> None:
    capture = _capture(store, "https://example.com/post").value
    store.approve_capture(
        capture_id=capture["id"],
        expected_revision=capture["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-00002",
    )

    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            """SELECT type, payload_json FROM control_audit
               WHERE aggregate_type = 'capture' ORDER BY id""",
        ).fetchall()

    assert [row[0] for row in rows] == ["capture.created", "capture.approved"]
    # The submitted payload never reaches the audit trail.
    assert all("example.com" not in str(row[1]) for row in rows)


def _approved(
    store: ControlStore, payload: str, *, prefix: str = "capture-key-0001"
) -> dict:
    capture = store.create_capture(
        payload=payload,
        note="",
        actor_id="local-operator",
        idempotency_key=f"{prefix}-create",
    ).value
    return store.approve_capture(
        capture_id=capture["id"],
        expected_revision=capture["revision"],
        actor_id="local-operator",
        idempotency_key=f"{prefix}-approve",
    ).value


def _claim(
    store: ControlStore,
    capture_id: str,
    *,
    worker: str = "p4-worker",
    lease: int = 60,
    key: str = "capture-key-0001-claim",
) -> dict:
    return store.claim_capture(
        capture_id=capture_id,
        worker_id=worker,
        lease_seconds=lease,
        actor_id="p4",
        idempotency_key=key,
    ).value


def test_pending_captures_are_the_approved_ones_only(store: ControlStore) -> None:
    """`pending` is never machine-claimable: approval is the whole gate."""

    staged = _capture(store, "https://example.com/a", key="capture-key-00001").value
    approved = _approved(store, "https://example.com/b", prefix="capture-key-0002")

    assert [item["id"] for item in store.list_pending_captures()] == [approved["id"]]
    assert staged["state"] == "pending"


def test_claim_fences_an_approved_capture(store: ControlStore) -> None:
    from cortex_platform.product.control import InvalidTransition

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])

    assert claimed["state"] == "claimed"
    assert claimed["claim_owner"] == "p4-worker"
    assert claimed["claim_epoch"] == approved["claim_epoch"] + 1
    assert claimed["claim_expires_at"] == "2026-09-01T12:01:00Z"
    assert store.list_pending_captures() == []

    with pytest.raises(InvalidTransition) as invalid:
        _claim(store, approved["id"], key="capture-key-0001-claim2")
    assert (invalid.value.source, invalid.value.target) == ("claimed", "claimed")


def test_claim_refuses_a_capture_the_operator_has_not_approved(
    store: ControlStore,
) -> None:
    from cortex_platform.product.control import InvalidTransition

    staged = _capture(store, "https://example.com/a").value

    with pytest.raises(InvalidTransition) as invalid:
        _claim(store, staged["id"])
    assert (invalid.value.source, invalid.value.target) == ("pending", "claimed")


def test_expired_claim_becomes_uncertain_and_is_never_redelivered(
    store: ControlStore, clock: MovableClock
) -> None:
    """Ingest mints a paper_dir, so redelivery would be the forbidden retry."""

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])
    clock.advance(61)

    assert store.list_pending_captures() == []

    swept = store.get_capture(approved["id"])
    assert swept["state"] == "uncertain"
    assert swept["failure_category"] == "outcome_unknown"
    assert swept["claim_owner"] is None
    assert swept["claim_epoch"] == claimed["claim_epoch"]
    assert swept["revision"] == claimed["revision"] + 1


def test_completion_is_refused_on_a_stale_claim_epoch(
    store: ControlStore, clock: MovableClock
) -> None:
    from cortex_platform.product.control import InvalidTransition

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])
    clock.advance(61)
    store.list_pending_captures()
    store.reopen_capture(
        capture_id=approved["id"],
        expected_revision=store.get_capture(approved["id"])["revision"],
        acknowledged=True,
        actor_id="local-operator",
        idempotency_key="capture-key-0001-reopen",
    )
    reclaimed = _claim(store, approved["id"], key="capture-key-0001-claim2")

    assert reclaimed["claim_epoch"] == claimed["claim_epoch"] + 1
    with pytest.raises(InvalidTransition) as invalid:
        store.complete_capture(
            capture_id=approved["id"],
            claim_owner="p4-worker",
            claim_epoch=claimed["claim_epoch"],
            outcome="consumed",
            consumed_source_ids=[_insert_source(store, "source-1")],
            actor_id="p4",
            idempotency_key="capture-key-0001-stale0",
        )
    assert invalid.value.source == "claim_fence_mismatch"


def test_completion_records_the_consumed_sources(store: ControlStore) -> None:
    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])
    _insert_source(store, "source-42")

    completed = store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="consumed",
        consumed_source_ids=["source-42"],
        actor_id="p4",
        idempotency_key="capture-key-0001-done00",
    ).value

    assert completed["state"] == "consumed"
    assert completed["consumed_source_ids"] == ["source-42"]
    assert completed["claim_owner"] is None
    assert completed["claim_expires_at"] is None
    assert completed["failure_category"] is None


def test_completion_refuses_source_ids_that_name_nothing(
    store: ControlStore,
) -> None:
    """`known_source_id` is republished on the next paste of the same key.

    A capture row can never be deleted, so a completion that names a source
    which does not exist plants a permanent hint at an identity nobody can
    resolve. The artifact path already applies this check to an equally
    internal-only caller.
    """

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])
    _insert_source(store, "source-real")

    for bad in (
        ["source-real", "source-missing"],
        ["../../etc/passwd"],
        "source-real",
        [b"source-real"],
        [""],
        ["   "],
        [None],
    ):
        with pytest.raises(ValueError, match="consumed_source_id"):
            store.complete_capture(
                capture_id=approved["id"],
                claim_owner="p4-worker",
                claim_epoch=claimed["claim_epoch"],
                outcome="consumed",
                consumed_source_ids=bad,
                actor_id="p4",
                idempotency_key="capture-key-0001-bad000",
            )

    assert store.get_capture(approved["id"])["state"] == "claimed"

    completed = store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="consumed",
        consumed_source_ids=["source-real"],
        actor_id="p4",
        idempotency_key="capture-key-0001-good00",
    ).value

    assert completed["consumed_source_ids"] == ["source-real"]


def test_completion_enforces_the_frozen_failure_allowlist(
    store: ControlStore,
) -> None:
    """Raw exception text is never persisted, only an allowlisted category."""

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])

    with pytest.raises(ValueError, match="failure category"):
        store.complete_capture(
            capture_id=approved["id"],
            claim_owner="p4-worker",
            claim_epoch=claimed["claim_epoch"],
            outcome="failed",
            failure_category="ConnectionError at /Users/operator/data/paper.pdf",
            actor_id="p4",
            idempotency_key="capture-key-0001-fail01",
        )
    with pytest.raises(ValueError, match="outcome"):
        store.complete_capture(
            capture_id=approved["id"],
            claim_owner="p4-worker",
            claim_epoch=claimed["claim_epoch"],
            outcome="imported",
            actor_id="p4",
            idempotency_key="capture-key-0001-fail02",
        )

    failed = store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="failed",
        failure_category="adapter_unavailable",
        actor_id="p4",
        idempotency_key="capture-key-0001-fail03",
    ).value

    assert failed["state"] == "failed"
    assert failed["failure_category"] == "adapter_unavailable"
    assert failed["consumed_source_ids"] is None


def test_completion_with_an_unknown_outcome_records_outcome_unknown(
    store: ControlStore,
) -> None:
    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])

    uncertain = store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        actor_id="p4",
        idempotency_key="capture-key-0001-unsure",
    ).value

    assert uncertain["state"] == "uncertain"
    assert uncertain["failure_category"] == "outcome_unknown"
    assert uncertain["claim_epoch"] == claimed["claim_epoch"]
    assert store.list_pending_captures() == []


def test_reopen_requires_an_acknowledgement_and_returns_to_approved(
    store: ControlStore,
) -> None:
    from cortex_platform.product.control import InvalidTransition

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])
    uncertain = store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        actor_id="p4",
        idempotency_key="capture-key-0001-unsure",
    ).value

    with pytest.raises(ValueError, match="acknowledged"):
        store.reopen_capture(
            capture_id=approved["id"],
            expected_revision=uncertain["revision"],
            acknowledged=False,
            actor_id="local-operator",
            idempotency_key="capture-key-0001-reopen",
        )

    reopened = store.reopen_capture(
        capture_id=approved["id"],
        expected_revision=uncertain["revision"],
        acknowledged=True,
        actor_id="local-operator",
        idempotency_key="capture-key-0001-reopen",
    ).value

    assert reopened["state"] == "approved"
    assert reopened["failure_category"] is None
    # The epoch is retained so the previous consumer's fence stays dead.
    assert reopened["claim_epoch"] == uncertain["claim_epoch"]
    assert [item["id"] for item in store.list_pending_captures()] == [approved["id"]]

    with pytest.raises(InvalidTransition) as invalid:
        store.reopen_capture(
            capture_id=approved["id"],
            expected_revision=reopened["revision"],
            acknowledged=True,
            actor_id="local-operator",
            idempotency_key="capture-key-0001-reopen2",
        )
    assert (invalid.value.source, invalid.value.target) == ("approved", "approved")


def test_dismiss_is_legal_from_uncertain_but_not_from_claimed(
    store: ControlStore,
) -> None:
    from cortex_platform.product.control import InvalidTransition

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])

    with pytest.raises(InvalidTransition) as invalid:
        store.dismiss_capture(
            capture_id=approved["id"],
            expected_revision=claimed["revision"],
            actor_id="local-operator",
            idempotency_key="capture-key-0001-dism01",
        )
    assert (invalid.value.source, invalid.value.target) == ("claimed", "dismissed")

    uncertain = store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        actor_id="p4",
        idempotency_key="capture-key-0001-unsure",
    ).value
    dismissed = store.dismiss_capture(
        capture_id=approved["id"],
        expected_revision=uncertain["revision"],
        actor_id="local-operator",
        idempotency_key="capture-key-0001-dism02",
    ).value

    assert dismissed["state"] == "dismissed"


def test_consumed_capture_hints_the_next_paste_of_the_same_key(
    store: ControlStore,
) -> None:
    """Anything gen 8 itself ingested is discoverable on the next paste."""

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])
    _insert_source(store, "source-42")
    store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="consumed",
        consumed_source_ids=["source-42"],
        actor_id="p4",
        idempotency_key="capture-key-0001-done00",
    )

    again = store.create_capture(
        payload="HTTPS://Example.com/b",
        note="",
        actor_id="local-operator",
        idempotency_key="capture-key-0003-create",
    ).value

    assert again["state"] == "pending"
    assert again["known_source_id"] == "source-42"


def test_claim_replays_only_for_the_current_fence(store: ControlStore) -> None:
    from cortex_platform.product.control import InvalidTransition

    approved = _approved(store, "https://example.com/b")
    claimed = _claim(store, approved["id"])

    replay = store.claim_capture(
        capture_id=approved["id"],
        worker_id="p4-worker",
        lease_seconds=60,
        actor_id="p4",
        idempotency_key="capture-key-0001-claim",
    )
    assert replay.replayed is True
    assert replay.value == claimed

    store.complete_capture(
        capture_id=approved["id"],
        claim_owner="p4-worker",
        claim_epoch=claimed["claim_epoch"],
        outcome="uncertain",
        actor_id="p4",
        idempotency_key="capture-key-0001-unsure",
    )
    with pytest.raises(InvalidTransition) as invalid:
        store.claim_capture(
            capture_id=approved["id"],
            worker_id="p4-worker",
            lease_seconds=60,
            actor_id="p4",
            idempotency_key="capture-key-0001-claim",
        )
    assert invalid.value.source == "stale_claim_receipt"
