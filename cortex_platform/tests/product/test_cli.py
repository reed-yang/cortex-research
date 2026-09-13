from __future__ import annotations

import os
import sys
from pathlib import Path

from cortex_platform.product.cli import main
from cortex_platform.product.lifecycle import DaemonStatus


def _environment(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg" / "config"),
        "XDG_DATA_HOME": str(home / "xdg" / "data"),
        "XDG_STATE_HOME": str(home / "xdg" / "state"),
        "XDG_CACHE_HOME": str(home / "xdg" / "cache"),
    }


def test_cli_init_doctor_start_status_stop_round_trip(
    tmp_path: Path, capsys
) -> None:
    environment = _environment(tmp_path / "home")

    assert main(["init"], environ=environment, platform=sys.platform) == 0
    assert main(["doctor"], environ=environment, platform=sys.platform) == 0
    assert main(["start", "--timeout", "8"], environ=environment, platform=sys.platform) == 0
    assert main(["status"], environ=environment, platform=sys.platform) == 0
    assert main(["stop", "--timeout", "5"], environ=environment, platform=sys.platform) == 0
    assert main(["stop"], environ=environment, platform=sys.platform) == 0

    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    assert "initialized" in output
    assert "running" in output
    assert output.rstrip().endswith("stopped")


def test_cli_stop_reports_replacement_as_current_non_success(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    environment = _environment(tmp_path / "home")
    monkeypatch.setattr(
        "cortex_platform.product.cli.stop_daemon",
        lambda paths, timeout: DaemonStatus(
            "running", pid=123, port=456, instance_id="replacement"
        ),
    )

    assert main(["stop"], environ=environment, platform=sys.platform) == 3
    assert capsys.readouterr().out.strip() == "running"
