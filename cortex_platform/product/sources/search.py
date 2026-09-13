"""Adoption-scoped lexical retrieval without legacy initialization or vectors."""

from __future__ import annotations

import hashlib
import re

from .reader import (
    SourceContentUnavailable,
    SourceQueryInvalid,
    _database,
    _directory,
    _integer_limit,
)

MAX_QUERY_BYTES = 1_024
MAX_QUERY_TERMS = 32
MAX_RESULTS = 50
MAX_CHUNK_BYTES = 64 * 1024
MAX_EXCERPT_BYTES = 2_000
MAX_CANDIDATES = 200
_STOP = frozenset(
    "the and for are was were with from this that does via into onto per its our "
    "their not but can has have had will would should or near".split()
)


def _terms(query: str) -> tuple[list[str], list[str]]:
    # Match legacy _fts_or_query's English OR/stop-word semantics verbatim in
    # behavior, without importing the MCP module's provider-capable dependency tree.
    english = list(dict.fromkeys(
        term for term in re.findall(r"[a-z0-9]+", query.lower())
        if len(term) >= 3 and term not in _STOP
    ))
    unicode = list(dict.fromkeys(
        term for term in re.findall(r"[^\W_]+", query.casefold())
        if not term.isascii()
    ))
    if len(english) + len(unicode) > MAX_QUERY_TERMS:
        raise SourceQueryInvalid("query has too many terms")
    if not english and not unicode:
        raise SourceQueryInvalid("query has no searchable terms")
    return english, unicode


def _bounded(value: str, maximum: int) -> str:
    return value.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def _result(source: dict, *, text: str, section: str, locator: str, project) -> dict:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "source_id": source["id"], "canonical_id": source["canonical_id"],
        "title": _bounded(project(source["official_title"]), 2_000),
        "evidence_id": f"source:{source['id']}:{locator}:sha256:{digest}",
        "section": _bounded(project(section), 1_000),
        "excerpt": _bounded(project(text), MAX_EXCERPT_BYTES), "content_sha256": digest,
    }


def search_knowledge(reader, query, *, limit=10) -> dict:
    """FTS5 OR/BM25 plus explicit Unicode title-substring fallback.

    Fallback examines only adopted titles via primary-key lookups. It never scans
    chunk text or loads embeddings. Chunk evidence hashes cover the complete
    indexed text, not the shortened excerpt or the current on-disk document.
    """
    limit = _integer_limit(limit, MAX_RESULTS)
    if not isinstance(query, str) or not query.strip():
        raise SourceQueryInvalid("query is empty or invalid")
    try:
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES or any(ord(c) < 32 for c in query):
            raise SourceQueryInvalid("query exceeds its byte limit or contains controls")
    except UnicodeEncodeError:
        raise SourceQueryInvalid("query is not valid Unicode") from None
    english, unicode = _terms(query)
    mode = "fts5_or" if english else "unicode_title_fallback"
    if english and unicode:
        mode = "fts5_or+unicode_title_fallback"
    try:
        with reader._registered() as (registration, root, sources):
            chunk_limit = min(registration["max_bytes"], MAX_CHUNK_BYTES)
            def available(paper_dir):
                try:
                    with _directory(root / paper_dir):
                        return True
                except (OSError, RuntimeError):
                    return False

            with _database(root.parent / "research.db") as connection:
                results = []
                if english and sources:
                    placeholders = ",".join("?" for _ in sources)
                    expression = " OR ".join(english)
                    rows = connection.execute(
                        f"""SELECT c.id, c.paper_dir,
                                   CASE WHEN length(CAST(c.section AS BLOB)) <= 4000
                                        THEN c.section END AS section,
                                   CASE WHEN length(CAST(c.text AS BLOB)) <= ?
                                        THEN c.text END AS text,
                                   length(CAST(c.text AS BLOB)) AS byte_length
                            FROM chunks_fts
                            JOIN chunks c ON chunks_fts.rowid = c.id
                            JOIN papers p ON p.paper_dir = c.paper_dir
                            WHERE chunks_fts MATCH ? AND c.paper_dir IN ({placeholders})
                            ORDER BY chunks_fts.rank, c.paper_dir, c.id LIMIT ?""",
                        (chunk_limit, expression, *sources, MAX_CANDIDATES),
                    ).fetchall()
                    for row in rows:
                        if not available(row["paper_dir"]):
                            continue
                        if row["byte_length"] > chunk_limit or row["section"] is None:
                            raise SourceContentUnavailable("knowledge chunk byte limit exceeded")
                        results.append(_result(
                            sources[row["paper_dir"]], text=row["text"],
                            section=row["section"], locator=f"chunk:{row['id']}",
                            project=reader._project_evidence,
                        ))
                        if len(results) == limit:
                            break
                if unicode and sources:
                    # Per-adopted-paper primary-key lookup is bounded by
                    # MAX_SOURCES; do not use LIKE over the entire corpus.
                    title_results = []
                    for paper_dir, source in sources.items():
                        row = connection.execute(
                            """SELECT CASE WHEN length(CAST(title AS BLOB)) <= ?
                                           THEN title END AS title
                               FROM papers WHERE paper_dir = ?""",
                            (chunk_limit, paper_dir),
                        ).fetchone()
                        if row is None:
                            continue
                        title = row["title"]
                        if title is None:
                            raise SourceContentUnavailable("knowledge title byte limit exceeded")
                        if any(term in title.casefold() for term in unicode) and available(paper_dir):
                            title_results.append(_result(
                                source, text=title, section="__title__", locator="title",
                                project=reader._project_evidence,
                            ))
                            if len(title_results) == limit:
                                break
                    # Unicode hits lead mixed requests so Chinese cannot be
                    # silently discarded by an English arm filling the limit.
                    results = title_results + results
                return {"query": query, "retrieval_mode": mode, "results": results[:limit]}
    except (SourceQueryInvalid, SourceContentUnavailable):
        raise
    except Exception:
        raise SourceContentUnavailable("source search is unavailable") from None
