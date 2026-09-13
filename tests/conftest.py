"""Fixtures and helpers shared by the packaging and product test suites.

The locator for the vendored cp311 worker runtime is here because two suites
need it: `tests/packaging` packages it into a release and
`tests/product/runtime_update` stages it for real.

The permissive approval gate that used to sit beside it is in
`tests/approval_gate.py` instead. Anything imported BY NAME from another
directory cannot live in a `conftest.py`: `tests/distribution/conftest.py`
claims the bare module name `conftest` in any session that collects both
directories, so `from conftest import ...` was an ImportError in exactly the
runs that collect the suite whole. Fixtures are unaffected — pytest finds them
by directory, not by module name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]

WORKER_RUNTIME_ARCHIVE = "cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz"
WORKER_RUNTIME_PIN = "cpython-3.11.15-cp311-macosx_11_0_arm64.pin.json"
# The staging directory inside THIS checkout, beside where the cp314 archive is
# vendored. It used to be searched from the checkout upwards, so a worktree
# silently consumed a supply belonging to some ancestor directory: the tests
# passed while resolving an input the checkout never declared, which is exactly
# what an independence check has to be able to rule out. A supply held outside
# the checkout is named with CORTEX_TEST_WORKER_RUNTIME instead.
WORKER_RUNTIME_RETAINED = "output/hermes-runtime-cp311"

#: ⟦S32-R-08⟧ Set this in any check that gates a merge. `output/` is untracked
#: (`git ls-files output/` is empty), so the archive is host supply rather than
#: source, and a skip exits 0 with tallies identical to a run that really staged
#: a CPython. On a host without the directory the suite is green and the only
#: honest test never ran; with the flag set, absence is a failure.
REQUIRE_REAL_RUNTIME = "CORTEX_REQUIRE_REAL_RUNTIME"


def _require_real_runtime() -> bool:
    value = os.environ.get(REQUIRE_REAL_RUNTIME, "").strip().lower()
    return value not in {"", "0", "false", "no"}


def _worker_runtime_directory() -> Path | None:
    configured = os.environ.get("CORTEX_TEST_WORKER_RUNTIME")
    if configured:
        # Held to the same test as a discovered directory. A mistyped override
        # used to reach the pin check and report "no pin document", which names
        # the wrong problem — and under the flag the failure has to say that the
        # runtime is absent, because that is what a merge check is asking.
        candidate = Path(configured)
        return candidate if (candidate / WORKER_RUNTIME_ARCHIVE).is_file() else None
    candidate = REPOSITORY / WORKER_RUNTIME_RETAINED
    return candidate if (candidate / WORKER_RUNTIME_ARCHIVE).is_file() else None


@pytest.fixture(scope="session")
def vendored_worker_runtime() -> tuple[Path, Path]:
    """The real cp311 archive and pin, or a loud skip — or, under the flag, a failure.

    Loud on purpose. Every other test in these two suites runs against a
    stand-in, so this fixture is the only place a real CPython tree is ever
    packaged or staged, and a quiet skip would turn the one honest test into one
    that never ran. But a skip still exits 0, and the reported tallies are
    identical either way, so "loud" was only ever loud to a human reading
    output. `CORTEX_REQUIRE_REAL_RUNTIME=1` makes absence fatal, and that is
    what a merge check sets.
    """

    directory = _worker_runtime_directory()
    if directory is None:
        absent = (
            "the vendored cp311 worker runtime is absent, so the only test that "
            f"packages and stages a REAL interpreter did not run. Expected "
            f"{WORKER_RUNTIME_ARCHIVE} under {REPOSITORY / WORKER_RUNTIME_RETAINED}, "
            "or set CORTEX_TEST_WORKER_RUNTIME. Vendor it with "
            "`python tools/vendor_python_runtime.py --profile cp311 "
            "--vendor-root <dir>`."
        )
        if _require_real_runtime():
            pytest.fail(f"{REQUIRE_REAL_RUNTIME} is set and {absent}")
        pytest.skip(absent)
    archive = directory / WORKER_RUNTIME_ARCHIVE
    pin = directory / WORKER_RUNTIME_PIN
    if not pin.is_file():
        pytest.fail(f"the vendored cp311 runtime has no pin document at {pin}")
    return archive.resolve(), pin.resolve()
