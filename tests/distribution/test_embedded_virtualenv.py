from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from distribution.install import (
    _CLOSED_PIP_ENVIRONMENT,
    _create_embedded_virtualenv,
    _relocate_venv,
    _verify_relocated_venv,
    DistributionInstaller,
    InstallError,
)
from distribution.product_manifest import stage_python_runtime

from test_installer import _bundle


def _staged_runtime(tmp_path: Path, pin: dict[str, object], archive: Path) -> Path:
    stage_root = tmp_path / "stage"
    stage_root.mkdir(mode=0o700, exist_ok=True)
    destination = stage_root / "python-runtime"
    described = pin["archive"]
    stage_python_runtime(
        archive,
        destination,
        stage_root=stage_root,
        expected={
            "sha256": described["sha256"],
            "size": described["size"],
            "version": pin["version"],
            "abi_tag": pin["abi_tag"],
        },
        expected_system="Darwin",
        expected_machine="arm64",
    )
    return destination


def test_the_embedded_virtualenv_is_the_staged_interpreter_itself(
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    tmp_path: Path,
) -> None:
    """The one assumption the whole tier rests on, measured rather than assumed."""

    interpreter = _staged_runtime(tmp_path, embedded_python_pin, embedded_python_runtime)
    runtime = tmp_path / "stage" / "runtime"

    _create_embedded_virtualenv(interpreter / "bin" / "python3.14", runtime)

    python = runtime / "bin" / "python"
    assert python.is_file() and not python.is_symlink()
    assert (
        hashlib.sha256(python.read_bytes()).hexdigest()
        == hashlib.sha256((interpreter / "bin" / "python3.14").read_bytes()).hexdigest()
    )
    assert not (runtime / "bin" / "python3").exists()
    assert not (runtime / "bin" / "python3.14").exists()
    assert not any(path.is_symlink() for path in runtime.rglob("*"))


def test_the_embedded_virtualenv_runs_without_a_host_interpreter(
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    tmp_path: Path,
) -> None:
    interpreter = _staged_runtime(tmp_path, embedded_python_pin, embedded_python_runtime)
    runtime = tmp_path / "stage" / "runtime"
    _create_embedded_virtualenv(interpreter / "bin" / "python3.14", runtime)

    completed = subprocess.run(
        [
            str(runtime / "bin" / "python"),
            "-I",
            "-c",
            "import sys;print(sys.prefix);print(sys.base_prefix);print(sys.executable)",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/nonexistent"},
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        str(runtime),
        str(interpreter),
        str(runtime / "bin" / "python"),
    ]


def test_relocation_binds_the_virtualenv_to_the_published_generation(
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    tmp_path: Path,
) -> None:
    interpreter = _staged_runtime(tmp_path, embedded_python_pin, embedded_python_runtime)
    stage = tmp_path / "stage"
    runtime = stage / "runtime"
    _create_embedded_virtualenv(interpreter / "bin" / "python3.14", runtime)
    destination = tmp_path / "versions" / "cortex-dev-1-0123456789abcdef"

    _relocate_venv(runtime, source_root=stage, destination_root=destination)

    _verify_relocated_venv(runtime, destination_root=destination, version="3.14.6")
    configuration = (runtime / "pyvenv.cfg").read_text()
    assert f"home = {destination}/python-runtime/bin" in configuration
    assert "include-system-site-packages = false" in configuration


def test_relocation_refuses_a_virtualenv_still_naming_the_staging_root(
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    tmp_path: Path,
) -> None:
    interpreter = _staged_runtime(tmp_path, embedded_python_pin, embedded_python_runtime)
    stage = tmp_path / "stage"
    runtime = stage / "runtime"
    _create_embedded_virtualenv(interpreter / "bin" / "python3.14", runtime)
    destination = tmp_path / "versions" / "cortex-dev-1-0123456789abcdef"

    with pytest.raises(InstallError, match="virtual environment binding is invalid"):
        _verify_relocated_venv(runtime, destination_root=destination, version="3.14.6")


def test_relocation_refuses_a_virtualenv_whose_recorded_version_disagrees(
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    tmp_path: Path,
) -> None:
    interpreter = _staged_runtime(tmp_path, embedded_python_pin, embedded_python_runtime)
    stage = tmp_path / "stage"
    runtime = stage / "runtime"
    _create_embedded_virtualenv(interpreter / "bin" / "python3.14", runtime)
    destination = tmp_path / "versions" / "cortex-dev-1-0123456789abcdef"
    _relocate_venv(runtime, source_root=stage, destination_root=destination)

    with pytest.raises(InstallError, match="virtual environment binding is invalid"):
        _verify_relocated_venv(runtime, destination_root=destination, version="3.13.0")


def test_the_installer_never_hands_a_child_the_host_pip_configuration(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline install must not be able to reach a host-configured index."""

    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(tmp_path / "pip.conf"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "host-venv"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "host-python"))
    observed: list[tuple[list[str], dict[str, str]]] = []
    original = subprocess.run

    def recording(command, *args, **keywords):  # type: ignore[no-untyped-def]
        environment = keywords.get("env")
        if environment is not None:
            observed.append(([str(item) for item in command], dict(environment)))
        return original(command, *args, **keywords)

    monkeypatch.setattr(subprocess, "run", recording)
    prefix = tmp_path / "prefix"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)

    DistributionInstaller(prefix).install(bundle, allow_unsigned_developer=True)

    # `venv.EnvBuilder` hands `ensurepip` a copy of the caller's environment
    # (CPython's own `_call_new_python`), so the legacy tier still leaks there.
    # That is exactly why the embedded tier does not use `EnvBuilder` at all.
    installer_calls = [
        (command, environment)
        for command, environment in observed
        if "ensurepip" not in command
    ]
    assert installer_calls
    assert any("pip" in command and "install" in command for command, _ in installer_calls)
    forbidden = {"PIP_INDEX_URL", "VIRTUAL_ENV", "PYTHONHOME"}
    for command, environment in installer_calls:
        assert not forbidden & set(environment), (command, sorted(environment))
        assert environment.get("PIP_CONFIG_FILE", os.devnull) == os.devnull
    assert _CLOSED_PIP_ENVIRONMENT["PIP_CONFIG_FILE"] == os.devnull
