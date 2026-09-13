import hashlib
from pathlib import Path


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(str(p.stat().st_size).encode())
    return h.hexdigest()


def test_index_does_not_write_agent_readings(tmp_path, monkeypatch, research_db):
    ar = tmp_path / "agent-readings"
    papers = ar / "papers" / "20260101-A"; papers.mkdir(parents=True)
    (papers / "notes.md").write_text("# Notes: A\n\n## Paper Summary\n\nsummary long enough text.\n\n### Keywords\n\n`x`\n", encoding="utf-8")
    (papers / "full_text.md").write_text("## Method\n\nbody text long enough to chunk.\n", encoding="utf-8")
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(ar))
    before = _tree_digest(ar)
    from cortex_research.index_papers import build_index
    build_index(full=True)
    assert _tree_digest(ar) == before, "indexer must not modify agent-readings"
