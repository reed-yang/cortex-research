"""Read-only catalog over the preserved legacy Research database.

The fixtures are built by executing the REAL legacy DDL
(`m1c_schema.sql`, `m1d_schema.sql`, `m1h_schema.sql`) rather than hand-written
tables: the catalog's only job is to survive the shape the research engine
actually wrote. Those three files are not part of the supported research
package, so their bytes are preserved as test data under
`profiles/research/tests/fixtures/legacy_catalog_ddl/`, which is where this
reads them from. The two CHECK enums are widened exactly as `m1d_schema.py`
widens them on a live database, because the preserved copy is a migrated one.
Nothing here imports `cortex_research` -- importing it initializes schemas.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.research import catalog as catalog_module
from cortex_platform.product.research.catalog import (
    IDEA_STATUSES,
    MAX_HISTORY,
    MAX_ITEMS,
    MAX_LIMIT,
    CatalogItemNotFound,
    CatalogUnavailable,
    ResearchCatalog,
)

SCHEMA_DIR = (
    Path(__file__).resolve().parents[4]
    / "profiles/research/tests/fixtures/legacy_catalog_ddl"
)
SCHEMA_FILES = ("m1c_schema.sql", "m1d_schema.sql", "m1h_schema.sql")
# `_STATUS_ENUM` / `_ORIGIN_ENUM` in m1d_schema.py, applied there by a table
# rebuild. Asserted below, so a schema edit fails loudly instead of silently
# testing a narrower shape than production carries.
WIDENED = (
    ("'dormant', 'awaiting_human'", "'dormant', 'exploring', 'awaiting_human'"),
    ("'pivot_of_kill', 'fork_of_round'",
     "'pivot_of_kill', 'fork_of_round', 'exploration', 'from_exploration', 'revival'"),
)

if not all((SCHEMA_DIR / name).exists() for name in SCHEMA_FILES):
    pytest.skip("legacy research DDL is not in this checkout", allow_module_level=True)


def _apply_schema(connection: sqlite3.Connection) -> None:
    for name in SCHEMA_FILES:
        text = (SCHEMA_DIR / name).read_text(encoding="utf-8")
        for old, new in WIDENED:
            if old in text:
                text = text.replace(old, new)
        connection.executescript(text)
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idea_seeds'"
    ).fetchone()[0].count("'exploring'") == 1


def _insert_idea(connection, idea_id, *, status, slug=None, origin="manual",
                 seed_text="seed", rounds=0, updated_at="2026-05-01T00:00:00Z",
                 dormant_reason=None, kill_reason=None, project_ref=None,
                 completed_at=None, md_path=None):
    connection.execute(
        """INSERT INTO idea_seeds
           (idea_id, seed_text, slug, status, n_rounds_completed, md_path, origin,
            updated_at, dormant_reason, kill_reason, graduated_to_project_ref,
            completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (idea_id, seed_text, slug or idea_id, status, rounds,
         md_path or f"/legacy/ideas/{idea_id}.md", origin, updated_at,
         dormant_reason, kill_reason, project_ref, completed_at),
    )


def _insert_round(connection, exploration_id, round_n, *, digest="digest",
                  completed_at="2026-06-02T00:00:00Z"):
    connection.execute(
        """INSERT INTO exploration_rounds
           (exploration_id, round_n, mode, digest_text, started_at, completed_at)
           VALUES (?, ?, 'discover', ?, '2026-06-01T00:00:00Z', ?)""",
        (exploration_id, round_n, digest, completed_at),
    )


def _populate(connection: sqlite3.Connection) -> None:
    """One record of every shape the preserved inventory actually contains."""

    _insert_idea(connection, "id_alpha", status="dormant", slug="alpha-idea",
                 seed_text="alpha question", rounds=3,
                 dormant_reason="operator stopped all existing exploration",
                 updated_at="2026-05-04T00:00:00Z")
    _insert_idea(connection, "id_beta", status="graduated", slug="beta-idea",
                 rounds=6, project_ref="helios-zero-inference-cost-foresight",
                 completed_at="2026-05-09T00:00:00Z", updated_at="2026-05-09T00:00:00Z")
    _insert_idea(connection, "id_gamma", status="awaiting_human", slug="gamma-idea",
                 updated_at="2026-05-02T00:00:00Z")
    # A parked exploration thread keeps origin='exploration' but an idea state.
    _insert_idea(connection, "id_expl_parked", status="dormant", slug="parked-exploration",
                 origin="exploration", dormant_reason="round budget exhausted",
                 updated_at="2026-05-07T00:00:00Z",
                 md_path="/legacy/explorations/id_expl_parked.md")
    _insert_idea(connection, "id_expl_live", status="exploring", slug="live-exploration",
                 origin="exploration", updated_at="2026-05-08T00:00:00Z",
                 md_path="/legacy/explorations/id_expl_live.md")

    for round_n in (1, 2, 3):
        _insert_round(connection, "id_expl_parked", round_n)
    _insert_round(connection, "id_expl_live", 1)
    # An exploration identity whose idea_seeds row is gone. Two rounds, the
    # later one inserted first, so the aggregate must take the maximum.
    _insert_round(connection, "id_expl_orphan", 2, completed_at="2026-04-05T00:00:00Z")
    _insert_round(connection, "id_expl_orphan", 1, completed_at="2026-04-01T00:00:00Z")

    connection.execute(
        """INSERT INTO exploration_angles
           (angle_id, exploration_id, title, rationale, status, created_at)
           VALUES ('ang_1', 'id_expl_parked', 'sparse cache angle', 'why', 'greenlit',
                   '2026-06-03T00:00:00Z')""",
    )
    connection.execute(
        """INSERT INTO idea_attempts
           (idea_id, round_n, papers_pulled, decompositions, convergence_verdict,
            convergence_reason, started_at, completed_at)
           VALUES ('id_alpha', 3, '[]', '[]', 'not_converged', 'evidence too thin',
                   '2026-05-03T00:00:00Z', '2026-05-04T00:00:00Z')""",
    )
    connection.execute(
        """INSERT INTO idea_challenges
           (idea_id, round_n, challenge_text, dimension, evidence_papers,
            evidence_quotes, importance, confidence, model_agreement, created_at)
           VALUES ('id_alpha', 3, 'baseline is unproven', 'feasibility', '[]', '[]',
                   4, 3, 'both', '2026-05-04T01:00:00Z')""",
    )
    for project_ref, registered_at in (
        ("helios-zero-inference-cost-foresight", "2026-03-01T00:00:00Z"),
        ("realtime-aware-sparsecache-memory", "2026-03-02T00:00:00Z"),
    ):
        connection.execute(
            "INSERT INTO spar_monitored_projects (project_ref, registered_at) VALUES (?, ?)",
            (project_ref, registered_at),
        )


def _build(path: Path, populate=_populate) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        _apply_schema(connection)
        if populate is not None:
            populate(connection)
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return _build(tmp_path / "research.db")


@pytest.fixture
def catalog(database: Path) -> ResearchCatalog:
    return ResearchCatalog(database)


def _identity(kind: str, origin_id: str) -> str:
    digest = hashlib.sha256(f"legacy-research\0{kind}\0{origin_id}".encode()).hexdigest()
    return "ri_" + digest[:32]


def _state(path: Path) -> dict:
    return {
        sidecar.name: hashlib.sha256(sidecar.read_bytes()).hexdigest()
        for sidecar in sorted(path.parent.iterdir())
    }


def test_lists_every_kind_with_original_identity(catalog):
    page = catalog.list_items(limit=MAX_LIMIT)

    assert page["total"] == len(page["items"]) == 8
    assert {item["kind"] for item in page["items"]} == {"idea", "exploration", "project"}
    by_origin = {item["origin_id"]: item for item in page["items"]}
    assert set(by_origin) == {
        "id_alpha", "id_beta", "id_gamma", "id_expl_parked", "id_expl_live",
        "id_expl_orphan", "helios-zero-inference-cost-foresight",
        "realtime-aware-sparsecache-memory",
    }
    alpha = by_origin["id_alpha"]
    assert alpha == {
        "id": _identity("idea", "id_alpha"),
        "kind": "idea",
        "origin_id": "id_alpha",
        "title": "alpha-idea",
        "status": "dormant",
        "summary": "alpha question",
        "pause_reason": "operator stopped all existing exploration",
        "round_count": 3,
        "updated_at": "2026-05-04T00:00:00Z",
    }
    # Kind is part of the hashed material, so identities never collide.
    assert _identity("idea", "id_alpha") != _identity("exploration", "id_alpha")


def test_public_items_carry_no_filesystem_paths(catalog, database):
    for item in catalog.list_items(limit=MAX_LIMIT)["items"]:
        detail = catalog.get_item(item["id"])
        rendered = repr(detail)
        assert "md_path" not in detail and "/legacy/" not in rendered
        assert str(database) not in rendered


def test_all_six_idea_states_are_enumerable(tmp_path):
    def populate(connection):
        for status in IDEA_STATUSES:
            _insert_idea(connection, f"id_{status}", status=status)

    catalog = ResearchCatalog(_build(tmp_path / "states.db", populate))

    assert len(IDEA_STATUSES) == 6
    for status in IDEA_STATUSES:
        page = catalog.list_items(status=status)
        assert [item["origin_id"] for item in page["items"]] == [f"id_{status}"]
        assert page["total"] == 1
        assert page["items"][0]["kind"] == "idea"
    assert catalog.list_items(kind="idea", limit=MAX_LIMIT)["total"] == 6


def test_exploration_rounds_group_by_original_exploration_identity(catalog):
    page = catalog.list_items(kind="exploration", limit=MAX_LIMIT)
    rounds = {item["origin_id"]: item["round_count"] for item in page["items"]}

    assert rounds == {"id_expl_parked": 3, "id_expl_live": 1, "id_expl_orphan": 2}
    orphan = next(item for item in page["items"] if item["origin_id"] == "id_expl_orphan")
    assert orphan["updated_at"] == "2026-04-05T00:00:00Z"
    statuses = {item["origin_id"]: item["status"] for item in page["items"]}
    # A parked exploration keeps its exploration kind and its idea state; an
    # exploration identity with no seed row is reported as unknown, not invented.
    assert statuses == {
        "id_expl_parked": "dormant", "id_expl_live": "exploring",
        "id_expl_orphan": "unknown",
    }
    detail = catalog.get_item(_identity("exploration", "id_expl_parked"))
    assert [entry["label"] for entry in detail["history"] if entry["kind"] == "round"] == [
        "round 3 (discover)", "round 2 (discover)", "round 1 (discover)",
    ]
    assert [entry["text"] for entry in detail["history"] if entry["kind"] == "angle"] == [
        "sparse cache angle"
    ]


def test_round_counts_exceed_the_row_bound_of_a_small_catalog(tmp_path):
    """More rounds than the row bound, aggregated exactly, on a 3-item catalog.

    Counting raw round rows under a LIMIT would report the bound instead of the
    truth and would drop whichever identity SQLite returned last.
    """

    total = MAX_ITEMS + 500

    def populate(connection):
        _insert_idea(connection, "id_deep", status="dormant", origin="exploration")
        _insert_idea(connection, "id_shallow", status="exploring", origin="exploration")
        _insert_idea(connection, "id_plain", status="incubating", rounds=2)
        connection.executemany(
            """INSERT INTO exploration_rounds
               (exploration_id, round_n, started_at, completed_at)
               VALUES ('id_deep', ?, '2026-06-01T00:00:00Z', ?)""",
            [(round_n, f"2026-06-01T00:00:00Z/{round_n:06d}") for round_n in range(1, total + 1)],
        )
        _insert_round(connection, "id_shallow", 1, completed_at="2026-06-09T00:00:00Z")

    catalog = ResearchCatalog(_build(tmp_path / "deep.db", populate))
    page = catalog.list_items(limit=MAX_LIMIT)

    assert page["total"] == 3
    counts = {item["origin_id"]: item["round_count"] for item in page["items"]}
    assert counts == {"id_deep": total, "id_shallow": 1, "id_plain": 2}
    deep = next(item for item in page["items"] if item["origin_id"] == "id_deep")
    assert deep["updated_at"] == "2026-05-01T00:00:00Z"
    # The bounded history is unaffected by the round volume.
    assert len(catalog.get_item(deep["id"])["history"]) == MAX_HISTORY


def test_too_many_exploration_identities_are_refused(tmp_path, monkeypatch):
    def populate(connection):
        for index in range(4):
            _insert_round(connection, f"id_expl_{index}", 1)
            connection.execute(
                """INSERT INTO exploration_angles
                   (angle_id, exploration_id, title, rationale)
                   VALUES (?, ?, 'angle', 'why')""",
                (f"ang_{index}", f"id_expl_{index}"),
            )

    catalog = ResearchCatalog(_build(tmp_path / "wide.db", populate))
    assert catalog.list_items(limit=MAX_LIMIT)["total"] == 4

    monkeypatch.setattr(catalog_module, "MAX_ITEMS", 3)
    with pytest.raises(CatalogUnavailable, match="larger than the read limit"):
        catalog.list_items()


def test_a_seed_reached_only_through_angles_is_listed_once(tmp_path):
    """An angle-only exploration keeps its seed's real state and document.

    Its identity must be known before seeds are classified; otherwise the row
    is listed both as an idea and as a seed-less unknown exploration.
    """

    def populate(connection):
        _insert_idea(connection, "id_angle_only", status="incubating", origin="manual",
                     slug="angle-only", md_path="/legacy/explorations/id_angle_only.md")
        connection.execute(
            """INSERT INTO exploration_angles
               (angle_id, exploration_id, title, rationale, status, created_at)
               VALUES ('ang_only', 'id_angle_only', 'only angle', 'why', 'proposed',
                       '2026-06-05T00:00:00Z')""",
        )

    catalog = ResearchCatalog(_build(tmp_path / "angles.db", populate))
    page = catalog.list_items(limit=MAX_LIMIT)

    assert page["total"] == 1
    item = page["items"][0]
    assert item["kind"] == "exploration" and item["origin_id"] == "id_angle_only"
    assert item["status"] == "incubating" and item["title"] == "angle-only"
    assert item["round_count"] == 0
    assert catalog.list_items(kind="idea")["items"] == []
    assert catalog.document_candidates(item["id"]) == [
        {"kind": "exploration", "registered_path": "/legacy/explorations/id_angle_only.md"}
    ]
    assert [entry["text"] for entry in catalog.get_item(item["id"])["history"]] == ["only angle"]


def test_monitored_projects_stay_projects(catalog):
    project = catalog.get_item(_identity("project", "helios-zero-inference-cost-foresight"))

    assert project["kind"] == "project" and project["status"] == "monitored"
    assert project["origin_id"] == "helios-zero-inference-cost-foresight"
    assert project["updated_at"] == "2026-03-01T00:00:00Z"
    # The graduated idea remains an idea; the project records the association.
    assert project["history"] == [{
        "kind": "graduated_idea", "label": "beta-idea (graduated)",
        "text": "id_beta", "created_at": "2026-05-09T00:00:00Z",
    }]
    idea = catalog.get_item(_identity("idea", "id_beta"))
    assert idea["kind"] == "idea" and idea["status"] == "graduated"
    assert {"kind": "graduation", "label": "graduated_to_project_ref",
            "text": "helios-zero-inference-cost-foresight",
            "created_at": "2026-05-09T00:00:00Z"} in idea["history"]


def test_detail_history_is_bounded_and_ordered(tmp_path):
    def populate(connection):
        _insert_idea(connection, "id_busy", status="incubating", rounds=200)
        for round_n in range(1, 201):
            connection.execute(
                """INSERT INTO idea_attempts
                   (idea_id, round_n, papers_pulled, decompositions, convergence_reason,
                    started_at, completed_at)
                   VALUES ('id_busy', ?, '[]', '[]', ?, '2026-05-01T00:00:00Z', ?)""",
                (round_n, "x" * 4000, f"2026-05-01T00:00:{round_n:02d}Z"),
            )

    catalog = ResearchCatalog(_build(tmp_path / "busy.db", populate))
    history = catalog.get_item(_identity("idea", "id_busy"))["history"]

    assert len(history) == MAX_HISTORY
    assert [entry["created_at"] for entry in history] == sorted(
        (entry["created_at"] for entry in history), reverse=True)
    assert all(len(entry["text"]) <= 1001 and entry["text"].endswith("…") for entry in history)
    assert set(history[0]) == {"kind", "label", "text", "created_at"}


def test_pagination_is_stable_and_bounded(catalog):
    everything = catalog.list_items(limit=MAX_LIMIT)["items"]
    paged = []
    for offset in range(0, 10, 3):
        page = catalog.list_items(limit=3, offset=offset)
        assert page == {"items": everything[offset:offset + 3], "total": 8,
                        "limit": 3, "offset": offset}
        paged.extend(page["items"])

    assert paged == everything
    assert len({item["id"] for item in paged}) == 8
    # Newest first, then kind and original identity, so a boundary never moves.
    assert [item["origin_id"] for item in everything] == [
        "id_beta", "id_expl_live", "id_expl_parked", "id_alpha", "id_gamma",
        "id_expl_orphan", "realtime-aware-sparsecache-memory",
        "helios-zero-inference-cost-foresight",
    ]
    assert catalog.list_items(limit=3, offset=99)["items"] == []
    for bad in ({"limit": 0}, {"limit": MAX_LIMIT + 1}, {"limit": True}, {"offset": -1},
                {"kind": "paper"}, {"status": "sleeping"}):
        with pytest.raises(ValueError):
            catalog.list_items(**bad)


def test_document_candidates_are_internal_and_kind_specific(catalog):
    # `kind` stays in the item vocabulary, so an adoption source root matches
    # the owning research item without translation.
    assert catalog.document_candidates(_identity("idea", "id_alpha")) == [
        {"kind": "idea", "registered_path": "/legacy/ideas/id_alpha.md"}
    ]
    assert catalog.document_candidates(_identity("exploration", "id_expl_live")) == [
        {"kind": "exploration", "registered_path": "/legacy/explorations/id_expl_live.md"}
    ]
    # Monitored projects register no document path in the Research DB, and an
    # exploration identity without a seed row has none either.
    assert catalog.document_candidates(
        _identity("project", "realtime-aware-sparsecache-memory")) == []
    assert catalog.document_candidates(_identity("exploration", "id_expl_orphan")) == []


def test_unknown_item_is_not_found(catalog):
    for missing in (_identity("idea", "id_absent"), "ri_" + "0" * 32):
        with pytest.raises(CatalogItemNotFound):
            catalog.get_item(missing)
        with pytest.raises(CatalogItemNotFound):
            catalog.document_candidates(missing)
    with pytest.raises(ValueError):
        catalog.get_item("")


def test_empty_and_missing_schema_are_distinct(tmp_path):
    empty = ResearchCatalog(_build(tmp_path / "empty.db", populate=None))
    assert empty.list_items() == {"items": [], "total": 0, "limit": 100, "offset": 0}

    absent = ResearchCatalog(tmp_path / "nowhere.db")
    with pytest.raises(CatalogUnavailable):
        absent.list_items()

    foreign = tmp_path / "foreign.db"
    sqlite3.connect(foreign).close()
    with pytest.raises(CatalogUnavailable, match="schema is not present"):
        ResearchCatalog(foreign).list_items()


def test_corrupt_database_is_reported_not_swallowed(tmp_path, database):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(database.read_bytes()[:200] + b"\xff" * 4096)

    with pytest.raises(CatalogUnavailable):
        ResearchCatalog(corrupt).list_items()


def test_reads_committed_wal_and_preserves_the_database(database, catalog):
    """Rows committed into the write-ahead log are visible, and nothing moves.

    A writer holding the database open leaves the commit in `-wal`. An
    `immutable=1` read would report the pre-commit rows instead; this asserts
    the newly committed row is returned while the main file's bytes and the
    log's contents stay exactly as found.
    """

    writer = sqlite3.connect(database)
    try:
        _insert_idea(writer, "id_wal", status="killed", slug="wal-idea",
                     kill_reason="hypothesis failed", updated_at="2026-07-01T00:00:00Z")
        writer.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        before = _state(database)

        page = catalog.list_items(status="killed")

        assert [item["origin_id"] for item in page["items"]] == ["id_wal"]
        assert page["items"][0]["pause_reason"] == "hypothesis failed"
        assert catalog.get_item(_identity("idea", "id_wal"))["status"] == "killed"
        after = _state(database)
        # The database and its log are byte-identical; only `-shm`, SQLite's
        # shared-memory reader index, may record that a reader was present.
        assert set(after) == set(before)
        assert {name: digest for name, digest in after.items() if not name.endswith("-shm")} == {
            name: digest for name, digest in before.items() if not name.endswith("-shm")
        }
    finally:
        writer.close()
