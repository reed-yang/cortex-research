from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from distribution.bundle import BundleBuilder
from distribution.install import DistributionInstaller, InstallError
from distribution.product_paths import resolve_installed_product_paths


def _bundle(root: Path, wheels: tuple[Path, Path]) -> Path:
    return BundleBuilder(root).assemble(
        release_id="cortex-dev-1",
        release_sequence=1,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheels,
        created_at="2026-07-28T12:00:01Z",
    ).path


@pytest.fixture
def installer(tmp_path: Path, wheel_pair: tuple[Path, Path]) -> DistributionInstaller:
    value = DistributionInstaller(tmp_path / "distribution")
    value.install(
        _bundle(tmp_path / "bundle", wheel_pair),
        allow_unsigned_developer=True,
    )
    return value


@pytest.fixture
def installed_root(installer: DistributionInstaller) -> Path:
    return installer.root


def _write_uninstall_journal(root: Path, payload: str) -> Path:
    path = root / ".uninstall-transaction.json"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


def _recovery_report() -> dict[str, object]:
    return {
        "installed": True,
        "developer_usable": False,
        "ga_ready": False,
        "web_url": None,
        "category": "recovery_required",
    }


def test_uninstall_journal_rejects_wrong_schema(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _write_uninstall_journal(
        installed_root,
        '{"schema_version": 2}',
    )

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.exists()


def test_uninstall_journal_rejects_symlink(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    target = tmp_path / "uninstall-journal-target"
    target.write_text("{}", encoding="utf-8")
    (installed_root / ".uninstall-transaction.json").symlink_to(target)

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert target.read_text(encoding="utf-8") == "{}"


def test_uninstall_journal_rejects_unknown_field(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _write_uninstall_journal(
        installed_root,
        (
            '{"schema_version":1,"operation":"uninstall","phase":"prepared",'
            '"current_before":null,"lkg_before":null,"unexpected":true}'
        ),
    )

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.exists()


def test_uninstall_journal_rejects_null_document(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    # A present-but-null journal is valid JSON but a malformed document; the
    # shared archive reader must never conflate it with an absent journal.
    journal = _write_uninstall_journal(installed_root, "null")

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.exists()


def test_pointer_journal_rejects_null_document(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    # Same shared-reader contract for the pointer journal: a null document is
    # malformed and must fail closed, not be treated as "no journal present".
    path = installed_root / ".pointer-transaction.json"
    path.write_text("null", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(InstallError, match="update journal schema is invalid"):
        installer.recover()

    assert path.exists()


def test_doctor_reports_uninstall_recovery_required(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _write_uninstall_journal(installed_root, "unsafe evidence")
    before = journal.read_bytes()

    assert installer.doctor() == _recovery_report()
    assert journal.read_bytes() == before


def test_doctor_reports_orphan_trash_recovery_required(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    trash = installed_root / ".uninstall-trash"
    trash.mkdir(mode=0o700)

    assert installer.doctor() == _recovery_report()
    assert trash.is_dir()


def test_recover_none_without_journal(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    assert os.path.lexists(installed_root)
    assert installer.recover() == "none"


def _uninstall_context(
    tmp_path: Path,
    installer: DistributionInstaller,
) -> tuple[Path, Path]:
    current = installer._read_uninstall_transaction()
    assert current is None
    pointer = (installer.root / "current.json").read_text(encoding="utf-8")
    assert pointer
    version = json.loads(pointer)["version"]
    home = tmp_path / "home"
    paths = resolve_installed_product_paths(
        installer.root / "versions" / version / "runtime",
        home=home,
        environment={},
    )
    return paths.runtime_update_root, home


def test_uninstall_still_removes_root_completely(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    runtime_root, home = _uninstall_context(tmp_path, installer)

    installer.uninstall(runtime_root=runtime_root, home=home, environment={})

    assert not os.path.lexists(installed_root)


def test_uninstall_refuses_when_journal_already_present(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    runtime_root, home = _uninstall_context(tmp_path, installer)
    journal = _write_uninstall_journal(installed_root, "existing recovery evidence")

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.uninstall(runtime_root=runtime_root, home=home, environment={})

    assert journal.read_text(encoding="utf-8") == "existing recovery evidence"
    assert (installed_root / "current.json").is_file()


def test_uninstall_refuses_when_trash_already_present(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    runtime_root, home = _uninstall_context(tmp_path, installer)
    trash = installed_root / ".uninstall-trash"
    trash.mkdir(mode=0o700)

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.uninstall(runtime_root=runtime_root, home=home, environment={})

    assert trash.is_dir()
    assert (installed_root / "current.json").is_file()


def test_uninstall_preserves_external_paths(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    payload = external / "state.bin"
    payload.write_bytes(b"external-state\n")
    alias = external / "state-link"
    alias.symlink_to(payload.name)
    before = (payload.read_bytes(), os.readlink(alias))
    runtime_root, home = _uninstall_context(tmp_path, installer)

    installer.uninstall(runtime_root=runtime_root, home=home, environment={})

    assert (payload.read_bytes(), os.readlink(alias)) == before
    assert not os.path.lexists(installed_root)


def test_uninstall_prepared_checkpoint_leaves_closed_journal_and_installation(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    class PausingUninstaller(DistributionInstaller):
        @staticmethod
        def _uninstall_checkpoint(phase: str) -> None:
            if phase == "prepared":
                raise RuntimeError("prepared checkpoint")

    runtime_root, home = _uninstall_context(tmp_path, installer)

    with pytest.raises(RuntimeError, match="prepared checkpoint"):
        PausingUninstaller(installed_root).uninstall(
            runtime_root=runtime_root,
            home=home,
            environment={},
        )

    assert (installed_root / ".uninstall-transaction.json").is_file()
    assert (installed_root / "current.json").is_file()
    assert not (installed_root / ".uninstall-trash").exists()


def _pointer(root: Path, name: str) -> dict[str, object] | None:
    path = root / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _stage_uninstall_journal(root: Path, *, phase: str = "prepared") -> Path:
    return _write_uninstall_journal(
        root,
        json.dumps(
            {
                "schema_version": 1,
                "operation": "uninstall",
                "phase": phase,
                "current_before": _pointer(root, "current.json"),
                "lkg_before": _pointer(root, "last-known-good.json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _isolate_owned_directories(root: Path) -> Path:
    trash = root / ".uninstall-trash"
    trash.mkdir(mode=0o700)
    for name in ("bin", "snapshots", "versions"):
        source = root / name
        if source.exists():
            os.rename(source, trash / name)
    return trash


def _commit_staged_uninstall(root: Path, trash: Path) -> None:
    lkg = root / "last-known-good.json"
    if lkg.exists():
        os.rename(lkg, trash / lkg.name)
    os.rename(root / "current.json", trash / "current.json")


def test_recover_aborts_pre_commit_state(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    current = _pointer(installed_root, "current.json")
    lkg = _pointer(installed_root, "last-known-good.json")
    _stage_uninstall_journal(installed_root, phase="isolated")
    _isolate_owned_directories(installed_root)

    assert installer.recover() == "aborted"

    assert _pointer(installed_root, "current.json") == current
    assert _pointer(installed_root, "last-known-good.json") == lkg
    assert all(
        (installed_root / name).exists()
        for name in ("bin", "versions")
    )
    assert not (installed_root / ".uninstall-trash").exists()
    assert not (installed_root / ".uninstall-transaction.json").exists()
    assert installer.recover() == "none"


def test_recover_completes_post_commit_state(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    _stage_uninstall_journal(installed_root, phase="committed")
    trash = _isolate_owned_directories(installed_root)
    _commit_staged_uninstall(installed_root, trash)

    assert installer.recover() == "completed"
    assert not os.path.lexists(installed_root)
    assert installer.recover() == "none"


def test_recover_manual_on_both_journals_preserves_all_evidence(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    uninstall_journal = _stage_uninstall_journal(installed_root)
    pointer_journal = installed_root / ".pointer-transaction.json"
    pointer_journal.write_bytes(b"pointer evidence must remain unchanged\n")
    before = {
        path: path.read_bytes()
        for path in (
            uninstall_journal,
            pointer_journal,
            installed_root / "current.json",
            installed_root / "last-known-good.json",
        )
    }

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert {path: path.read_bytes() for path in before} == before


def test_recover_manual_on_foreign_trash_entry_preserves_evidence(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root)
    trash = _isolate_owned_directories(installed_root)
    foreign = trash / "foreign-state"
    foreign.write_bytes(b"do not delete\n")
    before = journal.read_bytes()

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.read_bytes() == before
    assert foreign.read_bytes() == b"do not delete\n"
    assert (trash / "versions").is_dir()


def test_recover_manual_on_unowned_root_entry_preserves_evidence(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root)
    foreign = installed_root / "foreign-state"
    foreign.write_bytes(b"do not delete\n")
    before = journal.read_bytes()

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.read_bytes() == before
    assert foreign.read_bytes() == b"do not delete\n"


def test_recover_manual_on_name_in_both_places_preserves_both(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root, phase="isolated")
    trash = _isolate_owned_directories(installed_root)
    root_bin = installed_root / "bin"
    root_bin.mkdir(mode=0o700)
    root_marker = root_bin / "root-copy"
    root_marker.write_bytes(b"root\n")
    trash_marker = trash / "bin" / "trash-copy"
    trash_marker.write_bytes(b"trash\n")
    before = journal.read_bytes()

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.read_bytes() == before
    assert root_marker.read_bytes() == b"root\n"
    assert trash_marker.read_bytes() == b"trash\n"


def test_recover_manual_on_current_mismatch_preserves_evidence(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root)
    current_path = installed_root / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["release_sequence"] += 1
    current_path.write_text(json.dumps(current, sort_keys=True) + "\n", encoding="utf-8")
    before = journal.read_bytes()

    with pytest.raises(InstallError, match="manual distribution recovery"):
        installer.recover()

    assert journal.read_bytes() == before
    assert json.loads(current_path.read_text(encoding="utf-8")) == current


def test_abort_preserves_journal_when_restored_generation_is_corrupt(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root, phase="isolated")
    before = journal.read_bytes()
    trash = _isolate_owned_directories(installed_root)
    manifest = next((trash / "versions").glob("*/bundle/manifest.json"))
    manifest.write_bytes(b"corrupt generation\n")

    with pytest.raises(InstallError):
        installer.recover()

    assert journal.read_bytes() == before
    assert not trash.exists()
    assert all((installed_root / name).exists() for name in ("bin", "versions"))

    with pytest.raises(InstallError):
        installer.recover()

    assert journal.read_bytes() == before
    assert not trash.exists()
    assert all((installed_root / name).exists() for name in ("bin", "versions"))


def test_uninstall_succeeds_after_aborted_recovery(
    tmp_path: Path,
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    runtime_root, home = _uninstall_context(tmp_path, installer)
    _stage_uninstall_journal(installed_root, phase="isolated")
    _isolate_owned_directories(installed_root)

    assert installer.recover() == "aborted"
    installer.uninstall(runtime_root=runtime_root, home=home, environment={})

    assert not os.path.lexists(installed_root)


def _create_external_sentinel(tmp_path: Path) -> tuple[Path, Path]:
    external = tmp_path / "external-state"
    external.mkdir(mode=0o700)
    payload = external / "state.bin"
    payload.write_bytes(b"external-state-must-remain-byte-exact\n")
    payload.chmod(0o640)
    alias = external / "state-link"
    alias.symlink_to(payload.name)
    return payload, alias


def _sentinel_evidence(paths: tuple[Path, ...]) -> tuple[dict[str, object], ...]:
    evidence: list[dict[str, object]] = []
    for path in paths:
        details = path.lstat()
        item: dict[str, object] = {
            "type": stat.S_IFMT(details.st_mode),
            "mode": stat.S_IMODE(details.st_mode),
            "uid": details.st_uid,
            "size": details.st_size,
        }
        if stat.S_ISREG(details.st_mode):
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(details.st_mode):
            item["target"] = os.readlink(path)
        evidence.append(item)
    return tuple(evidence)


def _run_uninstall_process(
    prefix: Path,
    *,
    runtime_root: Path,
    home: Path,
    crash_phase: str,
) -> subprocess.CompletedProcess[str]:
    script = r'''
import os
import sys
from pathlib import Path

from distribution.install import DistributionInstaller

phase = sys.argv[1]

class CrashUninstaller(DistributionInstaller):
    @staticmethod
    def _uninstall_checkpoint(checkpoint):
        if checkpoint == phase:
            os._exit({"prepared": 94, "isolated": 95, "committed": 96}[phase])

CrashUninstaller(Path(sys.argv[2])).uninstall(
    runtime_root=Path(sys.argv[3]),
    home=Path(sys.argv[4]),
    environment={},
)
'''
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            crash_phase,
            str(prefix),
            str(runtime_root),
            str(home),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        },
    )


@pytest.mark.parametrize(
    ("checkpoint", "exit_code", "recovery_action"),
    [
        ("prepared", 94, "aborted"),
        ("isolated", 95, "aborted"),
        ("committed", 96, "completed"),
    ],
)
def test_uninstall_os_exit_crash_matrix(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    checkpoint: str,
    exit_code: int,
    recovery_action: str,
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair)
    installer = DistributionInstaller(prefix)
    installed = installer.install(bundle, allow_unsigned_developer=True)
    runtime = prefix / "versions" / installed.version / "runtime"
    home = tmp_path / "home"
    paths = resolve_installed_product_paths(runtime, home=home, environment={})
    external = _create_external_sentinel(tmp_path)
    product_sentinels = (
        paths.config_dir / "config.toml",
        paths.state_dir / "state.bin",
        paths.log_dir / "events.log",
    )
    for path, payload in zip(
        product_sentinels,
        (b"config\n", b"state\n", b"logs\n"),
        strict=True,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    sentinels = (*external, *product_sentinels)
    evidence = _sentinel_evidence(sentinels)
    current_before = _pointer(prefix, "current.json")
    lkg_before = _pointer(prefix, "last-known-good.json")

    crashed = _run_uninstall_process(
        prefix,
        runtime_root=paths.runtime_update_root,
        home=home,
        crash_phase=checkpoint,
    )

    assert crashed.returncode == exit_code, (crashed.stdout, crashed.stderr)
    recovered = DistributionInstaller(prefix)
    assert recovered.recover() == recovery_action
    if recovery_action == "aborted":
        assert _pointer(prefix, "current.json") == current_before
        assert _pointer(prefix, "last-known-good.json") == lkg_before
        assert all((prefix / name).exists() for name in ("bin", "versions"))
        recovered._verify_pointer_generation(current_before)
        recovered._health_check(runtime)
        assert recovered.doctor()["installed"] is True
    else:
        assert not os.path.lexists(prefix)
    assert _sentinel_evidence(sentinels) == evidence
    assert recovered.recover() == "none"
    if recovery_action == "aborted":
        recovered.uninstall(
            runtime_root=paths.runtime_update_root,
            home=home,
            environment={},
        )
        assert not os.path.lexists(prefix)
        assert _sentinel_evidence(sentinels) == evidence

    shutil.rmtree(tmp_path / "bundle")
    fresh_bundle = _bundle(tmp_path / "fresh-bundle", wheel_pair)
    fresh = DistributionInstaller(prefix).install(
        fresh_bundle,
        allow_unsigned_developer=True,
    )
    assert fresh.action == "installed"
    assert (prefix / "current.json").is_file()
    assert _sentinel_evidence(sentinels) == evidence


def test_recover_completes_when_only_uninstall_journal_remains(
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root, phase="committed")
    for path in tuple(installed_root.iterdir()):
        if path == journal:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    assert {path.name for path in installed_root.iterdir()} == {
        ".uninstall-transaction.json"
    }
    assert installer.recover() == "completed"
    assert not os.path.lexists(installed_root)


def test_install_recovers_journal_only_teardown_window_and_installs_fresh(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    installed_root: Path,
    installer: DistributionInstaller,
) -> None:
    journal = _stage_uninstall_journal(installed_root, phase="committed")
    for path in tuple(installed_root.iterdir()):
        if path == journal:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    fresh_bundle = _bundle(tmp_path / "fresh-bundle", wheel_pair)

    result = DistributionInstaller(installed_root).install(
        fresh_bundle,
        allow_unsigned_developer=True,
    )

    assert result.action == "installed"
    assert (installed_root / ".cortex-distribution-root.json").is_file()
    assert (installed_root / "current.json").is_file()
    assert not journal.exists()


def test_empty_unmarked_root_residue_is_inert_and_reinstallable(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    prefix.mkdir(mode=0o700)
    installer = DistributionInstaller(prefix)

    assert installer.recover() == "none"
    assert (prefix / ".cortex-distribution-root.json").is_file()

    result = installer.install(
        _bundle(tmp_path / "bundle", wheel_pair),
        allow_unsigned_developer=True,
    )
    assert result.action == "installed"
    assert (prefix / "current.json").is_file()


@pytest.mark.parametrize(
    ("committed", "action"),
    [(False, "aborted"), (True, "completed")],
)
def test_recover_cli_passes_through_uninstall_action(
    installed_root: Path,
    installer: DistributionInstaller,
    capsys: pytest.CaptureFixture[str],
    committed: bool,
    action: str,
) -> None:
    from distribution.cli import main

    _stage_uninstall_journal(
        installed_root,
        phase="committed" if committed else "isolated",
    )
    trash = _isolate_owned_directories(installed_root)
    if committed:
        _commit_staged_uninstall(installed_root, trash)

    assert main(["recover", "--prefix", str(installed_root)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "result": {"action": action},
    }
