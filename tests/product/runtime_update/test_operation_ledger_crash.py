from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REQUEST_DIGEST = "a" * 64
RESULT_DIGEST = "b" * 64


def _run(state: Path, phase: str, action: str) -> subprocess.CompletedProcess[str]:
    script = r'''
import json
import os
import sys
from pathlib import Path

from cortex_platform.product.runtime_update.operation_ledger import OperationLedger

crash_phase = sys.argv[1]
action = sys.argv[2]
state = Path(sys.argv[3])
request_digest = sys.argv[4]
result_digest = sys.argv[5]

class CrashLedger(OperationLedger):
    @staticmethod
    def checkpoint_hook(phase):
        if phase == crash_phase:
            os._exit({
                "begin_recorded": 97,
                "finish_recorded": 98,
                "uncertain_recorded": 99,
            }[phase])

ledger_class = CrashLedger if crash_phase != "none" else OperationLedger
ledger = ledger_class.open(state)
if action == "begin":
    result = {"begin": ledger.begin("op-1", "run", request_digest)}
elif action == "finish":
    ledger.finish("op-1", "committed", result_digest)
    result = {"finished": True}
elif action == "inspect":
    status = ledger.status("op-1")
    result = {
        "state": status.state,
        "request_digest": status.request_digest,
        "result_digest": status.result_digest,
        "begin": ledger.begin("op-1", "run", request_digest),
    }
else:
    raise AssertionError(action)
print(json.dumps(result, sort_keys=True))
'''
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            phase,
            action,
            str(state),
            REQUEST_DIGEST,
            RESULT_DIGEST,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        },
    )


def _result(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize(
    ("checkpoint", "returncode", "setup", "surviving_state", "result_digest"),
    [
        ("begin_recorded", 97, "begin", "uncertain", None),
        ("finish_recorded", 98, "finish", "committed", RESULT_DIGEST),
        ("uncertain_recorded", 99, "reopen", "uncertain", None),
    ],
)
def test_fsynced_checkpoint_survives_real_process_loss(
    tmp_path: Path,
    checkpoint: str,
    returncode: int,
    setup: str,
    surviving_state: str,
    result_digest: str | None,
) -> None:
    state = tmp_path / checkpoint
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_bytes(b"outside-ledger-evidence")

    if setup in {"finish", "reopen"}:
        assert _result(_run(state, "none", "begin")) == {"begin": "accepted"}
    if setup == "begin":
        crashed = _run(state, checkpoint, "begin")
    elif setup == "finish":
        crashed = _run(state, checkpoint, "finish")
    else:
        crashed = _run(state, checkpoint, "inspect")

    assert crashed.returncode == returncode
    assert crashed.stdout == ""
    observed = _result(_run(state, "none", "inspect"))
    assert observed == {
        "state": surviving_state,
        "request_digest": REQUEST_DIGEST,
        "result_digest": result_digest,
        "begin": "duplicate",
    }
    assert sentinel.read_bytes() == b"outside-ledger-evidence"
