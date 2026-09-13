from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from distribution.bundle import BundleBuilder
from distribution.install import (
    DistributionInstaller,
    InstallError,
    _remove_generation_tree,
    default_distribution_root,
)
from distribution.lifecycle import FRONT_DOOR_PROBE_TIMEOUT, load_generation
from distribution.product_manifest import NodeRuntime
from distribution.state_safety import UpgradeAuthorization, UpgradeRequest
from cortex_platform.product.paths import resolve_paths


def test_installed_product_paths_resolve_all_darwin_roles_from_exact_runtime(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    from distribution.product_paths import resolve_installed_product_paths

    prefix = tmp_path / "distribution"
    result = DistributionInstaller(prefix).install(
        _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1),
        allow_unsigned_developer=True,
    )
    runtime = prefix / "versions" / result.version / "runtime"
    home = tmp_path / "clean home"

    paths = resolve_installed_product_paths(runtime, home=home, environment={})

    support = home / "Library" / "Application Support" / "Cortex"
    assert paths.config_file == support / "config.toml"
    assert paths.config_dir == support
    assert paths.data_dir == support / "Data"
    assert paths.state_dir == support / "State"
    assert paths.cache_dir == home / "Library" / "Caches" / "Cortex"
    assert paths.log_dir == home / "Library" / "Logs" / "Cortex"
    assert paths.control_database_file == support / "Data" / "control.db"
    assert paths.runtime_update_root == support / "State" / "runtime-update"


def test_installed_product_paths_use_exact_isolated_python_and_closed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from distribution.product_paths import resolve_installed_product_paths

    python = tmp_path / "generation" / "runtime" / "bin" / "python"
    observed: dict[str, object] = {}
    home = tmp_path / "home"
    support = home / "Library" / "Application Support" / "Cortex"
    payload = {
        "cache_dir": str(home / "cache"),
        "config_dir": str(support),
        "config_file": str(support / "config.toml"),
        "control_database_file": str(home / "data" / "control.db"),
        "data_dir": str(home / "data"),
        "log_dir": str(home / "logs"),
        "runtime_update_root": str(home / "state" / "runtime-update"),
        "state_dir": str(home / "state"),
    }

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload) + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    resolve_installed_product_paths(
        python,
        home=home,
        environment={
            "CORTEX_DATA_DIR": str(home / "data"),
            "CORTEX_STATE_DIR": str(home / "state"),
            "SECRET_TOKEN": "must-not-cross-boundary",
        },
    )

    command = observed["command"]
    assert isinstance(command, list)
    assert command[:2] == [str(python), "-I"]
    assert "from cortex_platform.product.paths import resolve_paths" in command[-1]
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["HOME"] == str(home)
    assert environment["CORTEX_DATA_DIR"] == str(home / "data")
    assert environment["CORTEX_STATE_DIR"] == str(home / "state")
    assert "SECRET_TOKEN" not in environment


@pytest.mark.parametrize(
    "payload",
    [
        "not-json\n",
        "{}\n",
        '{"config_file":"relative"}\n',
        json.dumps({
            "cache_dir": "/cache",
            "config_dir": "/config",
            "config_file": "/config/config.toml",
            "control_database_file": "/data/control.db",
            "data_dir": "/data",
            "log_dir": "/logs",
            "runtime_update_root": "/state/runtime-update",
            "state_dir": "",
        }) + "\n",
        json.dumps({
            "cache_dir": "/cache",
            "config_dir": "/config",
            "config_file": "/config/config.toml",
            "control_database_file": "/data/control.db",
            "data_dir": "/data",
            "log_dir": "/logs",
            "runtime_update_root": "/state/runtime-update",
            "state_dir": 7,
        }) + "\n",
        (
            '{"cache_dir":"/cache","config_dir":"/config",'
            '"config_file":"/config/config.toml",'
            '"control_database_file":"/data/control.db",'
            '"data_dir":"/data","log_dir":"/logs",'
            '"runtime_update_root":"/state/runtime-update",'
            '"state_dir":"/state","state_dir":"/replacement"}\n'
        ),
    ],
)
def test_installed_product_paths_reject_invalid_output_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    from distribution.product_paths import (
        InstalledProductPathsError,
        resolve_installed_product_paths,
    )

    marker = "private-resolver-output"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            payload,
            marker,
        ),
    )

    with pytest.raises(InstalledProductPathsError) as raised:
        resolve_installed_product_paths(
            tmp_path / "runtime",
            home=tmp_path / "home",
            environment={},
        )
    assert marker not in str(raised.value)


def test_installed_product_paths_sanitize_nonzero_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from distribution.product_paths import (
        InstalledProductPathsError,
        resolve_installed_product_paths,
    )

    marker = "private-runtime-failure"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 9, "", marker),
    )
    with pytest.raises(InstalledProductPathsError) as nonzero:
        resolve_installed_product_paths(
            tmp_path / "runtime",
            home=tmp_path / "home",
            environment={},
        )
    assert marker not in str(nonzero.value)

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired([marker], 1, stderr=marker)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(InstalledProductPathsError) as timed_out:
        resolve_installed_product_paths(
            tmp_path / "runtime",
            home=tmp_path / "home",
            environment={},
        )
    assert marker not in str(timed_out.value)


def _bundle(
    root: Path,
    wheels: tuple[Path, Path],
    release_id: str,
    sequence: int,
) -> Path:
    return BundleBuilder(root).assemble(
        release_id=release_id,
        release_sequence=sequence,
        source_commit=f"{sequence:x}" * 40,
        lock_sha256=f"{sequence + 1:x}" * 64,
        wheels=wheels,
        created_at=f"2026-07-23T12:00:0{sequence}Z",
    ).path


def _composed_bundle(
    root: Path,
    wheels: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    release_id: str,
    sequence: int,
) -> Path:
    web_root, web_ledger = web_closure
    return BundleBuilder(root).assemble(
        release_id=release_id,
        release_sequence=sequence,
        source_commit=f"{sequence:x}" * 40,
        lock_sha256=f"{sequence + 1:x}" * 64,
        wheels=wheels,
        created_at=f"2026-07-28T12:00:0{sequence}Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=f"{sequence + 2:x}" * 64,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path


def _fake_node(path: Path, analyser_node: Path) -> Path:
    # Verifying a composed generation parses its Web payload with the staged
    # Node, so this fixture hands that one call to a real Node.
    delegate = (
        f'if [ "$1" = "--no-warnings" ]; then exec "{analyser_node}" "$@"; fi\n'
    ).encode()
    path.write_bytes(
        b"#!/bin/sh\n" + delegate + b"exit 0\n" + b"# node fixture\n" * 4096
    )
    path.chmod(0o500)
    return path


def _stage_fake_node(
    observed_sources: list[Path],
):
    def stage(source: Path, destination: Path, *, stage_root: Path) -> NodeRuntime:
        observed_sources.append(source)
        assert destination.parent == stage_root
        shutil.copyfile(source, destination)
        destination.chmod(0o500)
        return NodeRuntime(
            version="v26.0.0",
            platform="darwin",
            architecture="arm64",
            executable_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        )

    return stage


def test_schema2_install_composes_a_startable_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    node = _fake_node(tmp_path / "node", analyser_node)
    observed_sources: list[Path] = []
    monkeypatch.setattr(
        install,
        "stage_node_runtime",
        _stage_fake_node(observed_sources),
    )
    bundle = _composed_bundle(
        tmp_path / "bundle",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-1",
        1,
    )
    installer = DistributionInstaller(prefix)

    result = installer.install(
        bundle,
        node_executable=node,
        allow_unsigned_developer=True,
    )

    generation = installer.current_generation()
    loaded = load_generation(generation)
    assert result.version == generation.name
    assert loaded.web_root == generation / "web"
    assert loaded.node_executable == generation / "node-runtime" / "bin" / "node"
    assert (generation / "product-manifest.json").is_file()
    assert observed_sources == [node]


def test_schema2_install_requires_node_before_pointer_publication(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _composed_bundle(
        tmp_path / "bundle",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-1",
        1,
    )

    # A composed bundle cannot be admitted without a Node: its Web closure is
    # decided by parsing the payload, so verification refuses before any state
    # is created.
    with pytest.raises(InstallError, match="requires a Node executable"):
        DistributionInstaller(prefix).install(
            bundle,
            allow_unsigned_developer=True,
        )

    assert not (prefix / "current.json").exists()
    assert not (prefix / "last-known-good.json").exists()
    assert not (prefix / "versions").exists()


def test_schema2_upgrade_reuses_current_staged_node(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    node = _fake_node(tmp_path / "node", analyser_node)
    observed_sources: list[Path] = []
    monkeypatch.setattr(
        install,
        "stage_node_runtime",
        _stage_fake_node(observed_sources),
    )
    first = _composed_bundle(
        tmp_path / "bundle one",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-1",
        1,
    )
    second = _composed_bundle(
        tmp_path / "bundle two",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-2",
        2,
    )
    installer = DistributionInstaller(prefix, state_safety=_RecordingStateSafety())
    installed = installer.install(
        first,
        node_executable=node,
        allow_unsigned_developer=True,
    )
    _paths, runtime_root, home = _upgrade_context(tmp_path)

    upgraded = installer.upgrade(
        second,
        runtime_root=runtime_root,
        home=home,
        allow_unsigned_developer=True,
    )

    first_staged_node = prefix / "versions" / installed.version / "node-runtime/bin/node"
    assert observed_sources == [node, first_staged_node]
    assert load_generation(prefix / "versions" / upgraded.version).node_executable.is_file()


def test_schema2_candidate_composition_failure_preserves_current_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    node = _fake_node(tmp_path / "node", analyser_node)
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    first = _composed_bundle(
        tmp_path / "bundle one",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-1",
        1,
    )
    second = _composed_bundle(
        tmp_path / "bundle two",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-2",
        2,
    )
    installer = DistributionInstaller(prefix)
    installed = installer.install(
        first,
        node_executable=node,
        allow_unsigned_developer=True,
    )
    pointer_before = (prefix / "current.json").read_bytes()
    _paths, runtime_root, home = _upgrade_context(tmp_path)

    def fail_stage(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected Node composition failure")

    monkeypatch.setattr(install, "stage_node_runtime", fail_stage)
    with pytest.raises(InstallError, match="generation composition failed"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )

    assert (prefix / "current.json").read_bytes() == pointer_before
    assert [path.name for path in (prefix / "versions").iterdir()] == [installed.version]


def test_installed_cortex_launcher_resolves_current_composed_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.install as install
    from distribution.cli import main

    prefix = tmp_path / "distribution"
    home = tmp_path / "clean home"
    node = _fake_node(tmp_path / "node", analyser_node)
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    bundle = _composed_bundle(
        tmp_path / "bundle",
        wheel_pair,
        web_closure,
        analyser_node,
        "cortex-dev-1",
        1,
    )
    assert main(
        [
            "install",
            "--bundle",
            str(bundle),
            "--prefix",
            str(prefix),
            "--node-executable",
            str(node),
            "--allow-unsigned-developer",
        ]
    ) == 0
    installed = json.loads(capsys.readouterr().out)["result"]
    launcher = prefix / "bin" / "cortex"

    completed = subprocess.run(
        [str(launcher), "status", "--home", str(home)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] == {"state": "stopped"}
    assert launcher.stat().st_mode & 0o777 == 0o700
    body = launcher.read_text()
    assert str(prefix) not in body
    assert installed["version"] not in body
    assert str(node) not in body
    assert "token" not in body.casefold()


def _upgrade_context(tmp_path: Path) -> tuple[object, Path, Path]:
    home = tmp_path / "home"
    paths = resolve_paths(environ={"HOME": str(home)}, platform="darwin")
    return paths, paths.runtime_update_root, home


class _RecordingStateSafety:
    def __init__(self) -> None:
        self.authorized: UpgradeRequest | None = None
        self.consumed: UpgradeRequest | None = None
        self.authorization: UpgradeAuthorization | None = None

    def authorize(self, request: UpgradeRequest) -> UpgradeAuthorization:
        self.authorized = request
        self.authorization = UpgradeAuthorization()
        return self.authorization

    def consume(
        self,
        authorization: UpgradeAuthorization,
        request: UpgradeRequest,
    ) -> None:
        assert authorization is self.authorization
        assert request == self.authorized
        self.consumed = request


def test_install_repeat_upgrade_and_rollback_are_transactional(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:1/forbidden")
    monkeypatch.setenv("PIP_NO_INDEX", "0")
    prefix = tmp_path / "clean home with spaces" / "distribution"
    state_safety = _RecordingStateSafety()
    installer = DistributionInstaller(prefix, state_safety=state_safety)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)

    result = installer.install(first, allow_unsigned_developer=True)
    assert result.action == "installed"
    assert installer.doctor()["developer_usable"] is False
    current_digest = installer.doctor()["version_digest"]
    assert installer.install(first, allow_unsigned_developer=True).action == "unchanged"

    with pytest.raises(InstallError, match="explicit upgrade"):
        installer.install(second, allow_unsigned_developer=True)
    paths, runtime_root, home = _upgrade_context(tmp_path)
    upgraded = installer.upgrade(
        second,
        runtime_root=runtime_root,
        home=home,
        allow_unsigned_developer=True,
    )
    assert upgraded.action == "upgraded"
    assert installer.doctor()["release_id"] == "cortex-dev-2"
    assert len(tuple((prefix / "versions").iterdir())) == 2
    assert len(tuple((prefix / "snapshots").iterdir())) == 1
    assert state_safety.authorized == state_safety.consumed
    assert state_safety.authorized is not None
    assert state_safety.authorized.current_bundle_digest == current_digest
    assert state_safety.authorized.candidate_bundle_digest == installer.doctor()[
        "version_digest"
    ]
    assert state_safety.authorized.current_version == result.version
    assert state_safety.authorized.candidate_version == upgraded.version
    assert state_safety.authorized.candidate_control_schema == 10
    assert state_safety.authorized.control_database == paths.control_database_file
    assert state_safety.authorized.identity_companion == (
        paths.control_database_file.with_name(".control.db.transport.key")
    )
    rolled_back = installer.rollback(
        runtime_root=runtime_root,
        home=home,
    )
    assert rolled_back.release_id == "cortex-dev-1"
    assert installer.doctor()["release_id"] == "cortex-dev-1"


def test_unsigned_downgrade_and_failed_upgrade_fail_closed(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)

    with pytest.raises(InstallError, match="unsigned"):
        installer.install(first)
    installer.install(first, allow_unsigned_developer=True)
    before = installer.doctor()

    monkeypatch.setattr(installer, "_health_check", lambda _runtime: (_ for _ in ()).throw(InstallError("injected health failure")))
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    with pytest.raises(InstallError, match="injected"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )
    assert installer.doctor()["version_digest"] == before["version_digest"]

    healthy = DistributionInstaller(prefix)
    healthy.upgrade(
        second,
        runtime_root=runtime_root,
        home=home,
        allow_unsigned_developer=True,
    )
    with pytest.raises(InstallError, match="downgrade"):
        healthy.upgrade(
            first,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )


def test_a_sealed_generation_tree_is_removed_rather_than_half_removed(
    tmp_path: Path,
) -> None:
    """A refused upgrade must not leave the sealed interpreter behind.

    `stage_python_runtime` seals its tree read-only, and `shutil.rmtree` cannot
    unlink out of a directory with no write bit. The two cleanup paths that
    matter -- a failed staging and a refused authorization -- both used
    `ignore_errors=True`, so they removed the 1.5 GB venv, left the 92 MB
    sealed interpreter, and reported success. The next upgrade to that version
    then found the directory present and tried to verify a partial generation.
    """

    generation = tmp_path / "versions" / "cortex-dev-1-aaaaaaaaaaaaaaaa"
    sealed = generation / "python-runtime" / "lib"
    sealed.mkdir(parents=True)
    (sealed / "payload").write_bytes(b"interpreter")
    (generation / "runtime").mkdir()
    (generation / "runtime" / "marker").write_bytes(b"venv")
    for directory in (sealed, sealed.parent):
        directory.chmod(0o500)

    # Pin the failure mode itself: without unsealing, this silently half-works.
    shutil.rmtree(generation, ignore_errors=True)
    assert generation.exists()
    assert (sealed / "payload").exists()
    assert not (generation / "runtime").exists()

    _remove_generation_tree(generation, ignore_errors=False)
    assert not generation.exists()


def test_doctor_reports_whether_a_stateful_upgrade_is_authorized(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    installer.install(
        _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1),
        allow_unsigned_developer=True,
    )
    paths, runtime_root, home = _upgrade_context(tmp_path)

    fresh = installer.doctor(runtime_root=runtime_root, home=home)
    assert fresh["stateful_upgrade"] == {
        "required": False,
        "authorized": True,
        "reason": None,
        "proof_id": None,
    }

    paths.data_dir.mkdir(parents=True, mode=0o700)
    connection = sqlite3.connect(paths.control_database_file)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            [(version,) for version in range(1, 11)],
        )
        connection.commit()
    finally:
        connection.close()
    paths.control_database_file.chmod(0o600)
    companion = paths.control_database_file.with_name(".control.db.transport.key")
    companion.write_bytes(b"i" * 32)
    companion.chmod(0o600)

    stateful = installer.doctor(runtime_root=runtime_root, home=home)
    assert stateful["stateful_upgrade"] == {
        "required": True,
        "authorized": False,
        "reason": "verified backup authorization is required",
        "proof_id": None,
    }


def test_upgrade_rejects_partial_or_unprotected_control_state(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    paths, runtime_root, home = _upgrade_context(tmp_path)
    paths.data_dir.mkdir(parents=True, mode=0o700)
    paths.control_database_file.write_bytes(b"SQLite format 3\0partial")

    with pytest.raises(InstallError, match="control state is incomplete"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )

    companion = paths.control_database_file.with_name(".control.db.transport.key")
    companion.write_bytes(b"i" * 32)
    companion.chmod(0o600)
    paths.control_database_file.chmod(0o600)
    # The pair is now complete and private, but these bytes are not a database.
    with pytest.raises(InstallError, match="control state is unreadable"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )

    # The real ControlStore creates both files 0o600 and re-validates that on
    # every open, so a readable-by-anyone control database is refused before
    # anything else is read out of it.
    companion.chmod(0o644)
    with pytest.raises(InstallError, match="control state is unsafe"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )
    assert installer.doctor()["release_id"] == "cortex-dev-1"
    assert len(tuple((prefix / "versions").iterdir())) == 1


def test_upgrade_rejects_an_alternate_lifecycle_root(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    _paths, _runtime_root, home = _upgrade_context(tmp_path)

    with pytest.raises(InstallError, match="canonical lifecycle root"):
        installer.upgrade(
            second,
            runtime_root=tmp_path / "decoy-runtime",
            home=home,
            allow_unsigned_developer=True,
        )

    assert installer.doctor()["release_id"] == "cortex-dev-1"
    assert len(tuple((prefix / "versions").iterdir())) == 1


def test_install_rejects_an_existing_root_with_a_missing_current_pointer(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    (prefix / "current.json").unlink()

    with pytest.raises(InstallError, match="missing its current pointer"):
        installer.install(second, allow_unsigned_developer=True)

    assert (prefix / "last-known-good.json").is_file()
    assert len(tuple((prefix / "versions").iterdir())) == 1


def test_first_install_can_retry_after_an_empty_staging_failure(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    original_stage = installer._stage_version

    def fail_after_parent_creation(_bundle_path: object, destination: Path) -> None:
        destination.parent.mkdir(parents=True)
        raise InstallError("injected staging failure")

    monkeypatch.setattr(installer, "_stage_version", fail_after_parent_creation)
    with pytest.raises(InstallError, match="injected staging failure"):
        installer.install(bundle, allow_unsigned_developer=True)
    assert not (prefix / "versions").exists()

    monkeypatch.setattr(installer, "_stage_version", original_stage)
    assert installer.install(bundle, allow_unsigned_developer=True).action == "installed"


def test_upgrade_rejects_current_pointer_drift_without_publishing(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    pointer_path = prefix / "current.json"
    original = json.loads(pointer_path.read_text())
    drifted = {**original, "release_sequence": 99}

    @contextlib.contextmanager
    def drift_before_commit(*_args: object, **_kwargs: object):
        pointer_path.write_text(json.dumps(drifted, sort_keys=True) + "\n")
        yield

    monkeypatch.setattr(install, "lifecycle_quiescence", drift_before_commit)
    with pytest.raises(InstallError, match="changed during upgrade"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )

    assert json.loads(pointer_path.read_text()) == drifted
    assert json.loads((prefix / "last-known-good.json").read_text()) == original
    assert not (prefix / "snapshots").exists()
    assert len(tuple((prefix / "versions").iterdir())) == 1


def test_upgrade_rejects_product_path_drift_without_publishing(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    original_resolver = install.resolve_installed_product_paths
    calls = 0

    def changing_paths(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        resolved = original_resolver(*args, **kwargs)
        if calls == 1:
            return resolved
        return SimpleNamespace(
            control_database_file=tmp_path / "changed" / "control.db",
            runtime_update_root=resolved.runtime_update_root,
        )

    monkeypatch.setattr(install, "resolve_installed_product_paths", changing_paths)
    with pytest.raises(InstallError, match="product paths changed"):
        installer.upgrade(
            second,
            runtime_root=runtime_root,
            home=home,
            allow_unsigned_developer=True,
        )

    assert installer.doctor()["release_id"] == "cortex-dev-1"
    assert not (prefix / "snapshots").exists()
    assert len(tuple((prefix / "versions").iterdir())) == 1


def test_install_and_uninstall_preserve_external_user_assets(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    user_root = tmp_path / "user"
    prefix = user_root / "distribution"
    paths = resolve_paths(environ={"HOME": str(user_root)}, platform="darwin")
    sentinels = {
        user_root / "data" / "research.db": b"SQLite format 3\0database",
        user_root / "gdrive" / "paper.md": b"research artifact",
        user_root / "keys" / "cortex.agekey": b"AGE-SECRET-KEY-test",
        user_root / "config" / "config.toml": b"secret_ref='keychain://cortex/token'",
        user_root / "hermes" / "state.db": b"hermes state",
        paths.control_database_file: b"SQLite format 3\0control state",
        paths.control_database_file.with_name(
            ".control.db.transport.key"
        ): b"i" * 32,
    }
    for path, contents in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sentinels}

    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installer.install(bundle, allow_unsigned_developer=True)
    installer.uninstall(
        runtime_root=paths.runtime_update_root,
        home=user_root,
    )

    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sentinels} == before
    assert not prefix.exists()


def test_concurrent_install_waits_for_uninstall_root_removal(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "distribution"
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    DistributionInstaller(prefix).install(bundle, allow_unsigned_developer=True)
    cleanup_reached = threading.Event()
    release_cleanup = threading.Event()
    install_finished = threading.Event()
    failures: list[BaseException] = []
    install_actions: list[str] = []
    original_rmdir = Path.rmdir

    def paused_rmdir(path: Path) -> None:
        if path == prefix:
            cleanup_reached.set()
            if not release_cleanup.wait(timeout=5):
                raise AssertionError("test did not release uninstall cleanup")
        original_rmdir(path)

    def uninstall() -> None:
        try:
            DistributionInstaller(prefix).uninstall(
                runtime_root=runtime_root,
                home=home,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def install() -> None:
        try:
            result = DistributionInstaller(prefix).install(
                bundle,
                allow_unsigned_developer=True,
            )
            install_actions.append(result.action)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            install_finished.set()

    monkeypatch.setattr(Path, "rmdir", paused_rmdir)
    uninstall_thread = threading.Thread(target=uninstall)
    uninstall_thread.start()
    assert cleanup_reached.wait(timeout=5)

    install_thread = threading.Thread(target=install)
    install_thread.start()
    assert not install_finished.wait(timeout=0.2)
    release_cleanup.set()
    uninstall_thread.join(timeout=10)
    install_thread.join(timeout=30)

    assert not uninstall_thread.is_alive()
    assert not install_thread.is_alive()
    assert failures == []
    assert install_actions == ["installed"]
    assert DistributionInstaller(prefix).doctor()["developer_usable"] is False
    assert (prefix / ".cortex-distribution-root.json").is_file()


def test_rollback_rejects_a_target_older_than_the_current_control_schema(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    paths, runtime_root, home = _upgrade_context(tmp_path)
    installer.upgrade(
        second,
        runtime_root=runtime_root,
        home=home,
        allow_unsigned_developer=True,
    )
    paths.control_database_file.parent.mkdir(parents=True)
    with sqlite3.connect(paths.control_database_file) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'now')",
            ((version,) for version in range(1, 11)),
        )
    paths.control_database_file.with_name(".control.db.transport.key").write_bytes(
        b"i" * 32
    )
    monkeypatch.setattr(installer, "_control_schema", lambda _runtime: 9)

    with pytest.raises(InstallError, match="cannot read the current control schema"):
        installer.rollback(runtime_root=runtime_root, home=home)

    assert installer.doctor()["release_id"] == "cortex-dev-2"


def test_rollback_recovers_lkg_when_current_pointer_write_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle one", wheel_pair, "cortex-dev-1", 1)
    second = _bundle(tmp_path / "bundle two", wheel_pair, "cortex-dev-2", 2)
    installer.install(first, allow_unsigned_developer=True)
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    installer.upgrade(
        second,
        runtime_root=runtime_root,
        home=home,
        allow_unsigned_developer=True,
    )
    current_before = json.loads((prefix / "current.json").read_text())
    lkg_before = json.loads((prefix / "last-known-good.json").read_text())
    original_atomic = install._atomic_json
    failed = False

    def fail_current_once(path: Path, value: object) -> None:
        nonlocal failed
        if path == prefix / "current.json" and not failed:
            failed = True
            raise OSError("injected current pointer failure")
        original_atomic(path, value)

    monkeypatch.setattr(install, "_atomic_json", fail_current_once)
    with pytest.raises(OSError, match="injected current pointer failure"):
        installer.rollback(runtime_root=runtime_root, home=home)

    assert json.loads((prefix / "current.json").read_text()) == current_before
    assert json.loads((prefix / "last-known-good.json").read_text()) == lkg_before


def test_uninstall_requires_quiescence_without_removing_the_installation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install
    from distribution.lifecycle import LifecycleError

    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installed = installer.install(bundle, allow_unsigned_developer=True)
    _paths, runtime_root, home = _upgrade_context(tmp_path)

    @contextlib.contextmanager
    def running_lifecycle(*_args: object, **_kwargs: object):
        raise LifecycleError("operation requires the lifecycle to be stopped")
        yield

    monkeypatch.setattr(install, "lifecycle_quiescence", running_lifecycle)
    with pytest.raises(InstallError, match="requires the lifecycle to be stopped"):
        installer.uninstall(runtime_root=runtime_root, home=home)

    assert installer.doctor()["release_id"] == "cortex-dev-1"
    assert (prefix / "versions" / installed.version).is_dir()


def test_distribution_cli_classifies_an_upgrade_that_requires_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.cli as cli

    class FailingInstaller:
        def __init__(self, _prefix: Path | None) -> None:
            pass

        def upgrade(self, *_args: object, **_kwargs: object) -> object:
            raise InstallError("operation requires the lifecycle to be stopped")

    monkeypatch.setattr(cli, "DistributionInstaller", FailingInstaller)
    result = cli.main(
        [
            "upgrade",
            "--bundle",
            str(tmp_path / "bundle"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--home",
            str(tmp_path / "home"),
        ]
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out) == {
        "category": "upgrade_requires_stop",
        "error": "operation requires the lifecycle to be stopped",
        "ok": False,
    }


def test_doctor_reports_installed_but_not_usable_without_runtime_root(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install

    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installer.install(bundle, allow_unsigned_developer=True)
    missing_runtime = tmp_path / "missing-runtime"
    monkeypatch.setattr(
        install,
        "LifecycleManager",
        lambda *_args, **_kwargs: pytest.fail(
            "doctor constructed lifecycle state for a missing runtime root"
        ),
        raising=False,
    )

    report = installer.doctor(runtime_root=missing_runtime, home=tmp_path / "home")

    assert report["installed"] is True
    assert report["developer_usable"] is False
    assert report["web_url"] is None
    assert report["category"] == "runtime_root_missing"
    assert not missing_runtime.exists()
    rendered = json.dumps(report, sort_keys=True)
    assert str(Path(__file__).resolve().parents[2]) not in rendered
    assert "token" not in rendered.casefold()


@pytest.mark.parametrize(
    ("status", "expected_category", "probe"),
    [
        (
            {"state": "stopped", "generation_identity": None},
            "stopped",
            None,
        ),
        (
            {"state": "stale", "generation_identity": "x" * 64},
            "wrong_generation",
            None,
        ),
        (
            {"state": "stale", "generation_identity": "g" * 64},
            "lifecycle_unhealthy",
            None,
        ),
        (
            {"state": "unhealthy", "generation_identity": "g" * 64},
            "lifecycle_unhealthy",
            None,
        ),
        (
            {
                "state": "running",
                "generation_identity": "g" * 64,
                "control_port": 8765,
                "web_port": None,
            },
            "lifecycle_unhealthy",
            None,
        ),
        (
            {
                "state": "running",
                "generation_identity": "g" * 64,
                "control_port": 8765,
                "web_port": 8766,
            },
            "front_door_unhealthy",
            {
                "healthy": False,
                "web_url": None,
                "category": "front_door_unhealthy",
            },
        ),
    ],
    ids=(
        "stopped",
        "wrong-generation",
        "stale-current-generation",
        "unhealthy",
        "control-only",
        "failed-probe",
    ),
)
def test_doctor_rejects_non_usable_lifecycle_states(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    status: dict[str, object],
    expected_category: str,
    probe: dict[str, object] | None,
) -> None:
    import distribution.install as install
    from distribution.lifecycle import LifecycleStatus, LocalFrontDoorHealth

    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installed = installer.install(bundle, allow_unsigned_developer=True)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    observed_probes = 0

    class DiagnosticLifecycle:
        def __init__(
            self,
            generation: Path,
            selected_runtime: Path,
            *,
            home: Path,
            pin_tools: bool = True,
        ) -> None:
            assert generation == prefix / "versions" / installed.version
            assert selected_runtime == runtime_root
            assert home == tmp_path / "home"
            # An installed generation is anchored by the pointer, so `doctor`
            # exempts it from the bundled-tools admission pin.
            assert pin_tools is False
            self.generation = SimpleNamespace(identity="g" * 64)

        def status(self) -> LifecycleStatus:
            return LifecycleStatus(**status)

        def probe_local_front_door(
            self, *, timeout: float = FRONT_DOOR_PROBE_TIMEOUT
        ) -> LocalFrontDoorHealth:
            nonlocal observed_probes
            observed_probes += 1
            # `doctor` names the budget explicitly rather than inheriting a
            # default, so a double that ignored the keyword would let the call
            # site drop it again without a test noticing.
            assert timeout == FRONT_DOOR_PROBE_TIMEOUT
            assert probe is not None
            return LocalFrontDoorHealth(**probe)

    monkeypatch.setattr(install, "LifecycleManager", DiagnosticLifecycle, raising=False)

    report = installer.doctor(runtime_root=runtime_root, home=tmp_path / "home")

    assert report["developer_usable"] is False
    assert report["web_url"] is None
    assert report["category"] == expected_category
    assert observed_probes == (1 if probe is not None else 0)


def test_doctor_rejects_current_generation_change_during_probe(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.install as install
    from distribution.lifecycle import LifecycleStatus, LocalFrontDoorHealth

    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installed = installer.install(bundle, allow_unsigned_developer=True)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)

    class ChangingLifecycle:
        def __init__(
            self,
            generation: Path,
            selected_runtime: Path,
            *,
            home: Path,
            pin_tools: bool = True,
        ) -> None:
            assert generation == prefix / "versions" / installed.version
            assert selected_runtime == runtime_root
            assert home == tmp_path / "home"
            # An installed generation is anchored by the pointer, so `doctor`
            # exempts it from the bundled-tools admission pin.
            assert pin_tools is False
            self.generation = SimpleNamespace(identity="g" * 64)

        def status(self) -> LifecycleStatus:
            return LifecycleStatus(
                "running",
                generation_identity="g" * 64,
                control_port=8765,
                web_port=8766,
            )

        def probe_local_front_door(
            self, *, timeout: float = FRONT_DOOR_PROBE_TIMEOUT
        ) -> LocalFrontDoorHealth:
            assert timeout == FRONT_DOOR_PROBE_TIMEOUT
            replacement = {
                "schema_version": 1,
                "version": f"cortex-dev-2-{'2' * 16}",
                "release_id": "cortex-dev-2",
                "release_sequence": 2,
                "bundle_digest": "a" * 64,
            }
            (prefix / "current.json").write_text(
                json.dumps(replacement, sort_keys=True) + "\n"
            )
            return LocalFrontDoorHealth(
                healthy=True,
                web_url="http://127.0.0.1:8766",
                category="healthy",
            )

    monkeypatch.setattr(install, "LifecycleManager", ChangingLifecycle)

    report = installer.doctor(runtime_root=runtime_root, home=tmp_path / "home")

    assert report["developer_usable"] is False
    assert report["web_url"] is None
    assert report["category"] == "wrong_generation"


def test_doctor_detects_installed_bundle_tampering(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installed = installer.install(bundle, allow_unsigned_developer=True)
    manifest = prefix / "versions" / installed.version / "bundle" / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"tampered")

    report = installer.doctor()
    assert report["developer_usable"] is False
    assert report["ga_ready"] is False
    assert report["category"] == "installation_unhealthy"
    assert "error" not in report
    with pytest.raises(InstallError, match="checksum"):
        installer.install(bundle, allow_unsigned_developer=True)


def test_doctor_sanitizes_installed_health_probe_failures(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installer.install(bundle, allow_unsigned_developer=True)
    private_failure = f"token-at-{prefix}"
    monkeypatch.setattr(
        installer,
        "_health_check",
        lambda _runtime: (_ for _ in ()).throw(OSError(private_failure)),
    )

    report = installer.doctor(runtime_root=tmp_path / "runtime", home=tmp_path / "home")

    assert report["installed"] is True
    assert report["developer_usable"] is False
    assert report["category"] == "installation_unhealthy"
    rendered = json.dumps(report, sort_keys=True)
    assert private_failure not in rendered
    assert str(prefix) not in rendered
    assert "token" not in rendered.casefold()


def test_doctor_rejects_unbound_pointer_identity_without_disclosure(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installer.install(bundle, allow_unsigned_developer=True)
    pointer_path = prefix / "current.json"
    pointer = json.loads(pointer_path.read_text())
    original_version = pointer["version"]
    private_identity = "token-private-release"
    pointer["release_id"] = private_identity
    pointer["version"] = f"{private_identity}-{pointer['bundle_digest'][:16]}"
    (prefix / "versions" / original_version).rename(
        prefix / "versions" / pointer["version"]
    )
    pointer_path.write_text(json.dumps(pointer, sort_keys=True) + "\n")

    report = installer.doctor()

    assert report == {
        "installed": True,
        "developer_usable": False,
        "ga_ready": False,
        "web_url": None,
        "category": "installation_unhealthy",
    }
    rendered = json.dumps(report, sort_keys=True)
    assert private_identity not in rendered
    assert str(prefix) not in rendered
    assert "token" not in rendered.casefold()


def test_doctor_rejects_boolean_pointer_schema_version(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
    installer = DistributionInstaller(prefix)
    installer.install(bundle, allow_unsigned_developer=True)
    pointer_path = prefix / "current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["schema_version"] = True
    pointer_path.write_text(json.dumps(pointer, sort_keys=True) + "\n")

    assert installer.doctor() == {
        "installed": True,
        "developer_usable": False,
        "ga_ready": False,
        "web_url": None,
        "category": "installation_unhealthy",
    }


def test_installer_rejects_hostile_root_symlink(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    target = tmp_path / "must remain empty"
    target.mkdir()
    prefix = tmp_path / "distribution"
    prefix.symlink_to(target, target_is_directory=True)
    bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)

    with pytest.raises(InstallError, match="root is a symlink"):
        DistributionInstaller(prefix).install(bundle, allow_unsigned_developer=True)
    assert list(target.iterdir()) == []


def test_default_install_roots_are_user_level(tmp_path: Path) -> None:
    assert default_distribution_root(home=tmp_path, system="Darwin") == (
        tmp_path / "Library" / "Application Support" / "Cortex" / "Distribution"
    )
    assert default_distribution_root(
        home=tmp_path,
        system="Linux",
        env={"XDG_DATA_HOME": str(tmp_path / "xdg data")},
    ) == tmp_path / "xdg data" / "cortex" / "distribution"


def test_generation_composition_failure_names_an_actionable_cause(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Composition can fail for unrelated reasons, and collapsing them into one
    # opaque message leaves an operator with nothing to act on. The project's own
    # errors are quoted; a foreign error must not leak a filesystem path.
    from distribution import install
    from distribution.lifecycle import LifecycleError

    prefix = tmp_path / "distribution"
    bundle = _composed_bundle(
        tmp_path / "bundle", wheel_pair, web_closure, analyser_node, "cortex-dev-1", 1
    )
    node = _fake_node(tmp_path / "node", analyser_node)
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))

    def curated(_stage: Path) -> object:
        raise LifecycleError("Node stage directory contains a symlink ancestor")

    monkeypatch.setattr(install, "load_generation", curated)
    with pytest.raises(InstallError, match="symlink ancestor"):
        DistributionInstaller(prefix).install(
            bundle, node_executable=node, allow_unsigned_developer=True
        )

    def leaky(_stage: Path) -> object:
        raise ValueError(f"detail about {tmp_path}/private/location")

    monkeypatch.setattr(install, "load_generation", leaky)
    with pytest.raises(InstallError) as caught:
        DistributionInstaller(prefix).install(
            bundle, node_executable=node, allow_unsigned_developer=True
        )
    assert str(caught.value) == "generation composition failed: ValueError"
    assert str(tmp_path) not in str(caught.value)


def _installed_composed_prefix(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, str]:
    import distribution.install as install
    from distribution.cli import main

    prefix = tmp_path / "distribution"
    node = _fake_node(tmp_path / "node", analyser_node)
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    bundle = _composed_bundle(
        tmp_path / "bundle", wheel_pair, web_closure, analyser_node, "cortex-dev-1", 1
    )
    assert main(
        [
            "install",
            "--bundle",
            str(bundle),
            "--prefix",
            str(prefix),
            "--node-executable",
            str(node),
            "--allow-unsigned-developer",
        ]
    ) == 0
    return prefix, json.loads(capsys.readouterr().out)["result"]["version"]


def test_the_launcher_prefers_the_generation_own_interpreter(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An installed generation that carries an interpreter must use it."""

    prefix, version = _installed_composed_prefix(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch, capsys
    )
    embedded = prefix / "versions" / version / "python-runtime" / "bin" / "python3.14"
    embedded.parent.mkdir(parents=True)
    embedded.write_text("#!/bin/sh\necho generation-interpreter-selected\n")
    embedded.chmod(0o700)

    completed = subprocess.run(
        [str(prefix / "bin" / "cortex"), "status"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={"HOME": str(tmp_path / "home"), "LANG": "C", "LC_ALL": "C", "PATH": "/nonexistent"},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "generation-interpreter-selected"


def test_the_launcher_refuses_a_pointer_it_cannot_parse(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prefix, _version = _installed_composed_prefix(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch, capsys
    )
    (prefix / "current.json").write_text('{"version":"../../escape-0123456789abcdef"}')

    completed = subprocess.run(
        [str(prefix / "bin" / "cortex"), "status"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={"HOME": str(tmp_path / "home"), "LANG": "C", "LC_ALL": "C", "PATH": os.defpath},
    )

    assert completed.returncode != 0
    assert "pointer is invalid" in completed.stderr


def _diverge_bundled_tools(previous: pytest.MonkeyPatch) -> None:
    """Make the builder stage a `tools/` tree this verifier will not expect.

    A previous generation ships a `distribution/` that differs from the one
    inspecting it later, which is what makes a cross-generation `doctor` or
    `upgrade` a different situation from admitting a bundle. Diverging the
    staged package marker reproduces exactly that: composition and admission
    both run while the divergence is in force, so the installed generation is
    self-consistent, and the verifier's expectation returns to normal after.
    """

    from distribution import bundle as bundle_module

    previous.setattr(
        bundle_module,
        "_TOOLS_PLATFORM_MARKER",
        bundle_module._TOOLS_PLATFORM_MARKER + "# previous generation\n",
    )


def _marker_file() -> Path:
    """A file holding exactly the marker `_verify_bundled_tools` expects."""

    from distribution import bundle as bundle_module

    path = Path(tempfile.mkdtemp()) / "__init__.py"
    path.write_text(bundle_module._TOOLS_PLATFORM_MARKER)
    return path


def _omit_bundled_platform_tools(previous: pytest.MonkeyPatch) -> None:
    """Stage a `tools/` tree with no `cortex_platform/` package at all.

    ⟦S32-01⟧ The gen-7 shape, and the one `_diverge_bundled_tools` cannot
    reproduce: that fixture mutates the marker's bytes, so the package is still
    there and the staging kernel beside it is still findable. A generation
    composed before the staging kernel existed carries neither, which is what
    made an ungated `_verify_wheel_staging_kernel` refuse a real installed gen 7
    with "bundle carries no staging kernel in its tools" — a bundle that was
    never being admitted, only re-verified under its own pointer.
    """

    from distribution import bundle as bundle_module

    original_copy = bundle_module._copy_distribution_tools
    original_verify = bundle_module._verify_bundled_tools
    package = bundle_module._TOOLS_PLATFORM_PACKAGE

    def without_platform_tools(destination: Path) -> None:
        original_copy(destination)
        shutil.rmtree(destination / "tools" / package)

    def verify_without_platform_tools(files: dict[str, Path]) -> None:
        # Composition and admission both run while the divergence is in force,
        # so the generation is self-consistent to the verifier that produced it
        # — the same discipline `_diverge_bundled_tools` uses. Only the
        # *later* verifier, restored below, sees a generation with no
        # `cortex_platform/`.
        original_verify(
            {
                relative: path
                for relative, path in files.items()
                if not relative.startswith(f"tools/{package}/")
            }
            | {
                f"tools/{bundle_module._TOOLS_RUNTIME_STAGING_RELATIVE}": Path(
                    bundle_module.__file__
                ).resolve().parent.parent
                / bundle_module._TOOLS_RUNTIME_STAGING_RELATIVE,
                f"tools/{package}/__init__.py": _marker_file(),
            }
        )

    previous.setattr(
        bundle_module, "_copy_distribution_tools", without_platform_tools
    )
    previous.setattr(
        bundle_module, "_verify_bundled_tools", verify_without_platform_tools
    )
    # The generation being modelled predates the staging kernel, so its own
    # verifier had no such check. Restored outside this context, which is where
    # the assertions run.
    previous.setattr(
        bundle_module, "_verify_wheel_staging_kernel", lambda files: None
    )


def _rewrite_bundle_ledger(bundle: Path) -> None:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"{path.relative_to(bundle).as_posix()}"
        for path in sorted(
            item
            for item in bundle.rglob("*")
            if item.is_file() and item.name != "checksums.sha256"
        )
    ]
    (bundle / "checksums.sha256").write_text("\n".join(lines) + "\n")


def _installed_previous_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    diverge=_diverge_bundled_tools,
) -> SimpleNamespace:
    """Install a composed generation whose bundled tools this verifier rejects."""

    import distribution.install as install

    prefix = tmp_path / "distribution"
    node = _fake_node(tmp_path / "node", analyser_node)
    monkeypatch.setattr(install, "stage_node_runtime", _stage_fake_node([]))
    installer = DistributionInstaller(prefix)
    with pytest.MonkeyPatch.context() as previous:
        diverge(previous)
        bundle = _composed_bundle(
            tmp_path / "bundle",
            wheel_pair,
            web_closure,
            analyser_node,
            "cortex-dev-1",
            1,
        )
        installed = installer.install(
            bundle,
            node_executable=node,
            allow_unsigned_developer=True,
        )
    return SimpleNamespace(
        installer=installer,
        prefix=prefix,
        bundle=bundle,
        node=node,
        version=installed.version,
        version_dir=prefix / "versions" / installed.version,
        pointer=json.loads((prefix / "current.json").read_text()),
    )


def test_the_bundle_digest_covers_every_bundled_tool(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """The exemption below rests on this: the ledger is the identity.

    Dropping the tools pin for an installed composed generation is only sound
    because no file under `tools/` — nor `cortex-dist` — can change without
    changing the checksum ledger, and the ledger's own digest is the bundle
    identity a distribution pointer records.
    """

    from distribution.bundle import verify_bundle

    bundle = _composed_bundle(
        tmp_path / "bundle", wheel_pair, web_closure, analyser_node, "cortex-dev-1", 1
    )

    verified = verify_bundle(bundle, node_executable=analyser_node)

    ledger = (bundle / "checksums.sha256").read_text()
    covered = {line.partition("  ")[2] for line in ledger.splitlines()}
    shipped = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    }
    tooling = {relative for relative in shipped if relative.startswith("tools/")}
    assert "tools/distribution/bundle.py" in tooling
    assert "tools/cortex_dist.py" in tooling
    assert tooling | {"cortex-dist"} <= covered
    assert covered == shipped
    assert verified.digest == hashlib.sha256(
        (bundle / "checksums.sha256").read_bytes()
    ).hexdigest()


def test_a_legacy_bundle_keeps_the_tools_pin_because_its_ledger_is_not_its_identity(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    """A schema-1 bundle is identified by its manifest, not by its ledger.

    Rewriting a legacy ledger to match a substituted tool leaves the recorded
    digest untouched, so the exemption does not hold and `pin_tools=False` must
    still refuse it.
    """

    from distribution.bundle import BundleVerificationError, verify_bundle

    with pytest.MonkeyPatch.context() as previous:
        _diverge_bundled_tools(previous)
        bundle = _bundle(tmp_path / "bundle", wheel_pair, "cortex-dev-1", 1)
        legacy = verify_bundle(bundle)

    assert legacy.digest == hashlib.sha256(
        (bundle / "manifest.json").read_bytes()
    ).hexdigest()
    with pytest.raises(BundleVerificationError, match="do not match the verifier"):
        verify_bundle(bundle, pin_tools=False)


def test_installed_generation_with_previous_tools_is_still_verified(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )

    verified = state.installer._verify_installed_version(
        state.pointer, state.version_dir
    )

    assert verified.digest == state.pointer["bundle_digest"]
    assert state.version_dir.name == state.pointer["version"]


def test_admission_still_pins_bundled_tools_to_the_verifier(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption is for installed generations only, never for admission."""

    from distribution.bundle import BundleVerificationError, verify_bundle

    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )

    with pytest.raises(BundleVerificationError, match="do not match the verifier"):
        verify_bundle(state.bundle, node_executable=analyser_node)
    with pytest.raises(InstallError, match="do not match the verifier"):
        DistributionInstaller(tmp_path / "second").install(
            state.bundle,
            node_executable=state.node,
            allow_unsigned_developer=True,
        )
    exempt = verify_bundle(
        state.bundle, node_executable=analyser_node, pin_tools=False
    )
    assert exempt.digest == state.pointer["bundle_digest"]


def test_installed_generation_with_a_substituted_tool_is_refused_by_the_ledger(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )
    parser = state.version_dir / "bundle" / "tools" / "distribution" / "bundle.py"
    parser.write_bytes(parser.read_bytes() + b"# substituted\n")

    with pytest.raises(InstallError, match="checksum mismatch"):
        state.installer._verify_installed_version(state.pointer, state.version_dir)


def test_installed_generation_with_a_rewritten_ledger_is_refused_by_the_pointer(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )
    parser = state.version_dir / "bundle" / "tools" / "distribution" / "bundle.py"
    parser.write_bytes(parser.read_bytes() + b"# substituted\n")
    _rewrite_bundle_ledger(state.version_dir / "bundle")

    with pytest.raises(InstallError, match="identity does not match pointer"):
        state.installer._verify_installed_version(state.pointer, state.version_dir)


def test_doctor_diagnoses_a_generation_whose_tools_precede_the_verifier(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipping defect: gen N+1's `doctor` on an installed gen N."""

    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    runtime_root.mkdir(parents=True, mode=0o700)

    report = state.installer.doctor(runtime_root=runtime_root, home=home)

    assert report["category"] == "stopped"
    assert report["stateful_upgrade"] == {
        "required": False,
        "authorized": True,
        "reason": None,
        "proof_id": None,
    }


def test_installed_product_manifest_tampering_is_refused(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freezing the contract must not stop being a contract.

    `product-manifest.json` lives beside the bundle rather than inside it, so it
    is covered by neither the bundle ledger nor the pointer digest: the frozen
    per-schema reference is the whole of its integrity.
    """

    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )
    path = state.version_dir / "product-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["processes"]["private_access"]["enabled_by_default"] = True
    path.write_text(json.dumps(manifest))

    with pytest.raises(InstallError, match="installed composed generation is unavailable"):
        state.installer._verify_installed_version(state.pointer, state.version_dir)


def test_installed_generation_without_platform_tools_is_still_verified(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⟦S32-01⟧ The real gen-7 shape: no `tools/cortex_platform/` whatsoever.

    `_verify_wheel_staging_kernel` looks for the staging kernel inside that
    package, so placing its call beside the tools pin rather than inside it
    refuses an already-installed generation older than the kernel — and that is
    `doctor` and `upgrade` for every gen7→gen8 machine.
    """

    from distribution import bundle as bundle_module

    state = _installed_previous_generation(
        tmp_path,
        wheel_pair,
        web_closure,
        analyser_node,
        monkeypatch,
        diverge=_omit_bundled_platform_tools,
    )
    staged_tools = state.version_dir / "bundle" / "tools"
    assert not (staged_tools / bundle_module._TOOLS_PLATFORM_PACKAGE).exists()
    # The rest of `tools/` is intact, so what is being modelled is a generation
    # that predates the staging kernel rather than a damaged bundle.
    assert (staged_tools / "distribution" / "bundle.py").is_file()

    verified = state.installer._verify_installed_version(
        state.pointer, state.version_dir
    )

    assert verified.digest == state.pointer["bundle_digest"]


def test_admission_still_requires_the_wheel_staging_kernel(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption is re-verification only. Admission still refuses it."""

    from distribution.bundle import BundleVerificationError, verify_bundle

    state = _installed_previous_generation(
        tmp_path,
        wheel_pair,
        web_closure,
        analyser_node,
        monkeypatch,
        diverge=_omit_bundled_platform_tools,
    )

    with pytest.raises(BundleVerificationError):
        verify_bundle(state.bundle, node_executable=analyser_node)


def test_the_launcher_injects_the_prefix_for_every_command_that_accepts_one() -> None:
    """DIST-1's second half: the injection set is derived, not remembered.

    Without the injection a `--prefix`-taking command run through an installed
    launcher binds `default_distribution_root()` instead of the installation
    the launcher belongs to. For `record-proof` that means taking a backup
    proof of a DIFFERENT installation's control state and recording it as this
    one's upgrade authorization -- the exact confusion the proof exists to
    prevent. The launcher execs the installed generation's own tools, so a
    generation whose `cortex-dist` has no such subcommand still refuses it in
    argparse; the injection only decides which installation it names.
    """

    import argparse
    import re

    import distribution.install as install
    from distribution.cli import _parser

    parser = _parser()
    subcommands = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    accepts_prefix = {
        name
        for name, sub in subcommands.choices.items()
        if any("--prefix" in (option.option_strings or []) for option in sub._actions)
    }

    source = Path(install.__file__).read_text()
    literal = re.search(r"prefix_commands = \{([^}]*)\}", source)
    assert literal is not None
    injected = {value.strip().strip("'") for value in literal.group(1).split(",")}

    assert injected == accepts_prefix
    assert "record-proof" in injected
