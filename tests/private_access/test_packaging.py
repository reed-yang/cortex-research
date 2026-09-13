from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cortex_wheel_owns_private_access_and_installed_console_entrypoints(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "PIP_INDEX_URL": "https://127.0.0.1:9/forbidden",
        }
    )
    wheel_directory = tmp_path / "wheel"
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--package",
            "cortex",
            "--out-dir",
            str(wheel_directory),
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_directory.glob("cortex-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        entry_points_name = next(
            name for name in members if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")
        wheel_payload = b"".join(
            archive.read(member) for member in members if not member.endswith("/")
        )

    assert {
        "deployment/private_access/__init__.py",
        "deployment/private_access/cli.py",
        "deployment/private_access/gateway.py",
        "deployment/private_access/supervision.py",
    } <= members
    assert not any(
        "__pycache__" in member or member.endswith((".pyc", ".pyo"))
        for member in members
    )
    assert b"test-only-live-secret-value" not in wheel_payload
    assert "cortex-private-access = deployment.private_access.cli:main" in entry_points
    assert (
        "cortex-private-access-gateway = deployment.private_access.gateway:main"
        in entry_points
    )
    assert (
        "cortex-private-access-supervisor = "
        "deployment.private_access.supervision:main" in entry_points
    )

    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--offline", "--python", "3.11", str(venv)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    install_environment = {
        **environment,
        "UV_INDEX_URL": "https://127.0.0.1:9/forbidden",
    }
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--python",
            str(venv / "bin" / "python"),
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
        env=install_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-I",
            "-c",
            "import deployment.private_access.supervision",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    for executable in (
        "cortex-private-access",
        "cortex-private-access-gateway",
        "cortex-private-access-supervisor",
    ):
        subprocess.run(
            [str(venv / "bin" / executable), "--help"],
            cwd=tmp_path,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
