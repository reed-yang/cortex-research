# profiles/research/tests/test_ingest_signal_lifecycle.py
"""R1 + R2 — the ingest path closes the radar-signal lifecycle.

These tests deliberately exercise the project's REAL storage/writer code
(db.connect + radar_schema.ensure_radar_schema + db.apply_schema + the real
index_paper / _write_paper_dir), NOT hand-rolled CREATE TABLE fixtures, per the
project's prod-consistent-tests rule. Each test pins a temp CORTEX_RESEARCH_DB +
CORTEX_AGENT_READINGS and sets CORTEX_SKIP_EMBED=1 so it never touches the live
research.db or OpenRouter.
"""
from __future__ import annotations

import cortex_research.db as db
import cortex_research.index_papers as index_papers
import cortex_research.paper_ingest as paper_ingest
from cortex_research.radar_schema import ensure_radar_schema


# --------------------------------------------------------------------------- #
# Helpers — real schema, real inserts
# --------------------------------------------------------------------------- #
def _isolate_db(monkeypatch, tmp_path):
    """Point every storage entry point at a temp DB + readings dir, no embeds."""
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "readings"))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")


def _insert_signal(conn, *, arxiv_id, status="pending", paper_dir=None,
                   signal_id=None, importance=4):
    """INSERT one radar_signals row using the REAL (ensure_radar_schema) columns."""
    sid = signal_id or f"sig-{arxiv_id}"
    with conn:
        conn.execute(
            "INSERT INTO radar_signals "
            "(signal_id, source, source_ref, title, arxiv_id, importance, "
            " importance_breakdown, scoring_method, processed_status, paper_dir) "
            "VALUES (?, 'arxiv', ?, ?, ?, ?, '{}', 'rule', ?, ?)",
            (sid, f"arxiv:{arxiv_id}", f"Title {arxiv_id}", arxiv_id,
             importance, status, paper_dir),
        )
    return sid


def _signal_row(conn, signal_id):
    return conn.execute(
        "SELECT processed_status, paper_dir FROM radar_signals WHERE signal_id=?",
        (signal_id,),
    ).fetchone()


# --------------------------------------------------------------------------- #
# R1 — _mark_signal_indexed
# --------------------------------------------------------------------------- #
def test_mark_signal_indexed_closes_matching_pending_signal(monkeypatch, tmp_path):
    """A successful ingest flips a matching pending signal to indexed + dash paper_dir."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        ensure_radar_schema(conn)
        sid = _insert_signal(conn, arxiv_id="2605.28819", status="pending")
    finally:
        conn.close()

    n = paper_ingest._mark_signal_indexed("2605.28819", "arxiv-2605.28819")
    assert n == 1

    conn = db.connect()
    try:
        row = _signal_row(conn, sid)
    finally:
        conn.close()
    assert row["processed_status"] == "indexed"
    assert row["paper_dir"] == "arxiv-2605.28819"  # dash form, overwrites colon stub


def test_mark_signal_indexed_is_noop_for_nonradar_and_nonpending(monkeypatch, tmp_path):
    """(a) no matching signal -> 0 (idea-crux / direct-CLI ingest is a no-op).
    (b) a 'skipped' signal is NEVER resurrected by the pending-guard."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        ensure_radar_schema(conn)
        # (b) seed a deliberately-skipped decision with its own paper_dir
        sid_skip = _insert_signal(
            conn, arxiv_id="2601.00001", status="skipped",
            paper_dir="arxiv:2601.00001",
        )
    finally:
        conn.close()

    # (a) no row matches at all
    assert paper_ingest._mark_signal_indexed("9999.99999", "arxiv-9999.99999") == 0

    # (b) the skipped row stays skipped
    assert paper_ingest._mark_signal_indexed("2601.00001", "arxiv-2601.00001") == 0
    conn = db.connect()
    try:
        row = _signal_row(conn, sid_skip)
        # nothing was inserted for the non-matching id
        n_rows = conn.execute("SELECT COUNT(*) FROM radar_signals").fetchone()[0]
    finally:
        conn.close()
    assert row["processed_status"] == "skipped"
    assert row["paper_dir"] == "arxiv:2601.00001"  # untouched original
    assert n_rows == 1  # the (a) no-op inserted nothing


# --------------------------------------------------------------------------- #
# R2 — _already_in_corpus (dash-form papers membership only)
# --------------------------------------------------------------------------- #
def test_already_in_corpus_matches_full_text_dash_form_only(monkeypatch, tmp_path):
    """Dash-form papers row -> True; a colon stub is NOT a full-text hit -> False."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?, ?, ?)",
                ("arxiv-2605.28819", "Full Text Paper", "2026-05-30T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?, ?, ?)",
                ("arxiv:2605.30201", "Colon Stub", "2026-05-30T00:00:00Z"),
            )
    finally:
        conn.close()

    assert paper_ingest._already_in_corpus("2605.28819") is True
    # only a colon stub exists for this id -> NOT a full-text hit
    assert paper_ingest._already_in_corpus("2605.30201") is False


# --------------------------------------------------------------------------- #
# R2 integration — ingest_arxiv short-circuits when already in corpus
# --------------------------------------------------------------------------- #
def test_ingest_arxiv_short_circuits_when_already_in_corpus(monkeypatch, tmp_path):
    """Already-in-corpus -> skip fetch+embed, STILL close the signal, tag corpus."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO papers (paper_dir, title, indexed_at) VALUES (?, ?, ?)",
                ("arxiv-2605.28819", "Cached Paper", "2026-05-30T00:00:00Z"),
            )
        sid = _insert_signal(conn, arxiv_id="2605.28819", status="pending")
    finally:
        conn.close()

    # The expensive path must NEVER run.
    def _no(*a, **k):
        raise AssertionError("expensive ingest path must not run for an in-corpus paper")

    monkeypatch.setattr(paper_ingest, "fetch_full_text", _no)
    monkeypatch.setattr(paper_ingest, "_write_paper_dir", _no)
    monkeypatch.setattr(index_papers, "index_paper", _no)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "Cached Paper", "abstract": "", "published_at": "2026-05-30"},
    )

    result = paper_ingest.ingest_arxiv("2605.28819")
    assert result["already_ingested"] is True
    assert result["indexed"] is True
    assert result["full_text_source"] == "corpus"
    assert result["signals_closed"] == 1

    conn = db.connect()
    try:
        row = _signal_row(conn, sid)
    finally:
        conn.close()
    assert row["processed_status"] == "indexed"
    assert row["paper_dir"] == "arxiv-2605.28819"


# --------------------------------------------------------------------------- #
# R1 integration — the normal success path closes the signal (REAL index_paper)
# --------------------------------------------------------------------------- #
def test_ingest_arxiv_success_path_closes_signal(monkeypatch, tmp_path):
    """Full-text ingest through the REAL writer flips the signal to indexed."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        ensure_radar_schema(conn)
        sid = _insert_signal(conn, arxiv_id="2605.28819", status="pending")
    finally:
        conn.close()

    # Only the network functions are patched; _write_paper_dir + index_paper are REAL.
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "T", "abstract": "A", "published_at": "2026-05-30"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("full body text " * 200, "html", []),
    )

    result = paper_ingest.ingest_arxiv("2605.28819")
    assert result["already_ingested"] is False
    assert result["indexed"] is True
    assert result["signals_closed"] == 1
    # full-title naming with an 8-digit INGEST-date prefix (today, not the publish
    # date 2026-05-30); the id lives in the arxiv_id column, not the name.
    import re as _re
    assert _re.match(r"^\d{8}-", result["paper_dir"])
    assert not result["paper_dir"].startswith("20260530")  # NOT the publish date
    assert "2605.28819" not in result["paper_dir"]

    conn = db.connect()
    try:
        # the REAL index_paper wrote a full-title papers row with arxiv_id + the
        # publish date in published_at (separate from the ingest-date `date`).
        prow = conn.execute(
            "SELECT paper_dir, arxiv_id, published_at FROM papers WHERE paper_dir=?",
            (result["paper_dir"],),
        ).fetchone()
        srow = _signal_row(conn, sid)
    finally:
        conn.close()
    assert prow["published_at"] == "2026-05-30"  # publish date preserved separately
    assert prow is not None and prow["arxiv_id"] == "2605.28819"
    assert srow["processed_status"] == "indexed"
    assert srow["paper_dir"] == result["paper_dir"]


# --------------------------------------------------------------------------- #
# T3 — dedup by the authoritative papers.arxiv_id column (full-title naming)
# --------------------------------------------------------------------------- #
def test_corpus_lookup_matches_by_arxiv_id_column(monkeypatch, tmp_path):
    """A full-title paper (arxiv_id only in the column, NOT in paper_dir) is found
    by _corpus_lookup, which returns the real dir. An unknown id returns None."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    db.apply_schema(conn)
    ensure_radar_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO papers (paper_dir,title,indexed_at,source,arxiv_id) "
            "VALUES ('20260601-Helios_Real_Time','Helios',datetime('now'),"
            "'agent','2603.04379')"
        )
    conn.close()
    assert paper_ingest._corpus_lookup("2603.04379") == "20260601-Helios_Real_Time"
    assert paper_ingest._corpus_lookup("9999.99999") is None


def test_corpus_lookup_excludes_colon_stub(monkeypatch, tmp_path):
    """A radar colon stub 'arxiv:<id>' (abstract-only, arxiv_id column NULL) is
    NOT a full-text hit."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    db.apply_schema(conn)
    ensure_radar_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO papers (paper_dir,title,indexed_at,source) "
            "VALUES ('arxiv:2605.10759','Stub',datetime('now'),'radar')"
        )
    conn.close()
    assert paper_ingest._corpus_lookup("2605.10759") is None


def test_ingest_arxiv_endtoend_dedups_by_column_on_recall(monkeypatch, tmp_path):
    """Ingest once via the REAL writer (full-title dir + arxiv_id column), then a
    second ingest dedups via the arxiv_id column and returns the SAME full-title
    dir without re-fetching."""
    _isolate_db(monkeypatch, tmp_path)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "Helios Real Time", "abstract": "abs",
                     "published_at": "2026-03-04"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("## Method\n\n$\\alpha$ body text " * 80, "html", []),
    )
    res = paper_ingest.ingest_arxiv("2603.04379", source="agent")
    assert res["ok"] and res["indexed"] and res["already_ingested"] is False
    import re as _re
    assert _re.match(r"^\d{8}-Helios", res["paper_dir"])  # ingest-date prefix

    # second call: must NOT re-fetch (guard fetch_full_text), dedup via column
    def _boom(_aid, **k):
        raise AssertionError("fetch_full_text must not be called on a dedup hit")
    monkeypatch.setattr(paper_ingest, "fetch_full_text", _boom)
    res2 = paper_ingest.ingest_arxiv("2603.04379", source="agent")
    assert res2["already_ingested"] is True
    assert res2["paper_dir"] == res["paper_dir"]


def test_ingest_arxiv_ocr_path_copies_assets_and_counts(monkeypatch, tmp_path):
    """A no-HTML paper ingested via OCR: ingest_arxiv owns the OCR tempdir, the
    local figure file is copied into the real paper_dir/assets, and the result
    surfaces full_text_source='ocr' + images_saved/expected."""
    _isolate_db(monkeypatch, tmp_path)
    png = tmp_path / "ocr_fig.png"; png.write_bytes(b"\x89PNGFIG")
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "OCR Paper", "abstract": "A", "published_at": "2026-03-04"},
    )
    # the mock MUST accept the ocr_tmp kwarg the real ingest_arxiv now passes
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("ocr body text " * 200, "ocr",
                                   [(str(png), "assets/fig1.png")]),
    )
    result = paper_ingest.ingest_arxiv("2603.04379")
    assert result["full_text_source"] == "ocr"
    assert result["images_saved"] == 1 and result["images_expected"] == 1
    assert result["indexed"] is True
    pdir = paper_ingest.agent_readings_papers() / result["paper_dir"]
    assert (pdir / "assets" / "fig1.png").read_bytes() == b"\x89PNGFIG"


def test_backfilled_colon_stub_is_not_a_fulltext_hit(monkeypatch, tmp_path):
    """REGRESSION (review): a radar colon stub 'arxiv:<id>' (abstract-only) must
    NOT become a full-text hit after backfill_arxiv_id. Else _corpus_lookup would
    short-circuit ingest to full_text_chars=0 and reingest_legacy would delete the
    real full text. backfill excludes colon stubs, so the stub stays arxiv_id=NULL
    and _corpus_lookup returns None for a stub-only id."""
    _isolate_db(monkeypatch, tmp_path)
    from cortex_research.index_papers import backfill_arxiv_id
    conn = db.connect()
    db.apply_schema(conn)
    ensure_radar_schema(conn)
    with conn:
        conn.execute("INSERT INTO papers (paper_dir,title,indexed_at,source) "
                     "VALUES ('arxiv:2502.13995','Stub',datetime('now'),'radar')")
    backfill_arxiv_id(conn)
    stub_arxiv_id = conn.execute(
        "SELECT arxiv_id FROM papers WHERE paper_dir='arxiv:2502.13995'").fetchone()[0]
    conn.close()
    assert stub_arxiv_id is None                       # stub NOT backfilled
    assert paper_ingest._corpus_lookup("2502.13995") is None  # NOT a full-text hit
