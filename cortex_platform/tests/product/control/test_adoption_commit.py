"""The bulk adoption commit: one decision resolves a whole copied corpus."""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex_platform.product.control.schema import MIGRATION_VERSIONS
from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
)
from cortex_platform.product.sources.adoption import AdoptionEntry, build_manifest


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db",
        clock=lambda: datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        id_factory=DeterministicIds(),
    )
    value.initialize()
    value.register_asset_root(
        root_id="research-corpus",
        private_path=tmp_path / "data" / "research" / "corpus",
        max_bytes=1 << 40,
        enabled=True,
        actor_id="operator",
        idempotency_key="command-root0001",
    )
    return value


def _entry(
    *,
    paper_dir: str = "20260618-Speculative_Decoding",
    authority_id: str = "2401.12345",
    title: str = "Speculative Decoding",
    digest: str = "a" * 64,
) -> AdoptionEntry:
    return AdoptionEntry(
        paper_dir=paper_dir,
        authority="arxiv",
        authority_id=authority_id,
        official_title=title,
        content_digest=digest,
    )


def _commit(
    store: ControlStore,
    manifest,
    *,
    key: str = "command-adopt001",
    root_id: str = "research-corpus",
    actor_id: str = "operator",
):
    return store.commit_adoption_manifest(
        manifest=manifest,
        corpus_root_id=root_id,
        actor_id=actor_id,
        idempotency_key=key,
    )


def _sources(store: ControlStore) -> list[sqlite3.Row]:
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM sources ORDER BY canonical_id"
        ).fetchall()


class TestCommit:
    def test_every_entry_becomes_an_existing_source(self, store: ControlStore) -> None:
        manifest = build_manifest(
            [
                _entry(),
                _entry(paper_dir="20260619-blog-推测解码", authority_id="2402.00001"),
            ]
        )
        record = _commit(store, manifest)

        assert record.manifest_id == manifest.manifest_id
        assert record.entry_count == 2
        rows = _sources(store)
        assert len(rows) == 2
        for row in rows:
            # A copied corpus is already in the engine: 'existing', never
            # 'pending', so no import action is ever created for it.
            assert row["import_state"] == "existing"
            assert row["source_kind"] == "paper"
            assert row["engine_ref"].startswith("paper:")

    def test_no_import_action_is_created(self, store: ControlStore) -> None:
        _commit(store, build_manifest([_entry()]))
        with sqlite3.connect(store.path) as conn:
            count = conn.execute(
                "SELECT count(*) FROM source_import_actions"
            ).fetchone()[0]
        assert count == 0

    def test_entries_record_their_directory_and_digest(
        self, store: ControlStore
    ) -> None:
        manifest = build_manifest([_entry(paper_dir="arxiv:2401.12345")])
        _commit(store, manifest)
        with sqlite3.connect(store.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM adoption_entries").fetchone()
        assert row["paper_dir"] == "arxiv:2401.12345"
        assert row["content_digest"] == "a" * 64
        assert row["engine_ref"] == manifest.entries[0].engine_ref

    def test_the_manifest_binds_the_corpus_root(self, store: ControlStore) -> None:
        record = _commit(store, build_manifest([_entry()]))
        assert record.corpus_root_id == "research-corpus"


class TestIdempotency:
    def test_replaying_the_same_commit_returns_the_same_record(
        self, store: ControlStore
    ) -> None:
        manifest = build_manifest([_entry()])
        first = _commit(store, manifest)
        second = _commit(store, manifest)
        assert first == second
        assert len(_sources(store)) == 1

    def test_a_different_manifest_under_the_same_key_is_refused(
        self, store: ControlStore
    ) -> None:
        _commit(store, build_manifest([_entry()]))
        other = build_manifest(
            [_entry(paper_dir="20260101-Other", authority_id="2403.00001")]
        )
        with pytest.raises(IdempotencyConflict):
            _commit(store, other)

    def test_recommitting_one_manifest_under_a_new_key_is_refused(
        self, store: ControlStore
    ) -> None:
        manifest = build_manifest([_entry()])
        _commit(store, manifest)
        with pytest.raises(InvalidTransition) as error:
            _commit(store, manifest, key="command-adopt002")
        assert error.value.source == "already_adopted"

    def test_a_later_manifest_adopts_only_what_is_new(
        self, store: ControlStore
    ) -> None:
        # The cutover delta refresh: a second manifest re-lists what is
        # already adopted plus whatever arrived since.
        first = _entry()
        second = _entry(paper_dir="20260701-Later_Paper", authority_id="2407.00001")
        _commit(store, build_manifest([first]))
        record = _commit(store, build_manifest([first, second]), key="command-adopt002")

        assert record.entry_count == 2
        assert record.adopted_count == 1
        rows = _sources(store)
        assert len(rows) == 2

        # Every entry the manifest named is recorded against it, including the
        # one that was already adopted -- otherwise a later parity check
        # cannot prove which corpus this manifest actually saw.
        with sqlite3.connect(store.path) as conn:
            entries = conn.execute(
                "SELECT paper_dir FROM adoption_entries WHERE manifest_id = ?"
                " ORDER BY paper_dir",
                (record.manifest_id,),
            ).fetchall()
        assert [row[0] for row in entries] == [
            "20260618-Speculative_Decoding",
            "20260701-Later_Paper",
        ]


class TestRefusals:
    def test_an_unknown_corpus_root_is_refused(self, store: ControlStore) -> None:
        with pytest.raises(NotFound):
            _commit(store, build_manifest([_entry()]), root_id="missing-root")

    def test_a_disabled_corpus_root_is_refused(self, store: ControlStore) -> None:
        root = store.get_asset_root("research-corpus")
        store.update_asset_root(
            root_id="research-corpus",
            private_path=root.private_path,
            max_bytes=root.max_bytes,
            enabled=False,
            expected_revision=root.revision,
            actor_id="operator",
            idempotency_key="command-root0002",
        )
        with pytest.raises(InvalidTransition) as error:
            _commit(store, build_manifest([_entry()]))
        assert error.value.source == "corpus_root_disabled"

    def test_a_source_already_bound_to_a_different_directory_is_refused(
        self, store: ControlStore
    ) -> None:
        # The same canonical paper adopted twice under two directory names
        # must not silently repoint the engine_ref of a live source.
        _commit(store, build_manifest([_entry(paper_dir="20260101-First")]))
        with pytest.raises(InvalidTransition) as error:
            _commit(
                store,
                build_manifest([_entry(paper_dir="20260102-Second")]),
                key="command-adopt002",
            )
        assert error.value.source == "engine_ref_conflict"

    def test_one_directory_reclassified_under_a_new_authority_is_refused(
        self, store: ControlStore
    ) -> None:
        # The mirror of engine_ref_conflict: same directory, different
        # canonical id -- e.g. a source first adopted by content digest and
        # later correctly identified as an arXiv paper. sources.engine_ref is
        # UNIQUE, so without a guard this leaves the transaction to fail with
        # a raw IntegrityError, which is the opaque failure this design exists
        # to avoid.
        first = AdoptionEntry(
            paper_dir="20260101-Reclassified",
            authority="sha256",
            authority_id="e" * 64,
            official_title="Reclassified",
            content_digest="a" * 64,
        )
        _commit(store, build_manifest([first]))
        second = _entry(paper_dir="20260101-Reclassified")
        with pytest.raises(InvalidTransition) as error:
            _commit(store, build_manifest([second]), key="command-adopt002")
        assert error.value.source == "paper_dir_conflict"

    def test_a_foreign_manifest_object_is_refused(self, store: ControlStore) -> None:
        with pytest.raises(TypeError):
            store.commit_adoption_manifest(
                manifest={"entries": []},
                corpus_root_id="research-corpus",
                actor_id="operator",
                idempotency_key="command-adopt003",
            )


class TestDurability:
    def test_the_commit_survives_reopening_the_store(
        self, store: ControlStore, tmp_path: Path
    ) -> None:
        manifest = build_manifest([_entry()])
        _commit(store, manifest)
        reopened = ControlStore(store.path)
        record = reopened.get_adoption_manifest(manifest.manifest_id)
        assert record.manifest_id == manifest.manifest_id
        assert record.entry_count == 1

    def test_an_unknown_manifest_is_not_found(self, store: ControlStore) -> None:
        with pytest.raises(NotFound):
            store.get_adoption_manifest("f" * 64)

    def test_adoption_rows_cannot_be_deleted(self, store: ControlStore) -> None:
        manifest = build_manifest([_entry()])
        _commit(store, manifest)
        with sqlite3.connect(store.path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM adoption_manifests")

    def test_an_adopted_source_cannot_be_deleted_even_with_foreign_keys_off(
        self, store: ControlStore
    ) -> None:
        # Foreign keys are a per-connection pragma, so the FK alone protects
        # an adopted source only from writers that remembered to enable it.
        # Raw sqlite3 access to a cortex database is a documented recurring
        # hazard, so the guard has to be a trigger.
        _commit(store, build_manifest([_entry()]))
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
            with pytest.raises(sqlite3.IntegrityError, match="adopted source"):
                conn.execute("DELETE FROM sources")

    def test_an_unadopted_source_is_untouched_by_the_guard(
        self, store: ControlStore
    ) -> None:
        store.register_source(
            authority="arxiv",
            authority_id="2409.00001",
            source_kind="paper",
            official_title="Never Adopted",
            engine_ref="paper:20260901-Never_Adopted",
            actor_id="operator",
            idempotency_key="command-source01",
        )
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM sources")
            assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def test_interrupted_migration_11_leaves_no_adoption_tables_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other migration boundary in this store is pinned this way.

    A migration that half-applies is the worst failure mode a control store
    has, because the next start sees tables it cannot account for.
    """

    from cortex_platform.product.control import schema

    database = tmp_path / "control.db"
    # Pre-apply everything before 11, as the migration-10 test does. Starting
    # from an empty file would prove nothing: the rollback would discard
    # `schema_migrations` itself along with the rest.
    with sqlite3.connect(database) as seed:
        # Walked off `migration_scripts()` rather than `_MIGRATION_{n}`: the
        # scripts are named after what they create, not after the number they
        # were given, and a renumber moves the number only.
        for version, script in schema.migration_scripts():
            if version > 10:
                break
            seed.executescript(script)
            seed.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'old')",
                (version,),
            )
    original = schema._execute_script_in_transaction

    def interrupt(conn: sqlite3.Connection, script: str) -> None:
        if script == schema._MIGRATION_11:
            conn.execute("CREATE TABLE interrupted_adoption_migration(id TEXT)")
            raise RuntimeError("simulated migration interruption")
        original(conn, script)

    with sqlite3.connect(database, isolation_level=None) as conn:
        monkeypatch.setattr(schema, "_execute_script_in_transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            schema.apply_migrations(conn, now="2026-07-31T12:00:00.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(index,) for index in range(1, 11)]
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type = 'table' AND name IN (
                   'adoption_manifests',
                   'adoption_entries',
                   'interrupted_adoption_migration'
               )"""
        ).fetchone() == (0,)

        monkeypatch.setattr(schema, "_execute_script_in_transaction", original)
        schema.apply_migrations(conn, now="2026-07-31T12:00:01.000000Z")
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in MIGRATION_VERSIONS]
