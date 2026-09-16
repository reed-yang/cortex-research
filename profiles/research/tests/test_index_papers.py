import json
from pathlib import Path


def _make_paper(root: Path, name: str, title: str, body: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "notes.md").write_text(
        f"# Notes: {title}\n\n### 2026-01-01\n\n## Paper Summary\n\n{title} summary text here, long enough.\n\n"
        f"### Keywords / 关键术语\n\n`alpha`, `beta`\n", encoding="utf-8")
    (d / "full_text.md").write_text(f"## Method\n\n{body}\n", encoding="utf-8")


def test_index_builds_papers_and_chunks(tmp_path, monkeypatch, research_db):
    papers = tmp_path / "agent-readings" / "papers"
    _make_paper(papers, "20260101-A", "Paper A", "Method A does sequence concatenation.")
    _make_paper(papers, "20260102-B", "Paper B", "Method B extends sequence concatenation.")
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "agent-readings"))

    from cortex_research.index_papers import build_index
    n = build_index(full=True)
    assert n == 2

    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(research_db))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    # each paper has a __catalog__ chunk + >=1 real chunk
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE section='__catalog__'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 4
    conn.close()


def test_reindex_full_no_orphan_embeddings(tmp_path, monkeypatch, research_db):
    papers = tmp_path / "agent-readings" / "papers"
    _make_paper(papers, "20260101-A", "Paper A", "Method A does sequence concatenation.")
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "agent-readings"))
    from cortex_research.index_papers import build_index
    build_index(full=True)
    build_index(full=True)  # reindex same paper -> must not leave orphan embeddings
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(research_db))
    conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_emb = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    assert n_emb == n_chunks, f"orphan embeddings: {n_emb} emb vs {n_chunks} chunks"
    conn.close()


def test_incremental_refresh_preserves_provenance_and_reuses_vectors(tmp_path, monkeypatch, research_db):
    from cortex_research import index_papers
    from cortex_research.db import connect
    papers = tmp_path / 'papers'
    _make_paper(papers, '20260101-A', 'Paper A', 'Original method body.')
    notes = papers / '20260101-A' / 'notes.md'
    monkeypatch.delenv('CORTEX_SKIP_EMBED')
    calls = []
    def embed(texts):
        calls.append(texts)
        # Another connection must be able to acquire the write lock during HTTP.
        with connect() as other:
            other.execute('BEGIN IMMEDIATE')
        return [[1.0] * 4096 for _ in texts]
    monkeypatch.setattr(index_papers, 'embed_texts', embed)
    assert index_papers.index_paper(notes, source='agent', arxiv_id='2601.12345',
                                   full_text_source='html', published_at='2026-01-01')
    assert index_papers.build_index(papers_dir=papers) == 0
    (notes.parent / 'full_text.md').write_text('## Method\n\nChanged method body.\n')
    assert index_papers.build_index(papers_dir=papers) == 1
    assert len(calls) == 2 and len(calls[1]) == 1
    with connect() as conn:
        row = conn.execute('SELECT * FROM papers').fetchone()
        assert (row['source'], row['arxiv_id'], row['full_text_source'], row['published_at']) == (
            'agent', '2601.12345', 'html', '2026-01-01')
        assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'Changed'").fetchone()[0] == 1
    assert index_papers.build_index(papers_dir=papers) == 0


def test_invalid_embedding_response_preserves_previous_paper(tmp_path, monkeypatch, research_db):
    import pytest
    from cortex_research import index_papers
    from cortex_research.db import connect
    papers = tmp_path / 'papers'
    _make_paper(papers, '20260101-A', 'Paper A', 'Original body.')
    notes = papers / '20260101-A' / 'notes.md'
    index_papers.index_paper(notes)
    with connect() as conn:
        before = [tuple(r) for r in conn.execute('SELECT * FROM chunks')]
    (notes.parent / 'full_text.md').write_text('## Method\n\nReplacement body.\n')
    monkeypatch.delenv('CORTEX_SKIP_EMBED')
    for invalid in ([], [[float('nan')] * 4096], [[1.0] * 32]):
        monkeypatch.setattr(index_papers, 'embed_texts', lambda texts: invalid)
        with pytest.raises(ValueError, match='Embedding response'):
            index_papers.index_paper(notes)
        with connect() as conn:
            assert [tuple(r) for r in conn.execute('SELECT * FROM chunks')] == before


def test_concurrent_source_edit_does_not_publish_stale_index(tmp_path, monkeypatch, research_db):
    import pytest
    from cortex_research import index_papers
    from cortex_research.db import connect
    papers = tmp_path / 'papers'
    _make_paper(papers, '20260101-A', 'Paper A', 'Original body.')
    notes = papers / '20260101-A' / 'notes.md'
    monkeypatch.delenv('CORTEX_SKIP_EMBED')
    def embed(texts):
        notes.write_text(notes.read_text() + '\nConcurrent edit\n')
        return [[1.0] * 4096 for _ in texts]
    monkeypatch.setattr(index_papers, 'embed_texts', embed)
    with pytest.raises(RuntimeError, match='Paper changed'):
        index_papers.index_paper(notes)
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM papers').fetchone()[0] == 0
