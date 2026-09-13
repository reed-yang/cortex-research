"""D1: `_ocr_pdf(source, out_tmp)` — the arxiv-decoupled OCR core. It runs the
glm-ocr skill on ANY local PDF path OR direct PDF URL and parses the SAME output
shape, regardless of whether the source is a local path or a URL. The skill
subprocess is fully mocked — no real `uv run`, no GLM key, no network."""
import json
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


def _fake_skill(out_tmp: Path, *, captured: dict | None = None, rc: int = 0,
                stdout: str | None = None, dirname: str = "20260603-Joy_Paper"):
    """subprocess.Popen replacement: emulate the skill writing its output tree +
    printing one JSON object to stdout (mirrors test_paper_ingest_ocr's fake)."""
    def _popen(argv, **kw):
        if captured is not None:
            captured["argv"] = argv
            captured["cwd"] = kw.get("cwd")
            captured["env"] = kw.get("env")
        d = out_tmp / dirname
        (d / "assets").mkdir(parents=True, exist_ok=True)
        (d / "assets" / "image_001.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE1")
        (d / "full_text.md").write_text(
            '---\ntitle: "Joy"\n---\n\n'
            '# Joy Paper\n\n'
            '![Figure 1](./assets/image_001.png)\n\n'
            '## Method\n\n' + ('$\\beta$ body text ' * 60),
            encoding="utf-8")
        payload = stdout if stdout is not None else json.dumps({
            "status": "success",
            "markdown_path": str(d / "full_text.md"),
            "engine_used": "glm-ocr", "title": "Joy",
            "paper_dir": str(d)})
        return _FakePopen(argv, payload, rc)
    return _popen


@pytest.fixture(autouse=True)
def _skill_paths(tmp_path, monkeypatch):
    """Point the skill + uv at real existing files so the existence gate passes."""
    root = tmp_path / "paper-ingestion"
    skill = root / "scripts" / "ingest_paper.py"
    skill.parent.mkdir(parents=True); skill.write_text("# stub")
    uv = tmp_path / "uv"; uv.write_text("#stub"); uv.chmod(0o755)
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", skill)
    monkeypatch.setattr(paper_ingest, "_UV_BIN", str(uv))
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")


def _assert_parsed(md, pairs):
    # frontmatter + leading H1 stripped; ./assets/ normalized; LaTeX kept
    assert "---\ntitle:" not in md
    assert not md.lstrip().startswith("# Joy Paper")
    assert "](assets/image_001.png)" in md and "./assets/" not in md
    assert r"$\beta$" in md
    rels = sorted(rel for _s, rel in pairs)
    assert rels == ["assets/image_001.png"]
    for src, rel in pairs:
        assert Path(src).is_file() and rel in md


def test_ocr_pdf_on_local_path(tmp_path, monkeypatch):
    """_ocr_pdf passes a LOCAL pdf path through to the skill verbatim (no arxiv
    pre-download in the generic core) and parses its output."""
    out = tmp_path / "out"; out.mkdir()
    local_pdf = tmp_path / "joyai.pdf"; local_pdf.write_bytes(b"%PDF-" + b"x" * 3000)
    captured: dict = {}
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _fake_skill(out, captured=captured))
    res = paper_ingest._ocr_pdf(str(local_pdf), out, label="joyai")
    assert res is not None
    md, pairs = res
    _assert_parsed(md, pairs)
    # the skill was invoked with the LOCAL path as its source arg (per-page mode)
    assert captured["argv"][3] == str(local_pdf)
    # R2 invariants preserved: skill-root cwd + the GLM worker cap
    assert captured["cwd"] == str(paper_ingest._SKILL_INGEST.parent.parent)
    assert captured["env"].get("GLM_OCR_MAX_WORKERS") == "3"


def test_ocr_pdf_on_url(tmp_path, monkeypatch):
    """_ocr_pdf passes a PDF URL straight through to the skill (whole-PDF-URL
    mode) and parses the IDENTICAL output shape — same parse as the local path."""
    out = tmp_path / "out"; out.mkdir()
    url = "https://raw.githubusercontent.com/jd-opensource/JoyAI-Echo/main/paper.pdf"
    captured: dict = {}
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _fake_skill(out, captured=captured))
    res = paper_ingest._ocr_pdf(url, out, label=url)
    assert res is not None
    md, pairs = res
    _assert_parsed(md, pairs)
    # the URL is passed through verbatim as the skill's source arg
    assert captured["argv"][3] == url


def test_ocr_pdf_returns_none_on_nonzero_exit(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, rc=1))
    assert paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x") is None


def test_ocr_pdf_returns_none_when_skill_missing(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", tmp_path / "missing.py")

    def _never(*a, **k):
        raise AssertionError("must not spawn when skill is missing")
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _never)
    assert paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x") is None
