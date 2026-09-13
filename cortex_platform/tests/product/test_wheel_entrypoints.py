from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _run(
    command: list[str], *, cwd: Path, environ: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environ,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_built_wheel_entry_points_work_without_pythonpath(tmp_path: Path) -> None:
    distribution = tmp_path / "dist"
    build_environment = dict(os.environ)
    build_environment.pop("PYTHONPATH", None)
    _run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--package",
            "cortex",
            "--out-dir",
            str(distribution),
        ],
        cwd=PROJECT_ROOT,
        environ=build_environment,
    )
    wheel = next(distribution.glob("cortex-*.whl"))

    virtual_environment = tmp_path / "venv"
    _run(
        [sys.executable, "-m", "venv", str(virtual_environment)],
        cwd=tmp_path,
        environ=build_environment,
    )
    binary_dir = virtual_environment / ("Scripts" if os.name == "nt" else "bin")
    python = binary_dir / ("python.exe" if os.name == "nt" else "python")
    _run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=tmp_path,
        environ=build_environment,
    )

    home = tmp_path / "isolated-home"
    runtime_environment = {
        **build_environment,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg" / "config"),
        "XDG_DATA_HOME": str(home / "xdg" / "data"),
        "XDG_STATE_HOME": str(home / "xdg" / "state"),
        "XDG_CACHE_HOME": str(home / "xdg" / "cache"),
    }
    assert "PYTHONPATH" not in runtime_environment
    cortex = binary_dir / ("cortex.exe" if os.name == "nt" else "cortex")
    cortexd = binary_dir / ("cortexd.exe" if os.name == "nt" else "cortexd")

    assert _run(
        [str(cortex), "init"], cwd=tmp_path, environ=runtime_environment
    ).stdout.strip() == "initialized"
    runtime_status = _run(
        [str(cortex), "runtime", "status"],
        cwd=tmp_path,
        environ=runtime_environment,
    ).stdout
    assert '"frozen":false' in runtime_status
    assert _run(
        [str(cortex), "runtime", "freeze"],
        cwd=tmp_path,
        environ=runtime_environment,
    ).stdout.strip() == "frozen"
    assert _run(
        [str(cortex), "runtime", "thaw"],
        cwd=tmp_path,
        environ=runtime_environment,
    ).stdout.strip() == "thawed"
    assert "config: ok" in _run(
        [str(cortex), "doctor"], cwd=tmp_path, environ=runtime_environment
    ).stdout
    assert _run(
        [str(cortex), "start", "--timeout", "8"],
        cwd=tmp_path,
        environ=runtime_environment,
    ).stdout.strip() == "running"
    assert _run(
        [str(cortex), "status"], cwd=tmp_path, environ=runtime_environment
    ).stdout.strip() == "running"
    assert _run(
        [str(cortex), "stop", "--timeout", "5"],
        cwd=tmp_path,
        environ=runtime_environment,
    ).stdout.strip() == "stopped"
    assert "usage: cortexd" in _run(
        [str(cortexd), "--help"], cwd=tmp_path, environ=runtime_environment
    ).stdout

    direct_daemons = [
        subprocess.Popen(
            [str(cortexd)],
            cwd=tmp_path,
            env=runtime_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(2)
    ]
    try:
        deadline = time.monotonic() + 8.0
        direct_status: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < deadline:
            direct_status = subprocess.run(
                [str(cortex), "status"],
                cwd=tmp_path,
                env=runtime_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if direct_status.returncode == 0:
                break
            if all(process.poll() is not None for process in direct_daemons):
                break
            time.sleep(0.05)
        assert direct_status is not None
        assert direct_status.returncode == 0
        assert direct_status.stdout.strip() == "running"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and sum(
            process.poll() is None for process in direct_daemons
        ) != 1:
            time.sleep(0.05)
        alive = [process for process in direct_daemons if process.poll() is None]
        assert len(alive) == 1
        assert _run(
            [str(cortex), "stop", "--timeout", "5"],
            cwd=tmp_path,
            environ=runtime_environment,
        ).stdout.strip() == "stopped"
        assert alive[0].wait(timeout=5) == 0
    finally:
        for process in direct_daemons:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
