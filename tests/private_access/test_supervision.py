from __future__ import annotations

import json
import os
import plistlib
import stat
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from deployment.private_access import supervision
from deployment.private_access.config import load_config
from deployment.private_access.secrets import derive_bootstrap_token
from deployment.private_access.supervision import (
    GATEWAY_LABEL,
    WEB_LABEL,
    ServiceManager,
    ServiceSpec,
    SupervisionError,
    exec_web,
)


def _write_config(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _replace_owned_file_for_test(path: Path, content: bytes) -> None:
    path.unlink(missing_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _spec(
    tmp_path: Path,
    config_dict: dict[str, object],
    *,
    release_id: str = "cortex-private-access-1",
    release_sequence: int = 1,
) -> ServiceSpec:
    config = tmp_path / f"{release_id}.json"
    _write_config(config, config_dict)
    gateway = tmp_path / "cortex-private-access-gateway"
    supervisor = tmp_path / "cortex-private-access-supervisor"
    web = tmp_path / "node"
    for path in (gateway, supervisor, web):
        _executable(path)
    working = tmp_path / "web"
    working.mkdir(exist_ok=True)
    return ServiceSpec(
        config_path=config,
        service_root=tmp_path / "service",
        gateway_launcher=gateway,
        supervisor_launcher=supervisor,
        web_working_directory=working,
        web_command=(str(web), "server.js", "--host=127.0.0.1"),
        release_id=release_id,
        release_sequence=release_sequence,
    )


def _manager(spec: ServiceSpec) -> ServiceManager:
    return ServiceManager(spec.service_root)


def _plist(spec: ServiceSpec, version: str, label: str) -> dict[str, object]:
    path = spec.service_root / "versions" / version / f"{label}.plist"
    return plistlib.loads(path.read_bytes())


def test_plan_and_dry_run_are_redacted_and_have_no_side_effects(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)

    plan = manager.plan(spec)
    dry_run = manager.install(spec, dry_run=True)
    rendered = json.dumps(plan, sort_keys=True)

    assert plan["launchctl_invoked"] is False
    assert plan["activation_allowed"] is False
    assert plan["validated_scope"] == "access_boundary_only"
    assert plan["cortex_control_ready"] is False
    assert plan["cleanup_deferred_to_p2_devrel"] is True
    assert plan["launch_agents_published"] is False
    assert plan["launch_agent_descriptors_staged"] == [
        f"{GATEWAY_LABEL}.plist",
        f"{WEB_LABEL}.plist",
    ]
    assert plan["resolved_secret_persisted"] is False
    assert plan["environment"]["CORTEX_ACCESS_BOOTSTRAP_SECRET_REF"] == "<external-reference>"
    assert config_dict["session_bootstrap_secret_ref"] not in rendered
    assert "CORTEX_ACCESS_BOOTSTRAP_TOKEN" not in rendered
    assert "CORTEX_CONTROL_TOKEN" not in rendered
    assert dry_run.action == "would-install"
    assert not spec.service_root.exists()


@pytest.mark.parametrize("nested", [False, True])
def test_supervision_rejects_real_launchagents_root_before_mutation(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, mode=0o700)
    sentinel = launch_agents / "foreign.plist"
    sentinel.write_bytes(b"foreign-launch-agent")
    before = launch_agents.stat()
    monkeypatch.setenv("HOME", str(home))
    root = launch_agents / "Cortex" if nested else launch_agents
    spec = replace(_spec(tmp_path, config_dict), service_root=root)

    with pytest.raises(SupervisionError, match="LaunchAgents"):
        ServiceManager(root).install(spec)

    after = launch_agents.stat()
    assert sentinel.read_bytes() == b"foreign-launch-agent"
    assert not (launch_agents / ".cortex-private-access-service.json").exists()
    assert (before.st_ino, before.st_mode, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def test_supervision_rejects_account_home_launchagents_when_home_is_overridden(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_home = tmp_path / "account-home"
    launch_agents = account_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "environment-home"))
    monkeypatch.setattr(supervision, "_account_home", lambda: account_home)
    spec = replace(_spec(tmp_path, config_dict), service_root=launch_agents)

    with pytest.raises(SupervisionError, match="LaunchAgents"):
        ServiceManager(launch_agents).install(spec)

    assert list(launch_agents.iterdir()) == []


def test_supervision_rejects_symlink_and_writable_ancestors_before_mutation(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    actual = tmp_path / "actual-parent"
    actual.mkdir(mode=0o700)
    link = tmp_path / "linked-parent"
    link.symlink_to(actual, target_is_directory=True)
    linked_root = link / "service"

    with pytest.raises(SupervisionError, match="ancestor"):
        ServiceManager(linked_root).install(replace(spec, service_root=linked_root))

    assert not (actual / "service").exists()

    writable = tmp_path / "writable-parent"
    writable.mkdir(mode=0o700)
    writable.chmod(0o777)
    writable_root = writable / "service"
    before = writable.stat()

    with pytest.raises(SupervisionError, match="ancestor"):
        ServiceManager(writable_root).install(
            replace(spec, service_root=writable_root)
        )

    after = writable.stat()
    assert not writable_root.exists()
    assert (before.st_ino, before.st_mode, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def test_unowned_service_root_is_rejected_without_metadata_changes(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    root = spec.service_root
    root.mkdir(mode=0o750)
    sentinel = root / "foreign.txt"
    sentinel.write_bytes(b"foreign-state")
    before = root.stat()

    with pytest.raises(SupervisionError, match="unsafe|ownership marker"):
        ServiceManager(root).install(spec)

    after = root.stat()
    assert sentinel.read_bytes() == b"foreign-state"
    assert not (root / ".cortex-private-access-service.json").exists()
    assert (before.st_ino, before.st_mode, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def test_supervision_creates_each_missing_service_ancestor_privately(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    parent = tmp_path / "new-private-parent"
    root = parent / "service"
    private_spec = replace(spec, service_root=root)
    previous_umask = os.umask(0)
    try:
        result = ServiceManager(root).install(private_spec)
    finally:
        os.umask(previous_umask)

    assert result.action == "installed"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_install_stages_descriptors_without_publishing_launchagents_or_secrets(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    foreign = launch_agents / f"{GATEWAY_LABEL}.plist"
    foreign.write_bytes(b"foreign-launch-agent")

    result = manager.install(spec)

    assert result.action == "installed"
    assert result.activation_allowed is False
    assert result.launchctl_invoked is False
    gateway = _plist(spec, result.version, GATEWAY_LABEL)
    web = _plist(spec, result.version, WEB_LABEL)
    managed_config = (
        spec.service_root / "versions" / result.version / "private-access.json"
    )
    assert managed_config.stat().st_mode & 0o777 == 0o600
    assert load_config(managed_config).public_origin == config_dict["public_origin"]
    assert gateway["ProgramArguments"] == [
        str(spec.gateway_launcher),
        "--config",
        str(managed_config),
    ]
    assert web["ProgramArguments"][:5] == [
        str(spec.supervisor_launcher),
        "exec-web",
        "--config",
        str(managed_config),
        "--",
    ]
    for document in (gateway, web):
        environment = document["EnvironmentVariables"]
        assert environment == {
            "CORTEX_ACCESS_BOOTSTRAP_SECRET_REF": config_dict[
                "session_bootstrap_secret_ref"
            ],
            "CORTEX_PRIVATE_ACCESS_CONFIG": str(managed_config),
            "CORTEX_PUBLIC_ORIGIN": config_dict["public_origin"],
        }
        rendered = plistlib.dumps(document)
        assert b"CORTEX_ACCESS_BOOTSTRAP_TOKEN" not in rendered
        assert b"CORTEX_CONTROL_TOKEN" not in rendered
    assert foreign.read_bytes() == b"foreign-launch-agent"
    assert not (launch_agents / f"{WEB_LABEL}.plist").exists()
    assert manager.install(spec).action == "unchanged"


def test_upgrade_and_rollback_switch_only_staged_immutable_generation(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    installed = manager.install(first)
    first_gateway = (
        first.service_root
        / "versions"
        / installed.version
        / f"{GATEWAY_LABEL}.plist"
    ).read_bytes()

    second_config = json.loads(json.dumps(config_dict))
    second_config["public_origin"] = "https://cortex-two.example-tailnet.ts.net"
    second = _spec(
        tmp_path,
        second_config,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )
    upgraded = manager.install(second, upgrade=True)

    assert upgraded.action == "upgraded"
    assert upgraded.version != installed.version
    assert _plist(second, upgraded.version, GATEWAY_LABEL)["EnvironmentVariables"][
        "CORTEX_PUBLIC_ORIGIN"
    ] == second_config["public_origin"]

    rolled_back = manager.rollback()

    assert rolled_back.action == "rolled-back"
    assert rolled_back.version == installed.version
    assert (
        first.service_root
        / "versions"
        / installed.version
        / f"{GATEWAY_LABEL}.plist"
    ).read_bytes() == first_gateway
    assert json.loads((first.service_root / "current.json").read_text())["version"] == installed.version


def test_failed_or_foreign_state_is_preserved(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    spec.service_root.mkdir()
    foreign = spec.service_root / "foreign-state"
    foreign.write_bytes(b"foreign-service-state")

    with pytest.raises(SupervisionError, match="unsafe|no ownership marker"):
        _manager(spec).install(spec)

    assert foreign.read_bytes() == b"foreign-service-state"


def test_tampered_staged_descriptor_blocks_rollback_and_uninstall(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    installed = manager.install(spec)
    gateway = (
        spec.service_root
        / "versions"
        / installed.version
        / f"{GATEWAY_LABEL}.plist"
    )
    gateway.write_bytes(b"foreign replacement")

    with pytest.raises(SupervisionError, match="integrity check failed"):
        manager.uninstall()

    assert gateway.read_bytes() == b"foreign replacement"
    assert spec.service_root.exists()


def test_failed_upgrade_pointer_write_preserves_mixed_state_for_manual_recovery(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    installed = manager.install(first)
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )
    original = supervision._rename_at
    failed = False

    def fail_once(
        source_descriptor: int,
        source: str,
        destination_descriptor: int,
        destination: str,
        *,
        exchange: bool = False,
    ) -> None:
        nonlocal failed
        if destination == "current.json" and exchange and not failed:
            failed = True
            raise OSError("test-only pointer failure")
        original(
            source_descriptor,
            source,
            destination_descriptor,
            destination,
            exchange=exchange,
        )

    monkeypatch.setattr(supervision, "_rename_at", fail_once)

    with pytest.raises(SupervisionError, match="mixed"):
        manager.install(second, upgrade=True)

    assert json.loads((first.service_root / "current.json").read_text())[
        "version"
    ] == installed.version
    assert json.loads((first.service_root / "previous.json").read_text())[
        "version"
    ] == installed.version
    assert (first.service_root / "transaction.json").is_file()


def test_failed_generation_candidate_retains_racing_replacement(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    original = manager._write_generation_file
    detached = tmp_path / "detached-original-candidate"
    replaced = False

    def replace_candidate(descriptor: int, name: str, content: bytes) -> None:
        nonlocal replaced
        original(descriptor, name, content)
        if replaced:
            return
        candidate = next((spec.service_root / "versions").glob(".candidate-*"))
        candidate.rename(detached)
        candidate.mkdir(mode=0o700)
        foreign = candidate / "foreign.txt"
        foreign.write_bytes(b"foreign-candidate-replacement")
        foreign.chmod(0o600)
        replaced = True
        raise SupervisionError("test-only candidate interruption")

    monkeypatch.setattr(manager, "_write_generation_file", replace_candidate)

    with pytest.raises(SupervisionError, match="candidate interruption"):
        manager.install(spec)

    retained = next(
        (spec.service_root / supervision._OPERATION_HISTORY).glob("candidate-*")
    )
    assert replaced is True
    assert (retained / "foreign.txt").read_bytes() == b"foreign-candidate-replacement"
    assert any(detached.iterdir())
    assert list((spec.service_root / "versions").glob(".candidate-*")) == []


def test_transaction_creation_never_replaces_racing_journal(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )
    original = manager._write_root_file_no_replace
    injected = False

    def race_journal(name: str, content: bytes) -> None:
        nonlocal injected
        if name == "transaction.json" and not injected:
            foreign = first.service_root / name
            foreign.write_bytes(b"foreign-racing-journal")
            foreign.chmod(0o600)
            injected = True
        original(name, content)

    monkeypatch.setattr(manager, "_write_root_file_no_replace", race_journal)

    with pytest.raises(SupervisionError, match="already exists|unsafe"):
        manager.install(second, upgrade=True)

    assert injected is True
    assert (first.service_root / "transaction.json").read_bytes() == (
        b"foreign-racing-journal"
    )


def test_transaction_close_retains_racing_journal_replacement(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )
    original = supervision._rename_at
    expected_journal = tmp_path / "expected-transaction.json"
    replaced = False

    def replace_journal_before_close(
        source_descriptor: int,
        source: str,
        destination_descriptor: int,
        destination: str,
        *,
        exchange: bool = False,
    ) -> None:
        nonlocal replaced
        if (
            source == "transaction.json"
            and destination == "committed-manifest.json"
            and not replaced
        ):
            journal = first.service_root / "transaction.json"
            journal.rename(expected_journal)
            journal.write_bytes(b"foreign-close-replacement")
            journal.chmod(0o600)
            replaced = True
        original(
            source_descriptor,
            source,
            destination_descriptor,
            destination,
            exchange=exchange,
        )

    monkeypatch.setattr(supervision, "_rename_at", replace_journal_before_close)

    with pytest.raises(SupervisionError, match="manifest|history"):
        manager.install(second, upgrade=True)

    transaction_history = max(
        (first.service_root / supervision._OPERATION_HISTORY).glob("transaction-*"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    assert replaced is True
    assert json.loads(expected_journal.read_text())["operation"] == "upgrade"
    assert (transaction_history / "committed-manifest.json").read_bytes() == (
        b"foreign-close-replacement"
    )


def test_pointer_exchange_retains_racing_replacement(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    current = first.service_root / "current.json"
    expected_pointer = tmp_path / "expected-current.json"
    expected_bytes = current.read_bytes()
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )
    original = supervision._rename_at
    replaced = False

    def replace_pointer_before_exchange(
        source_descriptor: int,
        source: str,
        destination_descriptor: int,
        destination: str,
        *,
        exchange: bool = False,
    ) -> None:
        nonlocal replaced
        if destination == "current.json" and exchange and not replaced:
            current.rename(expected_pointer)
            current.write_bytes(b"foreign-pointer-replacement")
            current.chmod(0o600)
            replaced = True
        original(
            source_descriptor,
            source,
            destination_descriptor,
            destination,
            exchange=exchange,
        )

    monkeypatch.setattr(supervision, "_rename_at", replace_pointer_before_exchange)

    with pytest.raises(SupervisionError, match="pointer|history"):
        manager.install(second, upgrade=True)

    transaction_history = max(
        (first.service_root / supervision._OPERATION_HISTORY).glob("transaction-*"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    assert replaced is True
    assert expected_pointer.read_bytes() == expected_bytes
    assert (transaction_history / "current-swap.json").read_bytes() == (
        b"foreign-pointer-replacement"
    )
    assert json.loads(current.read_text())["release_id"] == "cortex-private-access-2"


def test_absent_pointer_transition_preserves_racing_foreign_pointer(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    original = manager._advance_pointer_transaction
    injected = False

    def inject_previous(
        transaction: dict[str, object],
        pointer_name: str,
        before: dict[str, object] | None,
        after: dict[str, object] | None,
    ) -> None:
        nonlocal injected
        if pointer_name == "previous.json" and not injected:
            previous = spec.service_root / pointer_name
            previous.write_bytes(b"foreign-absent-pointer")
            previous.chmod(0o600)
            injected = True
        original(transaction, pointer_name, before, after)

    monkeypatch.setattr(manager, "_advance_pointer_transaction", inject_previous)

    with pytest.raises(SupervisionError, match="pointer"):
        manager.install(spec)

    assert injected is True
    assert (spec.service_root / "previous.json").read_bytes() == (
        b"foreign-absent-pointer"
    )


def test_service_lifecycle_never_calls_physical_delete_or_replace(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )

    def reject_delete(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("service lifecycle must retain displaced objects")

    monkeypatch.setattr(os, "unlink", reject_delete)
    monkeypatch.setattr(os, "remove", reject_delete)
    monkeypatch.setattr(os, "rmdir", reject_delete)
    monkeypatch.setattr(os, "replace", reject_delete)
    monkeypatch.setattr(Path, "unlink", reject_delete)
    monkeypatch.setattr(Path, "rmdir", reject_delete)

    manager.install(first)
    manager.install(second, upgrade=True)
    manager.rollback()
    assert manager.uninstall()["action"] == "uninstalled"


def test_exec_web_resolves_only_at_exec_and_overwrites_untrusted_environment(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    secret = b"runtime-only-bootstrap-secret-value"
    captured: dict[str, object] = {}

    def resolver(reference: str) -> bytes:
        captured["reference"] = reference
        return secret

    def execute(
        executable: str, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> object:
        captured.update(
            executable=executable, arguments=arguments, environment=environment
        )
        return "executed"

    result = exec_web(
        spec.config_path,
        spec.web_command,
        environ={
            "CORTEX_ACCESS_BOOTSTRAP_TOKEN": "attacker",
            "CORTEX_ACCESS_BOOTSTRAP_SECRET_REF": "env://ATTACKER",
            "CORTEX_CONTROL_TOKEN": "must-not-cross-supervision-boundary",
            "CORTEX_DEV_ORIGIN": "http://127.0.0.1:3000",
            "CORTEX_PRIVATE_ACCESS_CONFIG": "/attacker/config.json",
            "CORTEX_PUBLIC_ORIGIN": "https://attacker.example",
            "PATH": "/usr/bin:/bin",
        },
        resolver=resolver,
        execute=execute,
    )

    assert result == "executed"
    assert captured["reference"] == config_dict["session_bootstrap_secret_ref"]
    assert captured["executable"] == str(spec.web_command[0])
    environment = captured["environment"]
    assert environment["CORTEX_ACCESS_BOOTSTRAP_TOKEN"] == derive_bootstrap_token(secret)
    assert environment["CORTEX_PUBLIC_ORIGIN"] == config_dict["public_origin"]
    assert "CORTEX_DEV_ORIGIN" not in environment
    assert "CORTEX_ACCESS_BOOTSTRAP_SECRET_REF" not in environment
    assert "CORTEX_PRIVATE_ACCESS_CONFIG" not in environment
    assert "CORTEX_CONTROL_TOKEN" not in environment
    assert environment["PATH"] == "/usr/bin:/bin"
    assert set(environment) == {
        "CORTEX_ACCESS_BOOTSTRAP_TOKEN",
        "CORTEX_PUBLIC_ORIGIN",
        "PATH",
    }
    assert secret.decode() not in json.dumps(environment)
    assert not spec.service_root.exists()


def test_uninstall_removes_only_owned_service_files(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    source_config = spec.config_path.read_bytes()
    web_executable = Path(spec.web_command[0]).read_bytes()
    current_inode = (spec.service_root / "current.json").stat().st_ino
    gateway = (
        spec.service_root
        / "versions"
        / manager.plan(spec)["version"]
        / f"{GATEWAY_LABEL}.plist"
    )
    gateway_inode = gateway.stat().st_ino

    result = manager.uninstall()

    assert result == {
        "action": "uninstalled",
        "activation_allowed": False,
        "cleanup_deferred_to_p2_devrel": True,
        "launchctl_invoked": False,
        "validated_scope": "access_boundary_only",
    }
    assert spec.service_root.is_dir()
    assert {path.name for path in spec.service_root.iterdir()} == {
        ".cortex-private-access-service.json",
        supervision._OPERATION_HISTORY,
        ".service.lock",
        supervision._UNINSTALL_TOMBSTONES,
    }
    tombstone_store = spec.service_root / supervision._UNINSTALL_TOMBSTONES
    tombstone = next(tombstone_store.iterdir())
    assert (tombstone / "manifest.json").is_file()
    assert (
        tombstone
        / manager._tombstone_name("file", "current.json")
    ).stat().st_ino == current_inode
    gateway_relative = f"versions/{manager.plan(spec)['version']}/{GATEWAY_LABEL}.plist"
    assert (
        tombstone / manager._tombstone_name("file", gateway_relative)
    ).stat().st_ino == gateway_inode
    assert spec.config_path.read_bytes() == source_config
    assert Path(spec.web_command[0]).read_bytes() == web_executable
    assert manager.uninstall()["action"] == "unchanged"


def test_uninstall_terminal_tombstone_never_unlinks_or_removes_directories(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)

    def reject_delete(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("P2-ACCESS uninstall must not physically delete")

    monkeypatch.setattr(Path, "unlink", reject_delete)
    monkeypatch.setattr(Path, "rmdir", reject_delete)

    result = manager.uninstall()

    assert result["action"] == "uninstalled"
    assert result["cleanup_deferred_to_p2_devrel"] is True
    assert (spec.service_root / supervision._UNINSTALL_TOMBSTONES).is_dir()


def test_interrupted_uninstall_recovers_after_first_file_isolation(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    original_rename = supervision._rename_no_replace
    interrupted = False

    def interrupt_after_first_isolation(source: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source, destination)
        if destination.name.startswith("file-") and not interrupted:
            interrupted = True
            raise OSError("test-only uninstall interruption")

    monkeypatch.setattr(supervision, "_rename_no_replace", interrupt_after_first_isolation)

    with pytest.raises(SupervisionError, match="could not be isolated"):
        manager.uninstall()

    assert interrupted is True
    assert (spec.service_root / "transaction.json").is_file()
    monkeypatch.setattr(supervision, "_rename_no_replace", original_rename)

    assert manager.uninstall()["action"] == "unchanged"
    assert {path.name for path in spec.service_root.iterdir()} == {
        ".cortex-private-access-service.json",
        supervision._OPERATION_HISTORY,
        ".service.lock",
        supervision._UNINSTALL_TOMBSTONES,
    }


def test_uninstall_recovery_preserves_foreign_retained_content(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    original_rename = supervision._rename_no_replace
    interrupted = False

    def interrupt_after_first_isolation(source: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source, destination)
        if destination.name.startswith("file-") and not interrupted:
            interrupted = True
            raise OSError("test-only uninstall interruption")

    monkeypatch.setattr(supervision, "_rename_no_replace", interrupt_after_first_isolation)
    with pytest.raises(SupervisionError, match="could not be isolated"):
        manager.uninstall()
    monkeypatch.setattr(supervision, "_rename_no_replace", original_rename)

    tombstone_store = spec.service_root / supervision._UNINSTALL_TOMBSTONES
    tombstone = next(tombstone_store.iterdir())
    remaining = next(path for path in tombstone.iterdir() if path.name.startswith("file-"))
    before = remaining.read_bytes()
    foreign = spec.service_root / "foreign.txt"
    foreign.write_bytes(b"foreign-state")

    with pytest.raises(SupervisionError, match="unowned|foreign|does not belong"):
        manager.uninstall()

    assert remaining.read_bytes() == before
    assert foreign.read_bytes() == b"foreign-state"
    assert (spec.service_root / "transaction.json").is_file()


def test_uninstall_tombstone_preserves_replacement_racing_after_inventory(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    installed = manager.install(spec)
    target = (
        spec.service_root
        / "versions"
        / installed.version
        / f"{WEB_LABEL}.plist"
    )
    original_rename = supervision._rename_no_replace
    replaced = False

    def replace_before_isolation(source: Path, destination: Path) -> None:
        nonlocal replaced
        if source == target and not replaced:
            replaced = True
            source.unlink()
            source.write_bytes(b"foreign-racing-replacement")
            source.chmod(0o600)
        original_rename(source, destination)

    monkeypatch.setattr(supervision, "_rename_no_replace", replace_before_isolation)

    with pytest.raises(SupervisionError, match="does not belong|foreign"):
        manager.uninstall()

    assert replaced is True
    assert target.read_bytes() == b"foreign-racing-replacement"
    assert (spec.service_root / "transaction.json").is_file()


def test_uninstall_recovery_preserves_foreign_pointer_symlink(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    transaction = manager._build_uninstall_transaction()
    assert transaction is not None
    _replace_owned_file_for_test(
        spec.service_root / "transaction.json", supervision._canonical_json(transaction)
    )
    foreign = tmp_path / "foreign-pointer.json"
    foreign.write_bytes(b"foreign-state")
    current = spec.service_root / "current.json"
    current.unlink()
    current.symlink_to(foreign)

    with pytest.raises(SupervisionError, match="does not belong"):
        manager.uninstall()

    assert current.is_symlink()
    assert foreign.read_bytes() == b"foreign-state"
    assert (spec.service_root / "transaction.json").is_file()


def test_transaction_recovery_preserves_foreign_pointer_before_overwrite(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    current_path = first.service_root / "current.json"
    current = json.loads(current_path.read_text())
    expected_after = {
        "config_fingerprint": "1" * 64,
        "release_id": "expected-next",
        "release_sequence": 2,
        "schema_version": 1,
        "version": "expected-next-1111111111111111",
    }
    foreign = {
        "config_fingerprint": "2" * 64,
        "release_id": "foreign",
        "release_sequence": 99,
        "schema_version": 1,
        "version": "foreign-2222222222222222",
    }
    manager._begin_transaction(
        "upgrade",
        current_before=current,
        previous_before=None,
        current_after=expected_after,
        previous_after=current,
    )
    _replace_owned_file_for_test(
        current_path, supervision._canonical_json(foreign)
    )
    before = current_path.read_bytes()

    with pytest.raises(SupervisionError, match="does not belong"):
        manager.uninstall()

    assert current_path.read_bytes() == before
    assert (first.service_root / "transaction.json").is_file()


def test_transaction_recovery_preserves_mixed_before_after_pointer_state(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    current_before = json.loads((first.service_root / "current.json").read_text())
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )
    upgraded = manager.install(second, upgrade=True)
    current_after = json.loads((first.service_root / "current.json").read_text())
    assert current_after["version"] == upgraded.version
    manager._begin_transaction(
        "upgrade",
        current_before=current_before,
        previous_before=None,
        current_after=current_after,
        previous_after=current_before,
    )
    _replace_owned_file_for_test(
        first.service_root / "current.json",
        supervision._canonical_json(current_before),
    )
    current_bytes = (first.service_root / "current.json").read_bytes()
    previous_bytes = (first.service_root / "previous.json").read_bytes()

    with pytest.raises(SupervisionError, match="mixed"):
        manager.uninstall()

    assert (first.service_root / "current.json").read_bytes() == current_bytes
    assert (first.service_root / "previous.json").read_bytes() == previous_bytes
    assert (first.service_root / "transaction.json").is_file()


def test_existing_lock_mode_is_rejected_without_repair(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    lock = spec.service_root / ".service.lock"
    lock.chmod(0o644)
    before = lock.stat()

    with pytest.raises(SupervisionError, match="lock is unsafe"):
        manager.uninstall()

    after = lock.stat()
    assert stat.S_IMODE(after.st_mode) == 0o644
    assert (before.st_ino, before.st_mode, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_ctime_ns,
    )
    assert (spec.service_root / "current.json").is_file()


def test_existing_lock_hardlink_is_rejected_without_repair(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    lock = spec.service_root / ".service.lock"
    alias = tmp_path / "lock-alias"
    os.link(lock, alias)
    before = lock.stat()

    with pytest.raises(SupervisionError, match="lock is unsafe"):
        manager.uninstall()

    after = lock.stat()
    assert before.st_nlink == after.st_nlink == 2
    assert (before.st_ino, before.st_mode, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_ctime_ns,
    )
    assert (spec.service_root / "current.json").is_file()


def test_service_lock_replacement_during_critical_section_stays_in_one_lock_domain(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    lock = spec.service_root / ".service.lock"
    retained = spec.service_root / ".service.lock.retained"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_waiting_on_anchor = threading.Event()
    second_entered = threading.Event()
    errors: dict[str, SupervisionError | AssertionError] = {}
    anchor_inode = supervision._trusted_service_anchor(spec.service_root).stat().st_ino
    original_flock = supervision.fcntl.flock

    def observed_flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "second-service-operation"
            and operation == supervision.fcntl.LOCK_EX
            and os.fstat(descriptor).st_ino == anchor_inode
        ):
            second_waiting_on_anchor.set()
        original_flock(descriptor, operation)

    monkeypatch.setattr(supervision.fcntl, "flock", observed_flock)

    def first_worker() -> None:
        try:
            with manager._locked():
                first_entered.set()
                assert release_first.wait(timeout=5)
        except (SupervisionError, AssertionError) as exc:
            errors["first"] = exc

    first = threading.Thread(target=first_worker, name="first-service-operation")
    first.start()
    assert first_entered.wait(timeout=5)
    original_inode = lock.stat().st_ino
    lock.rename(retained)
    lock.write_bytes(retained.read_bytes())
    lock.chmod(0o600)
    assert lock.stat().st_ino != original_inode

    def second_worker() -> None:
        try:
            with manager._locked():
                second_entered.set()
        except (SupervisionError, AssertionError) as exc:
            errors["second"] = exc

    second = threading.Thread(target=second_worker, name="second-service-operation")
    second.start()
    assert second_waiting_on_anchor.wait(timeout=5)
    assert not second_entered.is_set()

    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert isinstance(errors.get("first"), SupervisionError)
    assert "pathname changed" in str(errors["first"])
    assert "second" not in errors
    assert second_entered.is_set()


def test_service_lock_replacement_between_open_and_flock_never_enters_twice(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    lock = spec.service_root / ".service.lock"
    retained = spec.service_root / ".service.lock.retained"
    replacement_done = threading.Event()
    second_waiting_on_anchor = threading.Event()
    first_entered = threading.Event()
    second_entered = threading.Event()
    errors: dict[str, SupervisionError | AssertionError] = {}
    anchor_inode = supervision._trusted_service_anchor(spec.service_root).stat().st_ino
    original_verify = manager._verify_lock_binding
    original_flock = supervision.fcntl.flock

    def observed_flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "second-open-flock-operation"
            and operation == supervision.fcntl.LOCK_EX
            and os.fstat(descriptor).st_ino == anchor_inode
        ):
            second_waiting_on_anchor.set()
        original_flock(descriptor, operation)

    first_verification = True

    def replace_after_initial_verification(
        root_descriptor: int, descriptor: int
    ) -> None:
        nonlocal first_verification
        original_verify(root_descriptor, descriptor)
        if (
            threading.current_thread().name == "first-open-flock-operation"
            and first_verification
        ):
            first_verification = False
            original_inode = lock.stat().st_ino
            lock.rename(retained)
            lock.write_bytes(retained.read_bytes())
            lock.chmod(0o600)
            assert lock.stat().st_ino != original_inode
            replacement_done.set()
            assert second_waiting_on_anchor.wait(timeout=5)

    monkeypatch.setattr(supervision.fcntl, "flock", observed_flock)
    monkeypatch.setattr(manager, "_verify_lock_binding", replace_after_initial_verification)

    def first_worker() -> None:
        try:
            with manager._locked():
                first_entered.set()
        except (SupervisionError, AssertionError) as exc:
            errors["first"] = exc

    def second_worker() -> None:
        assert replacement_done.wait(timeout=5)
        try:
            with manager._locked():
                second_entered.set()
        except (SupervisionError, AssertionError) as exc:
            errors["second"] = exc

    second = threading.Thread(
        target=second_worker, name="second-open-flock-operation"
    )
    first = threading.Thread(target=first_worker, name="first-open-flock-operation")
    second.start()
    first.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not first_entered.is_set()
    assert isinstance(errors.get("first"), SupervisionError)
    assert "pathname changed" in str(errors["first"])
    assert "second" not in errors
    assert second_entered.is_set()


def test_service_root_replacement_during_critical_section_stays_under_anchor_lock(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    retained_root = tmp_path / "retained-service-root"
    marker_name = ".cortex-private-access-service.json"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_waiting_on_anchor = threading.Event()
    second_entered = threading.Event()
    errors: dict[str, SupervisionError | AssertionError] = {}
    anchor_inode = supervision._trusted_service_anchor(spec.service_root).stat().st_ino
    original_flock = supervision.fcntl.flock

    def observed_flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "second-root-operation"
            and operation == supervision.fcntl.LOCK_EX
            and os.fstat(descriptor).st_ino == anchor_inode
        ):
            second_waiting_on_anchor.set()
        original_flock(descriptor, operation)

    monkeypatch.setattr(supervision.fcntl, "flock", observed_flock)

    def first_worker() -> None:
        try:
            with manager._locked():
                first_entered.set()
                assert release_first.wait(timeout=5)
        except (SupervisionError, AssertionError) as exc:
            errors["first"] = exc

    first = threading.Thread(target=first_worker, name="first-root-operation")
    first.start()
    assert first_entered.wait(timeout=5)
    original_root_inode = spec.service_root.stat().st_ino
    marker_bytes = (spec.service_root / marker_name).read_bytes()
    lock_bytes = (spec.service_root / ".service.lock").read_bytes()
    spec.service_root.rename(retained_root)
    spec.service_root.mkdir(mode=0o700)
    (spec.service_root / marker_name).write_bytes(marker_bytes)
    (spec.service_root / marker_name).chmod(0o600)
    (spec.service_root / ".service.lock").write_bytes(lock_bytes)
    (spec.service_root / ".service.lock").chmod(0o600)
    assert spec.service_root.stat().st_ino != original_root_inode

    def second_worker() -> None:
        try:
            with manager._locked():
                second_entered.set()
        except (SupervisionError, AssertionError) as exc:
            errors["second"] = exc

    second = threading.Thread(target=second_worker, name="second-root-operation")
    second.start()
    assert second_waiting_on_anchor.wait(timeout=5)
    assert not second_entered.is_set()

    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert isinstance(errors.get("first"), SupervisionError)
    assert "root pathname changed" in str(errors["first"])
    assert "second" not in errors
    assert second_entered.is_set()
    assert (retained_root / "current.json").is_file()


@pytest.mark.parametrize("target", ["versions", "generation"])
def test_uninstall_rejects_non_private_generation_directories_without_repair(
    tmp_path: Path, config_dict: dict[str, object], target: str
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    installed = manager.install(spec)
    versions = spec.service_root / "versions"
    path = versions if target == "versions" else versions / installed.version
    path.chmod(0o777)
    before = path.stat()

    with pytest.raises(SupervisionError, match="unsafe|integrity"):
        manager.uninstall()

    after = path.stat()
    assert stat.S_IMODE(after.st_mode) == 0o777
    assert (before.st_ino, before.st_mode, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_ctime_ns,
    )
    assert not (spec.service_root / supervision._UNINSTALL_TOMBSTONES).exists()


@pytest.mark.parametrize("target", ["non-private", "symlink"])
def test_install_rejects_unsafe_existing_versions_before_write(
    tmp_path: Path, config_dict: dict[str, object], target: str
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    manager.uninstall()
    versions = first.service_root / "versions"
    retained = tmp_path / "retained-versions"
    if target == "non-private":
        versions.mkdir(mode=0o700)
        versions.chmod(0o777)
        unsafe = versions
    else:
        retained.mkdir(mode=0o700)
        versions.symlink_to(retained, target_is_directory=True)
        unsafe = retained
    before = unsafe.stat()
    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )

    with pytest.raises(SupervisionError, match="versions.*unsafe|unavailable"):
        manager.install(second)

    after = unsafe.stat()
    assert list(unsafe.iterdir()) == []
    assert (before.st_ino, before.st_mode, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_ctime_ns,
    )


def test_install_rejects_unsafe_existing_generation_before_write(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    version = str(manager.plan(spec)["version"])
    generation = spec.service_root / "versions" / version
    with manager._locked():
        (spec.service_root / "versions").mkdir(mode=0o700)
        generation.mkdir(mode=0o700)
        generation.chmod(0o777)
    before = generation.stat()

    with pytest.raises(SupervisionError, match="generation.*unsafe"):
        manager.install(spec)

    after = generation.stat()
    assert list(generation.iterdir()) == []
    assert (before.st_ino, before.st_mode, before.st_ctime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_ctime_ns,
    )
    assert not (spec.service_root / "current.json").exists()


def test_generation_capability_rejects_symlinked_versions_component(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    versions = spec.service_root / "versions"
    retained = tmp_path / "retained-versions"
    versions.rename(retained)
    versions.symlink_to(retained, target_is_directory=True)
    sentinel = next(retained.rglob(f"{WEB_LABEL}.plist"))
    before = sentinel.read_bytes()

    with pytest.raises(SupervisionError, match="unsafe|unavailable"):
        manager.uninstall()

    assert versions.is_symlink()
    assert sentinel.read_bytes() == before
    assert not (spec.service_root / supervision._UNINSTALL_TOMBSTONES).exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("release_id", "foreign-release"),
        ("release_sequence", 999),
        ("config_fingerprint", "f" * 64),
    ],
)
def test_pointer_must_match_full_generation_identity(
    tmp_path: Path,
    config_dict: dict[str, object],
    field: str,
    replacement: object,
) -> None:
    spec = _spec(tmp_path, config_dict)
    manager = _manager(spec)
    manager.install(spec)
    current_path = spec.service_root / "current.json"
    pointer = json.loads(current_path.read_text())
    pointer[field] = replacement
    _replace_owned_file_for_test(
        current_path, supervision._canonical_json(pointer)
    )
    before = current_path.read_bytes()

    with pytest.raises(SupervisionError, match="pointer.*generation"):
        manager.uninstall()

    assert current_path.read_bytes() == before
    assert not (spec.service_root / supervision._UNINSTALL_TOMBSTONES).exists()


def test_supervision_rejects_secret_bearing_web_arguments(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    spec = _spec(tmp_path, config_dict)
    unsafe = replace(spec, web_command=(*spec.web_command, "--api-token=value"))

    with pytest.raises(SupervisionError, match="unsafe argument"):
        _manager(unsafe).plan(unsafe)


def test_supervision_rejects_environment_secret_reference_for_cold_start(
    tmp_path: Path, config_dict: dict[str, object]
) -> None:
    environment_config = json.loads(json.dumps(config_dict))
    environment_config["session_bootstrap_secret_ref"] = "env://CORTEX_ACCESS_SESSION"
    spec = _spec(tmp_path, environment_config)
    resolver_called = False

    def resolver(_reference: str) -> bytes:
        nonlocal resolver_called
        resolver_called = True
        return b"should-not-be-resolved-by-supervision"

    with pytest.raises(SupervisionError, match="requires a Keychain"):
        _manager(spec).plan(spec)
    with pytest.raises(SupervisionError, match="requires a Keychain"):
        exec_web(spec.config_path, spec.web_command, resolver=resolver)

    assert resolver_called is False
    assert load_config(spec.config_path).session_bootstrap_secret_ref.startswith(
        "env://"
    )


def test_concurrent_install_after_uninstall_reuses_lock_and_survives(
    tmp_path: Path,
    config_dict: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _spec(tmp_path, config_dict)
    manager = _manager(first)
    manager.install(first)
    lock_path = first.service_root / ".service.lock"
    original_lock_inode = lock_path.stat().st_ino
    uninstall_inside_lock = threading.Event()
    allow_uninstall = threading.Event()
    install_waiting_on_lock = threading.Event()
    original_inventory = manager._build_uninstall_transaction
    original_flock = supervision.fcntl.flock

    def gated_inventory() -> dict[str, object] | None:
        if threading.current_thread().name == "uninstall-worker":
            uninstall_inside_lock.set()
            assert allow_uninstall.wait(timeout=5)
        return original_inventory()

    def observed_flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "install-worker"
            and operation == supervision.fcntl.LOCK_EX
        ):
            install_waiting_on_lock.set()
        original_flock(descriptor, operation)

    monkeypatch.setattr(manager, "_build_uninstall_transaction", gated_inventory)
    monkeypatch.setattr(supervision.fcntl, "flock", observed_flock)
    results: dict[str, object] = {}
    errors: list[SupervisionError | AssertionError] = []

    def uninstall_worker() -> None:
        try:
            results["uninstall"] = manager.uninstall()
        except (SupervisionError, AssertionError) as exc:
            errors.append(exc)

    second = _spec(
        tmp_path,
        config_dict,
        release_id="cortex-private-access-2",
        release_sequence=2,
    )

    def install_worker() -> None:
        try:
            results["install"] = manager.install(second)
        except (SupervisionError, AssertionError) as exc:
            errors.append(exc)

    uninstall_thread = threading.Thread(
        target=uninstall_worker, name="uninstall-worker"
    )
    install_thread = threading.Thread(target=install_worker, name="install-worker")
    uninstall_thread.start()
    assert uninstall_inside_lock.wait(timeout=5)
    install_thread.start()
    assert install_waiting_on_lock.wait(timeout=5)
    allow_uninstall.set()
    uninstall_thread.join(timeout=5)
    install_thread.join(timeout=5)

    assert not uninstall_thread.is_alive()
    assert not install_thread.is_alive()
    assert errors == []
    assert results["install"].action == "installed"
    assert lock_path.stat().st_ino == original_lock_inode
    current = json.loads((first.service_root / "current.json").read_text())
    assert current["release_id"] == "cortex-private-access-2"
    version = first.service_root / "versions" / current["version"]
    assert (version / f"{GATEWAY_LABEL}.plist").is_file()
    assert (version / f"{WEB_LABEL}.plist").is_file()
