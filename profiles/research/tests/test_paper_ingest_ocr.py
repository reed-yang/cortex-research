"""_ocr_pdf_via_skill: shell out to the GLM-OCR paper-ingestion skill, adopt its
markdown + locally-cropped figure files. The skill subprocess is fully mocked —
no real `uv run`, no GLM key, no network."""
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
                stdout: str | None = None, dirname: str = "20260601-Fake_Paper",
                timeout: bool = False):
    """Return a subprocess.Popen replacement that emulates the skill writing its
    output tree + printing one JSON object to stdout."""
    def _popen(argv, **kw):
        if captured is not None:
            captured["argv"] = argv
            captured["cwd"] = kw.get("cwd")
            captured["env"] = kw.get("env")
        d = out_tmp / dirname
        (d / "assets").mkdir(parents=True, exist_ok=True)
        (d / "assets" / "image_001.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE1")
        (d / "assets" / "image_002.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE2")
        (d / "full_text.md").write_text(
            '---\ntitle: "Fake"\ndate_ingested: 2026-06-01\n---\n\n'
            '# Fake Paper\n\n'
            '![Figure 1](./assets/image_001.png)\n\n'
            '## Method\n\n' + ('$\\alpha$ body text ' * 60)
            + '\n\n![Figure 2](./assets/image_002.png)\n',
            encoding="utf-8")
        payload = stdout if stdout is not None else json.dumps({
            "status": "success",
            "markdown_path": str(d / "full_text.md"),
            "engine_used": "glm-ocr", "title": "Fake",
            "date": "2026-06-01", "paper_dir": str(d)})
        return _FakePopen(argv, payload, rc, timeout=timeout)
    return _popen


@pytest.fixture(autouse=True)
def _skill_paths(tmp_path, monkeypatch):
    """Point the skill + uv at real existing files so the existence gate passes
    hermetically (CI may lack /opt/homebrew/bin/uv). The skill stub lives under a
    scripts/ subdir mirroring the real layout so .parent.parent == the skill root
    (which `uv run` must use as cwd to pick the skill's own pyproject/uv.lock)."""
    root = tmp_path / "paper-ingestion"
    skill = root / "scripts" / "ingest_paper.py"
    skill.parent.mkdir(parents=True); skill.write_text("# stub")
    uv = tmp_path / "uv"; uv.write_text("#stub"); uv.chmod(0o755)
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", skill)
    monkeypatch.setattr(paper_ingest, "_UV_BIN", str(uv))
    # This file exercises the glm-ocr backend's parse/timeout/pre-download path;
    # pin the engine chain to glm-ocr (single engine) + its creds so the mock runs
    # and logs are clean. The multi-engine chain (deepseek primary -> glm fallback,
    # cred-skip, per-engine subdir) is covered in test_ocr_engine_chain.py.
    monkeypatch.setattr(paper_ingest, "_OCR_ENGINE_CHAIN", ["glm-ocr"])
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")
    # _ocr_pdf_via_skill pre-downloads the PDF (to force the skill's per-page
    # mode); mock that fetch so tests never hit the network.
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: type("R", (), {"content": b"%PDF-" + b"x" * 2000})())


def test_ocr_happy_path(tmp_path, monkeypatch):
    captured: dict = {}
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, captured=captured))
    res = paper_ingest._ocr_pdf_via_skill("2603.04379", out)
    assert res is not None
    md, pairs = res
    # frontmatter + leading "# title" both stripped
    assert "---\ntitle:" not in md
    assert not md.lstrip().startswith("# Fake Paper")
    # refs normalized ./assets/ -> assets/
    assert "](assets/image_001.png)" in md and "./assets/" not in md
    # LaTeX preserved
    assert r"$\alpha$" in md
    # both figure files adopted, rel matches md
    rels = sorted(rel for _src, rel in pairs)
    assert rels == ["assets/image_001.png", "assets/image_002.png"]
    for src, rel in pairs:
        assert Path(src).is_file() and rel in md
    # argv shape
    argv = captured["argv"]
    assert argv[0] == paper_ingest._UV_BIN and argv[1] == "run"
    # a successful pre-download means the skill gets a LOCAL pdf path (forces the
    # robust per-page mode), not the remote URL
    assert argv[3] == str(out / "2603.04379.pdf")
    assert "--engine" in argv and argv[argv.index("--engine") + 1] == "glm-ocr"
    # each engine writes to its OWN out_tmp/<engine> subdir (avoids the skill's
    # duplicate-title collision when a second engine is tried)
    assert "--output-dir" in argv and argv[argv.index("--output-dir") + 1] == str(out / "glm-ocr")
    # MUST run with cwd = the skill root so `uv run` resolves the skill's OWN
    # pyproject/uv.lock, not the cortex workspace (whose extras don't resolve).
    assert captured["cwd"] == str(paper_ingest._SKILL_INGEST.parent.parent)
    # large-paper GLM rate-limit knob is forwarded to the skill (default 3)
    assert captured["env"].get("GLM_OCR_MAX_WORKERS") == "3"


def test_ocr_filters_image_past_truncation(tmp_path, monkeypatch):
    """An image whose ref is cut by the _MAX_FULLTEXT_CHARS cap is dropped from
    the returned pairs (no orphan copy)."""
    monkeypatch.setattr(paper_ingest, "_MAX_FULLTEXT_CHARS", 1000)  # keeps fig1, cuts fig2
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out))
    md, pairs = paper_ingest._ocr_pdf_via_skill("2603.04379", out)
    assert len(md) <= 1000
    rels = [rel for _s, rel in pairs]
    assert "assets/image_002.png" not in rels  # truncated away


def test_ocr_returns_none_on_nonzero_exit(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, rc=1))
    assert paper_ingest._ocr_pdf_via_skill("2603.04379", out) is None


def test_ocr_returns_none_on_error_status(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _fake_skill(out, stdout='{"status":"error","message":"100 pages"}'))
    assert paper_ingest._ocr_pdf_via_skill("2603.04379", out) is None


def test_ocr_returns_none_on_non_json(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, stdout="not json at all"))
    assert paper_ingest._ocr_pdf_via_skill("2603.04379", out) is None


def test_ocr_timeout_budget_covers_url_mode():
    """The OCR subprocess budget must cover the skill's URL mode (up to 3x~300s
    server-side per attempt); a live EGOSTREAM run succeeded at 605s."""
    assert paper_ingest._OCR_TIMEOUT_SECONDS >= 900


def test_ocr_returns_none_on_timeout_and_kills_group(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    killed = {}
    monkeypatch.setattr(paper_ingest.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(paper_ingest.os, "killpg", lambda pgid, sig: killed.setdefault("pgid", pgid))
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _fake_skill(out, timeout=True))
    assert paper_ingest._ocr_pdf_via_skill("2603.04379", out) is None
    assert killed.get("pgid") == 999999   # whole process group SIGKILL'd


def test_strip_frontmatter_and_div_wrapped_h1():
    """REGRESSION (review MED): real GLM-OCR wraps the title in a centered div;
    both the div and the H1 must be stripped so the title isn't duplicated when
    _write_paper_dir/heal_one re-prepend '# {title}'."""
    body = ('---\ntitle: "Direct Preference Optimization"\ndate_ingested: 2026-02-20\n---\n\n'
            '<div align="center">\n\n# Direct Preference Optimization: Your LM is Secretly a RM\n\n'
            '</div>\n\n## 1 Introduction\n\nReal body text here.\n')
    out = paper_ingest._strip_frontmatter_and_h1(body)
    assert "---\ntitle:" not in out
    assert "# Direct Preference Optimization" not in out   # H1 stripped
    assert "<div" not in out and "</div>" not in out       # wrapper gone
    assert out.lstrip().startswith("## 1 Introduction")


def test_strip_div_h1_behind_leading_figure():
    """Live Helios format: a title-page figure precedes the div-wrapped title.
    The H1 + its <div>/</div> are removed but the leading figure is kept."""
    body = ('---\ntitle: "Helios"\n---\n\n'
            '![](assets/image_001.png)\n\n'
            '<div align="center">\n\n# Helios: Real Real-Time Long Video Generation Model\n\n'
            '</div>\n\nShenghai Yuan body text here and there.\n')
    out = paper_ingest._strip_frontmatter_and_h1(body)
    assert out.lstrip().startswith("![](assets/image_001.png)")  # leading figure kept
    assert "# Helios" not in out                                  # title H1 removed
    assert "<div" not in out and "</div>" not in out              # wrapper gone
    assert "Shenghai Yuan" in out


def test_ocr_returns_none_when_skill_missing(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", tmp_path / "does_not_exist.py")

    def _never(*a, **k):
        raise AssertionError("must not spawn when skill is missing")
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _never)
    assert paper_ingest._ocr_pdf_via_skill("2603.04379", out) is None
