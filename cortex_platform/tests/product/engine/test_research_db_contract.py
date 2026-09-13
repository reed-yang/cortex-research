"""A `research.db` the KEPT engine code creates satisfies every product reader.

The product couples to the engine's database by raw SQL, not by import
(`product/research/catalog.py:7-9` states the rule: importing `cortex_research`
initializes and migrates schemas). So no import graph can prove the narrowed
package still answers the product's queries -- only executing them can.

The three readers this pins are `product/sources/adoption.py` (`read_corpus`),
`product/sources/search.py` (the chunk and title lookups) and
`product/engine/child.py` (the indexed-chunk count). Each query below is issued
against a database built ONLY by `cortex_research.db.apply_schema` +
`radar_schema.ensure_radar_schema`, which is exactly what `EngineRoots.prepare`
plus a first ingest leave behind.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.engine.child import _chunk_count
from cortex_platform.product.research.catalog import CatalogUnavailable, ResearchCatalog
from cortex_platform.product.sources.adoption import CorpusReadError, read_corpus

from .conftest import build_research_db


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    return build_research_db(tmp_path / "research" / "research.db")


def _columns(database: Path, table: str) -> set[str]:
    connection = sqlite3.connect(str(database))
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def _objects(database: Path) -> set[str]:
    connection = sqlite3.connect(str(database))
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    finally:
        connection.close()


def test_the_tables_the_product_reads_exist(fresh_db: Path) -> None:
    assert {"papers", "chunks", "chunks_fts"} <= _objects(fresh_db)


def test_the_columns_the_product_reads_exist(fresh_db: Path) -> None:
    # adoption.read_corpus + search.py's title lookup.
    assert {"paper_dir", "title", "arxiv_id"} <= _columns(fresh_db, "papers")
    # search.py's chunk projection + child.py's chunk count.
    assert {"id", "paper_dir", "section", "text"} <= _columns(fresh_db, "chunks")


def test_the_adoption_reader_runs_on_a_fresh_database(fresh_db: Path, tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    paper = corpus_root / "20260101-A-Paper"
    paper.mkdir(parents=True)
    (paper / "notes.md").write_text("# Notes: A Paper\n", encoding="utf-8")
    connection = sqlite3.connect(str(fresh_db))
    try:
        connection.execute(
            "INSERT INTO papers(paper_dir, title, indexed_at, arxiv_id)"
            " VALUES (?, ?, ?, ?)",
            ("20260101-A-Paper", "A Paper", "2026-01-01T00:00:00Z", "2601.00001"),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    result = read_corpus(database=fresh_db, corpus_root=corpus_root)
    entries = result.manifest.entries
    assert [entry.paper_dir for entry in entries] == ["20260101-A-Paper"]
    assert entries[0].official_title == "A Paper"
    assert (entries[0].authority, entries[0].authority_id) == ("arxiv", "2601.00001")
    assert result.skipped == ()


def test_the_knowledge_search_queries_run_on_a_fresh_database(fresh_db: Path) -> None:
    """The exact shapes `product/sources/search.py` issues, executed verbatim."""

    connection = sqlite3.connect(str(fresh_db))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "INSERT INTO papers(paper_dir, title, indexed_at, arxiv_id)"
            " VALUES (?, ?, ?, ?)",
            ("20260101-A-Paper", "A Paper", "2026-01-01T00:00:00Z", "2601.00001"),
        )
        connection.execute(
            "INSERT INTO chunks(paper_dir, chunk_idx, section, text)"
            " VALUES (?, ?, ?, ?)",
            ("20260101-A-Paper", 0, "Method", "diffusion transformer latents"),
        )
        connection.commit()
        rows = connection.execute(
            """SELECT c.id, c.paper_dir,
                      CASE WHEN length(CAST(c.section AS BLOB)) <= 4000
                           THEN c.section END AS section,
                      CASE WHEN length(CAST(c.text AS BLOB)) <= ?
                           THEN c.text END AS text,
                      length(CAST(c.text AS BLOB)) AS byte_length
               FROM chunks_fts
               JOIN chunks c ON chunks_fts.rowid = c.id
               JOIN papers p ON p.paper_dir = c.paper_dir
               WHERE chunks_fts MATCH ? AND c.paper_dir IN (?)
               ORDER BY chunks_fts.rank, c.paper_dir, c.id LIMIT ?""",
            (4000, "diffusion", "20260101-A-Paper", 10),
        ).fetchall()
        assert [row["paper_dir"] for row in rows] == ["20260101-A-Paper"]
        assert rows[0]["section"] == "Method"
        title = connection.execute(
            """SELECT CASE WHEN length(CAST(title AS BLOB)) <= ?
                           THEN title END AS title
               FROM papers WHERE paper_dir = ?""",
            (4000, "20260101-A-Paper"),
        ).fetchone()
        assert title["title"] == "A Paper"
    finally:
        connection.close()


def test_the_child_chunk_count_query_runs_on_a_fresh_database(fresh_db: Path) -> None:
    connection = sqlite3.connect(str(fresh_db))
    try:
        connection.execute(
            "INSERT INTO chunks(paper_dir, chunk_idx, section, text)"
            " VALUES (?, ?, ?, ?)",
            ("20260101-A-Paper", 0, "Method", "body"),
        )
        connection.commit()
    finally:
        connection.close()
    assert _chunk_count(str(fresh_db), "20260101-A-Paper") == 1
    assert _chunk_count(str(fresh_db), "20260101-Another-Paper") == 0


def test_a_fresh_database_carries_no_legacy_catalog_and_says_so(fresh_db: Path) -> None:
    """A fresh install has no preserved legacy items -- and must not pretend to.

    `ResearchCatalog` refuses a database with none of its six tables rather than
    reporting an empty catalog, so the narrowed schema cannot silently turn a
    missing legacy corpus into "you have no ideas".
    """

    catalog = ResearchCatalog(fresh_db)
    with pytest.raises(CatalogUnavailable):
        catalog.list_items()


def test_an_absent_database_is_still_an_explicit_refusal(tmp_path: Path) -> None:
    with pytest.raises(CorpusReadError):
        read_corpus(
            database=tmp_path / "does-not-exist.db", corpus_root=tmp_path / "corpus"
        )
