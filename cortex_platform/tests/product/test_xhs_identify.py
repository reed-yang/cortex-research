"""Identification rules, the verbatim filter, the merge and title matching."""

from __future__ import annotations

import pytest

from cortex_platform.product.xhs import identify

CAPTION = "本周推荐三篇 Three picks this week; see arXiv 2312.00752 too"
TRANSCRIPTIONS = [
    (1, "Reading list\n\nAttention Is\nAll You Need\narXiv:1706.03762v7"),
    (2, "扩散模型入门 Diffusion Primer\n博客 https://blog.example/diffusion-primer。"),
    (4, "Price 2023.12 and version 1.2345 are not identifiers"),
]


def model_item(**fields):
    item = {"kind": "paper", "title": "", "image": None, "quote": "", "arxiv_id": None, "url": None}
    item.update(fields)
    return item


def test_rules_find_arxiv_ids_in_images_before_the_caption() -> None:
    items = identify.rule_items(CAPTION, TRANSCRIPTIONS)
    assert [(item["arxiv_id"], item["image"], item["quote"]) for item in items] == [
        ("1706.03762", 1, "arXiv:1706.03762v7"),
        ("2312.00752", None, "arXiv 2312.00752"),
    ]
    assert all(item["origin"] == "rule" and item["kind"] == "paper" for item in items)


def test_the_input_labels_each_image_by_its_original_number() -> None:
    text = identify.build_identify_input(CAPTION, TRANSCRIPTIONS)
    assert "## Caption" in text and "## Image 4" in text and "## Image 3" not in text
    assert identify.input_sha256(CAPTION, TRANSCRIPTIONS) == identify.input_sha256(
        CAPTION, list(reversed(TRANSCRIPTIONS))
    )
    with pytest.raises(ValueError):
        identify.build_identify_input(CAPTION, [(1, "a"), (1, "b")])


def test_the_verbatim_filter_normalizes_whitespace_and_case_only() -> None:
    kept, dropped = identify.verbatim_filter(
        [
            model_item(title="attention is all you need", image=1, quote="ATTENTION IS  ALL you need"),
            model_item(title="Three picks this week", image=None, quote="three picks"),
            model_item(title="Attention Is All You Need", image=None, quote="Attention"),
            model_item(title="Diffusion Primer", image=7, quote="Diffusion Primer"),
            model_item(title="Diffusion Primer", image=2, quote="an invented quote"),
            model_item(title="", image=2, quote="Diffusion"),
        ],
        CAPTION,
        TRANSCRIPTIONS,
    )
    assert [(item["title"], item["image"]) for item in kept] == [
        ("attention is all you need", 1),
        ("Three picks this week", None),
    ]
    assert dropped == 4


def test_identify_merges_rules_and_model_items() -> None:
    outcome = identify.identify(
        CAPTION,
        TRANSCRIPTIONS,
        [
            model_item(title="Attention Is All You Need", image=1,
                       quote="Attention Is All You Need", arxiv_id="arXiv:1706.03762"),
            model_item(kind="blog", title="Diffusion Primer", image=2,
                       quote="扩散模型入门 Diffusion Primer", url="https://invented.example/x"),
            model_item(kind="blog", title="diffusion primer", image=2, quote="Diffusion Primer"),
            model_item(title="Invented", image=1, quote="Invented"),
        ],
    )
    assert outcome.dropped == 1 and outcome.rule_items == 2 and outcome.model_items == 4
    by_key = {item["item_key"]: item for item in outcome.items}
    paper = by_key["arxiv:1706.03762"]
    assert paper["origin"] == "rule+model" and paper["title"] == "Attention Is All You Need"
    assert by_key["arxiv:2312.00752"]["origin"] == "rule"
    blogs = [item for item in outcome.items if item["kind"] == "blog"]
    # One blog: the duplicate title on the same image merged; the model's URL
    # is not in the text, so it is not trusted and the item awaits resolution.
    assert len(blogs) == 1
    assert blogs[0]["url"] is None and blogs[0]["url_state"] == "none"


def test_a_url_written_in_the_cited_text_is_from_text() -> None:
    outcome = identify.identify(
        CAPTION,
        TRANSCRIPTIONS,
        [model_item(kind="blog", title="Diffusion Primer", image=2,
                    quote="博客 https://blog.example/diffusion-primer")],
    )
    (blog,) = [item for item in outcome.items if item["kind"] == "blog"]
    assert blog["url"] == "https://blog.example/diffusion-primer"
    assert blog["url_state"] == "from_text"


@pytest.mark.parametrize(
    ("text", "proposed"),
    [
        # A different ID beside the written one, and an ID with none written.
        ("arXiv:2601.00042 Fast Inference from Transformers", "2601.00099"),
        ("Fast Inference from Transformers", "1706.03762"),
    ],
)
def test_a_model_arxiv_id_not_written_in_the_cited_text_is_dropped(
    text: str, proposed: str
) -> None:
    outcome = identify.identify(
        "", [(1, text)],
        [model_item(title="Fast Inference from Transformers", image=1,
                    quote="Fast Inference from Transformers", arxiv_id=proposed)],
    )
    model = [item for item in outcome.items if item["origin"] != "rule"]
    assert len(model) == 1
    assert model[0]["arxiv_id"] is None
    assert not model[0]["item_key"].startswith("arxiv:")
    assert proposed not in {item["arxiv_id"] for item in outcome.items}


def test_a_merged_item_keeps_the_caption_its_quote_was_checked_against() -> None:
    outcome = identify.identify(
        "Review of A Real Paper Title arXiv:2601.00042",
        [(1, "arXiv:2601.00042")],
        [model_item(title="A Real Paper Title", image=None,
                    quote="Review of A Real Paper Title", arxiv_id="2601.00042")],
    )
    (item,) = outcome.items
    assert (item["origin"], item["image"], item["arxiv_id"]) == ("rule+model", None, "2601.00042")
    # The image is preferred when it shows the title and quote too.
    outcome = identify.identify(
        "Review of A Real Paper Title arXiv:2601.00042",
        [(1, "Review of A Real Paper Title arXiv:2601.00042")],
        [model_item(title="A Real Paper Title", image=None,
                    quote="Review of A Real Paper Title", arxiv_id="2601.00042")],
    )
    (item,) = outcome.items
    assert (item["origin"], item["image"]) == ("rule+model", 1)


def test_identify_with_no_model_items_keeps_rule_items() -> None:
    outcome = identify.identify(CAPTION, TRANSCRIPTIONS, [])
    assert {item["arxiv_id"] for item in outcome.items} == {"1706.03762", "2312.00752"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        "[1, 2]",
        '{"items": {}}',
        '{"items": [{"kind": "video", "title": "x", "image": null, "quote": "x"}]}',
        '{"items": [{"kind": "paper", "title": "x", "image": "1", "quote": "x"}]}',
        '{"items": [{"kind": "paper", "title": 3, "image": null, "quote": "x"}]}',
    ],
)
def test_a_malformed_answer_is_refused(text: str) -> None:
    with pytest.raises(identify.IdentifyAnswerError):
        identify.parse_model_items(text)


def test_a_fenced_answer_parses() -> None:
    text = '```json\n{"items": [{"kind": "blog", "title": "T", "image": 2, "quote": "T", "url": null}]}\n```'
    assert identify.parse_model_items(text) == [
        {"kind": "blog", "title": "T", "image": 2, "quote": "T", "arxiv_id": None, "url": None}
    ]
    assert identify.parse_model_items('{"items": []}') == []


def test_link_answers() -> None:
    assert identify.parse_link_answer('{"url": null, "page_title": null}') == (None, None)
    assert identify.parse_link_answer(
        '{"url": "HTTPS://Blog.Example/Post#top", "page_title": " The Post "}'
    ) == ("https://blog.example/Post", "The Post")
    for bad in ('{"url": "javascript:alert(1)"}', '{"url": 5}', "nothing"):
        with pytest.raises(identify.IdentifyAnswerError):
            identify.parse_link_answer(bad)


@pytest.mark.parametrize(
    ("expected", "observed", "matched"),
    [
        ("The Illustrated Transformer", "The Illustrated Transformer – Jay's Blog", True),
        ("The Illustrated Transformer", "Illustrated Transformer", True),  # overlap 2/3
        ("The Illustrated Transformer", "Transformer", False),
        ("A Gentle Guide to Diffusion Models", "Guide to Diffusion Models", True),
        ("Understanding LSTM Networks", "colah's blog", False),
        ("扩散模型入门指南", "扩散模型入门指南 - 某博客", True),
        ("扩散模型入门指南", "完全不同的文章", False),
        ("Scaling Laws", None, False),
    ],
)
def test_title_matching(expected: str, observed: str | None, matched: bool) -> None:
    assert identify.title_matches(expected, [observed]) is matched
