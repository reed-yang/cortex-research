from __future__ import annotations

import pytest

from cortex_platform.product.sources import (
    CandidateObservation,
    canonicalize_arxiv_id,
    canonicalize_doi,
    canonicalize_locator,
    canonicalize_source_locator,
)
from cortex_platform.product.sources.identity import parse_arxiv_capture_payload


def test_arxiv_versions_are_observations_of_one_work() -> None:
    base = canonicalize_arxiv_id("2606.04527")
    versioned = canonicalize_arxiv_id("arXiv:2606.04527v3")

    assert base.canonical_id == versioned.canonical_id == "arxiv:2606.04527"
    assert base.version is None
    assert versioned.version == 3
    observation = CandidateObservation(
        claim_kind="arxiv",
        authority="arxiv",
        authority_id="2606.04527v3",
        official_title="Echo-Infinity",
    )
    assert observation.canonical_id == "arxiv:2606.04527"


def test_locator_normalization_is_conservative_and_never_uses_query_ids() -> None:
    absolute = canonicalize_locator("https://arxiv.org/pdf/2607.07675.pdf")
    abstract = canonicalize_locator("https://www.arxiv.org/abs/2607.07675v2")

    assert absolute.canonical_id == abstract.canonical_id == "arxiv:2607.07675"
    assert absolute.normalized_locator == "https://arxiv.org/abs/2607.07675"
    assert abstract.version == 2

    rejected = (
        "https://arxiv.org/?id=2607.07675",
        "https://arxiv.org/redirect/2607.07675",
        "https://arxiv.org/pdf/%EF%BC%92%EF%BC%96%EF%BC%90%EF%BC%97.07675",
        "https://user@arxiv.org/abs/2607.07675",
        "https://arxiv.org:443/abs/2607.07675",
        "http://arxiv.org/abs/2607.07675",
    )
    for locator in rejected:
        with pytest.raises(ValueError):
            canonicalize_locator(locator)


def test_doi_normalization_is_authority_scoped_and_conservative() -> None:
    raw = canonicalize_doi("doi:10.48550/ARXIV.2606.04527")
    url = canonicalize_doi("https://doi.org/10.48550/arXiv.2606.04527")
    assert raw.canonical_id == url.canonical_id
    assert raw.canonical_id == "doi:10.48550/arxiv.2606.04527"
    for value in (
        "https://doi.org/?doi=10.48550/arxiv.2606.04527",
        "https://doi.org/10.48550%2Farxiv.2606.04527",
        "http://doi.org/10.48550/arxiv.2606.04527",
        "doi:１０.48550/arxiv.2606.04527",
    ):
        with pytest.raises(ValueError):
            canonicalize_doi(value)
    with pytest.raises(ValueError, match="DOI"):
        CandidateObservation(
            claim_kind="doi",
            authority="doi",
            authority_id="not-a-doi",
            official_title="Invalid",
        )


def test_local_file_candidate_requires_a_content_hash_identity() -> None:
    candidate = CandidateObservation(
        claim_kind="local_file",
        authority="sha256",
        authority_id="A" * 64,
        official_title="Local paper",
        locator="cortex://imports/papers/local.pdf",
    )
    assert candidate.canonical_id == f"sha256:{'a' * 64}"
    with pytest.raises(ValueError, match="SHA-256"):
        CandidateObservation(
            claim_kind="local_file",
            authority="sha256",
            authority_id="local.pdf",
            official_title="Local paper",
        )


@pytest.mark.parametrize(
    "value,sha256,claim_kind,canonical_id,version",
    [
        ("2607.07675v2", None, "arxiv", "arxiv:2607.07675", 2),
        (
            "https://arxiv.org/pdf/2607.07675.pdf",
            None,
            "url",
            "arxiv:2607.07675",
            None,
        ),
        ("doi:10.48550/ARXIV.2606.04527", None, "doi", "doi:10.48550/arxiv.2606.04527", None),
        (
            "https://doi.org/10.48550/arXiv.2606.04527",
            None,
            "url",
            "doi:10.48550/arxiv.2606.04527",
            None,
        ),
        (
            "cortex://imports/papers/local.pdf",
            "a" * 64,
            "local_file",
            f"sha256:{'a' * 64}",
            None,
        ),
    ],
)
def test_top_level_locator_returns_claim_kind_and_canonical_identity(
    value: str,
    sha256: str | None,
    claim_kind: str,
    canonical_id: str,
    version: int | None,
) -> None:
    locator = canonicalize_source_locator(value, local_sha256=sha256)
    assert locator.claim_kind == claim_kind
    assert locator.canonical_id == canonical_id
    assert locator.version == version


def test_local_locator_without_hash_and_ambiguous_tokens_fail_closed() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        canonicalize_source_locator("cortex://imports/papers/local.pdf")
    for value in ("Echo-Infinity", "/tmp/paper.pdf", "https://example.com/paper"):
        with pytest.raises(ValueError):
            canonicalize_source_locator(value)


def test_title_and_url_candidates_remain_distinct_even_with_similar_prose() -> None:
    title = CandidateObservation(
        claim_kind="title",
        authority="arxiv",
        authority_id="2606.04527",
        official_title="Echo-Infinity",
        locator="https://arxiv.org/abs/2606.04527",
    )
    url = CandidateObservation(
        claim_kind="url",
        authority="arxiv",
        authority_id="2607.07675",
        official_title="Echo-Infinity related video model",
        locator="https://arxiv.org/abs/2607.07675",
    )

    assert title.canonical_id != url.canonical_id
    assert title.claim_kind != url.claim_kind


def test_unicode_authority_aliases_are_rejected() -> None:
    with pytest.raises(ValueError, match="ASCII"):
        CandidateObservation(
            claim_kind="title",
            authority="arxiv",
            authority_id="２６０６.04527",
            official_title="Echo-Infinity",
        )


def test_candidate_text_and_evidence_are_closed_bounded_json() -> None:
    for field, value in (
        ("official_title", "Echo\x00secret"),
        ("locator", "https://arxiv.org/abs/2606.04527\nsecret"),
    ):
        kwargs = {
            "claim_kind": "title",
            "authority": "arxiv",
            "authority_id": "2606.04527",
            "official_title": "Echo-Infinity",
            "locator": None,
        }
        kwargs[field] = value
        with pytest.raises(ValueError, match="control"):
            CandidateObservation(**kwargs)

    invalid_evidence = (
        {"resolver_secret": "hidden"},
        {"observation": {"nested": "not allowed"}},
        {"observation": object()},
        {"observation": "x" * 501},
        {"confidence": float("nan")},
    )
    for evidence in invalid_evidence:
        with pytest.raises(ValueError, match="evidence"):
            CandidateObservation(
                claim_kind="title",
                authority="arxiv",
                authority_id="2606.04527",
                official_title="Echo-Infinity",
                evidence=evidence,
            )


@pytest.mark.parametrize("unsafe", ["\x7f", "\x85", "\u202e", "\ue000"])
def test_all_unicode_control_format_and_private_categories_are_rejected(
    unsafe: str,
) -> None:
    with pytest.raises(ValueError, match="Unicode"):
        CandidateObservation(
            claim_kind="title",
            authority="arxiv",
            authority_id="2606.04527",
            official_title=f"Echo{unsafe}Infinity",
        )
    with pytest.raises(ValueError, match="evidence"):
        CandidateObservation(
            claim_kind="title",
            authority="arxiv",
            authority_id="2606.04527",
            official_title="Echo-Infinity",
            evidence={"observation": f"unsafe{unsafe}"},
        )


def test_source_text_uses_nfc_and_arxiv_version_is_explicit() -> None:
    candidate = CandidateObservation(
        claim_kind="arxiv",
        authority="arxiv",
        authority_id="2606.04527v3",
        official_title="Cafe\u0301 memory",
    )
    assert candidate.official_title == "Caf\u00e9 memory"
    assert candidate.version == 3
    assert candidate.to_record()["version"] == 3


@pytest.mark.parametrize("version", [True, False, 0, -1, 1.5, "2"])
def test_candidate_version_is_a_strict_positive_integer(version: object) -> None:
    with pytest.raises(ValueError, match="version"):
        CandidateObservation(
            claim_kind="arxiv",
            authority="arxiv",
            authority_id="2606.04527",
            official_title="Echo-Infinity",
            version=version,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="version"):
        CandidateObservation(
            claim_kind="doi",
            authority="doi",
            authority_id="10.48550/arxiv.2606.04527",
            official_title="Echo-Infinity",
            version=1,
        )


# -- Capture payloads: one arXiv token plus the operator's own words ---------

_WORK = "2601.00042"


def _legacy_child_paper(payload: str) -> str:
    """The paper the ingest child took from a raw payload before the parser.

    A Capture dispatched before the parser handed the child its raw payload,
    so every accepted payload must name the same paper both ways.
    """

    from cortex_research.arxiv_client import _strip_version

    return _strip_version(payload)


@pytest.mark.parametrize(
    "token",
    [
        _WORK,
        f"{_WORK}v2",
        f"arXiv:{_WORK}",
        f"ARXIV:{_WORK}v3",
        f"https://arxiv.org/abs/{_WORK}",
        f"https://www.arxiv.org/abs/{_WORK}v2",
        f"https://arxiv.org/pdf/{_WORK}",
        f"https://arxiv.org/pdf/{_WORK}.pdf",
        f"https://arxiv.org/pdf/{_WORK}v2.pdf",
        f"https://arxiv.org/html/{_WORK}v1",
        f"HTTPS://ArXiv.org/abs/{_WORK}",
        f"http://arxiv.org/abs/{_WORK}",
        f"arxiv.org/abs/{_WORK}",
        f"www.arxiv.org/pdf/{_WORK}v1",
        f"https://arxiv.org/abs/{_WORK}?context=cs.LG",
        f"https://arxiv.org/abs/{_WORK}#section-3",
        # A query never names the paper, even when it holds another ID.
        f"https://arxiv.org/abs/{_WORK}?ref=2602.00001",
    ],
)
def test_capture_payload_accepts_every_supported_arxiv_token(token: str) -> None:
    parsed = parse_arxiv_capture_payload(token)

    assert parsed.work_id == _WORK
    assert parsed.canonical_id == f"arxiv:{_WORK}"
    assert parsed.note == ""
    assert _legacy_child_paper(token) == _WORK


@pytest.mark.parametrize(
    "payload,note",
    [
        (f"https://arxiv.org/abs/{_WORK} 请总结方法部分", "请总结方法部分"),
        (f"{_WORK} 请总结方法部分", "请总结方法部分"),
        (f"{_WORK}\n请重点看实验\n以及局限", "请重点看实验\n以及局限"),
        # Prose before the locator, whitespace-separated, is the same note.
        (f"这篇值得读 https://arxiv.org/abs/{_WORK}", "这篇值得读"),
        (f"这篇值得读　https://arxiv.org/abs/{_WORK}", "这篇值得读"),
        (
            f"先看这篇\nhttps://arxiv.org/abs/{_WORK}\n重点：方法",
            "先看这篇\n\n重点：方法",
        ),
        (f"  {_WORK}  第一行\n\n  第二行  ", "第一行\n\n  第二行"),
    ],
)
def test_capture_payload_keeps_the_surrounding_text_as_the_note(
    payload: str, note: str
) -> None:
    parsed = parse_arxiv_capture_payload(payload)

    assert parsed.work_id == _WORK
    assert parsed.note == note
    assert _legacy_child_paper(payload) == _WORK


@pytest.mark.parametrize(
    "payload,note",
    [
        # Forms the consumer imported before the parser existed.
        (f"看看 https://arxiv.org/abs/{_WORK}，然后再说", "看看 ，然后再说"),
        (f"https://arxiv.org/abs/{_WORK}。", "。"),
        (f"(https://arxiv.org/abs/{_WORK})", "()"),
        (f"<https://arxiv.org/abs/{_WORK}>", "<>"),
        (f"https://arxiv.org/abs/{_WORK}, worth reading", ", worth reading"),
        (f"论文：https://arxiv.org/abs/{_WORK}", "论文："),
        (f"https://arxiv.org/abs/{_WORK}v2.", "."),
        (f"[x](https://arxiv.org/abs/{_WORK})", "[x]()"),
        # Any non-ASCII character ends a token, so CJK text may touch it.
        (f"看看{_WORK}的方法", "看看的方法"),
        (f"看看 {_WORK}，然后再说", "看看 ，然后再说"),
        (f"论文{_WORK}", "论文"),
        (f"请看https://arxiv.org/abs/{_WORK}", "请看"),
        (f"论文{_WORK} https://arxiv.org/abs/{_WORK}", "论文"),
        # ASCII brackets and quotes end a token; `.,;:!?` is trimmed from
        # its two ends and stays in the note.
        (f"({_WORK})", "()"),
        (f"{_WORK},", ","),
        (f'"arXiv:{_WORK}"', '""'),
        (f"'{_WORK}v2'", "''"),
        (f"论文:arXiv:{_WORK};", "论文:;"),
        (f"https://arxiv.org/abs/{_WORK}?", "?"),
        (f"https://arxiv.org/abs/{_WORK}?ref=2602.00001.", "."),
        (f"<a href=\"https://arxiv.org/abs/{_WORK}\">", '<a href="">'),
        (
            f"[https://arxiv.org/abs/{_WORK}](https://arxiv.org/abs/{_WORK})",
            "[]()",
        ),
        # A link on another host, set apart by full-width punctuation, is
        # note text and does not hide the paper after it.
        (
            f"https://example.com/post，https://arxiv.org/abs/{_WORK}",
            "https://example.com/post，",
        ),
    ],
)
def test_capture_payload_finds_a_locator_next_to_punctuation_or_cjk_text(
    payload: str, note: str
) -> None:
    parsed = parse_arxiv_capture_payload(payload)

    assert parsed.work_id == _WORK
    assert parsed.note == note
    assert _legacy_child_paper(payload) == _WORK


def test_capture_payload_note_is_not_bounded_by_the_explicit_note_limit() -> None:
    instructions = "请逐段解释。" * 500
    parsed = parse_arxiv_capture_payload(f"{_WORK} {instructions}")

    assert len(instructions) > 2_000
    assert parsed.note == instructions


def test_the_same_paper_named_twice_is_one_paper() -> None:
    parsed = parse_arxiv_capture_payload(
        f"{_WORK} compare with https://arxiv.org/abs/{_WORK}v2"
    )

    assert parsed.work_id == _WORK
    assert parsed.note == "compare with"


def test_the_same_paper_inside_the_note_is_still_one_paper() -> None:
    # ASCII letters do not end a token, so the first ID is note text.
    parsed = parse_arxiv_capture_payload(
        f"paper{_WORK}x https://arxiv.org/abs/{_WORK}"
    )

    assert parsed.work_id == _WORK
    assert parsed.note == f"paper{_WORK}x"


@pytest.mark.parametrize(
    "payload",
    [
        f"{_WORK} versus 2602.00001",
        f"https://arxiv.org/abs/{_WORK} 对比 arXiv:2602.00001",
        f"这篇 https://arxiv.org/pdf/{_WORK} 和 https://arxiv.org/abs/2602.00001",
        # An ID-shaped number for another paper in the note is a second paper
        # even when it touches other text: the ingest child once took the
        # first such number as the paper, so accepting it would let a reread
        # of an older dispatch name a different paper than the one it wrote.
        f"论文2602.00001 https://arxiv.org/abs/{_WORK}",
        f"https://arxiv.org/abs/{_WORK} 对比(2602.00001)",
        f"{_WORK} 和论文2602.00001v2比较",
        f"看看{_WORK}和2602.00001的区别",
        f"(https://arxiv.org/abs/{_WORK})(2602.00001)",
        f"foo2602.00001bar https://arxiv.org/abs/{_WORK}",
        f"https://arxiv.org/abs/{_WORK} foo2602.00001bar",
        # A bracket inside a query ends the token, so what follows it is no
        # longer part of the ignored query.
        f"https://arxiv.org/abs/{_WORK}?ref=(2602.00001)",
        # Full-width digits end a token but are digits to the ingest child's
        # pattern, which would read `１２３４.2601` or `2601.0004２` here.
        f"１２３４.{_WORK}",
        f"https://arxiv.org/abs/{_WORK} １２３４.{_WORK}",
        "2601.0004２",
    ],
)
def test_two_different_papers_are_refused(payload: str) -> None:
    with pytest.raises(ValueError, match="more than one"):
        parse_arxiv_capture_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "   \n ",
        "a note with no paper in it",
        "https://example.com/some/blog/post",
        # Misleading hosts that merely contain an arXiv path.
        f"https://example.com/arxiv.org/abs/{_WORK}",
        f"https://notarxiv.org/abs/{_WORK}",
        f"https://arxiv.org.example.net/abs/{_WORK}",
        f"https://arxiv.org./abs/{_WORK}",
        f"example.com/arxiv.org/abs/{_WORK}",
        # Userinfo, ports, encoded paths and other schemes.
        f"https://user@arxiv.org/abs/{_WORK}",
        f"https://arxiv.org:443/abs/{_WORK}",
        f"https://arxiv.org:/abs/{_WORK}",
        "https://arxiv.org/abs/2601%2E00042",
        f"ftp://arxiv.org/abs/{_WORK}",
        f"https:/arxiv.org/abs/{_WORK}",
        # Malformed versions and path suffixes.
        f"{_WORK}v0",
        f"{_WORK}v01",
        f"{_WORK}v",
        f"https://arxiv.org/abs/{_WORK}v0",
        f"https://arxiv.org/abs/{_WORK}.pdf",
        f"https://arxiv.org/html/{_WORK}.pdf",
        f"https://arxiv.org/abs/{_WORK}/",
        f"https://arxiv.org/abs/{_WORK}/extra",
        f"https://arxiv.org/list/{_WORK}",
        f"https://arxiv.org/ABS/{_WORK}",
        "2601.123",
        "2601.123456",
        "２６０１.00042",
        "hep-th/9901001",
        # ASCII text joined to an ID or link makes it prose, not a locator.
        f"paper{_WORK}",
        f"see:https://arxiv.org/abs/{_WORK}",
        # Brackets, quotes and CJK text around a link do not change its host.
        f"https://arxiv.org.example.com/abs/{_WORK}",
        f"https://example-arxiv.org/abs/{_WORK}",
        f"(https://arxiv.org.example.com/abs/{_WORK})",
        f"(https://user@arxiv.org/abs/{_WORK})",
        f"<https://arxiv.org:443/abs/{_WORK}>",
        f'"https://arxiv.org./abs/{_WORK}"',
        f"(https://example.com/arxiv.org/abs/{_WORK})",
        f"看看https://example-arxiv.org/abs/{_WORK}，",
        f"[x](ftp://arxiv.org/abs/{_WORK})",
        "(https://arxiv.org/abs/2601%2E00042)",
        f"(https://arxiv.org/abs/{_WORK}/extra)",
        # A bracket or quote inside another host's link does not start a new
        # locator: the text after it is still part of that link.
        f"https://example.com/wiki/(arxiv.org/abs/{_WORK})",
        f"https://example.com/wiki/(https://arxiv.org/abs/{_WORK})",
        f"https://user@(arxiv.org/abs/{_WORK})",
        f'https://example.com/?u="https://arxiv.org/abs/{_WORK}"',
        f"https://example.com/wiki/({_WORK})",
        f"example.com/x/(arxiv.org/abs/{_WORK})",
        # Control characters a URL parser would silently delete.
        f"(\x01https://arxiv.org/abs/{_WORK})",
        f"看看\x01https://arxiv.org/abs/{_WORK}",
        f"\x01https://arxiv.org/abs/{_WORK}",
        f"\x00https://arxiv.org/abs/{_WORK}",
        f"\x1bhttps://arxiv.org/abs/{_WORK}",
        f"https://arxiv.org/abs/{_WORK}\x01",
        f"\x01arxiv.org/abs/{_WORK}",
        f"https://arxiv.org/abs/{_WORK}?ref=\x01",
        f"\x01{_WORK}",
    ],
)
def test_capture_payload_refuses_everything_but_a_clean_arxiv_token(
    payload: str,
) -> None:
    with pytest.raises(ValueError):
        parse_arxiv_capture_payload(payload)


def test_capture_payload_refuses_a_non_string() -> None:
    with pytest.raises(ValueError):
        parse_arxiv_capture_payload(None)  # type: ignore[arg-type]


def test_capture_parsing_leaves_the_source_locator_forms_unchanged() -> None:
    # The capture parser is broader than the source-intent locator on
    # purpose; the locator keeps refusing http, query and html forms, and
    # the top-level parser keeps refusing scheme-less URLs and prose.
    for value in (
        f"http://arxiv.org/abs/{_WORK}",
        f"https://arxiv.org/abs/{_WORK}?context=cs.LG",
        f"https://arxiv.org/html/{_WORK}v1",
    ):
        with pytest.raises(ValueError):
            canonicalize_locator(value)
    for value in (f"arxiv.org/abs/{_WORK}", f"{_WORK} 请总结"):
        with pytest.raises(ValueError):
            canonicalize_source_locator(value)
