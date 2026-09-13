from pathlib import Path


def test_parse_notes_extracts_fields(tmp_path):
    from cortex_research.catalog import parse_notes
    p = tmp_path / "20260203-Demo_Paper"
    p.mkdir()
    (p / "notes.md").write_text(
        "# Notes: Demo Title\n\n### 2026-02-03\n\n## Paper Summary\n\n"
        "This is the summary paragraph describing the demo paper in enough detail.\n\n"
        "### Keywords / 关键术语\n\n`Diffusion`, `RoPE`, `Identity`\n\n"
        "相关项目：[[projects/2026-02-Human-Replacement-roadmap|HR]]\n",
        encoding="utf-8",
    )
    e = parse_notes(p / "notes.md")
    assert e["title"] == "Demo Title"
    assert e["date"] == "2026-02-03"
    assert "Diffusion" in e["keywords"] and "RoPE" in e["keywords"]
    assert e["summary"].startswith("This is the summary")
    assert "2026-02-Human-Replacement-roadmap" in e["projects"]
