"""The test-side D6 approval gate, in a module no other conftest can claim.

It is imported by name from `tests/packaging` and `tests/product/runtime_update`
and therefore cannot live in `tests/conftest.py`. A `conftest.py` in a directory
with no package marker claims the bare module name `conftest`, and
`tests/distribution/conftest.py` does exactly that — so in any session that
collects both directories, `conftest` bound to the distribution one and every
`from conftest import AllowUnapprovedReleases` raised `ImportError`. That is 18
collection errors on `pytest tests`, and the reason the suite was described as
runnable only in split sessions. A distinctly named module is owned by nobody
else, and `tests/distribution/__init__.py` is not an option: it would make
`tests.distribution` shadow the repository's real top-level `distribution/`
package (bundle.py, wheel_closure.py, lifecycle.py), measured at 728 collected
tests going to zero.
"""

from __future__ import annotations


class AllowUnapprovedReleases:
    """A gate that approves everything. ⟦ADJ-17⟧ Test-side, never shipped.

    It exists so that "no gate configured" can stay fail-closed: a test or tool
    exercising something other than D6 names this explicitly, which makes every
    place the approval decision is being skipped greppable — the thing an
    implicit permissive default would have hidden.

    It lives here rather than in `cortex_platform.product.runtime_update.
    approval` because the wheel ships that package, and a class whose whole
    behaviour is to approve every release is a working D6 bypass sitting one
    wired argument away from a shipped command. `tests/` is not packaged, so the
    escape hatch cannot leave this repository.
    """

    def approved(self, release_id: str, manifest_sha256: str) -> bool:
        _ = (release_id, manifest_sha256)
        return True
