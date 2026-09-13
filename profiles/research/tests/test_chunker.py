from pathlib import Path

def test_chunk_paper_sections(tmp_path):
    from cortex_research.chunker import chunk_paper
    ft = tmp_path / "full_text.md"
    ft.write_text("# Intro\n\nHello world body that is long enough to keep.\n\n## Method\n\nWe do X then Y.\n", encoding="utf-8")
    # paper_dir is derived from parent dir name
    (tmp_path / "20990101-Demo").mkdir()
    ft2 = tmp_path / "20990101-Demo" / "full_text.md"
    ft2.write_text("## Method\n\nWe propose a method with enough text to survive filtering.\n", encoding="utf-8")
    chunks = chunk_paper(ft2, "Demo Paper")
    assert chunks and all(set(c) >= {"chunk_id", "text", "section", "chunk_idx", "paper_dir"} for c in chunks)
    assert chunks[0]["paper_dir"] == "20990101-Demo"
