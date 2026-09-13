from __future__ import annotations

import pytest

from cortex_platform.product.sources import (
    CandidateObservation,
    canonicalize_arxiv_id,
    canonicalize_doi,
    canonicalize_locator,
    canonicalize_source_locator,
)


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
