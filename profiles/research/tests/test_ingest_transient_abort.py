"""C3: transient-429 abort — don't bake a plaintext body during a throttle storm.

When the OCR PDF pre-download fails due to a PERSISTENT 429 / rate-limit /
timeout (vs a genuine 404), _ocr_pdf_via_skill raises TransientIngestError
rather than falling through to the slow whole-PDF-URL OCR mode (which itself
exceeds 300s under throttle -> None -> _pdf_to_text plaintext bake). The
ingest/heal callers catch it and do NOT clobber/write a plaintext body; they
surface a 'transient — retry later' status. A GENUINE permanent no-HTML + a real
text-only PDF still falls to _pdf_to_text as before (back-compat).
"""
from pathlib import Path

import httpx
import pytest

from cortex_research import paper_ingest


def _raise_429(*a, **k):
    req = httpx.Request("GET", "https://arxiv.org/pdf/x")
    resp = httpx.Response(429, request=req)
    raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)


def _raise_timeout(*a, **k):
    raise httpx.ReadTimeout("timed out")


def _raise_404(*a, **k):
    req = httpx.Request("GET", "https://arxiv.org/pdf/x")
    resp = httpx.Response(404, request=req)
    raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)


@pytest.fixture(autouse=True)
def _skill_paths(tmp_path, monkeypatch):
    """Real skill+uv files so the existence gate passes; GLM keys present so the
    pre-emptive no-key warning is irrelevant to these transient/permanent tests."""
    root = tmp_path / "paper-ingestion"
    skill = root / "scripts" / "ingest_paper.py"
    skill.parent.mkdir(parents=True); skill.write_text("# stub")
    uv = tmp_path / "uv"; uv.write_text("#stub"); uv.chmod(0o755)
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", skill)
    monkeypatch.setattr(paper_ingest, "_UV_BIN", str(uv))
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")


def test_transient_error_class_exists():
    assert issubclass(paper_ingest.TransientIngestError, Exception)


def test_ocr_predownload_persistent_429_raises_transient(tmp_path, monkeypatch):
    """A persistent 429 on the OCR PDF pre-download -> TransientIngestError, and
    the skill subprocess is NEVER spawned (we abort before baking anything)."""
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest, "_get_with_retry", _raise_429)

    def _never(*a, **k):
        raise AssertionError("must not spawn the OCR skill during a 429 storm")
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _never)
    with pytest.raises(paper_ingest.TransientIngestError):
        paper_ingest._ocr_pdf_via_skill("2606.03982", out)


def test_ocr_predownload_persistent_timeout_raises_transient(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest, "_get_with_retry", _raise_timeout)
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn")))
    with pytest.raises(paper_ingest.TransientIngestError):
        paper_ingest._ocr_pdf_via_skill("2606.03982", out)


def test_fetch_full_text_transient_propagates_not_baked(tmp_path, monkeypatch):
    """fetch_full_text must let TransientIngestError propagate from the OCR leg —
    it must NOT fall through to _pdf_to_text and bake a plaintext body."""
    # HTML leg 404 (no usable html)
    class _Resp:
        status_code = 404; text = ""; url = "https://arxiv.org/abs/x"
    monkeypatch.setattr(paper_ingest.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(paper_ingest, "_ocr_pdf_via_skill",
                        lambda aid, td: (_ for _ in ()).throw(
                            paper_ingest.TransientIngestError("429 storm")))

    def _no_pdf_text(b):
        raise AssertionError("plaintext PDF dump must NOT run on a transient abort")
    monkeypatch.setattr(paper_ingest, "_pdf_to_text", _no_pdf_text)
    with pytest.raises(paper_ingest.TransientIngestError):
        paper_ingest.fetch_full_text("2606.03982", ocr_tmp=tmp_path)


def test_genuine_permanent_no_html_still_falls_to_pdf_text(tmp_path, monkeypatch):
    """Back-compat: a real no-HTML paper whose OCR genuinely returns None (not a
    transient) still falls to the PyMuPDF text dump (source='pdf')."""
    class _Resp:
        status_code = 404; text = ""; url = "https://arxiv.org/abs/x"
    monkeypatch.setattr(paper_ingest.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(paper_ingest, "_ocr_pdf_via_skill", lambda aid, td: None)
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: type("R", (), {"content": b"%PDF"})())
    monkeypatch.setattr(paper_ingest, "_pdf_to_text", lambda b: "plain text body " * 400)
    md, src, imgs = paper_ingest.fetch_full_text("2606.03982", ocr_tmp=tmp_path)
    assert src == "pdf"
    assert md.startswith("plain text body")


def test_permanent_404_predownload_falls_through_to_url_mode(tmp_path, monkeypatch):
    """A genuine permanent 404 on the local PDF pre-download is NOT transient: the
    skill is still invoked with the remote URL (legacy fall-through preserved)."""
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest, "_get_with_retry", _raise_404)
    captured = {}

    class _P:
        pid = 1; returncode = 1
        def __init__(self, argv, **kw): captured["argv"] = argv
        def communicate(self, timeout=None): return ("", "")
        def wait(self): return 1
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _P)
    # returncode 1 -> None (degrade), but importantly NO TransientIngestError and
    # the skill WAS invoked with the remote URL target.
    assert paper_ingest._ocr_pdf_via_skill("2606.03982", out) is None
    # argv = [uv, run, <skill>, <target>, ...] — target is the remote URL (404
    # pre-download means the local path was never produced; legacy URL fall-through)
    assert captured["argv"][3] == paper_ingest._PDF_URL.format(id="2606.03982")


def test_ingest_arxiv_transient_does_not_write_plaintext(research_db, tmp_path, monkeypatch):
    """ingest_arxiv on a transient OCR failure: NO papers row written, result
    signals a retryable transient (not a successful plaintext ingest)."""
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    monkeypatch.setattr(paper_ingest, "fetch_metadata",
                        lambda aid: {"title": "T", "abstract": "a", "published_at": ""})
    monkeypatch.setattr(paper_ingest, "fetch_full_text",
                        lambda aid, *, ocr_tmp=None, strict=False: (_ for _ in ()).throw(
                            paper_ingest.TransientIngestError("429 storm")))
    res = paper_ingest.ingest_arxiv("2606.03982", source="agent")
    assert res["ok"] is False
    assert res.get("transient") is True
    assert res.get("indexed") is False
    import sqlite3
    conn = sqlite3.connect(research_db)
    row = conn.execute("SELECT 1 FROM papers WHERE arxiv_id='2606.03982'").fetchone()
    conn.close()
    assert row is None  # nothing baked

