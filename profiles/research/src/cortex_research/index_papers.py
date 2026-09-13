"""Index agent-readings papers (READ-ONLY) into research.db (papers/chunks/vec0/fts5)."""
from __future__ import annotations

import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

from .catalog import parse_notes
from .chunker import chunk_paper
from .db import connect, apply_schema
from .embed import embed_texts


def agent_readings_papers() -> Path:
    """The corpus's `papers` directory, from `CORTEX_AGENT_READINGS`.

    No default. The engine binds this variable on every child from a
    product-owned root (`engine/bindings.py` `CORTEX_AGENT_READINGS`, BOUND,
    `path:readings_root`) and the child's environment is fully replacing, so a
    default can only be reached by a direct invocation outside the product --
    where guessing one machine's corpus directory would write papers into a
    path the caller never named. The engine's own service refuses the same way
    rather than guessing a corpus root.
    """

    root = os.environ.get("CORTEX_AGENT_READINGS")
    if not root:
        raise RuntimeError(
            "CORTEX_AGENT_READINGS is required to locate the readings corpus"
        )
    return Path(root) / "papers"


def _catalog_chunk_text(entry: dict) -> str:
    kws = " ".join(entry.get("keywords") or [])
    return f"{entry['title']} | {kws} | {entry.get('summary','')}"


def index_paper(notes_path: Path, *, source: str = "reader",
                arxiv_id: str | None = None,
                published_at: str | None = None,
                full_text_source: str | None = None,
                source_url: str | None = None) -> bool:
    """Index a single paper's notes.md into research.db. Returns True if indexed.

    F5: `source` kwarg defaults to 'reader' (M1a/M1b behavior, backward compat).
    M1f's radar_index.py wrapper passes source='radar'.

    `arxiv_id` is the authoritative dedup key, stored in papers.arxiv_id so that
    membership checks no longer depend on the paper_dir naming convention (which
    is now full-title for agent ingests). NULL for non-arxiv (reader) papers.

    `full_text_source` ('html'|'ocr'|'pdf') records how the body was extracted (the
    value fetch_full_text returns). NULL for reader/legacy papers. A 'pdf' value
    flags a plain-text (no-formula, no-image) dump that a backfill can target.

    `source_url` is the normalized origin URL for a NON-arxiv PDF ingest
    (ingest_pdf_url) — the dedup key so re-ingesting the same URL is an update,
    not a duplicate. NULL for arxiv/reader papers.
    """
    paper_dir = notes_path.parent.name
    entry = parse_notes(notes_path)
    if entry is None:
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    apply_schema(conn)
    from .radar_schema import ensure_radar_schema
    ensure_radar_schema(conn)

    try:
        with conn:
            old_ids = [r[0] for r in conn.execute(
                "SELECT id FROM chunks WHERE paper_dir = ?", (paper_dir,))]
            if old_ids:
                # vec0 has no ON DELETE CASCADE — purge old embeddings explicitly
                conn.execute(
                    f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({','.join('?' * len(old_ids))})",
                    old_ids,
                )
            conn.execute("DELETE FROM chunks WHERE paper_dir = ?", (paper_dir,))
            conn.execute(
                """INSERT INTO papers (paper_dir,title,date,keywords,summary,projects,indexed_at,source,arxiv_id,published_at,full_text_source,source_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(paper_dir) DO UPDATE SET
                     title=excluded.title, date=excluded.date, keywords=excluded.keywords,
                     summary=excluded.summary, projects=excluded.projects,
                     indexed_at=excluded.indexed_at, source=excluded.source,
                     arxiv_id=excluded.arxiv_id, published_at=excluded.published_at,
                     full_text_source=excluded.full_text_source,
                     source_url=excluded.source_url""",
                (paper_dir, entry["title"], entry.get("date"),
                 json.dumps(entry.get("keywords") or []), entry.get("summary"),
                 json.dumps(entry.get("projects") or []), now, source, arxiv_id,
                 published_at, full_text_source, source_url),
            )
            # build chunk list: catalog pseudo-chunk + real chunks
            chunk_rows = [("__catalog__", 0, _catalog_chunk_text(entry))]
            ft = notes_path.parent / "full_text.md"
            if ft.exists():
                for c in chunk_paper(ft, entry["title"]):
                    chunk_rows.append((c["section"], c["chunk_idx"], c["text"]))
            texts = [t for (_, _, t) in chunk_rows]
            if os.environ.get("CORTEX_SKIP_EMBED") == "1":
                vectors = [[0.0] * 4096 for _ in texts]
            else:
                vectors = embed_texts(texts)
            # Assign a paper-GLOBAL sequential chunk_idx in document order.
            # chunk_rows are already in document order (catalog, then
            # section-by-section); the chunker's per-section `_section_idx` is
            # NOT globally meaningful (single-sub-chunk sections all get 0), so
            # `ORDER BY chunk_idx` across a paper would be useless without this.
            for global_idx, ((section, _section_idx, text), vec) in enumerate(zip(chunk_rows, vectors)):
                cur = conn.execute(
                    "INSERT INTO chunks (paper_dir,section,chunk_idx,text) VALUES (?,?,?,?)",
                    (paper_dir, section, global_idx, text),
                )
                blob = struct.pack(f"{len(vec)}f", *vec)
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, embedding) VALUES (?, ?)",
                    (cur.lastrowid, blob),
                )
        return True
    finally:
        conn.close()


import re as _re

_ARXIV_ID_RE = _re.compile(r"\b(\d{4}\.\d{4,5})\b")


def backfill_arxiv_id(conn) -> int:
    """Populate papers.arxiv_id for FULL-TEXT rows where it is NULL by extracting
    the first arxiv id (NNNN.NNNNN) from paper_dir. Covers the dash form
    (arxiv-<id>) and idea_curate's {date}-{id}-{slug}. Reader full-title dirs
    (8-digit date, no dot) yield no match and stay NULL.

    CRITICAL: radar COLON stubs ('arxiv:<id>', source='radar') are abstract-only,
    NOT full text — they are EXCLUDED so arxiv_id stays an authoritative full-text
    key. If a colon stub got an arxiv_id, every membership check (_corpus_lookup /
    _papers_has_arxiv / the director wake-gate / radar_backfill) would treat the
    abstract-only stub as a full-text hit and (a) short-circuit ingest to
    full_text_chars=0, (b) retire the radar signal, and (c) let reingest_legacy
    delete the real full-text dir. Hence the `paper_dir NOT LIKE 'arxiv:%'` filter.

    Idempotent (only touches NULL rows). Returns the number of rows updated."""
    n = 0
    for (pd,) in conn.execute(
        "SELECT paper_dir FROM papers "
        "WHERE arxiv_id IS NULL AND paper_dir NOT LIKE 'arxiv:%'"
    ).fetchall():
        m = _ARXIV_ID_RE.search(pd or "")
        if m:
            conn.execute("UPDATE papers SET arxiv_id=? WHERE paper_dir=?",
                         (m.group(1), pd))
            n += 1
    conn.commit()
    return n


def backfill_chunk_idx(conn) -> int:
    """Rewrite every chunk's chunk_idx to a paper-GLOBAL sequential index.

    Idempotent migration for rows inserted before the global-idx fix (where
    chunk_idx was the chunker's per-section sub-chunk index, almost always 0).
    For each paper, chunk_idx becomes the count of same-paper rows with a smaller
    `id` — i.e. 0,1,2,... in `id` order (= insertion = document order). No
    re-embedding; only the chunk_idx column changes.

    Returns the number of rows updated. Safe to run repeatedly (a second run
    produces the identical assignment).
    """
    cur = conn.execute(
        """
        UPDATE chunks SET chunk_idx = (
            SELECT COUNT(*) FROM chunks c2
            WHERE c2.paper_dir = chunks.paper_dir AND c2.id < chunks.id
        )
        """
    )
    return cur.rowcount


def build_index(full: bool = False, papers_dir: Path | None = None) -> int:
    """Index papers. Returns number of papers (re)indexed. READ-ONLY on agent-readings.

    Default source='reader' for all papers indexed via this entry point.
    """
    papers_dir = papers_dir or agent_readings_papers()
    conn = connect()
    apply_schema(conn)
    existing = {r[0] for r in conn.execute("SELECT paper_dir FROM papers")}
    conn.close()

    indexed = 0
    for notes_path in sorted(papers_dir.glob("*/notes.md")):
        paper_dir = notes_path.parent.name
        if not full and paper_dir in existing:
            continue
        if index_paper(notes_path, source="reader"):
            indexed += 1
    return indexed

