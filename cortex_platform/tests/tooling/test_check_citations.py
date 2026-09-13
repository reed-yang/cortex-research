"""What `tools/check_citations.py` calls a stale citation, and what it does not.

⟦batchM 6⟧ The lint exists because a citation is a comment and a comment
compiles, so nothing was checking the design record's `<file>.py:<line>`
tokens. A lint nobody trusts is worse than none: it gets muted. So the false
negatives it accepts on purpose -- a drift inside the window, a symbol too far
from its token to be paired with it -- are pinned here alongside the failures,
because those are the bounds a reader has to be able to rely on.

Every case is built in a throwaway repository. Nothing here reads the real
product tree: a test that did would go red on an unrelated edit and teach
everyone to ignore it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_citations import run


def _repository(root: Path, files: dict[str, str]) -> Path:
    """A miniature repository: the paths given, with their content."""

    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _check(root: Path, *extra: str) -> int:
    return run(["--repository", str(root), str(root / "src"), *extra])


#: A cited module whose only symbol sits on a line the tests can count on.
_TARGET = "\n".join(
    [
        "import os",  # 1
        "",  # 2
        "",  # 3
        "def sweep_stalled():",  # 4
        "    return os.getpid()",  # 5
        "",  # 6
        "",  # 7
        "",  # 8
        "",  # 9
        "",  # 10
        "def converge():",  # 11
        "    return 1",  # 12
        "",  # 13
    ]
)
#: A trailing newline, so line 13 exists and is blank.
_TARGET += "\n"

#: A second module of the same basename, so a bare citation names two files.
_ELSEWHERE = "def sweep_stalled():\n    return 0\n"


def test_a_citation_that_resolves_and_still_names_its_symbol_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": (
                '"""Doc.\n\n`sweep_stalled` (target.py:4) is where the sweep lives.\n"""\n'
            ),
        },
    )

    assert _check(root) == 0
    assert capsys.readouterr().out == ""


def test_a_symbol_that_moved_out_of_the_window_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The drift the audits kept finding by hand: the token still resolves."""

    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": (
                "# `sweep_stalled` (target.py:11) is armed every tick.\n"
                "VALUE = 1\n"
            ),
        },
    )

    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/product/citing.py:1: target.py:11 -> symbol moved" in out
    assert "'sweep_stalled'" in out
    # No suggestion without the flag: the tool never volunteers an edit.
    assert "now at" not in out

    assert _check(root, "--fix-report") == 1
    assert "'sweep_stalled' now at src/product/target.py:4" in capsys.readouterr().out


def test_a_symbol_that_moved_inside_the_window_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deliberate: a decorator or a re-wrapped signature is not a stale record.

    The cost is that a shift of one to three lines passes, so a slice that
    moves a cited definition must still re-derive its own citations by hand.
    `--window 0` is the audit that refuses to absorb anything.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": "# `sweep_stalled` (target.py:5) moved a bit.\nX = 1\n",
        },
    )

    assert _check(root) == 0
    assert capsys.readouterr().out == ""

    assert _check(root, "--window", "0") == 1
    assert "symbol moved" in capsys.readouterr().out


def test_a_line_past_the_end_of_the_cited_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": "# See target.py:900 for the writer.\nX = 1\n",
        },
    )

    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "target.py:900 -> out of range: src/product/target.py has 13 lines" in out


def test_a_blank_cited_line_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A line that emptied out is the shape a deletion above leaves behind."""

    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": "# See target.py:7 for the writer.\nX = 1\n",
        },
    )

    assert _check(root) == 1
    assert "blank line: src/product/target.py:7 is empty" in capsys.readouterr().out


def test_a_basename_two_files_answer_to_is_reported_rather_than_guessed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two `store.py` in the repository, so the tool refuses to pick one."""

    root = _repository(
        tmp_path,
        {
            "src/product/control/store.py": _TARGET,
            "src/product/sources/store.py": _TARGET,
            "src/product/citing.py": "# The writer is at store.py:4.\nX = 1\n",
        },
    )

    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "store.py:4 -> ambiguous: 'store.py' matches 2 files" in out
    assert "src/product/control/store.py" in out
    assert "src/product/sources/store.py" in out


def test_a_basename_answered_both_inside_and_outside_the_product_tree_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchP ADJ-A1⟧ The silent guess, in the shape nothing downstream catches.

    One `target.py` in the product tree and one outside it, neither in the
    citing file's own package, and the cited line is a real non-blank line
    carrying the paired symbol in the first of them. Every later check passes,
    so if resolution picks a file the citation is reported clean -- and the
    reader is never told the path they wrote names two files.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/deep/target.py": _TARGET,
            "src/runtime/target.py": _ELSEWHERE,
            "src/product/transports/citing.py": (
                "# `sweep_stalled` (target.py:4) is the sweep.\nX = 1\n"
            ),
        },
    )

    assert _check(root) == 1
    assert (
        "src/product/transports/citing.py:1: target.py:4 -> ambiguous: "
        "'target.py' matches 2 files "
        "(src/product/deep/target.py, src/runtime/target.py)"
    ) in capsys.readouterr().out


def test_an_ambiguous_basename_is_not_diagnosed_inside_the_file_it_may_not_mean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchP ADJ-A3⟧ The reason names the edit to make, not one a guess implies.

    `target.py:900` is past the end of the product-tree `target.py` and a real
    line of the other one. "out of range ... has 13 lines" would send a reader
    hunting for a new line number for a citation whose only defect is the
    unqualified path, so the ambiguity is the whole reason and the riders that
    measured a consequence in the preferred file are gone with the preference.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/deep/target.py": _TARGET,
            "src/runtime/target.py": "".join(f"value_{n} = {n}\n" for n in range(1000)),
            "src/product/transports/citing.py": "# See target.py:900.\nX = 1\n",
        },
    )

    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "target.py:900 -> ambiguous: 'target.py' matches 2 files" in out
    assert "out of range" not in out
    assert "also matches" not in out


def test_a_range_token_checks_both_of_its_ends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/ok.py": "# `converge` (target.py:11-12) is the arm.\nX = 1\n",
            "src/product/bad.py": "# `converge` (target.py:11-900) is the arm.\nX = 1\n",
            "src/product/blank.py": "# `converge` (target.py:11-13) is the arm.\nX = 1\n",
        },
    )

    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/product/ok.py" not in out
    assert "target.py:11-900 -> out of range" in out
    assert "target.py:11-13 -> blank line: src/product/target.py:13 is empty" in out


def test_a_partial_path_resolves_against_the_citing_file_s_own_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(
        tmp_path,
        {
            "src/product/deep/target.py": _TARGET,
            "src/product/transports/citing.py": (
                "# `sweep_stalled` (deep/target.py:4) is the sweep.\nX = 1\n"
            ),
        },
    )

    assert _check(root) == 0
    assert capsys.readouterr().out == ""


def test_prose_between_a_symbol_and_a_token_breaks_the_pairing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bound that stops the tool inventing a pairing.

    `(target.py:4, the second write at target.py:12)` names two lines and one
    symbol; only the first belongs to the symbol. Four words of prose is the
    signal that the second one does not, so it gets the existence check alone.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": (
                "# `sweep_stalled` (target.py:4, the second write at target.py:12)\n"
                "X = 1\n"
            ),
        },
    )

    assert _check(root) == 0
    assert capsys.readouterr().out == ""


def test_a_symbol_may_introduce_a_token_on_a_later_comment_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Consecutive `#` lines are one region: this product wraps its sentences."""

    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": (
                "# The sweep is `sweep_stalled`\n# (target.py:11), armed per tick.\nX = 1\n"
            ),
        },
    )

    assert _check(root) == 1
    assert "src/product/citing.py:2: target.py:11 -> symbol moved" in capsys.readouterr().out


def test_a_path_no_file_answers_to_is_reported_as_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(
        tmp_path,
        {"src/product/citing.py": "# See gone.py:4 for the writer.\nX = 1\n"},
    )

    assert _check(root) == 1
    assert "gone.py:4 -> unresolved: no file named 'gone.py'" in capsys.readouterr().out


def test_a_citation_in_code_rather_than_in_a_comment_is_not_a_citation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the design record is checked; a string literal is data."""

    root = _repository(
        tmp_path,
        {
            "src/product/target.py": _TARGET,
            "src/product/citing.py": 'LABEL = "target.py:900"\n',
        },
    )

    assert _check(root) == 0
    assert capsys.readouterr().out == ""


def test_an_excluded_prefix_is_neither_scanned_nor_resolvable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both halves of what `--exclude` means, in one tree.

    A vendored copy of `target.py` makes the bare basename ambiguous for a
    citation that has nothing to do with it, and the vendored tree carries a
    stale citation of its own. Excluding it must silence both: a tree the
    reader may not edit is a tree the tool has nothing to say about, on either
    side of a citation.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/vendored/target.py": _ELSEWHERE,
            "src/product/vendored/citing.py": "# See target.py:900.\nX = 1\n",
            "src/product/deep/target.py": _TARGET,
            "src/product/transports/citing.py": (
                "# `sweep_stalled` (target.py:4) is the sweep.\nX = 1\n"
            ),
        },
    )

    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/product/vendored/citing.py:1: target.py:900" in out
    assert "src/product/transports/citing.py:1: target.py:4 -> ambiguous" in out

    assert _check(root, "--exclude", "src/product/vendored") == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    # The default exclusion is still in force; this one is added to it.
    assert captured.err.endswith(
        "excluding cortex_platform/product/runtime_update/worker_payload, "
        "src/product/vendored\n"
    )

    # Naming a file inside the excluded tree outright does not defeat it --
    # and ⟦batchR A4⟧ the answer says so: nothing was examined, which is exit
    # 2, not the exit 0 that used to be indistinguishable from a clean scan.
    assert (
        run(
            [
                "--repository",
                str(root),
                "--exclude",
                "src/product/vendored",
                str(root / "src/product/vendored/citing.py"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nothing scanned (every named path is excluded)" in captured.err


def test_the_vendored_worker_payload_is_excluded_until_the_caller_says_otherwise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default exclusion, by its real path, and the flag that lifts it.

    The Hermes payload is vendored byte for byte and fixed only by a
    re-import, so its citations are reported to nobody who can act on them.
    That is a policy, not a fact about the code, so `--no-default-excludes`
    has to be able to say otherwise.
    """

    root = _repository(
        tmp_path,
        {
            "cortex_platform/product/runtime_update/worker_payload/cortex_worker/"
            "approval.py": "# The map is at gone.py:4.\nX = 1\n",
            # A neighbour outside the payload, so the default run is a scan
            # that found nothing rather than ⟦batchR A4⟧ a scan of nothing.
            "cortex_platform/product/api/app.py": "# Nothing cited here.\nX = 1\n",
        },
    )

    assert run(["--repository", str(root), "cortex_platform/product"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "1 file(s) clean, excluding "
        "cortex_platform/product/runtime_update/worker_payload" in captured.err
    )

    assert (
        run(
            [
                "--repository",
                str(root),
                "--no-default-excludes",
                "cortex_platform/product",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "gone.py:4 -> unresolved: no file named 'gone.py'" in captured.out
    assert "excluding" not in captured.err


def test_a_path_that_does_not_exist_is_a_usage_error_not_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path, {"src/product/citing.py": "X = 1\n"})

    assert run(["--repository", str(root), str(root / "nowhere")]) == 2
    assert capsys.readouterr().out == ""


def test_a_path_outside_the_repository_is_a_usage_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchP ADJ-A2⟧ The documented exits are 0, 1 and 2 -- never a traceback.

    An absolute argument outside `--repository` used to reach the scan, where
    the first `relative_to(root)` raised, printing a traceback and exiting 1:
    the code that means "stale citations found". A caller wiring this into a
    gate could not tell the two apart.
    """

    root = _repository(tmp_path / "repo", {"src/product/citing.py": "X = 1\n"})
    outside = _repository(tmp_path / "outside", {"pkg/a.py": "# See a.py:1.\nX = 1\n"})

    assert run(["--repository", str(root), str(outside)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "path outside repository" in captured.err


def test_an_excluded_tree_still_answers_a_citation_that_names_it_by_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchR A1⟧ The half `--exclude` deliberately does NOT cover.

    A citation that names a file under the excluded tree by explicit path is
    written in a file that IS scanned, by somebody who can edit it, so it is
    checked like any other. What the exclusion buys is the other two halves,
    asserted here in the same tree: the vendored `citing.py` is not scanned,
    and the bare `target.py` resolves to the one remaining candidate instead
    of being reported ambiguous.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/vendored/target.py": _ELSEWHERE,
            "src/product/vendored/citing.py": "# See gone.py:4.\nX = 1\n",
            "src/product/deep/target.py": _TARGET,
            "src/product/transports/citing.py": (
                "# `sweep_stalled` (target.py:4) is the sweep, vendored as\n"
                "# src/product/vendored/target.py:900.\nX = 1\n"
            ),
        },
    )

    assert _check(root, "--exclude", "src/product/vendored") == 1
    out = capsys.readouterr().out
    assert (
        "src/product/transports/citing.py:2: src/product/vendored/target.py:900"
        " -> out of range: src/product/vendored/target.py has 2 lines"
    ) in out
    # The excluded tree's own stale citation is silent, and the bare basename
    # is answered rather than called ambiguous.
    assert "gone.py" not in out
    assert "ambiguous" not in out


def test_a_run_that_examined_nothing_is_a_usage_error_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchR A4⟧ `0 file(s) clean`, exit 0, is what a mis-wired gate prints.

    A gate pointed at a path that no longer exists under that name, or wholly
    covered by an exclusion, printed the same two things a holding tree does.
    Nothing examined is a usage error: the run could not answer the question.
    """

    root = _repository(
        tmp_path,
        {"src/product/vendored/citing.py": "# See gone.py:4.\nX = 1\n"},
    )

    assert _check(root, "--exclude", "src") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nothing scanned (every named path is excluded)" in captured.err
    assert "file(s) clean" not in captured.err


def test_an_absolute_exclusion_inside_the_repository_is_relativised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchR A5⟧ Carried as written it matched nothing and still printed.

    Everything downstream compares a repo-relative path, so an absolute
    prefix excluded nothing -- while the summary line, the one place a reader
    checks a clean run's scope, named it as in force.
    """

    root = _repository(
        tmp_path,
        {
            "src/product/vendored/target.py": _ELSEWHERE,
            "src/product/vendored/citing.py": "# See target.py:900.\nX = 1\n",
            "src/product/deep/target.py": _TARGET,
            "src/product/transports/citing.py": (
                "# `sweep_stalled` (target.py:4) is the sweep.\nX = 1\n"
            ),
        },
    )

    assert _check(root, "--exclude", str(root / "src/product/vendored")) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    # Named in the form every other path in this output takes.
    assert captured.err.endswith(
        "excluding cortex_platform/product/runtime_update/worker_payload, "
        "src/product/vendored\n"
    )


def test_an_absolute_exclusion_outside_the_repository_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchR A5⟧ It can never match, so honouring it silently is a lie."""

    root = _repository(tmp_path / "repo", {"src/product/citing.py": "X = 1\n"})
    outside = tmp_path / "outside"
    outside.mkdir()

    assert _check(root, "--exclude", str(outside)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exclusion outside repository" in captured.err


def test_an_exclusion_naming_the_repository_itself_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """⟦batchR A5⟧ Relativising the root yields `.`, which matches nothing.

    Excluding the whole repository is a mis-wiring rather than a request to
    scan nothing, and it is refused where the caller can see it instead of
    reaching the scan and answering "nothing scanned".
    """

    root = _repository(tmp_path, {"src/product/citing.py": "X = 1\n"})

    assert _check(root, "--exclude", str(root)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exclusion names the whole repository" in captured.err
