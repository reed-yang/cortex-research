"""Index agent-readings papers (READ-ONLY) into research.db (papers/chunks/vec0/fts5)."""
from __future__ import annotations

import hashlib
import json
import math
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


def _input_digest(notes_path: Path) -> str:
    digest = hashlib.sha256()
    for path in (notes_path, notes_path.parent / "full_text.md"):
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.exists() else b"<absent>")
    return digest.hexdigest()


def _chunk_rows(notes_path: Path, entry: dict) -> list[tuple[str, str]]:
    rows = [("__catalog__", _catalog_chunk_text(entry))]
    ft = notes_path.parent / "full_text.md"
    if ft.exists():
        rows.extend((c["section"], c["text"]) for c in chunk_paper(ft, entry["title"]))
    return rows


def _valid_vector(vector, *, allow_zero: bool = False) -> bool:
    return (len(vector) == 4096 and all(math.isfinite(x) for x in vector)
            and (allow_zero or any(vector)))


def index_paper(notes_path: Path, *, source: str | None = None,
                arxiv_id: str | None = None,
                published_at: str | None = None,
                full_text_source: str | None = None,
                source_url: str | None = None,
                skip_unchanged: bool = False) -> bool:
    """Atomically index a paper, preserving omitted identity/provenance fields.

    New papers default to source='reader'. Unchanged text reuses valid vectors;
    embedding calls finish before taking the SQLite write lock. Returns False
    when skip_unchanged finds a fully current index. Source files are read-only.
    """
    paper_dir = notes_path.parent.name
    fingerprint = _input_digest(notes_path)
    entry = parse_notes(notes_path)
    if entry is None:
        return False
    chunk_rows = _chunk_rows(notes_path, entry)
    skip_embed = os.environ.get("CORTEX_SKIP_EMBED") == "1"
    conn = connect()
    apply_schema(conn)
    from .radar_schema import ensure_radar_schema
    ensure_radar_schema(conn)
    try:
        previous = conn.execute("SELECT * FROM papers WHERE paper_dir=?", (paper_dir,)).fetchone()
        old_chunks = conn.execute(
            "SELECT c.section,c.text,e.embedding FROM chunks c "
            "LEFT JOIN chunk_embeddings e ON e.chunk_id=c.id "
            "WHERE c.paper_dir=? ORDER BY c.chunk_idx,c.id", (paper_dir,)).fetchall()
        reusable = {}
        for row in old_chunks:
            blob = row["embedding"]
            if blob is not None and len(blob) == 4096 * 4:
                vector = struct.unpack("4096f", blob)
                if _valid_vector(vector, allow_zero=skip_embed):
                    reusable[row["text"]] = vector
        values = (entry["title"], entry.get("date"),
                  json.dumps(entry.get("keywords") or []), entry.get("summary"),
                  json.dumps(entry.get("projects") or []))
        same_metadata = previous is not None and values == tuple(
            previous[name] for name in ("title", "date", "keywords", "summary", "projects"))
        supplied = {"source": source, "arxiv_id": arxiv_id, "published_at": published_at,
                    "full_text_source": full_text_source, "source_url": source_url}
        same_provenance = previous is not None and all(
            value is None or previous[name] == value for name, value in supplied.items())
        if (skip_unchanged and same_metadata and same_provenance
                and chunk_rows == [(r["section"], r["text"]) for r in old_chunks]
                and all(text in reusable for _, text in chunk_rows)):
            return False
        missing = list(dict.fromkeys(text for _, text in chunk_rows if text not in reusable))
        if missing:
            vectors = [[0.0] * 4096 for _ in missing] if skip_embed else embed_texts(missing)
            if len(vectors) != len(missing) or not all(
                    _valid_vector(v, allow_zero=skip_embed) for v in vectors):
                raise ValueError("Embedding response count, dimensions or values are invalid")
            reusable.update(zip(missing, vectors))
        if _input_digest(notes_path) != fingerprint:
            raise RuntimeError("Paper changed while indexing; retry with the current files")

        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM papers WHERE paper_dir=?", (paper_dir,)).fetchone()
            if (dict(current) if current else None) != (dict(previous) if previous else None):
                raise RuntimeError("Paper index changed concurrently; retry maintenance")
            conn.execute(
                "DELETE FROM chunk_embeddings WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE paper_dir=?)", (paper_dir,))
            conn.execute("DELETE FROM chunks WHERE paper_dir=?", (paper_dir,))
            conn.execute(
                """INSERT INTO papers (paper_dir,title,date,keywords,summary,projects,indexed_at,source,arxiv_id,published_at,full_text_source,source_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(paper_dir) DO UPDATE SET
                     title=excluded.title, date=excluded.date, keywords=excluded.keywords,
                     summary=excluded.summary, projects=excluded.projects,
                     indexed_at=excluded.indexed_at, source=COALESCE(?,papers.source),
                     arxiv_id=COALESCE(excluded.arxiv_id,papers.arxiv_id),
                     published_at=COALESCE(excluded.published_at,papers.published_at),
                     full_text_source=COALESCE(excluded.full_text_source,papers.full_text_source),
                     source_url=COALESCE(excluded.source_url,papers.source_url)""",
                (paper_dir, *values, now, source or "reader", arxiv_id,
                 published_at, full_text_source, source_url, source),
            )
            for global_idx, (section, text) in enumerate(chunk_rows):
                cur = conn.execute(
                    "INSERT INTO chunks (paper_dir,section,chunk_idx,text) VALUES (?,?,?,?)",
                    (paper_dir, section, global_idx, text),
                )
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, embedding) VALUES (?, ?)",
                    (cur.lastrowid, struct.pack("4096f", *reusable[text])),
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
    indexed = 0
    for notes_path in sorted(papers_dir.glob("*/notes.md")):
        if index_paper(notes_path, skip_unchanged=not full):
            indexed += 1
    return indexed
