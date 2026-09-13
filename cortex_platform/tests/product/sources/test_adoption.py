from __future__ import annotations

import hashlib
import json

import pytest

from cortex_platform.product.sources.adoption import (
    AdoptionEntry,
    build_manifest,
    decode_engine_ref,
    encode_engine_ref,
)
from cortex_platform.product.sources.models import validate_engine_ref


def _entry(
    *,
    paper_dir: str = "20260618-Speculative_Decoding",
    authority: str = "arxiv",
    authority_id: str = "2401.12345",
    official_title: str = "Speculative Decoding",
    content_digest: str = "a" * 64,
) -> AdoptionEntry:
    return AdoptionEntry(
        paper_dir=paper_dir,
        authority=authority,
        authority_id=authority_id,
        official_title=official_title,
        content_digest=content_digest,
    )


class TestEngineRefEncoding:
    def test_a_conforming_directory_embeds_verbatim(self) -> None:
        assert encode_engine_ref("20260618-Speculative_Decoding") == (
            "paper:20260618-Speculative_Decoding"
        )

    def test_a_radar_stub_directory_survives_its_second_colon(self) -> None:
        # radar_index.py writes paper_dir='arxiv:<id>', which the engine_ref
        # grammar forbids verbatim: it allows exactly one colon.
        encoded = encode_engine_ref("arxiv:2401.12345")
        assert encoded.count(":") == 1
        assert validate_engine_ref(encoded, namespace="paper") == encoded
        assert decode_engine_ref(encoded) == "arxiv:2401.12345"

    def test_a_chinese_blog_directory_survives_the_ascii_grammar(self) -> None:
        # _slug keeps Unicode word characters, so a Chinese title yields a CJK
        # paper_dir, while engine_ref and authority_id are ASCII-only.
        paper_dir = "20260619-blog-推测解码与蒸馏"
        encoded = encode_engine_ref(paper_dir)
        assert encoded.isascii()
        assert validate_engine_ref(encoded, namespace="paper") == encoded
        assert decode_engine_ref(encoded) == paper_dir

    def test_every_encoding_round_trips(self) -> None:
        for paper_dir in (
            "20260618-Speculative_Decoding",
            "arxiv:2401.12345",
            "20260619-blog-推测解码与蒸馏",
            "20260101-DreamX-World_1.0_Report",
            "enc",
            "a",
        ):
            encoded = encode_engine_ref(paper_dir)
            assert validate_engine_ref(encoded, namespace="paper") == encoded
            assert decode_engine_ref(encoded) == paper_dir

    @pytest.mark.parametrize(
        "paper_dir",
        [
            # Both are real directories from the operator's live corpus. An
            # accented Latin letter is a Unicode word character, so _slug keeps
            # it and the resulting name is outside the ASCII engine_ref
            # grammar -- the common case for encoding, far more common than the
            # Chinese-title case that motivated it.
            "20260309-MULTI_MARGINAL_TEMPORAL_SCHRÖDINGER_BRIDGE_MATCHING",
            "20260609-On_the_Content_Bias_in_Fréchet_Video_Distance",
        ],
    )
    def test_real_corpus_names_that_need_encoding(self, paper_dir: str) -> None:
        encoded = encode_engine_ref(paper_dir)
        assert encoded.startswith("paper:enc/")
        assert validate_engine_ref(encoded, namespace="paper") == encoded
        assert decode_engine_ref(encoded) == paper_dir

    def test_a_verbatim_directory_can_never_collide_with_an_encoded_one(self) -> None:
        # 'enc/...' is unclaimable verbatim because a directory name cannot
        # contain a path separator.
        verbatim = encode_engine_ref("enc")
        encoded = encode_engine_ref("arxiv:2401.12345")
        assert verbatim == "paper:enc"
        assert encoded.startswith("paper:enc/")
        assert verbatim != encoded

    def test_an_empty_directory_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="paper_dir is invalid"):
            encode_engine_ref("")

    def test_a_directory_name_too_long_to_encode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="paper_dir is too long"):
            encode_engine_ref("我" * 400)

    def test_decoding_refuses_a_foreign_namespace(self) -> None:
        with pytest.raises(ValueError, match="engine_ref is invalid"):
            decode_engine_ref("idea:20260618-Speculative_Decoding")

    def test_decoding_refuses_a_corrupt_encoded_payload(self) -> None:
        with pytest.raises(ValueError, match="engine_ref is invalid"):
            decode_engine_ref("paper:enc/1")

    @pytest.mark.parametrize("length", [1, 3, 6, 9, 11])
    def test_decoding_refuses_a_length_encode_could_never_emit(
        self, length: int
    ) -> None:
        # base32 emits 8 characters per 5 bytes, so a stripped payload length
        # mod 8 is only ever 0, 2, 4, 5 or 7. Accepting the rest would let
        # decode answer for strings encode cannot produce.
        with pytest.raises(ValueError, match="engine_ref is invalid"):
            decode_engine_ref("paper:enc/" + "a" * length)

    def test_every_length_encode_can_emit_still_decodes(self) -> None:
        for size in range(1, 40):
            encoded = encode_engine_ref("\u4e2d" * size)
            assert decode_engine_ref(encoded) == "\u4e2d" * size


class TestManifestBuilding:
    def test_a_manifest_carries_its_entries_and_a_content_digest(self) -> None:
        manifest = build_manifest([_entry()])
        assert len(manifest.entries) == 1
        assert len(manifest.manifest_id) == 64
        assert int(manifest.manifest_id, 16) >= 0

    def test_the_identity_is_the_digest_of_the_canonical_document(self) -> None:
        manifest = build_manifest([_entry()])
        expected = hashlib.sha256(
            json.dumps(
                manifest.to_document(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        assert manifest.manifest_id == expected

    def test_entry_order_does_not_change_the_identity(self) -> None:
        first = _entry(paper_dir="20260101-A", authority_id="2401.00001")
        second = _entry(paper_dir="20260102-B", authority_id="2401.00002")
        assert (
            build_manifest([first, second]).manifest_id
            == build_manifest([second, first]).manifest_id
        )

    def test_a_changed_digest_changes_the_identity(self) -> None:
        assert (
            build_manifest([_entry()]).manifest_id
            != build_manifest([_entry(content_digest="b" * 64)]).manifest_id
        )

    def test_an_empty_manifest_is_refused(self) -> None:
        with pytest.raises(ValueError, match="adoption manifest is empty"):
            build_manifest([])

    def test_two_directories_claiming_one_paper_are_refused_by_name(self) -> None:
        # The SCAIL-2 orphan class: two paper_dirs carrying one arXiv id.
        # sources.canonical_id is UNIQUE, so this must be named at build time
        # rather than failing opaquely inside the commit transaction.
        first = _entry(paper_dir="20260101-Echo_Infinity")
        second = _entry(paper_dir="20260102-Echo_Infinity_v2")
        with pytest.raises(ValueError) as error:
            build_manifest([first, second])
        message = str(error.value)
        assert "duplicate canonical source" in message
        assert "20260101-Echo_Infinity" in message
        assert "20260102-Echo_Infinity_v2" in message

    def test_a_repeated_directory_is_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate paper_dir"):
            build_manifest([_entry(), _entry(authority_id="2401.99999")])

    def test_entries_expose_the_engine_ref_the_commit_will_store(self) -> None:
        entry = _entry(paper_dir="arxiv:2401.12345")
        assert entry.engine_ref == encode_engine_ref("arxiv:2401.12345")

    def test_a_malformed_content_digest_is_refused(self) -> None:
        with pytest.raises(ValueError, match="content_digest is invalid"):
            _entry(content_digest="not-a-digest")

    def test_an_arxiv_version_suffix_is_canonicalized_once(self) -> None:
        versioned = _entry(authority_id="2401.12345v2")
        plain = _entry(authority_id="2401.12345")
        assert versioned.canonical_id == plain.canonical_id
