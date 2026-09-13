"""V3: the survivor scan may add candidates, and may never retract one."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from cortex_platform.product.engine import survivors

_NEEDLE = "cortex-p4-survivor-probe"


def _await_visible(pid: int) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if pid in survivors.snapshot_processes().processes:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} never appeared in the process snapshot")


def _pids(report: survivors.SurvivorReport) -> set[int]:
    return {int(entry.split(":", 1)[0]) for entry in report.processes}


def test_a_scanner_that_carries_the_marker_still_reports_an_unmarked_survivor(
    tmp_path: Path,
) -> None:
    """The intersect this module used to apply, reproduced where it engaged.

    `environment_marker_supported()` probed the SCANNER's own environment, so
    the filter only ever engaged inside the effect child -- whose environment
    does carry `CORTEX_EFFECT_MARKER`. There it narrowed: a survivor the
    arguments named, but whose environment the kernel does not hand back, was
    dropped and the effect read as clean.
    """

    marker = secrets.token_hex(16)
    survivor = subprocess.Popen(
        [sys.executable, "-c", f"import time  # {_NEEDLE}\ntime.sleep(30)"]
    )
    try:
        _await_visible(survivor.pid)
        scanner = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys\n"
                "from cortex_platform.product.engine import survivors\n"
                "report = survivors.scan(\n"
                "    state_dir=sys.argv[1], marker=sys.argv[2], needles=(sys.argv[3],)\n"
                ")\n"
                "print(json.dumps(report.to_dict()))\n",
                str(tmp_path),
                marker,
                _NEEDLE,
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(Path(__file__).resolve().parents[4]),
            env={**os.environ, "CORTEX_EFFECT_MARKER": marker},
        )
        report = json.loads(scanner.stdout)
        assert str(survivor.pid) in " ".join(report["processes"])
        assert report["clean"] is False
    finally:
        survivor.kill()
        survivor.wait(timeout=10)


def test_the_marker_adds_a_survivor_whose_arguments_match_nothing(
    tmp_path: Path,
) -> None:
    """The env-marker scan V3 asks for, as an addition rather than a filter."""

    marker = secrets.token_hex(16)
    baseline = survivors.snapshot_processes()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "CORTEX_EFFECT_MARKER": marker},
    )
    try:
        _await_visible(process.pid)
        report = survivors.scan(
            state_dir=tmp_path,
            marker=marker,
            needles=("a-needle-no-process-carries",),
            baseline=baseline,
        )
        assert process.pid in _pids(report)
        assert report.environment_scan == "arguments+marker"
    finally:
        process.kill()
        process.wait(timeout=10)
