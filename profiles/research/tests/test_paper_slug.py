"""Regression tests for paper_ingest._slug version-number fidelity.

Bug: the paper_dir slug dropped '.' from titles, so a version number like
"1.0" collapsed to "10" ("DreamX-World 1.0" -> dir "...DreamX-World_10..."),
misrepresenting the paper version in the corpus PRIMARY KEY. The slug must
preserve the decimal point while staying a safe single path component.
"""
import pytest

from cortex_research import paper_ingest


def test_slug_preserves_version_decimal_point():
    # The reported case: "DreamX-World 1.0" must NOT become "DreamX-World_10".
    out = paper_ingest._slug(
        "DreamX-World 1.0: A General-Purpose Interactive World Model"
    )
    assert out == "DreamX-World_1.0_A_General-Purpose_Interactive_World_Model"


@pytest.mark.parametrize(
    "title, expected",
    [
        ("HunyuanVideo 1.5 Technical Report", "HunyuanVideo_1.5_Technical_Report"),
        ("Mamoda2.5: Enhancing Unified Multimodal Model", "Mamoda2.5_Enhancing_Unified_Multimodal_Model"),
        ("Ternary Mamba: W1.58A16 State Space", "Ternary_Mamba_W1.58A16_State_Space"),
        ("GPT-4.5 Technical Report", "GPT-4.5_Technical_Report"),
    ],
)
def test_slug_version_numbers_do_not_merge(title, expected):
    assert paper_ingest._slug(title) == expected


def test_slug_stays_a_safe_path_component():
    # No '.'/'..' component, no leading/hidden-dir dot, no trailing dot.
    assert paper_ingest._slug("...") == "paper"
    assert paper_ingest._slug(".") == "paper"
    assert paper_ingest._slug("..") == "paper"
    assert ".." not in paper_ingest._slug("a..b")
    assert paper_ingest._slug("a..b") == "a.b"
    assert not paper_ingest._slug(".hidden version 2.0").startswith(".")
    assert not paper_ingest._slug("trailing dot v1.0.").endswith(".")
    assert paper_ingest._slug("trailing dot v1.0.") == "trailing_dot_v1.0"


def test_slug_empty_falls_back_to_paper():
    assert paper_ingest._slug("") == "paper"
    assert paper_ingest._slug(":::") == "paper"
