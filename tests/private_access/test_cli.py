from __future__ import annotations

import json
from pathlib import Path

from deployment.private_access.cli import main


def _write_config(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def test_validate_and_plan_are_offline_and_machine_readable(
    tmp_path: Path,
    config_dict: dict[str, object],
    capsys,
) -> None:
    path = tmp_path / "access.json"
    _write_config(path, config_dict)

    assert main(["--config", str(path), "validate"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated == {"provider": "tailscale-serve", "status": "valid"}

    assert main(["--config", str(path), "plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["daemon_remote_exposed"] is False
    assert plan["funnel_allowed"] is False
    assert len(plan["reviewed_policy_sha256"]) == 64


def test_generate_reports_only_relative_artifact_names(
    tmp_path: Path,
    config_dict: dict[str, object],
    capsys,
) -> None:
    path = tmp_path / "access.json"
    output = tmp_path / "review"
    _write_config(path, config_dict)

    assert (
        main(
            [
                "--config",
                str(path),
                "generate",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["files"] == [
        "tailnet-policy.fragment.json",
        "access-plan.json",
    ]
    assert str(tmp_path) not in json.dumps(result)
