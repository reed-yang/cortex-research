"""_html_to_markdown: preserve LaTeX (math alttext) + figures (assets/ + caption);
_download_images: bounded, best-effort."""
import httpx

from cortex_research import paper_ingest
from cortex_research.paper_ingest import _html_to_markdown

_HTML_MATH = """
<article>
<h2>3 Method</h2>
<p>We define <math alttext="\\bm{x}\\in\\mathbb{R}^{d}">x in R d</math> as input.</p>
<p>The loss is <math display="block" alttext="\\mathcal{L}=\\|x-y\\|_2^2">L=...</math></p>
</article>
"""


def test_inline_math_becomes_dollar_latex():
    md, _imgs = _html_to_markdown(_HTML_MATH, base_url="https://arxiv.org/html/x/")
    assert r"$\bm{x}\in\mathbb{R}^{d}$" in md
    assert "x in R d" not in md  # the mathml glyph-soup must not leak


def test_display_math_becomes_double_dollar():
    md, _imgs = _html_to_markdown(_HTML_MATH, base_url="https://arxiv.org/html/x/")
    assert r"\mathcal{L}=\|x-y\|_2^2" in md
    assert "$$" in md


def test_figure_emits_image_ref_and_records_url():
    html = (
        '<article><figure class="ltx_figure">'
        '<img src="x1.png"/><figcaption>Figure 1: Pipeline.</figcaption>'
        "</figure></article>"
    )
    md, imgs = _html_to_markdown(html, base_url="https://arxiv.org/html/2603.04379v1/")
    assert "![Figure 1: Pipeline.](assets/fig1.png)" in md
    assert imgs == [("https://arxiv.org/html/2603.04379v1/x1.png", "assets/fig1.png")]


def test_download_images_writes_assets(tmp_path, monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, content=b"PNGDATA",
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(paper_ingest.httpx, "get", fake_get)
    pdir = tmp_path / "paper"
    pdir.mkdir()
    n = paper_ingest._download_images(
        [("https://arxiv.org/html/x/x1.png", "assets/fig1.png")], pdir)
    assert n == 1
    assert (pdir / "assets" / "fig1.png").read_bytes() == b"PNGDATA"


def test_download_images_best_effort_skips_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ingest, "_sleep_with_jitter", lambda *a, **k: None)

    def boom(url, **kw):
        raise httpx.ConnectError("nope")
    monkeypatch.setattr(paper_ingest.httpx, "get", boom)
    pdir = tmp_path / "paper"
    pdir.mkdir()
    n = paper_ingest._download_images([("http://x/x1.png", "assets/fig1.png")], pdir)
    assert n == 0
    assert not (pdir / "assets" / "fig1.png").exists()


def test_download_images_copies_local_file(tmp_path, monkeypatch):
    """A non-URL src is treated as a local path and COPIED (the OCR-asset path);
    no HTTP is attempted."""
    def boom(url, **kw):  # any network call is a bug for a local src
        raise AssertionError("must not GET for a local-file src")
    monkeypatch.setattr(paper_ingest.httpx, "get", boom)
    src = tmp_path / "ocr_assets" / "image_001.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"LOCALPNG")
    pdir = tmp_path / "paper"
    pdir.mkdir()
    n = paper_ingest._download_images([(str(src), "assets/fig1.png")], pdir)
    assert n == 1
    assert (pdir / "assets" / "fig1.png").read_bytes() == b"LOCALPNG"


def test_download_images_skips_missing_local(tmp_path):
    pdir = tmp_path / "paper"
    pdir.mkdir()
    n = paper_ingest._download_images(
        [(str(tmp_path / "nope.png"), "assets/fig1.png")], pdir)
    assert n == 0
    assert not (pdir / "assets" / "fig1.png").exists()


def test_download_images_skips_oversize_local(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ingest, "_MAX_IMG_BYTES", 4)
    src = tmp_path / "big.png"
    src.write_bytes(b"x" * 50)
    pdir = tmp_path / "paper"
    pdir.mkdir()
    n = paper_ingest._download_images([(str(src), "assets/fig1.png")], pdir)
    assert n == 0
    assert not (pdir / "assets" / "fig1.png").exists()


def test_fetch_full_text_adds_trailing_slash_to_base(monkeypatch):
    """Regression: the arxiv page url /html/<id> (no trailing slash) must be
    treated as a DIRECTORY so figure srcs resolve to /html/<id>/x1.png, not
    /html/x1.png (which 404s). Live-verified bug 2026-06-01."""
    html = (
        "<html><article><figure><img src='x1.png'/>"
        "<figcaption>Fig 1</figcaption></figure>"
        "<p>body " + "text " * 400 + "</p></article></html>"
    )

    def fake_get(url, **kw):
        # request url has NO trailing slash, like the real arxiv redirect target
        return httpx.Response(200, text=html,
                              request=httpx.Request("GET", "https://arxiv.org/html/2404.09967"))

    monkeypatch.setattr(paper_ingest.httpx, "get", fake_get)
    md, source, images = paper_ingest.fetch_full_text("2404.09967")
    assert source == "html"
    assert images[0][0] == "https://arxiv.org/html/2404.09967/x1.png"


def test_register_img_dedoubles_version_prefixed_src():
    """REGRESSION (review HIGH): arxiv redirects /html/<id> to the UNVERSIONED
    page, but figure srcs are VERSION-prefixed (<id>vN/...). urljoin against the
    page dir then DOUBLES the id (/html/<id>/<id>vN/.. -> 404). The real src
    format (live-confirmed: ALL 35 dead papers) must resolve to /html/<id>vN/..."""
    html = ('<article><figure>'
            '<img src="1706.03762v7/Figures/ModalNet-21.png"/>'
            '<figcaption>Figure 1</figcaption></figure></article>')
    # base_url is exactly what fetch_full_text builds: unversioned page + '/'
    md, imgs = _html_to_markdown(html, base_url="https://arxiv.org/html/1706.03762/")
    assert imgs == [("https://arxiv.org/html/1706.03762v7/Figures/ModalNet-21.png",
                     "assets/fig1.png")]


def test_register_img_leaves_bare_src_untouched():
    """A bare src (no <id>vN/ prefix) resolves against the page dir unchanged."""
    html = ('<article><figure><img src="x1.png"/>'
            '<figcaption>F</figcaption></figure></article>')
    md, imgs = _html_to_markdown(html, base_url="https://arxiv.org/html/2508.07901/")
    assert imgs == [("https://arxiv.org/html/2508.07901/x1.png", "assets/fig1.png")]


def test_image_backoff_hardened_constants():
    """Stronger backoff so a bulk migration survives arxiv 429 (the dead-image bug)."""
    assert paper_ingest._IMG_RETRIES == 4
    assert len(paper_ingest._IMG_BACKOFF_SECONDS) == 4


def test_get_with_backoff_survives_four_429s(monkeypatch):
    """429 on the first 4 attempts, 200 on the 5th -> succeeds (no silent drop)."""
    monkeypatch.setattr(paper_ingest, "_sleep_with_jitter", lambda *a, **k: None)
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        code = 429 if calls["n"] <= 4 else 200
        return httpx.Response(code, content=b"PNG" if code == 200 else b"",
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(paper_ingest.httpx, "get", fake_get)
    r = paper_ingest._get_with_backoff("https://arxiv.org/html/x/x1.png")
    assert r is not None and r.status_code == 200
    assert calls["n"] == 5


def _html_404(url, **kw):
    return httpx.Response(404, text="no html", request=httpx.Request("GET", url))


def test_fetch_full_text_routes_to_ocr_when_html_404(tmp_path, monkeypatch):
    """No arxiv HTML + an ocr_tmp -> route through glm-ocr; source='ocr'."""
    monkeypatch.setattr(paper_ingest.httpx, "get", _html_404)
    png = tmp_path / "image_001.png"; png.write_bytes(b"PNG")
    monkeypatch.setattr(paper_ingest, "_ocr_pdf_via_skill",
                        lambda aid, td: ("## Body\n\nocr text " * 80,
                                         [(str(png), "assets/fig1.png")]))
    # the PyMuPDF dump must NOT run when OCR succeeds
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PDF")))
    md, source, images = paper_ingest.fetch_full_text("2603.04379", ocr_tmp=tmp_path)
    assert source == "ocr"
    assert images == [(str(png), "assets/fig1.png")]
    assert "ocr text" in md


def test_fetch_full_text_ocr_none_falls_back_to_pdf(tmp_path, monkeypatch):
    """OCR returns None -> graceful PyMuPDF text dump; source='pdf', never crash."""
    monkeypatch.setattr(paper_ingest.httpx, "get", _html_404)
    monkeypatch.setattr(paper_ingest, "_ocr_pdf_via_skill", lambda aid, td: None)
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: type("R", (), {"content": b"%PDF"})())
    monkeypatch.setattr(paper_ingest, "_pdf_to_text", lambda b: "plain pdf text " * 80)
    md, source, images = paper_ingest.fetch_full_text("2603.04379", ocr_tmp=tmp_path)
    assert source == "pdf" and images == []
    assert "plain pdf text" in md


def test_fetch_full_text_no_ocr_tmp_skips_skill(monkeypatch):
    """Default (no ocr_tmp) never invokes the OCR skill (backward-compat call shape)."""
    monkeypatch.setattr(paper_ingest.httpx, "get", _html_404)
    monkeypatch.setattr(paper_ingest, "_ocr_pdf_via_skill",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("skill called")))
    monkeypatch.setattr(paper_ingest, "_get_with_retry",
                        lambda *a, **k: type("R", (), {"content": b"%PDF"})())
    monkeypatch.setattr(paper_ingest, "_pdf_to_text", lambda b: "plain pdf text " * 80)
    md, source, images = paper_ingest.fetch_full_text("2603.04379")
    assert source == "pdf"


def test_figure_with_multiple_imgs_emits_all_panels():
    """A multi-panel <figure> (sibling <img>s, shared caption) emits one ref per
    panel — sub-figures are not dropped."""
    html = ('<article><figure>'
            '<img src="a.png"/><img src="b.png"/>'
            '<figcaption>Figure 2: two panels</figcaption></figure></article>')
    md, imgs = _html_to_markdown(html, base_url="https://arxiv.org/html/x/")
    assert len(imgs) == 2
    assert "![Figure 2: two panels](assets/fig1.png)" in md
    assert "![](assets/fig2.png)" in md


def test_truncation_filters_images_past_the_cap(monkeypatch):
    """A figure positioned past _MAX_FULLTEXT_CHARS has its md ref cut; its image
    must be filtered out of the returned list (no orphan downloads)."""
    filler = "<p>" + ("word " * 60000) + "</p>"  # > 200k chars after extraction
    html = ("<html><article>" + filler +
            '<figure><img src="late.png"/><figcaption>Late</figcaption></figure>'
            "</article></html>")

    def fake_get(url, **kw):
        return httpx.Response(200, text=html,
                              request=httpx.Request("GET", "https://arxiv.org/html/2404.00001/"))
    monkeypatch.setattr(paper_ingest.httpx, "get", fake_get)
    md, source, images = paper_ingest.fetch_full_text("2404.00001")
    assert source == "html"
    assert len(md) <= paper_ingest._MAX_FULLTEXT_CHARS
    assert images == []  # the late figure's ref was truncated away


def test_download_images_skips_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ingest, "_MAX_IMG_BYTES", 10)

    def fake_get(url, **kw):
        return httpx.Response(200, content=b"x" * 50,
                              headers={"content-type": "image/png"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(paper_ingest.httpx, "get", fake_get)
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert paper_ingest._download_images([("http://x/big.png", "assets/fig1.png")], pdir) == 0
    assert not (pdir / "assets" / "fig1.png").exists()


def test_download_images_skips_non_image(tmp_path, monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, content=b"<html>404</html>",
                              headers={"content-type": "text/html"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(paper_ingest.httpx, "get", fake_get)
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert paper_ingest._download_images([("http://x/x.png", "assets/fig1.png")], pdir) == 0
