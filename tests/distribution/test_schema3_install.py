from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from distribution.product_manifest import PRODUCT_MANIFEST_SCHEMA

from distribution.install import DistributionInstaller, InstallError

from test_bundle_schema3 import _Inputs, _assemble
from test_installer import _fake_node, _stage_fake_node


@pytest.fixture
def schema3_bundle(
    tmp_path: Path,
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> tuple[Path, Path]:
    inputs = _Inputs(tmp_path, embedded_python_runtime, embedded_python_pin)
    bundle = _assemble(tmp_path / "bundle", inputs, web_closure, analyser_node)
    return bundle, _fake_node(tmp_path / "node", analyser_node)


def test_schema3_install_creates_a_copies_venv_bound_to_the_staged_interpreter(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole tier, installed for real from a real embedded interpreter."""

    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    prefix = tmp_path / "distribution"

    result = DistributionInstaller(prefix).install(
        bundle,
        node_executable=node,
        allow_unsigned_developer=True,
    )

    generation = prefix / "versions" / result.version
    interpreter = generation / "python-runtime" / "bin" / "python3.14"
    venv_python = generation / "runtime" / "bin" / "python"
    assert interpreter.is_file() and not interpreter.is_symlink()
    assert venv_python.is_file() and not venv_python.is_symlink()
    assert (
        hashlib.sha256(venv_python.read_bytes()).hexdigest()
        == hashlib.sha256(interpreter.read_bytes()).hexdigest()
    )
    assert not (generation / "runtime" / "bin" / "python3").exists()
    assert not (generation / "runtime" / "bin" / "python3.14").exists()
    assert not any(path.is_symlink() for path in (generation / "runtime").rglob("*"))
    # Caches written after publication legitimately record the published path;
    # what must not survive is any reference to the staging directory, whose
    # names always carry the installer's `.candidate-` prefix. `direct_url.json`
    # is the one measured exception — pip's own provenance for each wheel it
    # installed — and this asserts it is the ONLY one rather than ignoring it.
    residual = [
        path.relative_to(generation / "runtime").as_posix()
        for path in (generation / "runtime").rglob("*")
        if path.is_file() and b".candidate-" in path.read_bytes()
    ]
    assert all(name.endswith(".dist-info/direct_url.json") for name in residual), residual
    configuration = (generation / "runtime" / "pyvenv.cfg").read_text()
    assert f"home = {generation}/python-runtime/bin" in configuration
    assert "include-system-site-packages = false" in configuration
    manifest = json.loads((generation / "product-manifest.json").read_text())
    assert manifest["schema_version"] == PRODUCT_MANIFEST_SCHEMA >= 2
    assert manifest["python_runtime"]["embedded"] is True
    assert manifest["processes"]["control"]["runtime"] == "embedded-python"


def test_the_installed_generation_runs_without_any_host_python(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the whole tier exists for, measured rather than asserted."""

    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    prefix = tmp_path / "distribution"
    result = DistributionInstaller(prefix).install(
        bundle,
        node_executable=node,
        allow_unsigned_developer=True,
    )
    generation = prefix / "versions" / result.version

    completed = subprocess.run(
        [
            str(generation / "runtime" / "bin" / "python"),
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
        str(generation / "runtime"),
        str(generation / "python-runtime"),
        str(generation / "runtime" / "bin" / "python"),
    ]


def test_the_installed_interpreter_reports_the_published_generation(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The staged prefix must name where the tree LANDS, not where it was built.

    The whole staging directory is renamed into the generation, so binding the
    substitution to the staging path leaves an installed interpreter reporting
    sysconfig paths that no longer exist. `pyvenv.cfg`-driven checks cannot see
    that; only asking the installed interpreter for its own config vars can.
    """

    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    prefix = tmp_path / "distribution"
    result = DistributionInstaller(prefix).install(
        bundle,
        node_executable=node,
        allow_unsigned_developer=True,
    )
    generation = prefix / "versions" / result.version

    completed = subprocess.run(
        [
            str(generation / "python-runtime" / "bin" / "python3.14"),
            "-I",
            "-S",
            "-B",
            "-c",
            "import sysconfig;"
            "print(sysconfig.get_config_var('BINDIR'));"
            "print(sysconfig.get_config_var('LIBDIR'));"
            "print(sysconfig.get_config_var('INCLUDEPY'))",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/nonexistent"},
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    embedded = generation / "python-runtime"
    assert completed.stdout.splitlines() == [
        f"{embedded}/bin",
        f"{embedded}/lib",
        f"{embedded}/include/python3.14",
    ]


def test_the_installed_closure_satisfies_pip_check(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    prefix = tmp_path / "distribution"
    result = DistributionInstaller(prefix).install(
        bundle,
        node_executable=node,
        allow_unsigned_developer=True,
    )
    generation = prefix / "versions" / result.version

    completed = subprocess.run(
        [str(generation / "runtime" / "bin" / "python"), "-I", "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": "",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.defpath,
            "PIP_NO_INDEX": "1",
            "PIP_NO_INPUT": "1",
        },
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_schema3_install_refuses_incompatible_wheels_before_invoking_pip(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip must never be reached with a closure the interpreter cannot load."""

    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    monkeypatch.setattr(
        install,
        "_require_compatible_wheels",
        lambda *_args: (_ for _ in ()).throw(
            InstallError("wheel ABI does not match the embedded interpreter")
        ),
    )
    original = subprocess.run

    def refuse_pip(command, *args, **keywords):  # type: ignore[no-untyped-def]
        rendered = [str(item) for item in command]
        assert "pip" not in rendered or "install" not in rendered, rendered
        return original(command, *args, **keywords)

    monkeypatch.setattr(install.subprocess, "run", refuse_pip)
    prefix = tmp_path / "distribution"

    with pytest.raises(InstallError, match="wheel ABI"):
        DistributionInstaller(prefix).install(
            bundle,
            node_executable=node,
            allow_unsigned_developer=True,
        )


def test_schema3_install_leaves_no_partial_generation_when_staging_fails(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    monkeypatch.setattr(
        install,
        "_create_embedded_virtualenv",
        lambda *_args: (_ for _ in ()).throw(
            InstallError("embedded virtual environment creation failed")
        ),
    )
    prefix = tmp_path / "distribution"

    with pytest.raises(InstallError, match="virtual environment"):
        DistributionInstaller(prefix).install(
            bundle,
            node_executable=node,
            allow_unsigned_developer=True,
        )

    assert not (prefix / "current.json").exists()
    versions = prefix / "versions"
    assert not versions.exists() or not any(
        path.is_dir() and not path.name.startswith(".") for path in versions.iterdir()
    )


def _run_schema3_staging_crash(
    prefix: Path,
    bundle: Path,
    node: Path,
    fault: str,
) -> subprocess.CompletedProcess[str]:
    script = r'''
import os
import subprocess
import sys
from pathlib import Path

import distribution.install as install
import distribution.product_manifest as product_manifest
from distribution.install import DistributionInstaller

sys.path.insert(0, str(Path.cwd() / "tests" / "distribution"))
from test_installer import _stage_fake_node

fault = sys.argv[1]
if fault == "venv":
    original_run = install.subprocess.run

    def crash_venv(command, *args, **keywords):
        if command[3:7] == ["-m", "venv", "--copies", command[-1]]:
            os._exit(94)
        return original_run(command, *args, **keywords)

    install.subprocess.run = crash_venv
elif fault == "python-runtime-publish":
    original_rename = product_manifest.os.rename

    def crash_python_runtime_publish(source, destination, **keywords):
        if str(source).startswith(".python-runtime.") and destination == "python-runtime":
            os._exit(95)
        return original_rename(source, destination, **keywords)

    product_manifest.os.rename = crash_python_runtime_publish
else:
    raise AssertionError(f"unknown fault: {fault}")

install.stage_node_runtime = _stage_fake_node([])
DistributionInstaller(Path(sys.argv[2])).install(
    Path(sys.argv[3]),
    node_executable=Path(sys.argv[4]),
    allow_unsigned_developer=True,
)
'''
    return subprocess.run(
        [sys.executable, "-I", "-c", script, fault, str(prefix), str(bundle), str(node)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_schema3_staging_crash_is_unpublished(prefix: Path) -> None:
    assert not (prefix / "current.json").exists()
    versions = prefix / "versions"
    assert not any(
        path.is_dir() and not path.name.startswith(".") for path in versions.iterdir()
    )
    candidates = tuple(versions.glob(".candidate-*"))
    assert candidates
    # A hard exit cannot clean the private candidate, but it is safe because
    # current.json is the sole commit point and no generation directory exists.
    assert all(path.is_dir() and not path.is_symlink() for path in candidates)


def test_schema3_process_exit_during_embedded_venv_creation_never_publishes_a_partial_generation(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
) -> None:
    bundle, node = schema3_bundle
    prefix = tmp_path / "distribution"

    crashed = _run_schema3_staging_crash(prefix, bundle, node, "venv")

    assert crashed.returncode == 94, crashed.stderr
    _assert_schema3_staging_crash_is_unpublished(prefix)


def test_schema3_process_exit_during_python_runtime_publish_never_publishes_a_partial_generation(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
) -> None:
    bundle, node = schema3_bundle
    prefix = tmp_path / "distribution"

    crashed = _run_schema3_staging_crash(
        prefix,
        bundle,
        node,
        "python-runtime-publish",
    )

    assert crashed.returncode == 95, crashed.stderr
    _assert_schema3_staging_crash_is_unpublished(prefix)


def test_the_staged_interpreter_tree_is_immutable(
    tmp_path: Path,
    schema3_bundle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    bundle, node = schema3_bundle
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    prefix = tmp_path / "distribution"
    result = DistributionInstaller(prefix).install(
        bundle,
        node_executable=node,
        allow_unsigned_developer=True,
    )

    root = prefix / "versions" / result.version / "python-runtime"
    assert stat.S_IMODE(root.stat().st_mode) == 0o500
    interpreter = root / "bin" / "python3.14"
    assert stat.S_IMODE(interpreter.stat().st_mode) == 0o500
    assert stat.S_IMODE((root / "lib" / "python3.14" / "os.py").stat().st_mode) == 0o400
