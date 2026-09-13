"""`_ocr_pdf` engine CHAIN: deepseek-ocr (primary) -> glm-ocr (fallback). Each
engine runs only if ITS creds are present, writes to its OWN out_tmp/<engine>
subdir, and the first to yield a good body wins. The skill subprocess is fully
mocked (engine-aware) — no real `uv run`, no keys, no network."""
import json
import logging
import subprocess
from pathlib import Path

import pytest

from cortex_research import paper_ingest


class _FakePopen:
    def __init__(self, argv, out, rc):
        self.args = argv; self._out = out; self.returncode = rc; self.pid = 999999
    def communicate(self, timeout=None):
        return (self._out, "stderr summary")
    def wait(self):
        return self.returncode


def _engine_aware_skill(fail_engines=(), calls=None):
    """subprocess.Popen replacement that succeeds or fails based on the --engine
    arg. `calls` (if given) collects (engine, output_dir) per invocation."""
    def _popen(argv, **kw):
        engine = argv[argv.index("--engine") + 1]
        out_dir = Path(argv[argv.index("--output-dir") + 1])
        if calls is not None:
            calls.append((engine, str(out_dir)))
        if engine in fail_engines:
            return _FakePopen(argv, "", 1)  # nonzero exit -> None
        d = out_dir / f"20260604-{engine}_Paper"
        (d / "assets").mkdir(parents=True, exist_ok=True)
        (d / "assets" / "image_001.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        # Put the engine marker in the BODY (the H1 title is stripped by
        # _strip_frontmatter_and_h1, so it can't identify the producing engine).
        (d / "full_text.md").write_text(
            f"# Paper\n\n![Figure 1](./assets/image_001.png)\n\n"
            f"## Method\n\n[ENGINE:{engine}] " + ("body text " * 80), encoding="utf-8")
        payload = json.dumps({"status": "success", "engine_used": engine,
                              "markdown_path": str(d / "full_text.md"),
                              "paper_dir": str(d)})
        return _FakePopen(argv, payload, 0)
    return _popen


@pytest.fixture(autouse=True)
def _skill_paths(tmp_path, monkeypatch):
    root = tmp_path / "paper-ingestion"
    skill = root / "scripts" / "ingest_paper.py"
    skill.parent.mkdir(parents=True); skill.write_text("# stub")
    uv = tmp_path / "uv"; uv.write_text("#stub"); uv.chmod(0o755)
    monkeypatch.setattr(paper_ingest, "_SKILL_INGEST", skill)
    monkeypatch.setattr(paper_ingest, "_UV_BIN", str(uv))
    # default chain (matches the prod default), restorable per-test
    monkeypatch.setattr(paper_ingest, "_OCR_ENGINE_CHAIN", ["deepseek-ocr", "glm-ocr"])


def _both_creds(monkeypatch):
    monkeypatch.setenv("NOVITA_API_KEY", "nv")
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")


def test_deepseek_is_primary_when_it_succeeds(tmp_path, monkeypatch):
    """With both creds present, deepseek-ocr runs FIRST and wins; glm is never tried."""
    _both_creds(monkeypatch)
    out = tmp_path / "out"; out.mkdir()
    calls = []
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _engine_aware_skill(calls=calls))
    res = paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x")
    assert res is not None
    md, pairs = res
    assert "[ENGINE:deepseek-ocr]" in md            # the deepseek body, not glm's
    assert [c[0] for c in calls] == ["deepseek-ocr"]   # glm NOT tried
    # deepseek wrote to its OWN subdir
    assert calls[0][1] == str(out / "deepseek-ocr")


def test_falls_back_to_glm_when_deepseek_fails(tmp_path, monkeypatch):
    """deepseek-ocr nonzero-exit -> glm-ocr runs and its body is returned."""
    _both_creds(monkeypatch)
    out = tmp_path / "out"; out.mkdir()
    calls = []
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _engine_aware_skill(fail_engines=("deepseek-ocr",), calls=calls))
    res = paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x")
    assert res is not None
    md, _pairs = res
    assert "[ENGINE:glm-ocr]" in md
    assert [c[0] for c in calls] == ["deepseek-ocr", "glm-ocr"]  # both tried, in order
    # each engine used its own subdir (no duplicate-title collision)
    assert calls[0][1] == str(out / "deepseek-ocr")
    assert calls[1][1] == str(out / "glm-ocr")


def test_returns_none_when_all_engines_fail(tmp_path, monkeypatch):
    _both_creds(monkeypatch)
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(paper_ingest.subprocess, "Popen",
                        _engine_aware_skill(fail_engines=("deepseek-ocr", "glm-ocr")))
    assert paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x") is None


def test_skips_deepseek_without_novita_key_then_runs_glm(tmp_path, monkeypatch):
    """No NOVITA_API_KEY -> deepseek SKIPPED (not spawned), glm runs."""
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")
    out = tmp_path / "out"; out.mkdir()
    calls = []
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _engine_aware_skill(calls=calls))
    res = paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x")
    assert res is not None
    assert [c[0] for c in calls] == ["glm-ocr"]  # deepseek never spawned


def test_returns_none_and_warns_when_no_engine_has_creds(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_ID", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    out = tmp_path / "out"; out.mkdir()

    def _never(*a, **k):
        raise AssertionError("must not spawn when no engine has creds")
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _never)
    with caplog.at_level(logging.WARNING, logger="cortex_research.paper_ingest"):
        assert paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x") is None
    assert any("no OCR engine had usable creds" in r.message for r in caplog.records)


def test_unresolved_placeholder_cred_is_treated_as_absent(tmp_path, monkeypatch):
    """Hermes interpolates an UNSET ${NOVITA_API_KEY} to the LITERAL string
    '${NOVITA_API_KEY}' (truthy junk). _env_cred must treat it as absent so
    deepseek is skipped (not spawned into a guaranteed-401 run) until the secret
    is provisioned."""
    monkeypatch.setenv("NOVITA_API_KEY", "${NOVITA_API_KEY}")   # unresolved literal
    monkeypatch.setenv("GLM_API_ID", "id"); monkeypatch.setenv("GLM_API_KEY", "key")
    assert paper_ingest._env_cred("NOVITA_API_KEY") == ""
    assert paper_ingest._ocr_engine_creds_ok("deepseek-ocr") is False
    out = tmp_path / "out"; out.mkdir()
    calls = []
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _engine_aware_skill(calls=calls))
    res = paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x")
    assert res is not None
    assert [c[0] for c in calls] == ["glm-ocr"]  # deepseek NOT spawned on a junk key


def test_engine_order_is_env_overridable(tmp_path, monkeypatch):
    """Reversing _OCR_ENGINE_CHAIN makes glm-ocr the primary."""
    _both_creds(monkeypatch)
    monkeypatch.setattr(paper_ingest, "_OCR_ENGINE_CHAIN", ["glm-ocr", "deepseek-ocr"])
    out = tmp_path / "out"; out.mkdir()
    calls = []
    monkeypatch.setattr(paper_ingest.subprocess, "Popen", _engine_aware_skill(calls=calls))
    res = paper_ingest._ocr_pdf("/tmp/x.pdf", out, label="x")
    assert res is not None
    md, _ = res
    assert "[ENGINE:glm-ocr]" in md
    assert [c[0] for c in calls] == ["glm-ocr"]  # glm first, wins
