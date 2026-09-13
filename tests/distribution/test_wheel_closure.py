from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from distribution import wheel_closure as _wheel_closure_module
from distribution.wheel_closure import (
    MAX_METADATA_BYTES,
    MAX_REQUIREMENT_EXTRAS,
    MAX_WHEEL_MEMBERS,
    ClosureEntry,
    WheelClosureError,
    compare,
    evaluate_marker,
    normalize_name,
    parse_marker,
    parse_requirements_hashes,
    parse_version,
    prove_closure,
    read_wheel_metadata,
    satisfies,
    split_requirement,
    wheel_tags_compatible,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Foo.Bar", "foo-bar"),
        ("foo_bar", "foo-bar"),
        ("FOO--BAR", "foo-bar"),
        ("cortex-platform-memory", "cortex-platform-memory"),
    ],
)
def test_names_normalize_per_pep_503(value: str, expected: str) -> None:
    assert normalize_name(value) == expected


@pytest.mark.parametrize("value", ["", "-leading", "trailing-", "has space", "has/slash"])
def test_an_unsupported_name_is_refused(value: str) -> None:
    with pytest.raises(WheelClosureError, match="distribution name"):
        normalize_name(value)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.0", "1.0.0", 0),
        ("1.0", "1.0.1", -1),
        ("1.0.1", "1.0", 1),
        ("1!1.0", "2.0", 1),
        ("2.0", "1!1.0", -1),
        ("1.0a1", "1.0b1", -1),
        ("1.0b1", "1.0rc1", -1),
        ("1.0rc1", "1.0", -1),
        ("1.0", "1.0.post1", -1),
        ("1.0.dev1", "1.0a1", -1),
        ("1.0.dev1", "1.0", -1),
        ("1.0", "1.0+local", -1),
        ("1.0+a", "1.0+b", -1),
        ("1.0+1", "1.0+a", 1),
        ("1.0alpha1", "1.0a1", 0),
        ("1.0c1", "1.0rc1", 0),
        ("1.0-1", "1.0.post1", 0),
        ("1.0rev1", "1.0.post1", 0),
        ("v1.0", "1.0", 0),
        ("1.0.0.0.1", "1.0", 1),
        # A local-version segment that is a proper prefix of another sorts
        # lower than it, per PEP 440's local-version comparison rule.
        ("1.0+a", "1.0+a.1", -1),
        ("1.0+a.1", "1.0+a", 1),
        # A "dev" substring inside an unrelated local-version segment must
        # not be mistaken for a dev-release marker (regression: the parser
        # previously used a bare substring search for "dev").
        ("1.0.dev0", "1.0+abcdev", -1),
        ("1.0", "1.0+abcdev", -1),
        ("1.0+abcdev", "1.0", 1),
    ],
)
def test_pep440_ordering(left: str, right: str, expected: int) -> None:
    assert compare(left, right) == expected


def test_a_dev_looking_local_segment_is_not_treated_as_a_dev_release() -> None:
    """Regression: `1.0+abcdev` is release `1.0` with local version `abcdev`,
    not a dev release — `dev` only counts when it is its own version segment,
    not merely a substring anywhere in the version text."""

    parsed = parse_version("1.0+abcdev")

    assert parsed.dev is None
    assert parsed.local is not None and parsed.local[0][1] == "abcdev"


@pytest.mark.parametrize(
    "value", ["", "1.0.0.", "1..0", "abc", "1.0-beta-gamma", "1.0+", "1 . 0", "1.0+LOCAL!"]
)
def test_an_unparseable_version_is_refused_not_guessed(value: str) -> None:
    with pytest.raises(WheelClosureError, match="version is unsupported"):
        compare(value, "1.0")


@pytest.mark.parametrize(
    ("version", "specifier", "expected"),
    [
        ("1.2.3", "==1.2.3", True),
        ("1.2.3", "==1.2.4", False),
        ("1.2.3", "!=1.2.4", True),
        ("1.2.3", "==1.2.*", True),
        ("1.3.0", "==1.2.*", False),
        ("1.2.3", ">=1.2,<2.0", True),
        ("2.0.0", ">=1.2,<2.0", False),
        ("1.4.0", "~=1.2", True),
        ("2.0.0", "~=1.2", False),
        ("1.2.5", "~=1.2.3", True),
        ("1.3.0", "~=1.2.3", False),
        ("1.2.3+local", ">=1.2.3", True),
        ("1.2.3", "===1.2.3", True),
        ("1.2.3", "=== 1.2.3", True),
        ("1.2.3", "===1.2.3+x", False),
        ("2.0.0.post1", ">2.0.0", False),
        ("2.0.0.post1", ">2.0.0.post0", True),
        ("2.0.0rc1", "<2.0.0", False),
        ("2.0.0rc1", "<2.0.0rc2", True),
        ("2.0.0rc1", ">=1.0", False),
        ("2.0.0rc1", "==2.0.0rc1", True),
    ],
)
def test_specifier_satisfaction(version: str, specifier: str, expected: bool) -> None:
    assert satisfies(version, specifier) is expected


@pytest.mark.parametrize("specifier", ["1.2.3", "=1.2.3", "~=1", "~=1.2+local", ">1.2.*"])
def test_an_unsupported_specifier_is_refused(specifier: str) -> None:
    with pytest.raises(WheelClosureError, match="specifier is unsupported"):
        satisfies("1.2.3", specifier)


def test_a_specifier_whose_version_is_unparseable_is_refused() -> None:
    with pytest.raises(WheelClosureError, match="unsupported"):
        satisfies("1.2.3", "<>1.0")


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("sys_platform == 'darwin'", True),
        ("sys_platform == 'win32'", False),
        ("sys_platform != 'win32'", True),
        ("platform_machine == 'arm64'", True),
        ("implementation_name != 'PyPy'", True),
        ("python_full_version < '3.12'", False),
        ("python_full_version >= '3.12'", True),
        ("python_version >= '3.14'", True),
        ("python_version > '3.14'", False),
        ("extra == 'strategy'", False),
        ("extra != 'strategy'", True),
        ("os_name == 'posix' and sys_platform == 'darwin'", True),
        ("os_name == 'nt' or sys_platform == 'darwin'", True),
        ("os_name == 'nt' or sys_platform == 'win32'", False),
        ("sys_platform != 'cygwin' and sys_platform != 'emscripten'", True),
        ("(os_name == 'nt' or sys_platform == 'darwin') and platform_machine == 'arm64'", True),
        ("os_name == 'nt' or sys_platform == 'darwin' and platform_machine == 'x86_64'", False),
        ("'win' in sys_platform", True),
        ("'win' not in sys_platform", False),
        ("'linux' in sys_platform", False),
        ("'linux' not in sys_platform", True),
        ("'darwin' in sys_platform", True),
        ("platform_system == 'Darwin'", True),
        ("python_version == '3.14'", True),
    ],
)
def test_marker_evaluation(marker: str, expected: bool) -> None:
    assert evaluate_marker(marker) is expected


def test_and_binds_tighter_than_or() -> None:
    """`A or B and C` must parse as `A or (B and C)`, not `(A or B) and C`."""

    assert evaluate_marker("sys_platform == 'darwin' or os_name == 'nt' and os_name == 'nt'") is True


@pytest.mark.parametrize(
    "marker",
    [
        "platform_release == '24.0'",
        "platform_version == 'x'",
        "unknown_variable == 'x'",
        "sys_platform ==",
        "sys_platform 'darwin'",
        "(sys_platform == 'darwin'",
        "sys_platform == 'darwin')",
        "sys_platform == 'darwin' and",
        "extra < 'x'",
        "sys_platform =~ 'darwin'",
        "",
    ],
)
def test_an_undecidable_marker_is_refused_not_defaulted(marker: str) -> None:
    with pytest.raises(WheelClosureError):
        evaluate_marker(marker)


@pytest.mark.parametrize("marker", ["platform_release == '24.0'", "platform_version == 'x'"])
def test_platform_release_and_version_are_refused_with_the_contract_message(marker: str) -> None:
    """§4.3 requires the message to contain this exact phrase, not just any error."""

    with pytest.raises(WheelClosureError, match="unsupported requirement marker"):
        evaluate_marker(marker)


def test_compatible_release_operator_is_supported_for_a_versioned_marker_variable() -> None:
    """`~=` is in the supported grammar (§4.3) and must work for the
    version-valued marker variables, not just inside `satisfies`."""

    assert evaluate_marker("python_version ~= '3.13'") is True
    assert evaluate_marker("python_version ~= '2.7'") is False


def test_parse_marker_round_trips_through_evaluate_marker() -> None:
    """`parse_marker` is part of the public API independent of `evaluate_marker`."""

    tree = parse_marker("sys_platform == 'darwin' and python_version >= '3.14'")

    assert tree == ("and", ("compare", "==", ("variable", "sys_platform"), ("literal", "darwin")), (
        "compare",
        ">=",
        ("variable", "python_version"),
        ("literal", "3.14"),
    ))


@pytest.mark.parametrize(
    ("python_tag", "abi_tag", "platform_tag", "expected"),
    [
        ("py3", "none", "any", True),
        ("cp314", "none", "any", True),
        ("cp313", "none", "any", False),
        ("py3", "none", "macosx_11_0_arm64", True),
        ("cp314", "cp314", "macosx_12_0_arm64", True),
        ("cp313", "cp313", "macosx_12_0_arm64", False),
        ("cp39", "abi3", "macosx_11_0_universal2", True),
        ("cp315", "abi3", "macosx_11_0_universal2", False),
        ("cp314", "cp314t", "macosx_11_0_arm64", False),
        ("cp313.cp314", "cp314", "macosx_11_0_arm64", True),
        ("py2.py3", "none", "any", True),
        ("cp314", "cp314", "macosx_27_0_arm64", False),
        ("cp314", "cp314", "macosx_26_0_arm64", True),
        ("cp314", "cp314", "manylinux_2_17_x86_64", False),
        ("cp314", "cp314", "macosx_11_0_x86_64", False),
        ("py3", "none", "any.macosx_27_0_arm64", True),
    ],
)
def test_tag_compatibility(
    python_tag: str, abi_tag: str, platform_tag: str, expected: bool
) -> None:
    assert wheel_tags_compatible(python_tag, abi_tag, platform_tag, major=3, minor=14) is expected


# Contract §10 A14: claude-agent-sdk 0.2.126 ships py3-none-macosx_11_0_arm64,
# so its `none` ABI must not bypass the independent platform-tag decision.
@pytest.mark.parametrize(
    ("platform_tag", "expected"),
    [
        ("macosx_11_0_arm64", True),
        ("macosx_11_0_x86_64", False),
        ("any", True),
    ],
)
def test_claude_agent_sdk_none_abi_still_obeys_the_platform_tag(
    platform_tag: str, expected: bool
) -> None:
    assert wheel_tags_compatible("py3", "none", platform_tag, major=3, minor=14) is expected


@pytest.mark.parametrize("tag", ["cp 314", "cp314-", "cp314/x"])
def test_an_unparseable_tag_is_refused(tag: str) -> None:
    with pytest.raises(WheelClosureError, match="artifact tag is unsupported"):
        wheel_tags_compatible(tag, "none", "any", major=3, minor=14)


def _wheel(directory: Path, name: str, version: str, *, requires: tuple[str, ...] = (), extra_metadata: int = 1) -> Path:
    normalized = name.replace("-", "_")
    path = directory / f"{normalized}-{version}-py3-none-any.whl"
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata.extend(f"Requires-Dist: {value}" for value in requires)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(extra_metadata):
            suffix = "" if index == 0 else f"-copy{index}"
            archive.writestr(
                f"{normalized}{suffix}-{version}.dist-info/METADATA", "\n".join(metadata) + "\n"
            )
    return path


def test_wheel_metadata_is_read_from_exactly_one_dist_info(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path, "Foo.Bar", "1.2.3", requires=("baz>=1.0 ; sys_platform == 'darwin'",))

    metadata = read_wheel_metadata(wheel)

    assert metadata.name == "foo-bar"
    assert metadata.version == "1.2.3"
    assert metadata.requires_dist == ("baz>=1.0 ; sys_platform == 'darwin'",)


def test_a_wheel_with_two_metadata_members_is_refused(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path, "foo", "1.0", extra_metadata=2)

    with pytest.raises(WheelClosureError, match="exactly one METADATA"):
        read_wheel_metadata(wheel)


def test_a_wheel_with_no_metadata_member_is_refused(tmp_path: Path) -> None:
    """§5.2: "more than one, or none, is an error" — the zero case, not just the
    duplicate case, must be refused with the same closed-set message."""

    path = tmp_path / "foo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("foo-1.0.data/scripts/foo", "not metadata")

    with pytest.raises(WheelClosureError, match="exactly one METADATA"):
        read_wheel_metadata(path)


def test_a_wheel_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken-1.0-py3-none-any.whl"
    path.write_bytes(b"not a zip")

    with pytest.raises(WheelClosureError, match="unreadable"):
        read_wheel_metadata(path)


def test_a_wheel_with_an_absurd_member_count_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2: "refuse a zip with an absurd member count" — bounded via a low
    ceiling rather than actually constructing 50,000+ zip members."""

    monkeypatch.setattr(_wheel_closure_module, "MAX_WHEEL_MEMBERS", 1)
    wheel = _wheel(tmp_path, "foo", "1.0", extra_metadata=2)

    with pytest.raises(WheelClosureError, match="member count is unsafe"):
        read_wheel_metadata(wheel)


def test_an_oversized_metadata_member_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2: "refuse ... an oversized METADATA member"."""

    monkeypatch.setattr(_wheel_closure_module, "MAX_METADATA_BYTES", 4)
    wheel = _wheel(tmp_path, "foo", "1.0")

    with pytest.raises(WheelClosureError, match="METADATA is oversized"):
        read_wheel_metadata(wheel)


def test_read_wheel_metadata_never_extracts_to_disk(tmp_path: Path) -> None:
    """The bundle verifier that imports this module runs against unsigned,
    untrusted bytes — the METADATA member is read straight from the zip into
    memory and no member is ever written out under `tmp_path`."""

    wheel = _wheel(tmp_path, "foo", "1.0", requires=("bar>=1.0",))
    before = set(tmp_path.iterdir())

    metadata = read_wheel_metadata(wheel)

    assert metadata.name == "foo"
    assert set(tmp_path.iterdir()) == before


def test_requirement_extras_are_parsed_and_normalized() -> None:
    """The real closure carries `fastmcp-slim[client,server]`.

    Extras were refused outright, on the correct reasoning that ignoring them
    would prove something other than what pip installs. But refusing them made
    every real bundle unverifiable, so they are parsed instead. PEP 685
    normalizes an extra exactly as a distribution name, or `Client` and
    `client` would select different dependencies.
    """

    assert split_requirement("fastmcp-slim[client,server]==3.3.1") == (
        "fastmcp-slim",
        ("client", "server"),
        "==3.3.1",
        "",
    )
    assert split_requirement("httpx[Socks_A]>=0.28")[1] == ("socks-a",)
    assert split_requirement("httpx[b,a,b]>=0.28")[1] == ("a", "b")


def test_a_parenthesised_specifier_is_accepted() -> None:
    """Metadata 2.1's form, still shipped by real wheels.

    PEP 508 is `versionspec = ('(' version_many ')') | version_many`. Five
    distributions in the real closure use the parenthesised spelling —
    `pexpect: ptyprocess (>=0.5)`, `rich: pygments (>=2.13.0,<3.0.0)` — so
    refusing it refused them.
    """

    assert split_requirement("ptyprocess (>=0.5)") == ("ptyprocess", (), ">=0.5", "")
    assert split_requirement("pygments (>=2.13.0,<3.0.0)")[2] == ">=2.13.0,<3.0.0"
    assert split_requirement('requests (>=2.31.0,<3.0.0) ; extra == "requests"') == (
        "requests",
        (),
        ">=2.31.0,<3.0.0",
        'extra == "requests"',
    )


@pytest.mark.parametrize("value", ["httpx (>=1", "httpx >=1)", "httpx ((>=1))"])
def test_an_unbalanced_specifier_is_refused(value: str) -> None:
    with pytest.raises(WheelClosureError, match="requirement is unsupported"):
        split_requirement(value)


def test_an_absurd_extras_list_is_refused() -> None:
    extras = ",".join(f"e{index}" for index in range(MAX_REQUIREMENT_EXTRAS + 1))
    with pytest.raises(WheelClosureError, match="too many extras"):
        split_requirement(f"httpx[{extras}]>=0.28")


def test_requirement_splitting() -> None:
    assert split_requirement("httpx>=0.28.0 ; sys_platform == 'darwin'") == (
        "httpx",
        (),
        ">=0.28.0",
        "sys_platform == 'darwin'",
    )
    assert split_requirement("anyio") == ("anyio", (), "", "")


def test_an_extra_pulls_in_the_dependencies_it_gates() -> None:
    """`B[client]` must reach B's `extra == "client"` requirements, and no more.

    The same distribution is traversed once per requested extra plus once for
    its base requirements, so visiting it by name alone would drop whichever
    dependency set was seen second.
    """

    entries = [
        _entry("root", "1.0", "server-lib[client]"),
        _entry(
            "server-lib",
            "2.0",
            "always-needed",
            'client-only ; extra == "client"',
            'server-only ; extra == "server"',
        ),
        _entry("always-needed", "1.0"),
        _entry("client-only", "1.0"),
    ]

    proof = prove_closure(entries, ["root"])

    assert not proof.missing and not proof.unsatisfied
    assert proof.reachable == {"root", "server-lib", "always-needed", "client-only"}
    assert proof.unreachable == ()


def test_an_ungated_extra_dependency_stays_unreachable() -> None:
    """The base pass must not pick up dependencies gated on an extra.

    This is the other half of the property above: without it, a proof that
    "supports" extras would simply admit everything.
    """

    entries = [
        _entry("root", "1.0", "server-lib"),
        _entry("server-lib", "2.0", 'client-only ; extra == "client"'),
        _entry("client-only", "1.0"),
    ]

    proof = prove_closure(entries, ["root"])

    assert proof.unreachable == ("client-only",)


def _entry(name: str, version: str, *requires: str) -> ClosureEntry:
    return ClosureEntry(name=name, version=version, requires_python="", requires_dist=requires)


def test_a_complete_closure_is_proven() -> None:
    proof = prove_closure(
        [_entry("cortex", "1.0.0", "httpx>=0.28"), _entry("httpx", "0.28.1", "anyio>=4"), _entry("anyio", "4.9.0")],
        ["cortex"],
    )

    assert proof.reachable == {"cortex", "httpx", "anyio"}
    assert proof.unreachable == ()
    assert proof.missing == ()
    assert proof.unsatisfied == ()


def test_a_missing_transitive_dependency_is_reported() -> None:
    proof = prove_closure([_entry("cortex", "1.0.0", "httpx>=0.28")], ["cortex"])

    assert proof.missing == (("cortex", "httpx"),)


def test_a_version_that_does_not_satisfy_its_specifier_is_reported() -> None:
    proof = prove_closure(
        [_entry("cortex", "1.0.0", "httpx>=0.28"), _entry("httpx", "0.27.0")], ["cortex"]
    )

    assert proof.unsatisfied == (("cortex", "httpx", ">=0.28"),)


def test_a_subgraph_of_smuggled_wheels_cannot_vouch_for_itself() -> None:
    """Reachability is forwards from the roots, so mutual reference is not enough."""

    proof = prove_closure(
        [
            _entry("cortex", "1.0.0"),
            _entry("smuggled-a", "1.0.0", "smuggled-b"),
            _entry("smuggled-b", "1.0.0", "smuggled-a"),
        ],
        ["cortex"],
    )

    assert proof.unreachable == ("smuggled-a", "smuggled-b")


def test_a_dependency_cycle_terminates() -> None:
    proof = prove_closure(
        [_entry("cortex", "1.0.0", "a"), _entry("a", "1.0.0", "b"), _entry("b", "1.0.0", "a")],
        ["cortex"],
    )

    assert proof.reachable == {"cortex", "a", "b"}
    assert proof.unreachable == ()


def test_names_that_normalize_to_the_same_distribution_are_refused() -> None:
    with pytest.raises(WheelClosureError, match="duplicate distribution"):
        prove_closure([_entry("Foo.Bar", "1.0"), _entry("foo_bar", "2.0")], ["foo-bar"])


def test_a_forbidden_distribution_is_reported_even_when_required() -> None:
    """§4.1 step 3 / §4.4 clause 5: a forbidden distribution (e.g. the AGPL
    `backtesting`) must be reported even when the roots genuinely require it —
    reachability alone must not excuse a license/policy violation."""

    proof = prove_closure(
        [_entry("cortex", "1.0.0", "backtesting>=1.0"), _entry("backtesting", "1.0.0")],
        ["cortex"],
        forbidden=("backtesting",),
    )

    assert proof.forbidden == ("backtesting",)
    assert proof.missing == ()
    assert "backtesting" in proof.reachable


def test_a_forbidden_distribution_is_reported_even_when_unreachable() -> None:
    """A smuggled forbidden wheel that no root references must still be named
    as forbidden, not merely folded into the generic `unreachable` bucket."""

    proof = prove_closure(
        [_entry("cortex", "1.0.0"), _entry("backtesting", "1.0.0")],
        ["cortex"],
        forbidden=("backtesting",),
    )

    assert proof.forbidden == ("backtesting",)
    assert proof.unreachable == ("backtesting",)


def test_the_forbidden_set_is_matched_after_normalization() -> None:
    """The denylist is matched on the normalized name, so `Backtesting` in the
    entry set cannot dodge a `backtesting` denylist entry (or vice versa)."""

    proof = prove_closure(
        [_entry("cortex", "1.0.0"), _entry("Back.Testing", "1.0.0")],
        ["cortex"],
        forbidden=("back_testing",),
    )

    assert proof.forbidden == ("back-testing",)


def test_a_marked_out_requirement_does_not_need_a_wheel() -> None:
    proof = prove_closure(
        [_entry("cortex", "1.0.0", "pywin32 ; sys_platform == 'win32'")], ["cortex"]
    )

    assert proof.missing == ()


def test_requirements_hashes_are_parsed_across_continuations(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "# comment\n"
        "httpx==0.28.1 \\\n"
        f"    --hash=sha256:{'a' * 64} \\\n"
        f"    --hash=sha256:{'b' * 64}\n"
        "anyio==4.9.0 ; python_full_version >= '3.12' \\\n"
        f"    --hash=sha256:{'c' * 64}\n"
    )

    hashes = parse_requirements_hashes(path)

    assert hashes == {"httpx": {"a" * 64, "b" * 64}, "anyio": {"c" * 64}}


def test_a_requirement_line_without_a_hash_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("httpx==0.28.1\n")

    with pytest.raises(WheelClosureError, match="carries no hash"):
        parse_requirements_hashes(path)


def test_a_non_sha256_hash_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("httpx==0.28.1 \\\n    --hash=md5:deadbeef\n")

    with pytest.raises(WheelClosureError, match="hash is unsupported"):
        parse_requirements_hashes(path)


def test_an_unpinned_requirement_line_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(f"httpx>=0.28.1 \\\n    --hash=sha256:{'a' * 64}\n")

    with pytest.raises(WheelClosureError, match="line is unsupported"):
        parse_requirements_hashes(path)


def test_an_oversized_requirements_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(WheelClosureError, match="oversized"):
        parse_requirements_hashes(path)


def test_a_requirements_file_listing_a_distribution_twice_is_refused(tmp_path: Path) -> None:
    """A shipped `requirements.txt` is expected to have exactly one surviving
    line per distribution (the marker-driven filter collapses the two-scipy
    case to one before the file is ever bundled — see the acquisition-side
    test below). A second line for the same name must be refused rather than
    silently unioned, which would weaken the §4.4 clause 6 digest binding."""

    path = tmp_path / "requirements.txt"
    path.write_text(
        f"scipy==1.17.1 ; python_full_version < '3.12' \\\n    --hash=sha256:{'a' * 64}\n"
        f"scipy==1.18.0 ; python_full_version >= '3.12' \\\n    --hash=sha256:{'b' * 64}\n"
    )

    with pytest.raises(WheelClosureError, match="lists a distribution twice"):
        parse_requirements_hashes(path)


def test_the_two_scipy_lines_resolve_to_exactly_one_for_the_target() -> None:
    """The real lock's only repeated distribution, decided by marker."""

    older = evaluate_marker("python_full_version < '3.12'")
    newer = evaluate_marker("python_full_version >= '3.12'")

    assert (older, newer) == (False, True)


# --- Regressions pinned from an adversarial pass. Each of these six inputs was
# once decided WRONGLY or crashed; the wrong answers were silent, which is the
# failure class this module exists to avoid.


@pytest.mark.parametrize("specifier", ["==1.0.dev5.*", "==1.0rc9.*", "!=1.0.post1.*", "==1.0+x.*"])
def test_a_wildcard_specifier_with_a_qualifier_is_refused(specifier: str) -> None:
    """PEP 440 admits `.*` on a release segment only; truncating accepted 1.0.99."""

    with pytest.raises(WheelClosureError, match="specifier is unsupported"):
        satisfies("1.0.99", specifier)


@pytest.mark.parametrize(
    ("value", "dev", "prerelease"),
    [("1.0+dev", None, False), ("1.0+abcdev", None, False), ("1.0.dev5", 5, True), ("1.0dev", 0, True)],
)
def test_a_local_label_containing_dev_is_not_a_development_release(
    value: str, dev: int | None, prerelease: bool
) -> None:
    parsed = parse_version(value)

    assert parsed.dev == dev
    assert parsed.is_prerelease is prerelease


def test_a_local_version_sorts_above_the_same_version_without_one() -> None:
    assert compare("1.0", "1.0+dev") == -1


@pytest.mark.parametrize("specifier", [">=1.0+local1", "<1.0+local1", ">1.0+x", "<=1.0+x"])
def test_an_ordered_comparison_against_a_local_version_is_refused(specifier: str) -> None:
    """Discarding the target's local segment made satisfies contradict compare."""

    with pytest.raises(WheelClosureError, match="specifier is unsupported"):
        satisfies("1.0", specifier)


@pytest.mark.parametrize(
    "marker",
    [
        " and ".join(["sys_platform == 'darwin'"] * 1000),
        "(" * 600 + "sys_platform == 'darwin'" + ")" * 600,
    ],
)
def test_an_adversarial_marker_is_refused_rather_than_crashing(marker: str) -> None:
    """These raised a bare RecursionError, which bundle.py does not catch."""

    with pytest.raises(WheelClosureError):
        evaluate_marker(marker)


def test_a_nesting_depth_beyond_the_bound_is_refused() -> None:
    with pytest.raises(WheelClosureError, match="nests too deeply"):
        evaluate_marker("(" * 40 + "sys_platform == 'darwin'" + ")" * 40)


def test_a_refused_version_comparison_inside_a_marker_propagates() -> None:
    """The refusal used to be swallowed and replaced by string ordering."""

    with pytest.raises(WheelClosureError, match="specifier is unsupported"):
        evaluate_marker('python_version > "3.*"')
