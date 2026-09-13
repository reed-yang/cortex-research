"""Read-only artifact and host capability detection."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Callable, Iterable


CommandRunner = Callable[[tuple[str, ...]], tuple[int, str]]


def _wheel_platform(path: Path) -> str:
    parts = path.name.removesuffix(".whl").rsplit("-", 4)
    return parts[-1].lower() if len(parts) == 5 else ""


def _sqlite_vec_matches(path: Path, system: str, machine: str) -> bool:
    name = path.name.lower().replace("-", "_")
    if not name.startswith("sqlite_vec_") and not name.startswith("sqlite_vec-"):
        return False
    tag = _wheel_platform(path)
    machine = machine.lower()
    if system == "Darwin":
        architecture = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
        return "macosx" in tag and (architecture in tag or "universal2" in tag)
    if system == "Linux":
        architecture = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
        return ("manylinux" in tag or "linux" in tag) and architecture in tag
    return False


def artifact_capabilities(
    artifacts: Iterable[Path],
    *,
    system: str | None = None,
    machine: str | None = None,
) -> dict[str, bool]:
    """Report only capabilities proven by target-compatible shipped artifacts."""

    system = system or platform.system()
    machine = machine or platform.machine()
    paths = tuple(artifacts)
    names = {path.name.lower().replace("-", "_") for path in paths}
    return {
        "sqlite_vec": any(_sqlite_vec_matches(path, system, machine) for path in paths),
        "ocr": any(name.startswith("cortex_ocr_runtime_") for name in names),
        "hermes_slot": False,
    }


def _subprocess_runner(command: tuple[str, ...]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return completed.returncode, completed.stdout + completed.stderr


def host_capabilities(
    *,
    system: str | None = None,
    bundle: Path | None,
    run: CommandRunner = _subprocess_runner,
) -> dict[str, object]:
    """Probe signing and desktop support without changing host state."""

    system = system or platform.system()
    report: dict[str, object] = {
        "system": system,
        "codesigned": False,
        "notarized": False,
        "quarantined": False,
        "desktop_integration": False,
        "ga_ready": False,
    }
    if system == "Darwin" and bundle is not None:
        target = str(bundle)
        report["codesigned"] = run(("codesign", "--verify", "--deep", "--strict", target))[0] == 0
        report["notarized"] = run(("spctl", "--assess", "--type", "execute", target))[0] == 0
        report["quarantined"] = run(("xattr", "-p", "com.apple.quarantine", target))[0] == 0
        report["ga_ready"] = bool(
            report["codesigned"] and report["notarized"] and not report["quarantined"]
        )
    elif system == "Linux":
        has_systemd = run(("systemctl", "--user", "--version"))[0] == 0
        has_xdg = run(("xdg-desktop-menu", "--help"))[0] == 0
        report["desktop_integration"] = has_systemd and has_xdg
    return report
