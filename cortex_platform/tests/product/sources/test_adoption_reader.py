"""Building an adoption manifest from a copied research database and corpus.

The fixtures here are built by the REAL `cortex_research` schema, not by
hand-written DDL: the reader's whole job is to survive the shape the engine
actually writes, and a fixture of my own assumptions would not test that.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from cortex_platform.product.sources.adoption import (
    CorpusReadError,
    read_corpus,
)


def read_adoption_manifest(**kwargs):
    return read_corpus(**kwargs).manifest


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    return root


@pytest.fixture
def database(tmp_path: Path) -> Path:
    from cortex_research import db as research_db

    path = tmp_path / "research.db"
    conn = research_db.connect(path)
    research_db.apply_schema(conn)
    conn.commit()
    conn.close()
    # A clean close checkpoints and removes the log, which is what makes the
    # copy readable as immutable. Assert it, because the reader depends on it.
    assert not path.with_name(path.name + "-wal").exists()
    return path


def _write(database: Path, statement: str, parameters: tuple = ()) -> None:
    """Write and CLOSE, because closing the last connection checkpoints.

    `with sqlite3.connect(...)` commits but does not close, so it leaves a
    non-empty write-ahead log -- exactly the state the reader refuses, and
    exactly why a real copy must be taken after the writers are gone.
    """

    conn = sqlite3.connect(database)
    try:
        conn.execute(statement, parameters)
        conn.commit()
    finally:
        conn.close()


def _add_paper(
    database: Path,
    corpus: Path,
    *,
    paper_dir: str,
    title: str,
    arxiv_id: str | None = None,
    body: str = "full text",
) -> None:
    _write(
        database,
        """INSERT INTO papers
           (paper_dir, title, date, keywords, summary, projects,
            indexed_at, arxiv_id)
           VALUES (?, ?, '2026-06-18', '[]', 's', '[]',
                   '2026-06-18T00:00:00Z', ?)""",
        (paper_dir, title, arxiv_id),
    )
    directory = corpus / paper_dir
    directory.mkdir(parents=True)
    (directory / "full_text.md").write_text(body, encoding="utf-8")
    (directory / "notes.md").write_text("notes for " + title, encoding="utf-8")


class TestReading:
    def test_an_arxiv_paper_is_read_under_the_arxiv_authority(
        self, database: Path, corpus: Path
    ) -> None:
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Speculative_Decoding",
            title="Speculative Decoding",
            arxiv_id="2401.12345",
        )
        manifest = read_adoption_manifest(database=database, corpus_root=corpus)
        (entry,) = manifest.entries
        assert entry.authority == "arxiv"
        assert entry.authority_id == "2401.12345"
        assert entry.official_title == "Speculative Decoding"
        assert entry.engine_ref == "paper:20260618-Speculative_Decoding"

    def test_a_paper_without_an_arxiv_id_falls_back_to_its_content(
        self, database: Path, corpus: Path
    ) -> None:
        _add_paper(
            database,
            corpus,
            paper_dir="20260619-blog-推测解码",
            title="推测解码",
            body="blog body",
        )
        manifest = read_adoption_manifest(database=database, corpus_root=corpus)
        (entry,) = manifest.entries
        assert entry.authority == "sha256"
        assert entry.authority_id == hashlib.sha256(b"blog body").hexdigest()
        assert entry.engine_ref.startswith("paper:enc/")

    def test_the_content_digest_covers_the_whole_directory(
        self, database: Path, corpus: Path
    ) -> None:
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Speculative_Decoding",
            title="Speculative Decoding",
            arxiv_id="2401.12345",
        )
        first = read_adoption_manifest(database=database, corpus_root=corpus)
        # A file the primary-artifact digest does not see must still change
        # the entry, or parity cannot detect a partial copy.
        (corpus / "20260618-Speculative_Decoding" / "grounding.md").write_text(
            "grounding", encoding="utf-8"
        )
        second = read_adoption_manifest(database=database, corpus_root=corpus)
        assert first.entries[0].content_digest != second.entries[0].content_digest
        assert first.manifest_id != second.manifest_id

    def test_reading_is_deterministic(self, database: Path, corpus: Path) -> None:
        _add_paper(
            database,
            corpus,
            paper_dir="20260101-A",
            title="A",
            arxiv_id="2401.00001",
        )
        _add_paper(
            database,
            corpus,
            paper_dir="20260102-B",
            title="B",
            arxiv_id="2401.00002",
        )
        first = read_adoption_manifest(database=database, corpus_root=corpus)
        second = read_adoption_manifest(database=database, corpus_root=corpus)
        assert first.manifest_id == second.manifest_id
        assert len(first.entries) == 2

    def test_reading_leaves_no_sidecars_behind(
        self, database: Path, corpus: Path
    ) -> None:
        # The R0-C lesson: a read-only open of a WAL database still creates
        # -wal and -shm unless it is opened immutable.
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Speculative_Decoding",
            title="Speculative Decoding",
            arxiv_id="2401.12345",
        )
        before = {entry.name for entry in database.parent.iterdir()}
        read_adoption_manifest(database=database, corpus_root=corpus)
        assert {entry.name for entry in database.parent.iterdir()} == before


class TestRefusals:
    def test_an_uncheckpointed_database_is_refused(
        self, database: Path, corpus: Path
    ) -> None:
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Speculative_Decoding",
            title="Speculative Decoding",
            arxiv_id="2401.12345",
        )
        # A non-empty write-ahead log means the file is not the whole state,
        # so a copy taken now would silently lose committed rows.
        database.with_name(database.name + "-wal").write_bytes(b"x" * 32)
        with pytest.raises(CorpusReadError, match="not checkpointed"):
            read_adoption_manifest(database=database, corpus_root=corpus)

    def test_a_radar_stub_is_skipped_rather_than_refused(
        self, database: Path, corpus: Path
    ) -> None:
        # 206 of the operator's 573 rows are radar stubs: the engine noticed
        # a paper and never ingested it, so paper_dir is 'arxiv:<id>' and no
        # directory exists BY DESIGN. Adopting one as an existing source would
        # claim content the engine does not have; refusing the whole read
        # because of them would make adoption impossible.
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Present",
            title="Present",
            arxiv_id="2401.12345",
        )
        _write(
            database,
            """INSERT INTO papers
               (paper_dir, title, date, keywords, summary, projects,
                indexed_at, arxiv_id)
               VALUES ('arxiv:2401.99999', 'Radar Stub', '2026-06-19', '[]',
                       's', '[]', '2026-06-19T00:00:00Z', '2401.99999')""",
        )
        result = read_corpus(database=database, corpus_root=corpus)
        assert [entry.paper_dir for entry in result.manifest.entries] == [
            "20260618-Present"
        ]
        assert result.skipped == (("arxiv:2401.99999", "radar_stub"),)

    def test_a_materialized_paper_that_lost_its_directory_is_named(
        self, database: Path, corpus: Path
    ) -> None:
        # Distinct from a radar stub: this row's shape says it WAS ingested,
        # so a missing directory is a corpus anomaly the operator must see.
        # The operator's live corpus has exactly one.
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Present",
            title="Present",
            arxiv_id="2401.12345",
        )
        _write(
            database,
            """INSERT INTO papers
               (paper_dir, title, date, keywords, summary, projects,
                indexed_at, arxiv_id)
               VALUES ('20260619-blog-Missing', 'Missing', '2026-06-19', '[]',
                       's', '[]', '2026-06-19T00:00:00Z', NULL)""",
        )
        result = read_corpus(database=database, corpus_root=corpus)
        assert result.skipped == (("20260619-blog-Missing", "missing_directory"),)
        assert len(result.manifest.entries) == 1

    def test_an_empty_directory_is_refused_by_name(
        self, database: Path, corpus: Path
    ) -> None:
        _add_paper(
            database,
            corpus,
            paper_dir="20260618-Empty",
            title="Empty",
            arxiv_id="2401.12345",
        )
        for entry in (corpus / "20260618-Empty").iterdir():
            entry.unlink()
        with pytest.raises(CorpusReadError) as error:
            read_adoption_manifest(database=database, corpus_root=corpus)
        assert "20260618-Empty" in str(error.value)

    def test_an_empty_corpus_is_refused(self, database: Path, corpus: Path) -> None:
        with pytest.raises(ValueError, match="adoption manifest is empty"):
            read_adoption_manifest(database=database, corpus_root=corpus)

    def test_a_missing_database_is_refused(self, tmp_path: Path, corpus: Path) -> None:
        with pytest.raises(CorpusReadError, match="database"):
            read_adoption_manifest(
                database=tmp_path / "absent.db", corpus_root=corpus
            )
