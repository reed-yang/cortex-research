# profiles/research/tests/test_paper_dedup_title_gate.py
"""Fix 1 — the title-match dedup gate in the ingest flow + normalize_title.

A legacy papers row with arxiv_id NULL/'' (the 231 reader/radar/agent rows the
2026-05-21 vault import left id-less) is invisible to the arxiv_id dedup, so a
re-ingest by id used to create a SECOND row/dir/chunks. The title-match gate
catches it: AFTER metadata fetch, BEFORE creating any new paper_dir, an exact
normalized-title match against the corpus short-circuits the ingest and self-heals
the kept row (claim-guarded id backfill).

Prod-consistent: temp DB built by the REAL schema-ensure functions (db.apply_schema
+ ensure_radar_schema), the network metadata/full-text fetch mocked at the same
seam the existing ingest tests use; no hand-rolled CREATE TABLE fixtures, no
network, no Telegram (the conftest stub handles the last).
"""
from __future__ import annotations

import pytest

import cortex_research.db as db
import cortex_research.index_papers as index_papers
import cortex_research.paper_ingest as paper_ingest
from cortex_research.radar_schema import ensure_radar_schema


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "readings"))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")


def _insert_paper(conn, *, paper_dir, title, arxiv_id=None, indexed_at="2026-05-21T00:00:00Z",
                  source="reader", published_at=None, source_url=None):
    with conn:
        conn.execute(
            "INSERT INTO papers (paper_dir, title, indexed_at, source, arxiv_id, "
            " published_at, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (paper_dir, title, indexed_at, source, arxiv_id, published_at, source_url),
        )


def _insert_signal(conn, *, arxiv_id, status="pending", paper_dir=None, signal_id=None):
    sid = signal_id or f"sig-{arxiv_id}"
    with conn:
        conn.execute(
            "INSERT INTO radar_signals "
            "(signal_id, source, source_ref, title, arxiv_id, importance, "
            " importance_breakdown, scoring_method, processed_status, paper_dir) "
            "VALUES (?, 'arxiv', ?, ?, ?, 4, '{}', 'rule', ?, ?)",
            (sid, f"arxiv:{arxiv_id}", f"Title {arxiv_id}", arxiv_id, status, paper_dir),
        )
    return sid


def _paper_row(conn, paper_dir):
    return conn.execute(
        "SELECT arxiv_id, published_at, source_url FROM papers WHERE paper_dir=?",
        (paper_dir,),
    ).fetchone()


# --------------------------------------------------------------------------- #
# normalize_title
# --------------------------------------------------------------------------- #
def test_normalize_title_punctuation_case_whitespace():
    n = paper_ingest.normalize_title
    # case-fold + punctuation->space + collapse whitespace + strip
    assert n("Causal-Forcing: A Study!") == "causal forcing a study"
    assert n("  MoC   (Mixture\tof\nContexts)  ") == "moc mixture of contexts"
    # two titles differing only in punctuation/case/whitespace collapse equal
    assert n("Attention Is All You Need") == n("attention—is, all  you   need!")


def test_normalize_title_cjk_passthrough():
    n = paper_ingest.normalize_title
    # CJK ideographs are alphanumeric -> kept; only ASCII punctuation flattens
    assert n("深入浅出 Transformer 模型") == "深入浅出 transformer 模型"
    assert n("《大模型》入门：第1讲") == "大模型 入门 第1讲"


def test_normalize_title_empty_and_none():
    n = paper_ingest.normalize_title
    assert n(None) == ""
    assert n("") == ""
    assert n("!!! ---") == ""  # all-punctuation -> empty, never matches a real row


# --------------------------------------------------------------------------- #
# (a) NULL-id row + same title -> no new row, id backfilled, signal closed
# --------------------------------------------------------------------------- #
def test_title_match_backfills_id_closes_signal_no_new_row(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        # a legacy reader row: full text present, arxiv_id NULL, NO published_at
        _insert_paper(conn, paper_dir="20260309-causal_forcing",
                      title="Causal Forcing: A Study", arxiv_id=None)
        sid = _insert_signal(conn, arxiv_id="2602.02214", status="pending")
    finally:
        conn.close()

    # The EXPENSIVE path must never run on a title-match hit.
    def _no(*a, **k):
        raise AssertionError("expensive ingest path must not run on a title-match hit")

    monkeypatch.setattr(paper_ingest, "fetch_full_text", _no)
    monkeypatch.setattr(paper_ingest, "_write_paper_dir", _no)
    monkeypatch.setattr(index_papers, "index_paper", _no)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "Causal-Forcing — a study!",  # same normalized title
                     "abstract": "", "published_at": "2026-02-03"},
    )

    res = paper_ingest.ingest_arxiv("2602.02214")
    assert res["already_ingested"] is True
    assert res["indexed"] is True
    assert res["full_text_source"] == "corpus"
    assert res["title_match"] is True
    assert res["id_backfilled"] is True
    assert res["paper_dir"] == "20260309-causal_forcing"   # the KEPT dir, no new one
    assert res["signals_closed"] == 1

    conn = db.connect()
    try:
        # exactly ONE papers row (no duplicate created)
        n_rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        prow = _paper_row(conn, "20260309-causal_forcing")
        srow = conn.execute(
            "SELECT processed_status, paper_dir FROM radar_signals WHERE signal_id=?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert n_rows == 1
    assert prow["arxiv_id"] == "2602.02214"               # backfilled
    assert prow["published_at"] == "2026-02-03"           # COALESCE-backfilled
    assert srow["processed_status"] == "indexed"
    assert srow["paper_dir"] == "20260309-causal_forcing"  # closed with the KEPT dir


# --------------------------------------------------------------------------- #
# (b) different-id row + same title -> title_collision, normal ingest proceeds
# --------------------------------------------------------------------------- #
def test_title_collision_with_different_id_proceeds_to_normal_ingest(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        # an existing row with the SAME normalized title but a DIFFERENT non-empty id
        _insert_paper(conn, paper_dir="20260101-twin_title",
                      title="Twin Title", arxiv_id="2401.00001", source="agent")
    finally:
        conn.close()

    # normal ingest proceeds: REAL _write_paper_dir + index_paper run (only network mocked)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "twin—title!", "abstract": "A", "published_at": "2026-05-30"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("full body text " * 200, "html", []),
    )

    res = paper_ingest.ingest_arxiv("2602.99999")  # a genuinely different id
    assert res["already_ingested"] is False         # did NOT short-circuit
    assert res.get("title_match") is None            # not a self-heal hit
    assert res["indexed"] is True

    conn = db.connect()
    try:
        # the original distinct-id row is UNTOUCHED, a SECOND row now exists
        orig = _paper_row(conn, "20260101-twin_title")
        n_rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    finally:
        conn.close()
    assert orig["arxiv_id"] == "2401.00001"          # never overwritten
    assert n_rows == 2                                # two genuinely distinct papers


# --------------------------------------------------------------------------- #
# (c) no title match -> normal ingest unchanged
# --------------------------------------------------------------------------- #
def test_no_title_match_normal_ingest_unchanged(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        _insert_paper(conn, paper_dir="20260101-unrelated",
                      title="Something Entirely Different", arxiv_id=None)
    finally:
        conn.close()

    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "A Brand New Paper", "abstract": "A",
                     "published_at": "2026-05-30"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("full body text " * 200, "html", []),
    )

    res = paper_ingest.ingest_arxiv("2605.28819")
    assert res["already_ingested"] is False
    assert res.get("title_match") is None
    assert res["indexed"] is True

    conn = db.connect()
    try:
        n_rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        # the unrelated legacy row was NOT mutated
        unrelated = _paper_row(conn, "20260101-unrelated")
    finally:
        conn.close()
    assert n_rows == 2
    assert unrelated["arxiv_id"] is None


# --------------------------------------------------------------------------- #
# (d) the existing arxiv_id dedup path is untouched (regression)
# --------------------------------------------------------------------------- #
def test_existing_arxiv_id_dedup_path_still_short_circuits(monkeypatch, tmp_path):
    """A row that ALREADY has the matching arxiv_id short-circuits via the
    PRE-EXISTING _corpus_lookup path (NOT the title gate) — result has no
    title_match marker, and the title gate is never consulted."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        _insert_paper(conn, paper_dir="20260601-helios", title="Helios",
                      arxiv_id="2603.04379", source="agent")
    finally:
        conn.close()

    # If the title gate were consulted it would raise (it scans titles) — instead the
    # arxiv_id lookup wins first. Guard the title-gate seam to prove it's not reached.
    def _guard(*a, **k):
        raise AssertionError("title gate must not run when arxiv_id dedup already hits")

    monkeypatch.setattr(paper_ingest, "_title_match_lookup", _guard)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "Helios", "abstract": "", "published_at": "2026-03-04"},
    )

    res = paper_ingest.ingest_arxiv("2603.04379")
    assert res["already_ingested"] is True
    assert res["paper_dir"] == "20260601-helios"
    assert res.get("title_match") is None  # the id path, not the title path


# --------------------------------------------------------------------------- #
# M2: a title-match self-heal must NOT fabricate a source_url. fetch_metadata has
# no source_url key for an arxiv paper, so source_url stays NULL (its schema says
# "NULL for arxiv/reader papers") — the old `or f"arxiv:{id}"` polluted it.
# --------------------------------------------------------------------------- #
def test_self_heal_does_not_fabricate_source_url(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        _insert_paper(conn, paper_dir="20260309-no_url",
                      title="A Paper Without A Source Url", arxiv_id=None,
                      source="reader", source_url=None)
    finally:
        conn.close()

    def _no(*a, **k):
        raise AssertionError("expensive ingest path must not run on a title-match hit")

    monkeypatch.setattr(paper_ingest, "fetch_full_text", _no)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "A Paper Without A Source Url",  # no source_url key
                     "abstract": "", "published_at": "2026-02-03"},
    )

    res = paper_ingest.ingest_arxiv("2602.02214")
    assert res["title_match"] is True
    assert res["id_backfilled"] is True

    conn = db.connect()
    try:
        prow = _paper_row(conn, "20260309-no_url")
    finally:
        conn.close()
    assert prow["arxiv_id"] == "2602.02214"   # id backfilled
    assert prow["source_url"] is None         # NEVER an invented 'arxiv:<id>'


# --------------------------------------------------------------------------- #
# B1: a radar COLON STUB (paper_dir='arxiv:<id>', NULL id, abstract-only) must NOT
# be matched by the title gate — else ingest_arxiv returns already_ingested with
# the stub dir (no full text fetched) and backfills the id onto the stub so
# _corpus_lookup hits it forever. The gate must skip stubs and let normal ingest
# (real full-text fetch) proceed. Reproduced live on arxiv:2605.30201.
# --------------------------------------------------------------------------- #
def test_title_gate_excludes_radar_colon_stub_normal_ingest_proceeds(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        # a radar colon stub: abstract-only placeholder, paper_dir='arxiv:<id>',
        # arxiv_id NULL, title = the official arxiv title (same the metadata fetch
        # will return). Shaped exactly like the ~78 live stubs.
        _insert_paper(conn, paper_dir="arxiv:2605.30201",
                      title="Drifting Towards Better Representations", arxiv_id=None,
                      source="radar")
        sid = _insert_signal(conn, arxiv_id="2605.30201", status="pending")
    finally:
        conn.close()

    # The title gate helper must return None for a stub-only corpus (B1).
    assert paper_ingest._title_match_lookup("Drifting Towards Better Representations") is None

    # End-to-end: normal ingest must run (real full-text fetch), NOT short-circuit
    # onto the stub dir. Only the network is mocked.
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "Drifting Towards Better Representations",
                     "abstract": "", "published_at": "2026-05-30"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("full body text " * 200, "html", []),
    )

    res = paper_ingest.ingest_arxiv("2605.30201")
    assert res["already_ingested"] is False          # did NOT short-circuit on the stub
    assert res.get("title_match") is None             # the stub was not a hit
    assert res["full_text_source"] == "html"          # real full text fetched
    assert res["paper_dir"] != "arxiv:2605.30201"     # a NEW full-text dir, not the stub
    assert res["indexed"] is True

    conn = db.connect()
    try:
        # the stub row is UNTOUCHED — its id never backfilled (that would poison
        # _corpus_lookup forever)
        stub = _paper_row(conn, "arxiv:2605.30201")
        n_rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    finally:
        conn.close()
    assert stub["arxiv_id"] is None                   # stub never claimed the id
    assert n_rows == 2                                 # stub + new full-text row


# --------------------------------------------------------------------------- #
# Gate-level empty-title guard: a papers row whose title is empty/punctuation-only
# + a fetched paper with an empty title must NOT match (the "" == "" trap). Asserted
# at the GATE (ingest_arxiv), not just the helper, so removing the empty-title guard
# is caught by this suite.
# --------------------------------------------------------------------------- #
def test_gate_empty_title_does_not_match_punctuation_only_row(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        # a row whose title normalizes to "" (punctuation-only)
        _insert_paper(conn, paper_dir="20260101-punct", title="!!! --- ???",
                      arxiv_id=None, source="reader")
    finally:
        conn.close()

    # the fetched paper ALSO has an empty/punctuation-only title -> normalizes to ""
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "   ...   ", "abstract": "", "published_at": "2026-05-30"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("full body text " * 200, "html", []),
    )

    # helper level
    assert paper_ingest._title_match_lookup("   ...   ") is None

    # gate level: must NOT self-heal the punctuation-only row, normal ingest proceeds
    res = paper_ingest.ingest_arxiv("2605.28888")
    assert res["already_ingested"] is False
    assert res.get("title_match") is None
    assert res["indexed"] is True

    conn = db.connect()
    try:
        punct = _paper_row(conn, "20260101-punct")
        n_rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    finally:
        conn.close()
    assert punct["arxiv_id"] is None   # the empty-title row was NOT backfilled
    assert n_rows == 2                  # a new distinct row was created


# --------------------------------------------------------------------------- #
# title_collision marker is in the RESULT dict (spec §2.4), arxiv path
# --------------------------------------------------------------------------- #
def test_title_collision_marker_in_result_dict_arxiv_path(monkeypatch, tmp_path):
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        _insert_paper(conn, paper_dir="20260101-twin", title="Twin Title",
                      arxiv_id="2401.00001", source="agent")
    finally:
        conn.close()
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "twin—title!", "abstract": "A", "published_at": "2026-05-30"},
    )
    monkeypatch.setattr(
        paper_ingest, "fetch_full_text",
        lambda aid, ocr_tmp=None, strict=False: ("full body text " * 200, "html", []),
    )

    res = paper_ingest.ingest_arxiv("2602.99999")
    assert res["title_collision"] is True   # spec §2.4 marker, not just a log line
    assert res["already_ingested"] is False  # fell through to normal ingest

    # a no-collision normal ingest carries the marker as False (key always present)
    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "A Brand New Distinct Paper", "abstract": "A",
                     "published_at": "2026-05-30"},
    )
    res2 = paper_ingest.ingest_arxiv("2605.11111")
    assert res2["title_collision"] is False


# --------------------------------------------------------------------------- #
# claim-guard: id already set by a concurrent writer between scan and write
# --------------------------------------------------------------------------- #
def test_title_match_claim_guard_does_not_overwrite_concurrent_id(monkeypatch, tmp_path):
    """If the matched row's arxiv_id gets set by another writer between the title
    scan and the backfill UPDATE, _backfill_paper_id's WHERE-guard makes the write
    a no-op (id_backfilled False) and never overwrites the concurrent id."""
    _isolate_db(monkeypatch, tmp_path)
    conn = db.connect()
    try:
        db.apply_schema(conn)
        ensure_radar_schema(conn)
        _insert_paper(conn, paper_dir="20260309-moc", title="MoC", arxiv_id=None)
    finally:
        conn.close()

    monkeypatch.setattr(
        paper_ingest, "fetch_metadata",
        lambda aid: {"title": "moc", "abstract": "", "published_at": "2025-08-30"},
    )

    real_lookup = paper_ingest._title_match_lookup

    def _lookup_then_race(title):
        hit = real_lookup(title)
        # Simulate a concurrent writer filling the id AFTER our scan saw it empty.
        c = db.connect()
        try:
            with c:
                c.execute("UPDATE papers SET arxiv_id='9999.88888' "
                          "WHERE paper_dir='20260309-moc'")
        finally:
            c.close()
        return hit  # still reports the (now-stale) empty-id hit

    monkeypatch.setattr(paper_ingest, "_title_match_lookup", _lookup_then_race)

    res = paper_ingest.ingest_arxiv("2508.21058")
    assert res["title_match"] is True
    assert res["id_backfilled"] is False  # claim lost — never overwrote 9999.88888

    conn = db.connect()
    try:
        prow = _paper_row(conn, "20260309-moc")
    finally:
        conn.close()
    assert prow["arxiv_id"] == "9999.88888"  # the concurrent id stands
