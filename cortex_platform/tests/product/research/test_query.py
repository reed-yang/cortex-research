"""The retrieval query is derived from the question without I/O or model calls."""

import pytest

from cortex_platform.product.research.query import FORMAT_TERMS, retrieval_query
from cortex_platform.product.sources.reader import SourceQueryInvalid
from cortex_platform.product.sources.search import (
    MAX_QUERY_BYTES, MAX_QUERY_TERMS, _terms, query_terms,
)


def terms(text):
    english, unicode = query_terms(text)
    return english + unicode


@pytest.mark.parametrize("question,expected", [
    ("Motion-Transfer\nstreaming\treplacement\r\n", "Motion Transfer streaming replacement"),
    ("cache memory　index stream\x7fnote\x85end", "cache memory index stream note end"),
    ("\x01", ""),
    (" ", ""),
])
def test_controls_and_whitespace_become_single_spaces(question, expected):
    result = retrieval_query(question)
    assert result == expected
    assert not any(ord(character) < 32 for character in result)
    assert "  " not in result and result == result.strip()


@pytest.mark.parametrize("label", [
    "[S1]", "[D1]", "[S1, S2]", "[S1 S2]", "[S1，S2]", "[S1、S2]", "[S1][D2]", "[D1][S2]",
    "[S1-S3]", "[S2; S9]", "[S 2]", "[ s3 ]",
])
def test_citation_label_groups_are_removed(label):
    assert retrieval_query(f"cache {label} replacement") == "cache replacement"


@pytest.mark.parametrize("question,expected", [
    ("cache [sic] note", "cache sic note"),
    ("[Supplementary] material", "Supplementary material"),
    ("[Self Forcing](https://example.org/a_b?x=1) stream", "Self Forcing stream"),
    ("see [S1](https://example.org/s1) cache", "see S1 cache"),
    ('[Data](https://example.org/(nested)/x "Title") memory', "Data memory"),
])
def test_ordinary_brackets_stay_and_links_keep_only_their_text(question, expected):
    assert retrieval_query(question) == expected


def test_numbers_versions_and_years_stay_while_length_requirements_go():
    result = retrieval_query("Model VA 2.0 Net2.1 14B 2025 YOLO 11 step 7 0.5 控制在500字内")
    english, unicode = query_terms(result)
    assert english == ["model", "va", "2.0", "net2.1", "14b", "2025", "yolo", "11", "step", "0.5"]
    assert unicode == []
    assert "500" not in result


@pytest.mark.parametrize("requirement", [
    "in 200 words", "under 3 paragraphs", "within 50 sentences", "no more than 5 bullets",
    "at most 100 tokens", "in 300 characters", "20 chars", "8 points", "不超过300字",
    "少于 5 条", "控制在800字左右", "控制在500字以内", "3个段", "IN 200 WORDS",
])
def test_length_requirements_are_removed(requirement):
    assert retrieval_query(f"cache memory {requirement} replacement") == "cache memory replacement"


def test_length_words_do_not_cut_into_neighbouring_terms():
    assert retrieval_query("plugin 200 words Net2.1 characters") == "plugin Net2.1"


def test_format_terms_are_removed_case_insensitively():
    assert retrieval_query("Summarize Self Forcing in 200 words with citations") == "Self Forcing"
    assert retrieval_query("PLEASE use Markdown and LaTeX, concisely") == "use"
    assert {"please", "summarize", "summarise", "latex", "markdown", "cite", "citations",
            "words", "concise", "briefly"} <= FORMAT_TERMS


def test_item_instruction_keeps_only_the_title_as_english_terms():
    question = "仅基于当前条目的保留文档回答，控制在500字内，并使用 [D1] 等引用"
    result = retrieval_query(question, ("streaming-cache-memory-note-2",))
    english, unicode = query_terms(result)
    assert english == ["streaming", "cache", "memory", "note"]
    assert result.startswith("streaming cache memory note ")
    assert unicode == ["仅基于当前条目的保留文档回答", "并使用", "等引用"]


def test_context_pieces_lead_and_duplicates_are_removed_case_insensitively():
    result = retrieval_query("Motion-Transfer streaming", ("motion-transfer-streaming-replacement",))
    assert result == "motion transfer streaming replacement"
    assert retrieval_query("推测解码", ()) == "推测解码"


def test_terms_are_capped_and_title_terms_are_kept():
    question = " ".join(f"term{index}" for index in range(40))
    result = retrieval_query(question, ("cache memory stream index",))
    found = terms(result)
    assert len(found) == MAX_QUERY_TERMS
    assert found[:4] == ["cache", "memory", "stream", "index"]
    assert found[4:] == [f"term{index}" for index in range(MAX_QUERY_TERMS - 4)]


@pytest.mark.parametrize("question", [
    " ".join(f"x{index}" + "a" * 58 for index in range(20)),
    "，".join("机器人" * 16 + str(index) for index in range(20)),
])
def test_bytes_are_capped_by_dropping_trailing_pieces(question):
    result = retrieval_query(question)
    assert 0 < len(result.encode("utf-8")) <= MAX_QUERY_BYTES
    assert question.replace("，", " ").startswith(result)


def test_fallback_keeps_term_free_questions_searching_as_before():
    assert retrieval_query("500") == "500"
    assert retrieval_query("the and OR") == "the and OR"
    with pytest.raises(SourceQueryInvalid):
        _terms(retrieval_query("the and OR"))


def test_fallback_is_bounded_by_the_same_term_and_byte_caps():
    labels = " ".join(f"[S{index}]" for index in range(1, 41))
    result = retrieval_query(labels)
    assert result == " ".join(f"[S{index}]" for index in range(1, MAX_QUERY_TERMS + 1))
    assert len(terms(result)) == MAX_QUERY_TERMS
    result = retrieval_query(" ".join(["please"] * 200))
    assert 0 < len(result.encode("utf-8")) <= MAX_QUERY_BYTES
    assert set(result.split()) == {"please"}
    # One indivisible piece above the byte cap leaves nothing searchable; the
    # empty result is refused by search instead of being sent unbounded.
    assert retrieval_query("的" * 400) == ""
    assert retrieval_query("500", ("机" * 400,)) == "500"


@pytest.mark.parametrize("question,context", [
    ("Model VA 2.0 Net2.1 14B 2025", ()),
    ("仅基于当前条目的保留文档回答，控制在500字内，并使用 [D1] 等引用", ("Robotics project",)),
    ("Summarize\n[S1, S2] cache-memory in 200 words", ("streaming-cache-memory-note-2",)),
    (" ".join(f"term{index}" for index in range(60)), ("alpha beta",)),
    ("café straße naïve résumé ÅNGSTRÖM", ()),
])
def test_search_retokenizes_the_derived_query_to_the_same_terms(question, context):
    result = retrieval_query(question, context)
    assert _terms(result) == query_terms(result)
    assert len(result.encode("utf-8")) <= MAX_QUERY_BYTES
    assert not any(ord(character) < 32 for character in result)


def test_derivation_is_deterministic_and_accepts_any_iterable_context():
    question = "cache memory [S1]"
    assert retrieval_query(question, iter(["Robotics project"])) == retrieval_query(
        question, ["Robotics project"]) == "Robotics project cache memory"
