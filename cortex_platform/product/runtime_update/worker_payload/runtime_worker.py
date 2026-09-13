"""The attested entrypoint a managed release is launched through.

The v2 launch contract is `<interpreter> -I <slot>/content/<worker_entrypoint>
--v2-descriptor <path>`. `-I` removes `PYTHONPATH` and user site, and the
interpreter is the one the release carries, so nothing from the product is
importable here — which is the point: S3.0's residual asked that the file the
slot attests be the file that runs.

⟦AMD-6⟧ bootstrap: this module puts its own *resolved* directory on `sys.path`
before importing `cortex_worker`. Never the working directory, never
`PYTHONPATH`. The directory it inserts is the sealed content root the slot's
`content_tree_sha256` witnesses, so the modules it then imports are exactly the
ones the release was attested with.

`handle` is the v1 surface the updater's activation probe still calls
(`cli._probe` runs the candidate through `WorkerSupervisor`). S3.2 answers it
from the release's own evidence; S3.3 replaces it with the real Hermes dispatch.
"""

from __future__ import annotations

import os
import sys


def handle(method, params):
    """Answer the v1 probe from evidence this file can see without imports."""

    if method == "health":
        return {
            "status": "healthy",
            "release_id": params.get("release_id") if isinstance(params, dict) else None,
        }
    if method == "identity":
        return {"entrypoint": os.path.basename(os.path.realpath(__file__))}
    return {"method": method, "params": params}


def _bootstrap() -> None:
    # A worker must not change the tree it is about to measure. `-I` does not
    # stop bytecode caching, so importing `cortex_worker` from a slot that
    # happened to be writable would drop `__pycache__` into `content/` and move
    # the slot's own `content_tree_sha256` out from under the identity check.
    # Production slots are sealed 0o555 and the writes would fail silently; this
    # makes the guarantee the worker's rather than the filesystem's.
    sys.dont_write_bytecode = True
    directory = os.path.dirname(os.path.realpath(__file__))
    if directory not in sys.path:
        sys.path.insert(0, directory)


if __name__ == "__main__":
    _bootstrap()
    from cortex_worker.serve import main

    raise SystemExit(main(sys.argv[1:]))
