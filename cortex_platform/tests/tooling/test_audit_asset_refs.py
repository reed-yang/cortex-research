"""The read-only corpus asset-reference audit in ``tools/audit_asset_refs.py``.

Every corpus is a synthetic fixture under a resolved ``tmp_path``: macOS
``/var`` is a symlink, and the audit refuses any symlink path component.
Expected spans and positions are derived with plain string searches on the
fixture text, never with the scanner under test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from tools.audit_asset_refs import audit_corpus, main, scan_markdown

REPO = Path(__file__).resolve().parents[3]
PNG = b"\x89PNG\r\n\x1a\nsynthetic"
CLASSES = ("present", "missing", "prefix_candidate", "unsafe", "unknown",
           "placeholder", "external", "non_asset", "unsupported")


def _counts(**nonzero: int) -> dict[str, int]:
    return {name: nonzero.get(name, 0) for name in CLASSES}


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "corpus"
    root.mkdir()
    return root


def _paper(corpus: Path, name: str, files: dict[str, str | bytes], assets=()) -> Path:
    paper = corpus / name
    paper.mkdir()
    for relative, body in files.items():
        (paper / relative).write_bytes(body.encode() if isinstance(body, str) else body)
    for relative in assets:
        path = paper / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
    return paper


def _scan(text: str | bytes):
    data = text.encode() if isinstance(text, str) else text
    references, unsupported = scan_markdown(data)
    return data, references, unsupported


def _run(capsys, *argv: str) -> tuple[int, dict | None, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


def _file(report: dict, path: str) -> dict:
    return next(f for f in report["files"] if f["path"] == path)


def _findings(record: dict) -> list[tuple]:
    return [(f["kind"], f["line"], f["raw_target"], f["reason"]) for f in record["findings"]]


# --- scanner -----------------------------------------------------------------


def test_inline_destinations_have_exact_byte_spans_and_decoded_targets():
    line1 = 'Intro ![a](assets/a.png) and [b](<assets/b c.png> "Title").'
    line2 = "![c](assets/c\\(1\\).png 't') ![d](assets/d(1).png) ![e](assets/a&amp;b.png)"
    data, refs, unsupported = _scan(f"{line1}\n{line2}\n")

    assert unsupported == ()
    assert [(r.syntax, r.line, r.column, r.raw_target, r.decoded_target) for r in refs] == [
        ("markdown_image", 1, line1.index("![a]") + 1, "assets/a.png", "assets/a.png"),
        ("markdown_link", 1, line1.index("[b]") + 1, "assets/b c.png", "assets/b c.png"),
        ("markdown_image", 2, 1, "assets/c\\(1\\).png", "assets/c(1).png"),
        ("markdown_image", 2, line2.index("![d]") + 1, "assets/d(1).png", "assets/d(1).png"),
        ("markdown_image", 2, line2.index("![e]") + 1, "assets/a&amp;b.png", "assets/a&b.png"),
    ]
    for ref in refs:
        assert data[ref.byte_start:ref.byte_end] == ref.raw_target.encode()
    assert refs[1].byte_start == data.index(b"assets/b c.png")


def test_html_img_and_anchor_attributes_are_references():
    text = ('<p><img alt="x" src="assets/q.png"> <IMG SRC=assets/u.png>\n'
            "<a href='assets/z.pdf'>z</a> <img\n  src=\"assets/n&amp;m.png\" /></p>\n")
    data, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [(r.syntax, r.line, r.raw_target, r.decoded_target) for r in refs] == [
        ("html_img", 1, "assets/q.png", "assets/q.png"),
        ("html_img", 1, "assets/u.png", "assets/u.png"),
        ("html_a", 2, "assets/z.pdf", "assets/z.pdf"),
        ("html_img", 2, "assets/n&amp;m.png", "assets/n&m.png"),
    ]
    assert refs[0].column == text.index("<img") + 1
    for ref in refs:
        assert data[ref.byte_start:ref.byte_end] == ref.raw_target.encode()


def test_used_reference_definitions_count_each_use_with_one_destination_span():
    text = ("See ![first][Fig] and ![again][fig] and [Fig][] and [fig].\n"
            "\n"
            '[FIG]: <assets/f.png> "Figure"\n'
            "[unused]: assets/unused.png\n")
    data, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [(r.syntax, r.line, r.column, r.definition_line) for r in refs] == [
        ("markdown_reference_image", 1, text.index("![first]") + 1, 3),
        ("markdown_reference_image", 1, text.index("![again]") + 1, 3),
        ("markdown_reference_link", 1, text.index("[Fig][]") + 1, 3),
        ("markdown_reference_link", 1, text.index("[fig].") + 1, 3),
    ]
    assert {(r.byte_start, r.byte_end) for r in refs} == {
        (data.index(b"assets/f.png"), data.index(b"assets/f.png") + len(b"assets/f.png"))}


def test_code_regions_and_comments_hide_references():
    text = ("```\n![a](assets/fenced.png)\n```\n"
            "~~~~md\n<img src=\"assets/tilde.png\">\n~~~~\n"
            "\n"
            "    ![b](assets/indented.png)\n"
            "\n"
            "Inline `![c](assets/inline.png)` and ``tick ` ![d](assets/inline2.png)``.\n"
            "<!-- ![e](assets/comment.png)\n\n<img src=\"assets/comment2.png\"> -->\n"
            "- item\n"
            "\n"
            "    ![f](assets/list-continuation.png)\n"
            "Live ![g](assets/live.png) \\![h](assets/escaped-bang.png)\n")
    _, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [(r.syntax, r.raw_target) for r in refs] == [
        ("markdown_image", "assets/list-continuation.png"),
        ("markdown_image", "assets/live.png"),
        ("markdown_link", "assets/escaped-bang.png"),
    ]


def test_multibyte_text_and_crlf_keep_line_column_and_byte_offsets():
    text = "# 标题\r\n\r\n![图 1：结果](assets/图1.png)\r\n说明 [链接](assets/表.pdf)\r\n"
    data, refs, _ = _scan(text)

    assert [(r.line, r.column, r.raw_target) for r in refs] == [
        (3, 1, "assets/图1.png"),
        (4, len("说明 ") + 1, "assets/表.pdf"),
    ]
    assert refs[0].byte_start == data.index("assets/图1.png".encode())
    assert refs[1].byte_end == data.index("assets/表.pdf".encode()) + len("assets/表.pdf".encode())


def test_unsupported_asset_forms_are_reported_not_dropped():
    text = ('<img srcset="assets/a.png 1x, assets/a@2x.png 2x" src="assets/a.png">\n'
            '<div style="background: url(assets/bg.png)"></div>\n'
            '<video src="assets/clip.mp4"></video> <img data-src="assets/lazy.png">\n'
            "![spaced](assets/a b.png)\n"
            "[bad]: assets/a b.png\n"
            "Cut at truncation ![tail](assets/fi")
    _, refs, unsupported = _scan(text)

    assert [(r.syntax, r.raw_target) for r in refs] == [("html_img", "assets/a.png")]
    assert [(u.form, u.line, u.snippet) for u in unsupported] == [
        ("html_srcset", 1, "assets/a.png 1x, assets/a@2x.png 2x"),
        ("css_url", 2, "assets/bg.png"),
        ("html_other_asset_attribute", 3, "assets/clip.mp4"),
        ("html_other_asset_attribute", 3, "assets/lazy.png"),
        ("unparsed_markdown_destination", 4, "assets/a b.png"),
        ("unparsed_reference_definition", 5, "assets/a b.png"),
        ("unparsed_markdown_destination", 6, "assets/fi"),
    ]


@pytest.mark.parametrize(("text", "targets"), [
    # A raw HTML tag starts before the backticks or comment marker inside it.
    ('<img alt="`sample`" src="assets/a.png">\n', ["assets/a.png"]),
    ('<img alt="<!--" src="assets/a.png"> and `code`\n', ["assets/a.png"]),
    # An inline marker without a closing "-->" in its paragraph is plain text.
    ("Text mentions <!-- marker.\n\n![](assets/a.png)\n<img src=\"assets/b.png\">\n",
     ["assets/a.png", "assets/b.png"]),
    ("x <!--> ![](assets/a.png) <!---> ![](assets/b.png)\n", ["assets/a.png", "assets/b.png"]),
    ("<!-->\n![](assets/a.png)\n", ["assets/a.png"]),
    ("- a\n      <!-- not a block\n\n![](assets/a.png) -->\n", ["assets/a.png"]),
    # Code spans never pair across a block boundary.
    ("Use `x\n## Results\n![](assets/a.png) shows `y`.\n", ["assets/a.png"]),
    ("Stray `tick\n```\ncode\n```\n![](assets/a.png) and `z`\n", ["assets/a.png"]),
    ("- item `x\n- ![](assets/a.png) `y\n", ["assets/a.png"]),
    ("Quoted `x\n> ![](assets/a.png) `y\n", ["assets/a.png"]),
    # A line that only looks like a definition is scanned as text.
    ("[Note]: see ![x](figures/x.png)\n", ["figures/x.png"]),
    ("[^1]: see ![x](figures/x.png)\n", ["figures/x.png"]),
    # A fence inside a list item closes relative to the item's indentation,
    # and a less indented line ends the item and its fence.
    ("- item\n  ```\n  code\n    ```\n\n![](assets/a.png)\n", ["assets/a.png"]),
    ("- item\n  ```\ncode\n\n![](assets/a.png)\n", ["assets/a.png"]),
    # A less indented line that could close the item's fence closes it
    # instead of reopening one; a fence line that cannot close it ends the
    # item and opens a fence at the outer level.
    ("- item\n  ```\n  ![](assets/code.png)\n```\n![](assets/a.png)\n", ["assets/a.png"]),
    ("- a\n  - b\n    ```\n    code\n  ```\n  ![](assets/a.png)\n", ["assets/a.png"]),
    ("- item\n  ```\n  code\n~~~\n![](assets/code.png)\n~~~\n![](assets/a.png)\n",
     ["assets/a.png"]),
    ("- item\n  ```\n  code\n```text\n![](assets/code.png)\n```\n![](assets/a.png)\n",
     ["assets/a.png"]),
    # A failed inline tail falls back to a shortcut reference, and so does a
    # label followed by an unmatched bracket; an undefined second label stays
    # text, so the inline link after it is still read.
    ("[fig](not a link)\n\n[fig]: assets/a.png\n", ["assets/a.png"]),
    ("[r1][ x\n\n[r1]: assets/a.png\n", ["assets/a.png"]),
    ("[r1][t](assets/a.png)\n\n[r1]: assets/b.png\n", ["assets/a.png"]),
    # HTML block lines are raw: no fences or code spans inside them, and a
    # lone tag outside the list item that holds a paragraph starts one.
    ('<div>\n```\n<img src="assets/a.png">\n</div>\n', ["assets/a.png"]),
    ('<div>\n`x <img src="assets/a.png"> `\n</div>\n', ["assets/a.png"]),
    ('- item\n<img src="assets/a.png">\n  ```\n<img src="assets/b.png">\n',
     ["assets/a.png", "assets/b.png"]),
    # A definition-shaped continuation line is text; a backtick line whose
    # info string has a backtick is not a fence; code may follow a heading.
    ("Intro text\n[n]: [r1]\n\n[r1]: assets/a.png\n", ["assets/a.png"]),
    ("Para `a\n```b`\n![](assets/a.png) `c`\n", ["assets/a.png"]),
    ("# Title\n    ![](assets/code.png)\n[f]: assets/a.png\n\n![x][f]\n", ["assets/a.png"]),
])
def test_live_references_survive_markup_edge_cases(text, targets):
    _, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [r.raw_target for r in refs] == targets


def test_a_definition_inside_a_paragraph_only_backs_otherwise_undefined_uses():
    # CommonMark reads line 2 as text. Resolving the use anyway keeps an error
    # in the approximate block model from hiding it; line 2 itself is no use.
    _, refs, unsupported = _scan("Intro text\n[f]: assets/a.png\n\n![x][f]\n")

    assert unsupported == ()
    assert [(r.syntax, r.line, r.raw_target, r.definition_line) for r in refs] == [
        ("markdown_reference_image", 4, "assets/a.png", 2)]


def test_a_leading_byte_order_mark_is_skipped_but_kept_in_byte_offsets():
    for text in ("﻿```\ncode\n```\n![](assets/a.png)\n",
                 "﻿[ref]: assets/a.png\n\n![a][ref]\n",
                 "﻿![](assets/a.png)\n"):
        data, refs, unsupported = _scan(text)
        assert unsupported == ()
        assert [r.raw_target for r in refs] == ["assets/a.png"], text
        assert refs[0].byte_start == data.index(b"assets/a.png")
    assert (refs[0].line, refs[0].column) == (1, 1)


@pytest.mark.parametrize(("text", "forms"), [
    ("![](page=3, bbox=[a, b])\n",
     [("unrecognized_pseudo_destination", "page=3, bbox=[a, b]")]),
    ("![](bbox=[1,2,3], page=3)\n",
     [("unrecognized_pseudo_destination", "bbox=[1,2,3], page=3")]),
    ('Cut at truncation <img src="assets/a.png',
     [("unparsed_html_tag", '<img src="assets/a.png')]),
    ('<a href="assets/a.pdf\n', [("unparsed_html_tag", '<a href="assets/a.pdf')]),
    ('<img src="assets/a.png alt=x>\n', [("unparsed_html_tag", '<img src="assets/a.png alt=x')]),
    # A block comment without "-->" runs to the end of the document; the
    # references it hides are flagged instead of silently dropped.
    ("Intro\n<!-- never closed\n\n![](assets/a.png)\n",
     [("unclosed_html_comment", "<!-- never closed")]),
])
def test_unparsable_asset_forms_are_reported_as_unsupported(text, forms):
    _, refs, unsupported = _scan(text)

    assert refs == ()
    assert [(u.form, u.snippet) for u in unsupported] == forms


@pytest.mark.parametrize(("text", "snippet"), [
    # Ingestion writes '![{caption}]({rel})' with the raw figcaption text. An
    # interval in the caption pairs '![' with its own ']', so the real '](' has
    # no opener and CommonMark renders the whole construct as text.
    ("![Accuracy for λ ∈ (0, 1] across seeds.](assets/fig3.png)\n", "assets/fig3.png"),
    ("![Panel A [1, 2]; range (0, 1] in Panel B.](./assets/fig4.png)\n", "./assets/fig4.png"),
    ("A citation [3] ](assets/fig5.png) without an opener.\n", "assets/fig5.png"),
    ("Set {x | x ∈ [0, 1]}](assets/fig6.png).\n", "assets/fig6.png"),
    ("Caption ends with a backslash \\](assets/fig7.png)\n", "assets/fig7.png"),
    ("![Rate (0, 1]](page=3,bbox=[1,2,3,4])\n", "page=3,bbox=[1,2,3,4]"),
    # An earlier stray '](' whose text is clean ends at the same ')', so its
    # cached result must not hide a later pseudo destination.
    ("A stray ](see text ![Bound (-2, 3]](page=7,bbox=[5,6,7,8])\n", "page=7,bbox=[5,6,7,8]"),
    ("A stray ](see text ![Bound (-2, 3]](bbox=[5,6,7,8])\n", "bbox=[5,6,7,8]"),
    ("A stray ](see text ![Bound (-2, 3]](PAGE=7)\n", "PAGE=7"),
    # A '](' inside a reported destination is not reported again.
    ("Two strays ](a ](assets/fig8.png)\n", "a ](assets/fig8.png"),
])
def test_a_destination_left_without_an_opening_bracket_is_unsupported(text, snippet):
    data, refs, unsupported = _scan(text)

    assert refs == ()
    assert [(u.form, u.line, u.snippet) for u in unsupported] == [
        ("unparsed_markdown_destination", 1, snippet)]
    start = data.index(snippet.encode())
    assert (unsupported[0].byte_start, unsupported[0].byte_end) == (
        start, start + len(snippet.encode()))
    assert unsupported[0].column == text.index(snippet) + 1


@pytest.mark.parametrize("text", [
    "Inline `](assets/code.png)` and ``x ](assets/code2.png)``.\n",
    "```\n![Rate (0, 1]](assets/fenced.png)\n```\n",
    "<!-- ![Rate (0, 1]](assets/comment.png) -->\n",
    "Prose with a stray ] and a link [x](https://example.org) ](not-an-asset).\n",
])
def test_code_and_non_asset_destinations_without_an_opener_stay_quiet(text):
    _, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert all(r.raw_target == "https://example.org" for r in refs)


@pytest.mark.parametrize(("text", "refs", "forms"), [
    # Without an opener the '](' is text, so the raw HTML and CSS after it
    # keep the classification their own scanners give them.
    ('Range (-2, 3]](see <img src="assets/present.png">)\n',
     [("html_img", "assets/present.png")], []),
    ('Range (-2, 3]](see <a href="assets/absent.svg">figure</a>)\n',
     [("html_a", "assets/absent.svg")], []),
    ('Range (-2, 3]](see <span style="background: url(assets/bg.png)">x</span>)\n',
     [], [("css_url", "assets/bg.png")]),
    ("Range (-2, 3]](see url(assets/bg.png) )\n", [], [("css_url", "assets/bg.png")]),
    # A '](' inside a tag attribute is not Markdown.
    ('<img alt="Range (0, 1]](assets/alt.png)" src="assets/present.png">\n',
     [("html_img", "assets/present.png")], []),
])
def test_html_and_css_after_a_destination_without_an_opener_keep_their_class(text, refs, forms):
    _, references, unsupported = _scan(text)

    assert [(r.syntax, r.raw_target) for r in references] == refs
    assert [(u.form, u.snippet) for u in unsupported] == forms


def test_a_bracket_inside_a_code_span_does_not_break_the_caption():
    # The ']' of the interval sits in a code span, so the image still parses
    # and no destination is reported twice.
    text = "![Accuracy for `(0, 1]` across seeds.](assets/fig3.png)\n"
    data, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [(r.syntax, r.raw_target) for r in refs] == [("markdown_image", "assets/fig3.png")]
    assert refs[0].byte_start == data.index(b"assets/fig3.png")


def test_prose_comparisons_and_closed_comments_are_not_unsupported():
    text = ("Values x <y and z.\n![](assets/a.png)\n"
            "<!-- closed\n\n![](assets/hidden.png)\n-->\n![](assets/b.png)\n")
    _, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [r.raw_target for r in refs] == ["assets/a.png", "assets/b.png"]


def test_production_page_bbox_placeholders_are_parsed_as_destinations():
    text = "![](page=3,bbox=[10,20,30,40])\n![](page=4, bbox=[1, 2, 3, 4])\n"
    data, refs, unsupported = _scan(text)

    assert unsupported == ()
    assert [(r.line, r.raw_target) for r in refs] == [
        (1, "page=3,bbox=[10,20,30,40]"), (2, "page=4, bbox=[1, 2, 3, 4]")]
    assert data[refs[1].byte_start:refs[1].byte_end] == b"page=4, bbox=[1, 2, 3, 4]"


def test_long_paragraphs_with_unmatched_brackets_and_backticks_scan_in_linear_time():
    # HTML-derived bodies join paragraphs with single newlines, so one
    # "paragraph" can span a whole section. A quadratic search takes minutes.
    text = "[a `b " * 20_000 + "![x](assets/end.png)\n"
    started = time.perf_counter()
    refs, _ = scan_markdown(text.encode())
    assert time.perf_counter() - started < 5
    assert [r.raw_target for r in refs] == ["assets/end.png"]


@pytest.mark.parametrize("body", [
    "[x](" * 50_000, "[x](a(" * 33_000, "[x](<a " * 28_000, "``x <!-- " * 22_000,
    "<b x=" * 40_000, "[" * 100_000 + "]" * 100_000,
    "](x " * 50_000, "](" * 50_000 + "assets/a.png",
], ids=["open-paren", "nested-paren", "angle", "backticks-comment", "tag", "nested-brackets",
        "orphan-close", "orphan-close-asset"])
def test_repeated_malformed_constructs_scan_in_linear_time(body):
    text = body + "\n\n![x](assets/end.png)\n"
    started = time.perf_counter()
    refs, _ = scan_markdown(text.encode())
    assert time.perf_counter() - started < 5
    assert refs[-1].raw_target == "assets/end.png"


def test_invalid_utf8_is_refused():
    with pytest.raises(UnicodeDecodeError):
        scan_markdown(b"![x](assets/a.png)\xff")


# --- corpus audit --------------------------------------------------------------


def _grouped_corpus(tmp_path: Path) -> Path:
    corpus = _corpus(tmp_path)
    _paper(corpus, "alpha", {
        "full_text.md": ("![](assets/fig1.png)\n"
                         "![](assets/missing.png)\n"
                         "![](assets/missing.png)\n"
                         "![](papers/alpha/assets/fig2.png)\n"
                         "[web](https://example.org/x.png)\n"
                         "![](page=1,bbox=[1,2,3,4])\n"),
        "notes.md": "![](assets/fig1.png) [notes link](full_text.md)\n",
        "full_text_ch.md": "![图](assets/missing-ch.png)\n",
        "widget_gallery.md": ('<img src="assets/gallery.png"> '
                              '<img src="https://example.org/badge.svg">\n'),
        "aside-sample.md": "![](papers/alpha/assets/fig2.png)\n",
    }, assets=("assets/fig1.png", "assets/fig2.png"))
    _paper(corpus, "beta", {"full_text.md": "![](assets/b.png)\n", "notes.md": "plain\n"},
           assets=("assets/b.png",))
    (corpus / "index.json").write_text("{}")
    return corpus


def _tree(root: Path) -> dict[str, tuple]:
    state = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        body = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        state[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, body)
    return state


def test_default_audit_is_read_only_and_grouped_by_basename(tmp_path, capsys, monkeypatch):
    corpus = _grouped_corpus(tmp_path)
    before = _tree(tmp_path.resolve())

    def refuse(*args, **kwargs):
        raise AssertionError("the audit must not open sockets or start processes")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    assert code == 0
    assert _tree(tmp_path.resolve()) == before
    assert report["format_version"] == 1
    assert report["corpus"] == str(corpus)
    assert report["paper_dirs"] == ["alpha", "beta"]
    assert report["files_scanned"] == 7
    assert report["unreadable_inputs"] == []
    assert [f["path"] for f in report["files"]] == [
        "alpha/aside-sample.md", "alpha/full_text.md", "alpha/full_text_ch.md",
        "alpha/notes.md", "alpha/widget_gallery.md", "beta/full_text.md", "beta/notes.md"]
    assert report["by_file_type"] == {
        "full_text.md": {
            "files": 2, "files_unreadable": 0, "files_affected": 1,
            "occurrences": _counts(present=2, missing=2, prefix_candidate=1,
                                   placeholder=1, external=1),
            "unique_targets": _counts(present=2, missing=1, prefix_candidate=1,
                                      placeholder=1, external=1),
            "repairable_destinations": 1,
        },
        "notes.md": {
            "files": 2, "files_unreadable": 0, "files_affected": 0,
            "occurrences": _counts(present=1, non_asset=1),
            "unique_targets": _counts(present=1, non_asset=1),
            "repairable_destinations": 0,
        },
        "full_text_ch.md": {
            "files": 1, "files_unreadable": 0, "files_affected": 1,
            "occurrences": _counts(missing=1), "unique_targets": _counts(missing=1),
            "repairable_destinations": 0,
        },
        "widget_gallery.md": {
            "files": 1, "files_unreadable": 0, "files_affected": 1,
            "occurrences": _counts(missing=1, external=1),
            "unique_targets": _counts(missing=1, external=1),
            "repairable_destinations": 0,
        },
        "aside-sample.md": {
            "files": 1, "files_unreadable": 0, "files_affected": 1,
            "occurrences": _counts(prefix_candidate=1),
            "unique_targets": _counts(prefix_candidate=1),
            "repairable_destinations": 0,
        },
    }

    full_text = _file(report, "alpha/full_text.md")
    body = (corpus / "alpha" / "full_text.md").read_bytes()
    assert full_text["sha256"] == __import__("hashlib").sha256(body).hexdigest()
    assert _findings(full_text) == [
        ("missing", 2, "assets/missing.png", None),
        ("missing", 3, "assets/missing.png", None),
        ("prefix_candidate", 4, "papers/alpha/assets/fig2.png", None),
        ("placeholder", 6, "page=1,bbox=[1,2,3,4]", None),
    ]
    prefix = full_text["findings"][2]
    assert (prefix["byte_start"], prefix["byte_end"]) == (
        body.index(b"papers/alpha"), body.index(b"papers/alpha") + len(b"papers/alpha/assets/fig2.png"))
    assert (prefix["prefix_paper_dir"], prefix["replacement"], prefix["repairable"]) == (
        "alpha", "assets/fig2.png", True)
    aside = _file(report, "alpha/aside-sample.md")
    assert _findings(aside) == [
        ("prefix_candidate", 1, "papers/alpha/assets/fig2.png", "protected_file_type")]
    assert aside["findings"][0]["repairable"] is False


def test_local_targets_are_classified_without_unsafe_resolution(tmp_path, capsys):
    corpus = _corpus(tmp_path)
    lines = [
        "![q](assets/q.png?raw=1)",
        "![h](assets/h.png#page=2)",
        "![g](assets/fig1.png#frag)",
        "![s](assets/with%20space.png)",
        "![t](assets/%2e%2e/%2e%2e/beta/assets/b.png)",
        "![e](assets%2Fescaped.png)",
        "![a](/abs/assets/a.png)",
        "![f](file:///abs/assets/a.png)",
        "![b](assets\\b.png)",
        "![n](assets/nul%00.png)",
        "![d](data:image/png;base64,AAAA)",
        "[x](#section) [y](other.md)",
        "![z](figures/z.png)",
        "![p](page=5)",
        "![i](assets/bad%ff.png)",
    ]
    _paper(corpus, "gamma", {"full_text.md": "\n".join(lines) + "\n"},
           assets=("assets/q.png", "assets/h.png", "assets/fig1.png#frag",
                   "assets/with space.png"))
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    assert code == 0
    record = _file(report, "gamma/full_text.md")
    assert record["occurrences"] == _counts(present=4, unsafe=7, missing=1, external=1,
                                            non_asset=2, unsupported=1)
    assert _findings(record) == [
        ("unsafe", 5, "assets/%2e%2e/%2e%2e/beta/assets/b.png", "traversal"),
        ("unsafe", 6, "assets%2Fescaped.png", "encoded_separator"),
        ("unsafe", 7, "/abs/assets/a.png", "absolute_path"),
        ("unsafe", 8, "file:///abs/assets/a.png", "file_url"),
        ("unsafe", 9, "assets\\b.png", "backslash"),
        ("unsafe", 10, "assets/nul%00.png", "nul"),
        ("missing", 13, "figures/z.png", None),
        ("unsupported", 14, "page=5", "unrecognized_pseudo_destination"),
        ("unsafe", 15, "assets/bad%ff.png", "invalid_percent_encoding"),
    ]


def test_an_uninspectable_asset_is_unknown_not_missing(tmp_path, capsys):
    corpus = _corpus(tmp_path)
    paper = _paper(corpus, "theta", {"full_text.md": "![](assets/a.png)\n"},
                   assets=("assets/a.png",))
    (paper / "assets").chmod(0)
    try:
        code, report, _ = _run(capsys, "--corpus", str(corpus))
    finally:
        (paper / "assets").chmod(0o700)

    assert code == 0
    record = _file(report, "theta/full_text.md")
    assert record["occurrences"] == _counts(unknown=1)
    assert [(f["kind"], f["reason"]) for f in record["findings"]] == [
        ("unknown", "permission_denied")]


def test_prefix_repairability_requires_same_paper_absent_original_and_regular_asset(
        tmp_path, capsys):
    corpus = _corpus(tmp_path)
    paper = _paper(corpus, "delta", {
        "full_text.md": ("![1](papers/delta/assets/ok.png)\n"
                         "![2](./papers/delta/assets/ok.png)\n"
                         "![3](papers/other/assets/ok.png)\n"
                         "![4](papers/delta/assets/absent.png)\n"
                         "![5](papers/delta/assets/present.png)\n"
                         "![6](papers/delta/assets/link.png)\n"
                         "![7](papers/delta/assets/dir.png)\n"
                         "![8][ref] ![9][ref]\n"
                         "![10](papers/d%65lta/assets/ok.png)\n"
                         "\n"
                         "[ref]: papers/delta/assets/ok2.png\n"),
        "notes.md": "![](papers/delta/assets/ok.png)\n",
    }, assets=("assets/ok.png", "assets/ok2.png", "assets/present.png",
               "papers/delta/assets/present.png"))
    os.symlink(paper / "assets" / "ok.png", paper / "assets" / "link.png")
    (paper / "assets" / "dir.png").mkdir()
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    assert code == 0
    record = _file(report, "delta/full_text.md")
    assert [(f["line"], f["replacement"], f["repairable"], f["reason"])
            for f in record["findings"]] == [
        (1, "assets/ok.png", True, None),
        (2, "assets/ok.png", True, None),
        (3, None, False, "different_paper"),
        (4, None, False, "replacement_missing"),
        (5, None, False, "original_resolves"),
        (6, None, False, "replacement_unsafe"),
        (7, None, False, "replacement_unsafe"),
        (8, "assets/ok2.png", True, None),
        (8, "assets/ok2.png", True, None),
        (9, None, False, "encoded_prefix"),
    ]
    assert record["repairable_destinations"] == 3
    notes = _file(report, "delta/notes.md")
    assert [(f["repairable"], f["reason"]) for f in notes["findings"]] == [
        (False, "protected_file_type")]
    assert report["by_file_type"]["notes.md"]["repairable_destinations"] == 0


def test_prefix_proof_applies_the_literal_suffix_rule_to_both_paths(tmp_path, capsys):
    corpus = _corpus(tmp_path)
    _paper(corpus, "kappa", {"full_text.md": ("![](papers/kappa/assets/f.png#frag)\n"
                                              "![](assets/literal.png#frag)\n"
                                              "![](papers/kappa/assets/literal.png#frag)\n")},
           assets=("papers/kappa/assets/f.png#frag", "assets/f.png",
                   "assets/literal.png#frag"))
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    assert code == 0
    record = _file(report, "kappa/full_text.md")
    assert record["occurrences"] == _counts(present=1, prefix_candidate=2)
    assert [(f["line"], f["replacement"], f["repairable"], f["reason"])
            for f in record["findings"]] == [
        (1, None, False, "original_resolves"),
        (3, "assets/literal.png#frag", True, None),
    ]


def test_references_after_markup_edge_cases_reach_the_report(tmp_path, capsys):
    lines = [
        '<img alt="`sample`" src="assets/l1.png">',
        "",
        "Text mentions <!-- marker.",
        "",
        "Use `x",
        "## Results",
        "![](assets/l7.png) shows `y`.",
        "",
        "[Note]: see ![x](figures/l9.png)",
        "",
        "- item",
        "  ```",
        "  code",
        "    ```",
        "",
        "[fig](not a link)",
        "",
        "[fig]: assets/l18.png",
    ]
    corpus = _corpus(tmp_path)
    _paper(corpus, "lambda", {"full_text.md": "\n".join(lines) + "\n"})
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    assert code == 0
    assert _findings(_file(report, "lambda/full_text.md")) == [
        ("missing", 1, "assets/l1.png", None),
        ("missing", 7, "assets/l7.png", None),
        ("missing", 9, "figures/l9.png", None),
        ("missing", 16, "assets/l18.png", None),
    ]


def test_an_ingested_caption_with_an_interval_is_a_finding_not_a_clean_file(tmp_path, capsys):
    body = ("## Results\n\n"
            "![Accuracy for λ ∈ (0, 1] across seeds.](assets/fig3.png)\n\n"
            "![Loss curve.](assets/fig4.png)\n")
    corpus = _corpus(tmp_path)
    _paper(corpus, "mu", {"full_text.md": body}, assets=["assets/fig4.png"])
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    record = _file(report, "mu/full_text.md")
    assert code == 0
    assert record["occurrences"] == _counts(present=1, unsupported=1)
    assert [(f["kind"], f["syntax"], f["line"], f["raw_target"]) for f in record["findings"]] == [
        ("unsupported", "unparsed_markdown_destination", 3, "assets/fig3.png")]


def test_tags_after_a_destination_without_an_opener_are_classified_in_the_report(tmp_path, capsys):
    body = ("## Results\n\n"
            'Range (-2, 3]](see <img src="assets/present.png">)\n\n'
            'Range (-2, 3]](see <a href="assets/absent.svg">figure</a>)\n')
    corpus = _corpus(tmp_path)
    _paper(corpus, "nu", {"full_text.md": body}, assets=["assets/present.png"])
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    record = _file(report, "nu/full_text.md")
    assert code == 0
    assert record["occurrences"] == _counts(present=1, missing=1)
    assert [(f["kind"], f["syntax"], f["line"], f["raw_target"]) for f in record["findings"]] == [
        ("missing", "html_a", 5, "assets/absent.svg")]


def test_symlinks_and_unreadable_inputs_are_reported_with_nonzero_status(tmp_path, capsys):
    corpus = _corpus(tmp_path)
    eps = _paper(corpus, "eps", {"full_text.md": "![](assets/a.png)\n",
                                 "full_text_ch.md": b"![](assets/a.png)\xff\n",
                                 "other.txt": "![](assets/ignored.png)\n"})
    os.symlink(eps / "full_text.md", eps / "notes.md")
    os.link(eps / "other.txt", eps / "linked-copy.md")
    (eps / "dir.md").mkdir()
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "a.png").write_bytes(PNG)
    zeta = _paper(corpus, "zeta", {"full_text.md": "![](assets/a.png)\n![](sub/l.png)\n"})
    os.symlink(outside, zeta / "assets")
    (zeta / "sub").mkdir()
    os.symlink(outside / "a.png", zeta / "sub" / "l.png")
    os.symlink(outside, corpus / "linked-paper")
    code, report, _ = _run(capsys, "--corpus", str(corpus))

    assert code == 1
    assert report["unreadable_inputs"] == [
        {"path": "eps/dir.md", "reason": "not_regular_file"},
        {"path": "eps/full_text_ch.md", "reason": "invalid_utf8"},
        {"path": "eps/linked-copy.md", "reason": "hardlink"},
        {"path": "eps/notes.md", "reason": "symlink"},
        {"path": "linked-paper", "reason": "symlink"},
    ]
    assert report["paper_dirs"] == ["eps", "zeta"]
    assert [f["path"] for f in report["files"]] == ["eps/full_text.md", "zeta/full_text.md"]
    assert report["by_file_type"]["notes.md"]["files_unreadable"] == 1
    assert report["by_file_type"]["full_text.md"]["files"] == 2
    assert _findings(_file(report, "eps/full_text.md")) == [
        ("missing", 1, "assets/a.png", None)]
    assert _findings(_file(report, "zeta/full_text.md")) == [
        ("unsafe", 1, "assets/a.png", "symlink"),
        ("unsafe", 2, "sub/l.png", "symlink"),
    ]


def test_explicit_selection_is_exact_and_ordering_is_stable(tmp_path, capsys):
    corpus = _grouped_corpus(tmp_path)
    code, report, _ = _run(capsys, "--corpus", str(corpus), "--paper-dir", "beta")

    assert code == 0
    assert report["paper_dirs"] == ["beta"]
    assert [f["path"] for f in report["files"]] == ["beta/full_text.md", "beta/notes.md"]
    first = main(["--corpus", str(corpus)])
    out1 = capsys.readouterr().out
    second = main(["--corpus", str(corpus)])
    assert (first, second) == (0, 0)
    assert capsys.readouterr().out == out1
    assert audit_corpus(corpus, paper_dirs=("beta", "alpha", "beta"))["paper_dirs"] == [
        "alpha", "beta"]


# "ALPHA" resolves to "alpha" on a case-insensitive file system; a selection
# must still spell the directory entry exactly, so no paper is audited twice.
@pytest.mark.parametrize("selection", ["missing", "../alpha", "alpha/assets", ".", "..",
                                       "", "index.json", "linked", "ALPHA"])
def test_invalid_paper_selection_fails_without_a_report(tmp_path, capsys, selection):
    corpus = _grouped_corpus(tmp_path)
    os.symlink(corpus / "alpha", corpus / "linked")
    code, report, err = _run(capsys, "--corpus", str(corpus), "--paper-dir", selection)

    assert (code, report) == (2, None)
    assert "paper" in err


def test_corpus_must_be_an_explicit_real_directory(tmp_path, capsys):
    corpus = _grouped_corpus(tmp_path)
    alias = tmp_path.resolve() / "alias"
    os.symlink(corpus, alias)
    for argument in ("corpus", str(alias), str(corpus / "missing"),
                     str(corpus / "index.json")):
        code, report, err = _run(capsys, "--corpus", argument)
        assert (code, report) == (2, None), argument
        assert "corpus" in err
    with pytest.raises(SystemExit) as exited:
        main([])
    assert exited.value.code == 2


def test_command_line_entry_point_imports_no_indexer_or_network_stack(tmp_path):
    corpus = _grouped_corpus(tmp_path)
    help_run = subprocess.run([sys.executable, "-B", "-m", "tools.audit_asset_refs", "--help"],
                              cwd=REPO, capture_output=True, text=True, check=False)
    assert help_run.returncode == 0
    assert "--corpus" in help_run.stdout

    probe = ("import sys\n"
             "from tools.audit_asset_refs import main\n"
             f"code = main(['--corpus', {str(corpus)!r}])\n"
             "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
             "{'cortex_research', 'sqlite3', 'socket', 'ssl', 'httpx', 'subprocess'})\n"
             "print(code, loaded, file=sys.stderr)\n")
    before = _tree(tmp_path.resolve())
    run = subprocess.run([sys.executable, "-B", "-c", probe], cwd=REPO,
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert run.stderr.strip() == "0 []"
    assert json.loads(run.stdout)["files_scanned"] == 7
    assert _tree(tmp_path.resolve()) == before
