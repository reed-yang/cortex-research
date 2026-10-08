"""The weekly fallback's pure parts: arXiv links, the rule plan, the input
digest and the checked decision shape."""

from __future__ import annotations

import pytest

from cortex_platform.product.sources.identity import arxiv_id_from_url
from cortex_platform.product.xhs import fallback

NOTE = "66f1a2b3c4d5e6f708192a3b"
OTHER = "66f1a2b3c4d5e6f708192a3c"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/2509.01234", "2509.01234"),
        ("https://arxiv.org/abs/2509.01234v3", "2509.01234"),
        ("http://arxiv.org/abs/2509.01234", "2509.01234"),
        ("https://www.arxiv.org/abs/2509.01234", "2509.01234"),
        ("https://arxiv.org/pdf/2509.01234", "2509.01234"),
        ("https://arxiv.org/pdf/2509.01234v2.pdf", "2509.01234"),
        ("https://arxiv.org/html/2509.01234v1", "2509.01234"),
        ("https://arxiv.org/html/2509.01234v1/", "2509.01234"),
        ("https://arxiv.org/abs/2509.01234?context=cs.CL", "2509.01234"),
        ("https://arxiv.org/abs/2509.01234.pdf", None),
        ("https://arxiv.org/html/2509.01234.pdf", None),
        ("https://arxiv.org/list/cs.CL/recent", None),
        ("https://arxiv.org/abs/hep-th/9901001", None),
        ("https://export.arxiv.org/abs/2509.01234", None),
        ("https://arxiv.org.example/abs/2509.01234", None),
        ("https://user@arxiv.org/abs/2509.01234", None),
        ("https://arxiv.org:8443/abs/2509.01234", None),
        ("ftp://arxiv.org/abs/2509.01234", None),
        ("https://huggingface.co/papers/2509.01234", None),
        (None, None),
    ],
)
def test_arxiv_ids_come_only_from_arxiv_page_urls(url, expected) -> None:
    assert arxiv_id_from_url(url) == expected


def _blog(rec_id: str, url: str | None, note: str = NOTE) -> dict:
    return {"id": rec_id, "note_id": note, "url": url}


def test_rules_convert_arxiv_links_and_mark_duplicates_within_one_note() -> None:
    blogs = [
        _blog("b1", "https://arxiv.org/abs/2509.00001"),
        _blog("b2", "https://arxiv.org/pdf/2509.00001v2"),
        _blog("b3", "https://blog.example/post"),
        _blog("b4", "https://arxiv.org/html/2509.00002v1"),
        _blog("b5", "https://arxiv.org/abs/2509.00002", note=OTHER),
        _blog("b6", None),
    ]
    papers = [{"id": "p1", "note_id": NOTE, "arxiv_id": "2509.00002"}]
    actions = fallback.plan_rules(blogs, papers)
    assert actions == [
        fallback.RuleAction("b1", NOTE, "2509.00001"),
        # The blog converted just before is the paper this one duplicates.
        fallback.RuleAction("b2", NOTE, "2509.00001", duplicate_of="b1"),
        fallback.RuleAction("b4", NOTE, "2509.00002", duplicate_of="p1"),
        # Another note's paper never makes a duplicate.
        fallback.RuleAction("b5", OTHER, "2509.00002"),
    ]
    assert fallback.plan_rules(blogs, papers, limit=2) == actions[:2]


def test_the_input_digest_covers_the_cited_evidence_and_the_prompt_version() -> None:
    recommendation = {
        "kind": "blog", "title": "Attention Sinks", "quote": "Attention Sinks",
        "url": None, "url_state": "none", "url_checked_title": None, "image_ordinal": None,
    }

    def digest(**changes) -> str:
        values = {"note_title": "weekly", "caption": "caption text", "image_text_sha256": None}
        return fallback.input_sha256(recommendation | changes.pop("rec", {}), **(values | changes))

    base = digest()
    assert len(base) == 64 and base == digest()
    assert digest(caption="another caption") != base
    assert digest(note_title="another title") != base
    assert digest(rec={"title": "Other"}) != base
    assert digest(rec={"url_state": "not_found"}) != base
    # An image is cited by its transcription's hash; the caption then is not evidence.
    image = digest(rec={"image_ordinal": 2}, image_text_sha256="c" * 64)
    assert image == digest(rec={"image_ordinal": 2}, image_text_sha256="c" * 64,
                           caption="another caption")
    assert image != digest(rec={"image_ordinal": 2}, image_text_sha256="d" * 64)


@pytest.mark.parametrize(
    "decision",
    [
        {"action": "blog", "url": "https://arxiv.org/abs/2509.00001", "checked_title": "t"},
        {"action": "blog", "url": "https://openreview.net/forum?id=x", "checked_title": "t"},
        {"action": "blog", "url": "https://www.aclanthology.org/x", "checked_title": "t"},
        {"action": "blog", "url": "https://doi.org/10.1/x", "checked_title": "t"},
        {"action": "blog", "url": "https://blog.example/paper.PDF", "checked_title": "t"},
        {"action": "blog", "url": "ftp://blog.example/post", "checked_title": "t"},
        {"action": "blog", "url": "https://blog.example/post", "checked_title": ""},
        {"action": "paper", "arxiv_id": "not-an-id"},
        {"action": "exclude", "reason_code": "insufficient_evidence"},
        {"action": "exclude", "reason_code": "operator"},
        {"action": "needs_operator", "reason_code": "duplicate"},
        {"action": "needs_operator", "reason_code": "title_mismatch", "url": "x"},
        {"action": "paper", "arxiv_id": None, "reason": "x" * 501},
        {"action": "paper", "arxiv_id": None, "reason": "line\x07bell"},
        {"action": "import"},
    ],
)
def test_a_decision_outside_its_table_is_refused(decision) -> None:
    with pytest.raises(ValueError):
        fallback.check_decision(decision)


def test_a_checked_decision_is_normalized() -> None:
    assert fallback.check_decision(
        {"action": "blog", "url": "HTTPS://Blog.Example/Post", "checked_title": "Post",
         "reason": "  The page names the post.  "}
    ) == {"action": "blog", "url": "https://blog.example/Post", "checked_title": "Post",
          "reason": "The page names the post."}
    assert fallback.check_decision({"action": "paper", "arxiv_id": "arXiv:2509.00001v2"}) == {
        "action": "paper", "arxiv_id": "2509.00001", "reason": None,
    }
    assert fallback.merge_corrected_fields(["url"], ("arxiv_id", "kind")) == [
        "kind", "arxiv_id", "url",
    ]
    assert fallback.run_summary(["blog_queued", None, "needs_operator", "needs_operator"]) == {
        "blog_queued": 1, "paper_corrected": 0, "paper_kept": 0, "excluded": 0,
        "needs_operator": 2, "stale": 1,
    }
