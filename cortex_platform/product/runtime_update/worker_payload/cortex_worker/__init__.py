"""The worker-side half of the `cortex-worker/2` contract, shipped in a release.

Imported by the release's attested entrypoint after that entrypoint puts its own
resolved directory on `sys.path` (⟦AMD-6⟧) — never through the working
directory and never through `PYTHONPATH`, both of which the launch contract's
`-I` deliberately removes.
"""

from __future__ import annotations

WORKER_PAYLOAD_VERSION = 1
