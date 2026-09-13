"""⟦S32-R-08⟧ The difference between "skipped" and "ran" must be observable.

`output/` is untracked, so the vendored cp311 archive is host supply. Without
it the two suites are green and the only test that packages and stages a real
interpreter silently did not run — identical tallies, exit 0. A merge check sets
`CORTEX_REQUIRE_REAL_RUNTIME=1`, and then absence is a failure.

⟦ADJ-21⟧ The probe run lives entirely under `tmp_path`. It used to be written
into `tests/`, run there, and unlinked in a `finally` — a suite that mutates the
tree it is testing, visible to anything watching the checkout and left behind by
any kill the `finally` does not survive. Nothing in a test run should create a
file inside the repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]

_PROBE = '''
def test_probe(vendored_worker_runtime):
    assert vendored_worker_runtime[0].is_file()
'''

#: The fixture is re-exported from the real `tests/conftest.py`, not reimplemented.
#: A copy would keep passing while the fixture it stands for regressed, and the
#: fixture's skip/fail behaviour is the entire subject of this file. Loaded by
#: path rather than imported, because `tests/` is not a package and the probe
#: runs with `tmp_path` as its rootdir.
_CONFTEST = '''
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "cortex_real_runtime_gate_conftest", {source!r}
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

vendored_worker_runtime = _module.vendored_worker_runtime
'''


def _run(tmp_path: Path, environment: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the probe against the real fixture, writing only under `tmp_path`."""

    root = tmp_path / "probe-run"
    root.mkdir(parents=True, exist_ok=True)
    probe = root / "test_real_runtime_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    (root / "conftest.py").write_text(
        _CONFTEST.format(source=str(REPOSITORY / "tests" / "conftest.py")),
        encoding="utf-8",
    )
    # An empty ini makes `root` the rootdir by discovery as well as by flag, so
    # no configuration above the temporary directory can reach this run.
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(root),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
    )


def _environment(**overrides: str) -> dict[str, str]:
    import os

    environment = dict(os.environ)
    # Point the locator at a directory that certainly holds no archive, so the
    # absent case is reproduced without touching the host's real supply.
    environment["CORTEX_TEST_WORKER_RUNTIME"] = "/nonexistent/cortex-worker-runtime"
    environment.pop("CORTEX_REQUIRE_REAL_RUNTIME", None)
    environment.update(overrides)
    return environment


def _tests_tree() -> set[str]:
    return {
        str(path.relative_to(REPOSITORY))
        for path in (REPOSITORY / "tests").rglob("*")
        if "__pycache__" not in path.parts
    }


def test_absent_runtime_skips_without_the_flag(tmp_path: Path) -> None:
    completed = _run(tmp_path, _environment())

    assert completed.returncode == 0, completed.stdout[-2000:]
    assert "skipped" in completed.stdout


def test_absent_runtime_fails_with_the_flag(tmp_path: Path) -> None:
    completed = _run(tmp_path, _environment(CORTEX_REQUIRE_REAL_RUNTIME="1"))

    assert completed.returncode != 0, completed.stdout[-2000:]
    assert "CORTEX_REQUIRE_REAL_RUNTIME is set" in completed.stdout
    # A fixture-level failure is reported as an error, not a failure — what
    # matters is that it is neither a skip nor a green exit.
    assert "1 error" in completed.stdout
    assert "skipped" not in completed.stdout


def test_the_gate_leaves_the_repository_untouched(tmp_path: Path) -> None:
    """⟦ADJ-21⟧ The probe was a file in `tests/`, removed only by a `finally`.

    Between `write_text` and `unlink` the checkout genuinely carried an extra
    test module, and a `SIGKILL` in that window left it there. What the gate
    needs is a probe, not a probe *in the repository*.
    """

    before = _tests_tree()
    completed = _run(tmp_path, _environment())
    assert completed.returncode == 0, completed.stdout[-2000:]

    assert _tests_tree() == before
    # Named explicitly, because the old path is what a stale checkout would show.
    assert not (REPOSITORY / "tests" / "test_zz_real_runtime_probe.py").exists()
    assert (tmp_path / "probe-run" / "test_real_runtime_probe.py").is_file()
