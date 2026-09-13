from __future__ import annotations

import copy
import fcntl
import json
import os
import stat
import tempfile
import threading
from collections import defaultdict, deque
from collections.abc import Sequence
from pathlib import Path

import pytest

from deployment.private_access import operations
from deployment.private_access.config import AccessConfig, config_fingerprint
from deployment.private_access.operations import (
    APPLY_APPROVAL,
    ROLLBACK_APPROVAL,
    AccessManager,
    AccessOperationError,
    CommandResult,
    GatewayAttestation,
    InjectedAccessOperationCrash,
)


class FakeRunner:
    def __init__(self) -> None:
        self.results: dict[tuple[str, ...], deque[CommandResult]] = defaultdict(deque)
        self.calls: list[tuple[str, ...]] = []

    def add(self, command: Sequence[str], *results: CommandResult) -> None:
        self.results[tuple(command)].extend(results)

    def run(self, arguments: Sequence[str]) -> CommandResult:
        command = tuple(arguments)
        self.calls.append(command)
        if not self.results[command]:
            raise AssertionError(f"unexpected command: {command}")
        return self.results[command].popleft()


def _json_result(value: object) -> CommandResult:
    return CommandResult(0, json.dumps(value), "")


def _tailnet(config: AccessConfig) -> dict[str, object]:
    return {
        "BackendState": "Running",
        "Self": {
            "DNSName": f"{config.public_hostname}.",
            "Tags": [config.identity.service_tag],
        },
        "Peer": {"private-information": {"HostName": "must-not-leak"}},
    }


def _empty_serve() -> dict[str, object]:
    return {"TCP": {}, "Web": {}, "AllowFunnel": {}}


def _active_serve(config: AccessConfig) -> dict[str, object]:
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            f"{config.public_hostname}:443": {
                "Handlers": {
                    "/": {
                        "Proxy": config.access_gateway.url,
                        "AcceptAppCaps": [config.identity.app_capability],
                    }
                }
            }
        },
        "AllowFunnel": {f"{config.public_hostname}:443": False},
    }


def _ready_manager(config: AccessConfig, runner: FakeRunner) -> AccessManager:
    return AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        probe=lambda host, port: True,
        gateway_probe=lambda candidate: GatewayAttestation(
            config_fingerprint(candidate), True
        ),
        composition_gate=lambda: True,
    )


def _apply(
    manager: AccessManager,
    state_directory: Path,
    *,
    approval: str = APPLY_APPROVAL,
    policy_hash: str | None = None,
) -> dict[str, object]:
    return manager.apply(
        state_directory,
        approval=approval,
        reviewed_policy_sha256=(
            policy_hash or str(manager.plan()["reviewed_policy_sha256"])
        ),
    )


def test_plan_is_deterministic_and_never_targets_daemon(config: AccessConfig) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)

    first = manager.plan()
    second = manager.plan()

    assert first == second
    assert first["daemon_remote_exposed"] is False
    assert first["activation_allowed"] is False
    assert first["validated_scope"] == "access_boundary_only"
    assert config.daemon_upstream.url not in json.dumps(first)
    assert config.access_gateway.url in first["commands"]["apply"]
    assert runner.calls == []


def test_generate_is_private_idempotent_and_does_not_execute(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    first = manager.generate(tmp_path)
    first_bytes = tuple(path.read_bytes() for path in first)
    first_inodes = tuple(path.stat().st_ino for path in first)
    second = manager.generate(tmp_path)

    assert first == second
    assert first_bytes == tuple(path.read_bytes() for path in second)
    assert first_inodes == tuple(path.stat().st_ino for path in second)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in first)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert runner.calls == []


@pytest.mark.parametrize("tamper", ["mode", "hardlink", "content", "symlink"])
def test_generate_rejects_unsafe_existing_output_without_changing_it(
    tmp_path: Path, config: AccessConfig, tamper: str
) -> None:
    manager = _ready_manager(config, FakeRunner())
    policy_path, _ = manager.generate(tmp_path)
    if tamper == "mode":
        policy_path.chmod(0o644)
    elif tamper == "hardlink":
        os.link(policy_path, tmp_path / "policy-alias")
    elif tamper == "content":
        policy_path.write_bytes(b"foreign-policy\n")
        policy_path.chmod(0o600)
    else:
        retained = tmp_path / "retained-policy"
        policy_path.rename(retained)
        policy_path.symlink_to(retained)
    retained_bytes = policy_path.read_bytes()
    retained_mode = stat.S_IMODE(policy_path.stat().st_mode)

    with pytest.raises(AccessOperationError, match="generated file"):
        manager.generate(tmp_path)

    assert policy_path.read_bytes() == retained_bytes
    assert stat.S_IMODE(policy_path.stat().st_mode) == retained_mode


def test_generate_never_uses_replace_or_pathname_unlink(
    tmp_path: Path, config: AccessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_path_mutation(*args: object, **kwargs: object) -> None:
        raise AssertionError("generation must not replace or unlink a pathname")

    monkeypatch.setattr(os, "replace", reject_path_mutation)
    monkeypatch.setattr(os, "unlink", reject_path_mutation)
    manager = _ready_manager(config, FakeRunner())

    first = manager.generate(tmp_path)
    second = manager.generate(tmp_path)

    assert first == second


def test_policy_fragment_requires_explicit_sources_tag_owners_and_capability(
    config: AccessConfig,
) -> None:
    fragment = AccessManager(config).policy_fragment()
    grant = fragment["grants"][0]
    assert grant["src"] == list(config.identity.allowed_sources)
    assert grant["dst"] == [config.identity.service_tag]
    assert grant["ip"] == ["443"]
    assert fragment["tagOwners"][config.identity.service_tag] == list(
        config.identity.tag_owners
    )
    assert config.identity.app_capability in grant["app"]
    assert "*" not in json.dumps(fragment)


def test_apply_requires_explicit_gate_and_ready_gateway(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    with pytest.raises(AccessOperationError, match="approval"):
        _apply(manager, tmp_path, approval="yes")
    assert runner.calls == []

    with pytest.raises(AccessOperationError, match="policy hash"):
        _apply(manager, tmp_path, policy_hash="0" * 64)
    assert runner.calls == []

    not_ready = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        probe=lambda host, port: False,
        gateway_probe=lambda candidate: None,
        composition_gate=lambda: True,
    )
    with pytest.raises(AccessOperationError, match="gateway"):
        _apply(not_ready, tmp_path)
    assert runner.calls == []


def test_apply_is_disabled_without_complete_composition_even_when_access_is_ready(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        probe=lambda host, port: True,
        gateway_probe=lambda candidate: GatewayAttestation(
            config_fingerprint(candidate), True
        ),
    )

    with pytest.raises(AccessOperationError, match="composition"):
        _apply(manager, tmp_path)

    assert runner.calls == []
    manifest = tmp_path / "rollback-manifest.json"
    assert manifest.read_bytes() == b""
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_global_lock_rejects_concurrent_operation_with_different_state_directory(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    lock_path = manager.operation_lock_path
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AccessOperationError, match="in progress"):
            _apply(manager, tmp_path / "different-state")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert runner.calls == []


def test_global_lock_path_replacement_during_yield_cannot_create_second_lock_domain(
    tmp_path: Path,
) -> None:
    lock_parent = tmp_path / "global-locks"
    lock_parent.mkdir(mode=0o700)
    lock_path = lock_parent / "operation.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors: dict[str, AccessOperationError | AssertionError] = {}

    def first_worker() -> None:
        try:
            with operations._operation_lock(tmp_path / "state-a", lock_path):
                first_entered.set()
                assert release_first.wait(timeout=5)
        except (AccessOperationError, AssertionError) as exc:
            errors["first"] = exc

    first = threading.Thread(target=first_worker, name="first-operation")
    first.start()
    assert first_entered.wait(timeout=5)
    original_inode = lock_path.stat().st_ino
    retained = lock_parent / "retained-operation.lock"
    lock_path.rename(retained)
    lock_path.write_bytes(retained.read_bytes())
    lock_path.chmod(0o600)
    assert lock_path.stat().st_ino != original_inode

    def second_worker() -> None:
        try:
            with operations._operation_lock(tmp_path / "state-b", lock_path):
                second_entered.set()
        except (AccessOperationError, AssertionError) as exc:
            errors["second"] = exc

    second = threading.Thread(target=second_worker, name="second-operation")
    second.start()
    second.join(timeout=5)

    assert not second.is_alive()
    assert not second_entered.is_set()
    assert isinstance(errors.get("second"), AccessOperationError)
    assert "in progress" in str(errors["second"])

    release_first.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert isinstance(errors.get("first"), AccessOperationError)
    assert "pathname changed" in str(errors["first"])


def test_global_lock_parent_replacement_during_yield_stays_under_trusted_anchor(
    tmp_path: Path,
) -> None:
    lock_parent = tmp_path / "replaceable-lock-parent"
    lock_parent.mkdir(mode=0o700)
    lock_path = lock_parent / "operation.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors: dict[str, AccessOperationError | AssertionError] = {}

    def first_worker() -> None:
        try:
            with operations._operation_lock(tmp_path / "state-a", lock_path):
                first_entered.set()
                assert release_first.wait(timeout=5)
        except (AccessOperationError, AssertionError) as exc:
            errors["first"] = exc

    first = threading.Thread(target=first_worker, name="first-parent-operation")
    first.start()
    assert first_entered.wait(timeout=5)
    original_parent_inode = lock_parent.stat().st_ino
    retained_parent = tmp_path / "retained-lock-parent"
    lock_parent.rename(retained_parent)
    lock_parent.mkdir(mode=0o700)
    replacement_lock = lock_parent / lock_path.name
    replacement_lock.write_bytes((retained_parent / lock_path.name).read_bytes())
    replacement_lock.chmod(0o600)
    assert lock_parent.stat().st_ino != original_parent_inode

    def second_worker() -> None:
        try:
            with operations._operation_lock(tmp_path / "state-b", lock_path):
                second_entered.set()
        except (AccessOperationError, AssertionError) as exc:
            errors["second"] = exc

    second = threading.Thread(target=second_worker, name="second-parent-operation")
    second.start()
    second.join(timeout=5)

    assert not second.is_alive()
    assert not second_entered.is_set()
    assert isinstance(errors.get("second"), AccessOperationError)
    assert "in progress" in str(errors["second"])

    release_first.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert isinstance(errors.get("first"), AccessOperationError)
    assert "parent pathname changed" in str(errors["first"])


def test_global_lock_rejects_cross_anchor_symlink_ancestor() -> None:
    with (
        tempfile.TemporaryDirectory(dir=Path.home()) as home_raw,
        tempfile.TemporaryDirectory() as temporary_raw,
    ):
        home_root = Path(home_raw)
        temporary_root = operations._normalized_real_path(Path(temporary_raw))
        home_target = home_root / "home-target"
        temporary_target = temporary_root / "temporary-target"
        for target in (home_target, temporary_target):
            (target / "locks").mkdir(parents=True, mode=0o700)
        alias = home_root / "lock-alias"
        alias.symlink_to(home_target, target_is_directory=True)
        lock_path = alias / "locks" / "operation.lock"

        with (
            pytest.raises(AccessOperationError, match="ancestor.*symlink"),
            operations._operation_lock(home_root / "state-a", lock_path),
        ):
            raise AssertionError("a symlinked lock ancestor must not be entered")

        alias.unlink()
        alias.symlink_to(temporary_target, target_is_directory=True)
        with (
            pytest.raises(AccessOperationError, match="ancestor.*symlink"),
            operations._operation_lock(home_root / "state-b", lock_path),
        ):
            raise AssertionError("a retargeted lock ancestor must not be entered")

        assert not (home_target / "locks" / lock_path.name).exists()
        assert not (temporary_target / "locks" / lock_path.name).exists()


def test_operation_lock_creates_missing_private_parent_descriptor_relatively(
    tmp_path: Path,
) -> None:
    lock_parent = tmp_path / "new-lock-root" / "nested"
    lock_path = lock_parent / "operation.lock"

    with operations._operation_lock(tmp_path / "state", lock_path):
        assert stat.S_IMODE(lock_parent.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_operation_lock_releases_every_lock_after_base_exception_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedOpenFailure(BaseException):
        pass

    lock_parent = tmp_path / "base-exception-lock-parent"
    lock_parent.mkdir(mode=0o700)
    lock_path = lock_parent / "operation.lock"
    original_open = operations.os.open
    injected = False

    def failing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if path == lock_path.name and dir_fd is not None and not injected:
            injected = True
            raise InjectedOpenFailure("after parent flock")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(operations.os, "open", failing_open)

    with (
        pytest.raises(InjectedOpenFailure, match="after parent flock"),
        operations._operation_lock(tmp_path / "state-a", lock_path),
    ):
        raise AssertionError("operation must not enter after injected open failure")

    entered = False
    with operations._operation_lock(tmp_path / "state-b", lock_path):
        entered = True

    assert entered is True


@pytest.mark.parametrize("tamper", ["mode", "hardlink", "symlink"])
def test_existing_operation_lock_metadata_is_never_repaired(
    tmp_path: Path, config: AccessConfig, tamper: str
) -> None:
    runner = FakeRunner()
    lock_path = tmp_path / "operation.lock"
    if tamper == "symlink":
        retained = tmp_path / "retained.lock"
        retained.write_bytes(b"retained-lock")
        retained.chmod(0o600)
        lock_path.symlink_to(retained)
    else:
        lock_path.write_bytes(b"retained-lock")
        lock_path.chmod(0o600)
        if tamper == "mode":
            lock_path.chmod(0o640)
        else:
            os.link(lock_path, tmp_path / "operation.lock.alias")
    original_bytes = lock_path.read_bytes()
    original_mode = stat.S_IMODE(lock_path.stat().st_mode)
    original_links = lock_path.stat().st_nlink
    manager = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        gateway_probe=lambda candidate: GatewayAttestation(
            config_fingerprint(candidate), True
        ),
        composition_gate=lambda: True,
        operation_lock_path=lock_path,
    )

    with pytest.raises(AccessOperationError, match="operation lock"):
        _apply(manager, tmp_path / "state")

    assert lock_path.read_bytes() == original_bytes
    assert stat.S_IMODE(lock_path.stat().st_mode) == original_mode
    assert lock_path.stat().st_nlink == original_links
    assert runner.calls == []


def test_apply_records_crash_safe_manifest_and_is_idempotent(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(
        ("tailscale", "status", "--json"),
        _json_result(_tailnet(config)),
        _json_result(_tailnet(config)),
    )
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
        _json_result(_active_serve(config)),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))

    manifest = _apply(manager, tmp_path)
    replay = _apply(manager, tmp_path)

    assert manifest == replay
    assert manifest["state"] == "applied"
    assert manifest["before_was_empty"] is True
    assert manifest["daemon_remote_exposed"] is False
    assert config.daemon_upstream.url not in json.dumps(manifest)
    path = tmp_path / "rollback-manifest.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert runner.calls.count(manager.serve_command) == 1


def test_apply_refuses_to_overwrite_existing_serve_configuration(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result({"Web": {"existing.ts.net:443": {"Handlers": {}}}}),
    )

    with pytest.raises(AccessOperationError, match="existing Serve"):
        _apply(manager, tmp_path)

    assert manager.serve_command not in runner.calls
    manifest_path = tmp_path / "rollback-manifest.json"
    assert manifest_path.exists()
    assert manifest_path.read_bytes() == b""
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_apply_rejects_funnel_and_never_runs_serve(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result({"AllowFunnel": {"example:443": True}}),
    )
    with pytest.raises(AccessOperationError, match="Funnel"):
        _apply(manager, tmp_path)
    assert manager.serve_command not in runner.calls


def test_apply_requires_expected_service_tag(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    status = _tailnet(config)
    self_status = status["Self"]
    assert isinstance(self_status, dict)
    self_status["Tags"] = ["tag:other-service"]
    runner.add(("tailscale", "status", "--json"), _json_result(status))

    with pytest.raises(AccessOperationError, match="service tag"):
        _apply(manager, tmp_path)

    assert manager.serve_command not in runner.calls


def test_failed_verification_rolls_back_and_preserves_manifest(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result({"AllowFunnel": {"example:443": True}}),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))

    with pytest.raises(AccessOperationError, match="verification"):
        _apply(manager, tmp_path)

    manifest = manager._load_manifest(tmp_path / "rollback-manifest.json")
    assert manifest["state"] == "rollback_required"
    assert manager.rollback_command not in runner.calls


def test_rollback_is_verified_and_idempotent(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
        _json_result(_active_serve(config)),
        _json_result(_empty_serve()),
        _json_result(_empty_serve()),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    runner.add(manager.rollback_command, CommandResult(0, "off", ""))
    _apply(manager, tmp_path)

    with pytest.raises(AccessOperationError, match="approval"):
        manager.rollback(tmp_path, approval="yes")
    rolled_back = manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)
    replay = manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert rolled_back == replay
    assert rolled_back["state"] == "rolled_back"
    assert runner.calls.count(manager.rollback_command) == 1


@pytest.mark.parametrize("drift", ["foreign_proxy", "unknown_top_level"])
def test_rollback_refuses_to_delete_foreign_serve_configuration(
    tmp_path: Path, config: AccessConfig, drift: str
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    current = _active_serve(config)
    if drift == "foreign_proxy":
        current["Web"][f"{config.public_hostname}:443"]["Handlers"]["/"][
            "Proxy"
        ] = "http://127.0.0.1:9999"
    else:
        current["FutureExposure"] = {"Listeners": ["0.0.0.0:443"]}
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
        _json_result(current),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)

    with pytest.raises(AccessOperationError, match="destructive rollback"):
        manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert manager.rollback_command not in runner.calls


def test_rollback_rejects_world_readable_manifest(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    manifest_path.chmod(0o644)
    call_count = len(runner.calls)

    with pytest.raises(AccessOperationError, match="unsafe"):
        manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert len(runner.calls) == call_count


def test_rollback_rejects_hardlinked_manifest_before_tailscale_command(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    os.link(manifest_path, tmp_path / "rollback-manifest.alias")
    before = manifest_path.read_bytes()
    call_count = len(runner.calls)

    with pytest.raises(AccessOperationError, match="rollback manifest"):
        manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert manifest_path.read_bytes() == before
    assert manifest_path.stat().st_nlink == 2
    assert len(runner.calls) == call_count


def test_rollback_rejects_same_content_path_replacement_before_tailscale_command(
    tmp_path: Path, config: AccessConfig
) -> None:
    apply_runner = FakeRunner()
    manager = _ready_manager(config, apply_runner)
    apply_runner.add(
        ("tailscale", "status", "--json"), _json_result(_tailnet(config))
    )
    apply_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    apply_runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    retained_path = tmp_path / "retained-original-manifest"
    original = manifest_path.read_bytes()

    def replace_with_same_content() -> None:
        manifest_path.rename(retained_path)
        manifest_path.write_bytes(original)
        manifest_path.chmod(0o600)

    rollback_runner = FakeRunner()
    replacing = AccessManager(
        config,
        runner=rollback_runner,
        binary_available=lambda: True,
        gateway_probe=lambda candidate: GatewayAttestation(
            config_fingerprint(candidate), True
        ),
        composition_gate=lambda: True,
        after_rollback_log_open=replace_with_same_content,
    )

    with pytest.raises(AccessOperationError, match="pathname changed"):
        replacing.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert retained_path.read_bytes() == original
    assert manifest_path.read_bytes() == original
    assert retained_path.stat().st_ino != manifest_path.stat().st_ino
    assert rollback_runner.calls == []


def test_apply_revalidates_manifest_after_binary_availability_callback(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manifest_path = tmp_path / "rollback-manifest.json"
    retained_path = tmp_path / "retained-apply-manifest"

    def replace_with_same_content() -> bool:
        original = manifest_path.read_bytes()
        manifest_path.rename(retained_path)
        manifest_path.write_bytes(original)
        manifest_path.chmod(0o600)
        return True

    manager = AccessManager(
        config,
        runner=runner,
        binary_available=replace_with_same_content,
        gateway_probe=lambda candidate: GatewayAttestation(
            config_fingerprint(candidate), True
        ),
        composition_gate=lambda: True,
    )

    with pytest.raises(AccessOperationError, match="pathname changed"):
        _apply(manager, tmp_path)

    assert retained_path.read_bytes() == manifest_path.read_bytes() == b""
    assert retained_path.stat().st_ino != manifest_path.stat().st_ino
    assert runner.calls == []


def test_rollback_revalidates_manifest_after_binary_availability_callback(
    tmp_path: Path, config: AccessConfig
) -> None:
    apply_runner = FakeRunner()
    applied = _ready_manager(config, apply_runner)
    apply_runner.add(
        ("tailscale", "status", "--json"), _json_result(_tailnet(config))
    )
    apply_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    apply_runner.add(applied.serve_command, CommandResult(0, "configured", ""))
    _apply(applied, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    retained_path = tmp_path / "retained-rollback-manifest"

    def replace_with_same_content() -> bool:
        original = manifest_path.read_bytes()
        manifest_path.rename(retained_path)
        manifest_path.write_bytes(original)
        manifest_path.chmod(0o600)
        return True

    rollback_runner = FakeRunner()
    replacing = AccessManager(
        config,
        runner=rollback_runner,
        binary_available=replace_with_same_content,
        composition_gate=lambda: True,
    )

    with pytest.raises(AccessOperationError, match="pathname changed"):
        replacing.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert retained_path.read_bytes() == manifest_path.read_bytes()
    assert retained_path.stat().st_ino != manifest_path.stat().st_ino
    assert rollback_runner.calls == []


def test_apply_opens_existing_manifest_before_gateway_callback(
    tmp_path: Path, config: AccessConfig
) -> None:
    apply_runner = FakeRunner()
    applied = _ready_manager(config, apply_runner)
    apply_runner.add(
        ("tailscale", "status", "--json"), _json_result(_tailnet(config))
    )
    apply_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    apply_runner.add(applied.serve_command, CommandResult(0, "configured", ""))
    _apply(applied, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    retained_path = tmp_path / "retained-gateway-manifest"

    def replacing_gateway(candidate: AccessConfig) -> GatewayAttestation:
        original = manifest_path.read_bytes()
        manifest_path.rename(retained_path)
        manifest_path.write_bytes(original)
        manifest_path.chmod(0o600)
        return GatewayAttestation(config_fingerprint(candidate), True)

    runner = FakeRunner()
    replacing = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        gateway_probe=replacing_gateway,
        composition_gate=lambda: True,
    )

    with pytest.raises(AccessOperationError, match="pathname changed"):
        _apply(replacing, tmp_path)

    assert retained_path.read_bytes() == manifest_path.read_bytes()
    assert retained_path.stat().st_ino != manifest_path.stat().st_ino
    assert runner.calls == []


def test_apply_opens_existing_manifest_before_composition_callback(
    tmp_path: Path, config: AccessConfig
) -> None:
    apply_runner = FakeRunner()
    applied = _ready_manager(config, apply_runner)
    apply_runner.add(
        ("tailscale", "status", "--json"), _json_result(_tailnet(config))
    )
    apply_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    apply_runner.add(applied.serve_command, CommandResult(0, "configured", ""))
    _apply(applied, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    retained_path = tmp_path / "retained-composition-manifest"
    gateway_called = False

    def replacing_composition_gate() -> bool:
        original = manifest_path.read_bytes()
        manifest_path.rename(retained_path)
        manifest_path.write_bytes(original)
        manifest_path.chmod(0o600)
        return True

    def gateway_probe(candidate: AccessConfig) -> GatewayAttestation:
        nonlocal gateway_called
        gateway_called = True
        return GatewayAttestation(config_fingerprint(candidate), True)

    runner = FakeRunner()
    replacing = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        gateway_probe=gateway_probe,
        composition_gate=replacing_composition_gate,
    )

    with pytest.raises(AccessOperationError, match="pathname changed"):
        _apply(replacing, tmp_path)

    assert retained_path.read_bytes() == manifest_path.read_bytes()
    assert retained_path.stat().st_ino != manifest_path.stat().st_ino
    assert gateway_called is False
    assert runner.calls == []


def test_runner_manifest_replacement_fails_post_call_binding_before_idempotent_return(
    tmp_path: Path, config: AccessConfig
) -> None:
    apply_runner = FakeRunner()
    applied = _ready_manager(config, apply_runner)
    apply_runner.add(
        ("tailscale", "status", "--json"), _json_result(_tailnet(config))
    )
    apply_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    apply_runner.add(applied.serve_command, CommandResult(0, "configured", ""))
    _apply(applied, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    retained_path = tmp_path / "retained-runner-manifest"

    class ReplacingRunner(FakeRunner):
        def run(self, arguments: Sequence[str]) -> CommandResult:
            result = super().run(arguments)
            original = manifest_path.read_bytes()
            manifest_path.rename(retained_path)
            manifest_path.write_bytes(original)
            manifest_path.chmod(0o600)
            return result

    runner = ReplacingRunner()
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    replacing = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: True,
        gateway_probe=lambda candidate: GatewayAttestation(
            config_fingerprint(candidate), True
        ),
        composition_gate=lambda: True,
    )

    with pytest.raises(AccessOperationError, match="pathname changed"):
        _apply(replacing, tmp_path)

    assert runner.calls == [("tailscale", "status", "--json")]
    assert retained_path.read_bytes() == manifest_path.read_bytes()
    assert retained_path.stat().st_ino != manifest_path.stat().st_ino


@pytest.mark.parametrize(
    "fault_at",
    [
        "after_rollback_frame_header",
        "after_rollback_frame_body",
        "after_rollback_checksum_partial",
    ],
)
def test_interrupted_rollback_transition_recovers_from_last_complete_frame(
    tmp_path: Path, config: AccessConfig, fault_at: str
) -> None:
    apply_runner = FakeRunner()
    manager = _ready_manager(config, apply_runner)
    apply_runner.add(
        ("tailscale", "status", "--json"), _json_result(_tailnet(config))
    )
    apply_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    apply_runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)

    crash_runner = FakeRunner()
    crash_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_active_serve(config)),
        _json_result(_empty_serve()),
    )
    crash_runner.add(manager.rollback_command, CommandResult(0, "off", ""))
    crashing = AccessManager(
        config,
        runner=crash_runner,
        binary_available=lambda: True,
        composition_gate=lambda: True,
        rollback_fault_at=fault_at,
    )
    with pytest.raises(InjectedAccessOperationCrash, match=fault_at):
        crashing.rollback(tmp_path, approval=ROLLBACK_APPROVAL)
    partial = (tmp_path / "rollback-manifest.json").read_bytes()

    recovery_runner = FakeRunner()
    recovery_runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
    )
    recovered = AccessManager(
        config,
        runner=recovery_runner,
        binary_available=lambda: True,
        composition_gate=lambda: True,
    ).rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert recovered["state"] == "rolled_back"
    assert (tmp_path / "rollback-manifest.json").read_bytes() != partial
    assert recovery_runner.calls == [("tailscale", "serve", "status", "--json")]


def test_complete_rollback_log_corruption_fails_before_tailscale_command(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    corrupted = bytearray(manifest_path.read_bytes())
    corrupted[-1] ^= 1
    manifest_path.write_bytes(corrupted)
    manifest_path.chmod(0o600)
    call_count = len(runner.calls)

    with pytest.raises(AccessOperationError, match="checksum"):
        manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert manifest_path.read_bytes() == corrupted
    assert len(runner.calls) == call_count


def test_partial_tail_is_preserved_until_next_legal_transition(
    tmp_path: Path, config: AccessConfig
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
        _json_result({"Web": {"foreign:443": {"Handlers": {}}}}),
        _json_result(_empty_serve()),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    _apply(manager, tmp_path)
    manifest_path = tmp_path / "rollback-manifest.json"
    with manifest_path.open("ab") as stream:
        stream.write(b"\x00\x00")
        stream.flush()
        os.fsync(stream.fileno())
    partial = manifest_path.read_bytes()

    with pytest.raises(AccessOperationError, match="destructive rollback"):
        manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)
    assert manifest_path.read_bytes() == partial

    recovered = manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)
    assert recovered["state"] == "rolled_back"
    assert manifest_path.read_bytes() != partial


def test_apply_and_rollback_never_replace_or_unlink_manifest_path(
    tmp_path: Path, config: AccessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_path_mutation(*args: object, **kwargs: object) -> None:
        raise AssertionError("rollback state must not replace or unlink a pathname")

    monkeypatch.setattr(os, "replace", reject_path_mutation)
    monkeypatch.setattr(os, "unlink", reject_path_mutation)
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_empty_serve()),
        _json_result(_active_serve(config)),
        _json_result(_active_serve(config)),
        _json_result(_empty_serve()),
    )
    runner.add(manager.serve_command, CommandResult(0, "configured", ""))
    runner.add(manager.rollback_command, CommandResult(0, "off", ""))

    _apply(manager, tmp_path)
    rolled_back = manager.rollback(tmp_path, approval=ROLLBACK_APPROVAL)

    assert rolled_back["state"] == "rolled_back"


def test_doctor_reports_local_browser_blocked_without_composition(
    config: AccessConfig,
) -> None:
    runner = FakeRunner()
    manager = AccessManager(
        config,
        runner=runner,
        binary_available=lambda: False,
        probe=lambda host, port: port in {3000, 8791},
    )

    report = manager.doctor().to_dict()

    assert report["status"] == "degraded"
    assert report["tailscale_available"] is False
    assert report["daemon_remote_exposed"] is None
    assert report["funnel_disabled"] is None
    assert report["local_url"] is None
    assert report["local_browser_available"] is False
    assert report["local_browser_status"] == "blocked_until_p2_devrel_compose"
    assert report["cleanup_deferred_to_p2_devrel"] is True
    assert report["iphone_pwa_url"] == config.public_origin
    assert "tailscale_unavailable" in report["issues"]
    assert "local_browser_front_door_not_composed" in report["issues"]
    assert runner.calls == []


def test_doctor_redacts_raw_status_identity_and_secret(config: AccessConfig) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(
        ("tailscale", "serve", "status", "--json"),
        _json_result(_active_serve(config)),
    )

    rendered = json.dumps(manager.doctor().to_dict(), sort_keys=True)

    assert '"status": "degraded"' in rendered
    assert "local_browser_front_door_not_composed" in rendered
    assert "must-not-leak" not in rendered
    assert "owner@example.com" not in rendered
    assert config.session_bootstrap_secret_ref not in rendered
    assert config.daemon_upstream.url not in rendered
    assert config.public_origin in rendered


@pytest.mark.parametrize(
    "drift", ["missing_caps", "extra_path", "extra_host", "unknown_top_level"]
)
def test_doctor_rejects_non_exact_serve_shape(
    config: AccessConfig, drift: str
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    serve = copy.deepcopy(_active_serve(config))
    web = serve["Web"]
    assert isinstance(web, dict)
    host = web[f"{config.public_hostname}:443"]
    assert isinstance(host, dict)
    handlers = host["Handlers"]
    assert isinstance(handlers, dict)
    root = handlers["/"]
    assert isinstance(root, dict)
    if drift == "missing_caps":
        root.pop("AcceptAppCaps")
    elif drift == "extra_path":
        handlers["/admin"] = copy.deepcopy(root)
    elif drift == "extra_host":
        web["other.example-tailnet.ts.net:443"] = copy.deepcopy(host)
    else:
        serve["FutureExposure"] = {"Listeners": ["0.0.0.0:443"]}
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(("tailscale", "serve", "status", "--json"), _json_result(serve))

    report = manager.doctor().to_dict()

    assert report["serve_private_https"] is False
    assert "private_serve_not_verified" in report["issues"]


def test_doctor_reports_unknown_network_state_without_claiming_safety(
    config: AccessConfig,
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    runner.add(
        ("tailscale", "status", "--json"),
        CommandResult(1, "", "unavailable with private details"),
    )

    report = manager.doctor().to_dict()

    assert report["status"] == "degraded"
    assert report["funnel_disabled"] is None
    assert report["daemon_remote_exposed"] is None
    assert "tailscale_status_unavailable" in report["issues"]


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:8791",
        "http://localhost:8791",
        "tcp://127.0.0.1:8791",
        "127.0.0.1:8791",
        "http://127.1:8791",
        "http://localhost.:8791",
    ],
)
def test_doctor_flags_daemon_target_without_disclosing_raw_status(
    config: AccessConfig, target: str
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    serve = _active_serve(config)
    serve["Web"][f"{config.public_hostname}:443"]["Handlers"]["/"][
        "Proxy"
    ] = target
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(("tailscale", "serve", "status", "--json"), _json_result(serve))

    rendered = manager.doctor().to_dict()

    assert rendered["daemon_remote_exposed"] is True
    assert "daemon_target_exposed" in rendered["issues"]
    assert target not in json.dumps(rendered)


def test_doctor_flags_tcp_forward_to_daemon(config: AccessConfig) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    serve = {"TCP": {"443": {"TCPForward": "127.0.0.1:8791"}}}
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(("tailscale", "serve", "status", "--json"), _json_result(serve))

    report = manager.doctor().to_dict()

    assert report["daemon_remote_exposed"] is True
    assert "daemon_target_exposed" in report["issues"]


def test_doctor_reports_unknown_daemon_alias_instead_of_safe(
    config: AccessConfig,
) -> None:
    runner = FakeRunner()
    manager = _ready_manager(config, runner)
    serve = {"TCP": {"443": {"TCPForward": "private-alias:8791"}}}
    runner.add(("tailscale", "status", "--json"), _json_result(_tailnet(config)))
    runner.add(("tailscale", "serve", "status", "--json"), _json_result(serve))

    report = manager.doctor().to_dict()

    assert report["daemon_remote_exposed"] is None
    assert "daemon_target_state_unknown" in report["issues"]
