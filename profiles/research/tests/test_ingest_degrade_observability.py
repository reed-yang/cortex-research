"""C2: the OCR->PyMuPDF degrade must be OBSERVABLE (operator-observability rule).

_ocr_pdf_via_skill returns None on ~9 distinct failure modes; each must emit a
clear logger line distinguishing the mode, and fetch_full_text must log when it
falls from ocr -> pdf (a degraded, plain-text ingest). Success must NOT spam.
"""
import json
import logging
import subprocess
from pathlib import Path

import pytest

from cortex_research import paper_ingest


class _FakePopen:
    def __init__(self, argv, out, rc, *, timeout=False):
        self.args = argv; self._out = out; self.returncode = rc
        self.pid = 999999; self._timeout = timeout
    def communicate(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return (self._out, "stderr summary")
    def wait(self):
        return self.returncode


def _fake_skill(out_tmp: Path, *, rc: int = 0, stdout: str | None = None,
                dirname: str = "20260601-Fake_Paper", timeout: bool = False,
                short_body: bool = False):
    def _popen(argv, **kw):
        d = out_tmp / dirname
        (d / "assets").mkdir(parents=True, exist_ok=True)
        body = "# Fake\n\nx" if short_body else ("# Fake\n\n" + "body text " * 200)
        (d / "full_text.md").write_text(body, encoding="utf-8")
        payload = stdout if stdout is not None else json.dumps({
            "status": "success", "markdown_path": str(d / "full_text.md"),
            "paper_dir": str(d)})
        return _FakePopen(argv, payload, rc, timeout=timeout)
    return _popen


@pytest.fixture(autouse=True)
def _skill_paths(tmp_path, monkeypatch):
    root = tmp_path / "paper-ingestion"
    skill = root / "scripts" / "ingest_paper.py"
    skill.parent.mkdir(parents=True); skill.write_text("# stub")
    uv = tmp_path / "uv"; uv.write_text("#stub"); uv.chmod(0o755)
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", skill)
    monkeypatch.setattr(paper_ingest, "_UV_BIN", str(uv))
    # Pin the engine chain to a single engine (glm-ocr) + its creds so each
    # failure mode is exercised against one engine with a clean log (the chain's
    # cred-skip / fallback warnings are tested in test_ocr_engine_chain.py).
    monkeypatch.setattr(paper_ingest, "_OCR_ENGINE_CHAIN", ["glm-ocr"])
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: type("R", (), {"content": b"%PDF-" + b"x" * 2000})())


def test_log_on_missing_skill(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", tmp_path / "nope.py")
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        assert paper_ingest._ocr_pdf_via_skill("2606.03982", out) is None
    assert any("2606.03982" in r.message for r in caplog.records)
    assert any("skill" in r.message.lower() or "uv" in r.message.lower()
               for r in caplog.records)


def test_log_on_nonzero_exit(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, rc=1))
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        assert paper_ingest._ocr_pdf_via_skill("2606.03982", out) is None
    assert any("exit" in r.message.lower() for r in caplog.records)


def test_log_on_timeout(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(paper_ingest.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, timeout=True))
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        assert paper_ingest._ocr_pdf_via_skill("2606.03982", out) is None
    assert any("timeout" in r.message.lower() or "timed out" in r.message.lower()
               for r in caplog.records)


def test_log_on_bad_json(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _fake_skill(out, stdout="not json"))
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        assert paper_ingest._ocr_pdf_via_skill("2606.03982", out) is None
    assert any("json" in r.message.lower() or "status" in r.message.lower()
               for r in caplog.records)


def test_log_on_empty_result(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _fake_skill(out, short_body=True))
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        assert paper_ingest._ocr_pdf_via_skill("2606.03982", out) is None
    assert any("short" in r.message.lower() or "empty" in r.message.lower()
               for r in caplog.records)


def test_no_log_on_success(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"; out.mkdir()
    # A real OCR success means GLM creds WERE present — set them so the pre-emptive
    # no-key warning (a genuine degrade signal in prod) doesn't fire here.
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out))
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        res = paper_ingest._ocr_pdf_via_skill("2606.03982", out)
    assert res is not None
    # No WARNING/ERROR spam on the happy path.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_fetch_full_text_logs_ocr_to_pdf_degrade(tmp_path, monkeypatch, caplog):
    """When HTML 404s AND OCR returns None, fetch_full_text falls to the plain
    PyMuPDF dump — that degrade must be logged."""
    # HTML leg -> 404 (no usable html)
    class _Resp:
        status_code = 404; text = ""; url = "https://arxiv.org/abs/x"
        content = b""
    monkeypatch.setattr(paper_ingest.httpx, "get", lambda *a, **k: _Resp())
    # OCR returns None (degrade)
    monkeypatch.setattr(paper_ingest, "_ocr_pdf_via_skill", lambda aid, td: None)
    # PDF text leg returns a usable body
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: type("R", (), {"content": b"%PDF"})())
    monkeypatch.setattr(paper_ingest, "_pdf_to_text", lambda b: "plain " * 500)
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        md, src, imgs = paper_ingest.fetch_full_text("2606.03982", ocr_tmp=tmp_path)
    assert src == "pdf"
    assert any("degrad" in r.message.lower() or "plain" in r.message.lower()
               or ("ocr" in r.message.lower() and "pdf" in r.message.lower())
               for r in caplog.records)
