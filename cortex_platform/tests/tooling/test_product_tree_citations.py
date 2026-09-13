"""The real product tree, run through the citation lint at the default window.

⟦batchM 6, batchP gate decision⟧ `test_check_citations.py` pins what the lint
CALLS a stale citation, in throwaway repositories, on purpose: a unit test that
read the real tree would go red on an unrelated edit and teach everyone to
ignore it. This module is the other thing -- the mechanical check batchM 6
asked for four rounds ago -- and it is separate precisely so that reasoning
stays true of the unit tests.

⟦P9-J⟧ It used to grade its outcome rather than assert it: zero findings was a
gate, findings under CORTEX_CITATIONS_STRICT were a failure, and findings
otherwise were a SKIP, because at that head the remaining ones lived in files
another branch under review was fixing. That scaffolding carried a demolition
date -- "once both have merged and the tree reports zero" -- and this is it.
The tree reports zero here, so the assertion is unconditional and an ordinary
`pytest` run fails on a stale citation. The environment variable is gone with
the grading; a switch that can only make a passing test pass is a switch that
teaches a reader to look for one.

⟦batchR A3⟧ The corpus is the TRACKED Python files, not a walk of the tree. A
walk makes an untracked scratch copy -- a `store.py.bak` renamed, a file
bisected out of a branch and left behind -- part of what the suite asserts
about, so a developer's own working directory could redden a run that has
nothing to do with their change. Only what is committed is something a reader
can be asked to fix. Outside a git checkout there is nothing to ask, so the
walk is the fallback and the scan is whatever the tool would have done.

⟦batchR A4⟧ Exit 2 is kept as its own failure with its own message: it means
the lint could not run at all -- a path that moved, an exclusion that covers
everything -- and a gate that read that as "no findings" would be the mute
this module exists to prevent. `scanned` is asserted positive for the same
reason, from the tool's own summary line rather than from the list handed in.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tools.check_citations import (
    DEFAULT_EXCLUDES,
    DEFAULT_PATHS,
    REPOSITORY,
    is_excluded,
    run,
)

#: The count on the tool's summary line, which is what it actually examined.
_SCANNED = re.compile(r"check_citations: (\d+) file\(s\) clean")


def _tracked_sources(root: Path) -> list[str] | None:
    """Every tracked `.py` file under the default paths, or None off a checkout.

    `git ls-files` rather than `git ls-tree HEAD`: a file added to the index
    and not yet committed is one its author is about to be asked about, and a
    gate that ignored it would report clean on the change that broke it.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = [
        name
        for name in completed.stdout.decode("utf-8").split("\0")
        if name
        and any(
            name == prefix or name.startswith(prefix + "/")
            for prefix in DEFAULT_PATHS
        )
        and not is_excluded(name, DEFAULT_EXCLUDES)
        # A path in the index that is not on disk (a staged deletion) is not
        # something to read; the tool would refuse the whole run over it.
        and (root / name).is_file()
    ]
    return sorted(names)


def test_the_product_tree_carries_no_stale_citation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`check_citations` over the tracked product tree, default window."""

    tracked = _tracked_sources(REPOSITORY)
    argv = list(tracked) if tracked else []
    exit_code = run(argv)
    captured = capsys.readouterr()
    findings = captured.out.strip()

    scope = (
        f"paths {', '.join(DEFAULT_PATHS)}; "
        f"excluding {', '.join(DEFAULT_EXCLUDES)}; "
        f"{'tracked files' if tracked else 'walked (not a git checkout)'}"
    )
    # Exit 2 is a usage error -- the lint could not run at all, which is never
    # the same answer as "no citation is stale".
    assert exit_code != 2, (
        f"check_citations could not run ({scope}): {captured.err.strip()}"
    )
    assert exit_code == 0, (
        f"the product tree carries stale citations ({scope}):\n{findings}"
    )
    assert findings == "", findings

    summary = _SCANNED.search(captured.err)
    assert summary is not None, captured.err.strip()
    assert int(summary.group(1)) > 0, captured.err.strip()
