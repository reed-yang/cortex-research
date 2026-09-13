"""_write_paper_dir: full-title naming (reader convention) + collision guard."""
from cortex_research import paper_ingest


def test_write_paper_dir_uses_ingest_date_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    meta = {"title": "Helios: Real Real-Time Long Video Generation",
            "abstract": "abs", "published_at": "2026-03-04T00:00:00Z"}
    # the {YYYYMMDD} prefix is the INGEST date (reader convention), NOT the publish
    # date; the publish date is labelled separately in notes.md.
    pdir, _n = paper_ingest._write_paper_dir("2603.04379", meta, "# body\n\nbody", [],
                                             ingest_date_iso="2026-05-20")
    assert pdir.name.startswith("20260520-Helios")   # INGEST date
    assert "20260304" not in pdir.name               # NOT the publish date
    assert "2603.04379" not in pdir.name             # id lives in the column
    notes = (pdir / "notes.md").read_text(encoding="utf-8")
    assert "### 2026-05-20" in notes                 # parsed date = ingest date
    assert "published: 2026-03-04" in notes          # publish date labelled apart
    assert (pdir / "full_text.md").exists()


def test_write_paper_dir_collision_appends_arxiv_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    meta = {"title": "Same Title", "abstract": "a", "published_at": "2026-03-04"}
    p1, _ = paper_ingest._write_paper_dir("2603.00001", meta, "x", [])
    p2, _ = paper_ingest._write_paper_dir("2603.00002", meta, "x", [])
    assert p1.name != p2.name
    assert "2603.00002" in p2.name


def test_write_paper_dir_downloads_images(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    captured = {}
    monkeypatch.setattr(paper_ingest, "_download_images",
                        lambda imgs, pdir: captured.setdefault("n", len(imgs)))
    meta = {"title": "Has Figures", "abstract": "a", "published_at": "2026-03-04"}
    paper_ingest._write_paper_dir(
        "2603.00003", meta, "body",
        [("https://arxiv.org/html/x/x1.png", "assets/fig1.png")])
    assert captured["n"] == 1


def test_write_paper_dir_returns_saved_count(tmp_path, monkeypatch):
    """Returns (paper_dir, n_saved); a local OCR-asset is copied into assets/."""
    monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path))
    src = tmp_path / "ocr" / "image_001.png"
    src.parent.mkdir(parents=True); src.write_bytes(b"PNG")
    meta = {"title": "Local Asset", "abstract": "a", "published_at": "2026-03-04"}
    pdir, n = paper_ingest._write_paper_dir(
        "2603.00004", meta, "body", [(str(src), "assets/fig1.png")])
    assert n == 1
    assert (pdir / "assets" / "fig1.png").read_bytes() == b"PNG"
