"""The worker-side sources a release carries inside its own artifact.

`package_hermes_release.py` copies `cortex_worker/` out of this directory into
the payload and records each file's digest in the manifest's `worker_modules`
map, so the modules the worker imports are attested by the slot's content-tree
digest like every other release byte.

They live here, in the product, rather than in the Hermes fork, because the
worker protocol is the product's contract and a fork that could rewrite it could
answer its own identity questions. They are stdlib-only and 3.11-grammar,
because the interpreter that imports them is the one the release carries.
"""

from __future__ import annotations

from pathlib import Path

WORKER_PACKAGE = "cortex_worker"
SOURCE_ROOT = Path(__file__).resolve().parent
ENTRYPOINT_SOURCE = SOURCE_ROOT / "runtime_worker.py"


def module_sources() -> dict[str, Path]:
    """Every shipped worker module, keyed by its path inside the payload."""

    package = SOURCE_ROOT / WORKER_PACKAGE
    return {
        f"{WORKER_PACKAGE}/{path.name}": path
        for path in sorted(package.glob("*.py"))
    }
