from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from distribution.bundle import BundleBuilder, verify_bundle
from distribution.install import DistributionInstaller, InstallError
from distribution.product_paths import resolve_installed_product_paths


def _bundle(
    root: Path,
    wheels: tuple[Path, Path],
    *,
    release_id: str = "cortex-dev-1",
    sequence: int = 1,
) -> Path:
    return BundleBuilder(root).assemble(
        release_id=release_id,
        release_sequence=sequence,
        source_commit=f"{sequence:x}" * 40,
        lock_sha256=f"{sequence + 1:x}" * 64,
        wheels=wheels,
        created_at=f"2026-07-28T12:00:0{sequence}Z",
    ).path


def _installed_pair(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> tuple[DistributionInstaller, Path, dict[str, object], dict[str, object]]:
    prefix = tmp_path / "distribution"
    installer = DistributionInstaller(prefix)
    first = _bundle(tmp_path / "bundle-one", wheel_pair)
    second = _bundle(
        tmp_path / "bundle-two",
        wheel_pair,
        release_id="cortex-dev-2",
        sequence=2,
    )
    installed = installer.install(first, allow_unsigned_developer=True)
    home = tmp_path / "home"
    paths = resolve_installed_product_paths(
        prefix / "versions" / installed.version / "runtime",
        home=home,
        environment={},
    )
    installer.upgrade(
        second,
        runtime_root=paths.runtime_update_root,
        home=home,
        allow_unsigned_developer=True,
    )
    current = json.loads((prefix / "current.json").read_text())
    lkg = json.loads((prefix / "last-known-good.json").read_text())
    return installer, prefix, current, lkg


def _rollback_journal(
    current: dict[str, object],
    lkg: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": "rollback",
        "phase": "prepared",
        "current_before": current,
        "current_after": lkg,
        "lkg_before": lkg,
        "lkg_after": current,
        "snapshot": None,
        "candidate_owned": False,
        "launchers_owned": False,
    }


def _write_journal(prefix: Path, journal: dict[str, object]) -> Path:
    path = prefix / ".pointer-transaction.json"
    path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)
    return path


def _create_external_sentinel(tmp_path: Path) -> tuple[Path, Path]:
    external = tmp_path / "external-state"
    external.mkdir(mode=0o700)
    payload = external / "state.bin"
    payload.write_bytes(b"external-state-must-remain-byte-exact\n")
    payload.chmod(0o640)
    alias = external / "state-link"
    alias.symlink_to(payload.name)
    return payload, alias


def _sentinel_evidence(paths: tuple[Path, Path]) -> tuple[dict[str, object], ...]:
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


def _run_operation_process(
    operation: str,
    prefix: Path,
    *,
    bundle: Path | None,
    runtime_root: Path | None,
    home: Path,
    crash_phase: str | None,
) -> subprocess.CompletedProcess[str]:
    script = r"""
import json
import os
import sys
from pathlib import Path

from distribution.install import DistributionInstaller

phase = sys.argv[1]

class CrashInstaller(DistributionInstaller):
    @staticmethod
    def _pointer_transaction_checkpoint(checkpoint):
        if checkpoint == phase:
            os._exit({"prepared": 91, "last_known_good_published": 92, "current_published": 93}[phase])

installer_class = CrashInstaller if phase != "none" else DistributionInstaller
installer = installer_class(Path(sys.argv[3]))
operation = sys.argv[2]
bundle = Path(sys.argv[4])
runtime_root = Path(sys.argv[5])
home = Path(sys.argv[6])
if operation == "install":
    result = installer.install(bundle, allow_unsigned_developer=True)
elif operation == "upgrade":
    result = installer.upgrade(
        bundle,
        runtime_root=runtime_root,
        home=home,
        environment={},
        allow_unsigned_developer=True,
    )
else:
    result = installer.rollback(
        runtime_root=runtime_root,
        home=home,
        environment={},
    )
print(json.dumps(result.__dict__, sort_keys=True))
"""
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            crash_phase or "none",
            operation,
            str(prefix),
            str(bundle or Path("unused")),
            str(runtime_root or Path("unused")),
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
    ("fault", "returncode"),
    [
        ("venv.EnvBuilder.create = lambda self, path: os._exit(91)", 91),
        (
            "DistributionInstaller._health_check = "
            "lambda self, runtime: os._exit(92)",
            92,
        ),
    ],
)
def test_process_exit_during_composition_never_publishes_a_partial_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    fault: str,
    returncode: int,
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair)
    verified = verify_bundle(bundle)
    version = f"{verified.manifest['release_id']}-{verified.digest[:16]}"
    script = """
import os
import sys
import venv
from pathlib import Path

from distribution.install import DistributionInstaller

{fault}
DistributionInstaller(Path(sys.argv[1])).install(
    Path(sys.argv[2]),
    allow_unsigned_developer=True,
)
""".format(fault=fault)

    crashed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(prefix), str(bundle)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert crashed.returncode == returncode
    versions = prefix / "versions"
    assert not (versions / version).exists()
    assert all(path.name.startswith(".candidate-") for path in versions.iterdir())

    result = DistributionInstaller(prefix).install(
        bundle,
        allow_unsigned_developer=True,
    )

    assert result.version == version
    assert sorted(path.name for path in versions.iterdir()) == [version]


def test_stale_candidate_symlink_fails_closed_without_following(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair)
    installer = DistributionInstaller(prefix)
    installer.install(bundle, allow_unsigned_developer=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("preserve me\n")
    trap = prefix / "versions" / ".candidate-trap"
    trap.symlink_to(external, target_is_directory=True)

    with pytest.raises(InstallError, match="stale candidate is unsafe"):
        installer.install(bundle, allow_unsigned_developer=True)

    assert trap.is_symlink()
    assert sentinel.read_text() == "preserve me\n"


def test_recover_aborts_an_uncommitted_pointer_transaction(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    journal = _write_journal(prefix, _rollback_journal(current, lkg))
    (prefix / "last-known-good.json").write_text(
        json.dumps(current, sort_keys=True) + "\n"
    )

    assert installer.recover() == "aborted"

    assert json.loads((prefix / "current.json").read_text()) == current
    assert json.loads((prefix / "last-known-good.json").read_text()) == lkg
    assert not journal.exists()


def test_recover_completes_a_committed_pointer_transaction(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    journal = _write_journal(prefix, _rollback_journal(current, lkg))
    (prefix / "current.json").write_text(json.dumps(lkg, sort_keys=True) + "\n")

    assert installer.recover() == "committed"

    assert json.loads((prefix / "current.json").read_text()) == lkg
    assert json.loads((prefix / "last-known-good.json").read_text()) == current
    assert not journal.exists()


def test_recover_preserves_a_foreign_mixed_pointer_state(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    journal = _write_journal(prefix, _rollback_journal(current, lkg))
    candidate = prefix / "versions" / ".candidate-forensics"
    candidate.mkdir()
    evidence = candidate / "evidence.txt"
    evidence.write_text("preserve transaction evidence\n")
    foreign = {**current, "release_sequence": 99}
    (prefix / "current.json").write_text(
        json.dumps(foreign, sort_keys=True) + "\n"
    )

    with pytest.raises(InstallError, match="manual distribution recovery is required"):
        installer.recover()

    assert journal.exists()
    assert json.loads((prefix / "current.json").read_text()) == foreign
    assert evidence.read_text() == "preserve transaction evidence\n"


def test_recover_rejects_an_open_journal_schema(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    journal = _rollback_journal(current, lkg)
    journal["private_path"] = "/must/not/be/accepted"
    path = _write_journal(prefix, journal)

    with pytest.raises(InstallError, match="update journal schema is invalid"):
        installer.recover()

    assert path.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda journal: journal.pop("phase"),
        lambda journal: journal.__setitem__("phase", "finished"),
        lambda journal: journal.__setitem__("candidate_owned", 1),
        lambda journal: journal.__setitem__("snapshot", "../private.json"),
        lambda journal: journal["current_after"].__setitem__("version", "unsafe"),
    ],
)
def test_recover_rejects_invalid_closed_journal_values(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    mutate: object,
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    journal = _rollback_journal(current, lkg)
    mutate(journal)  # type: ignore[operator]
    path = _write_journal(prefix, journal)

    with pytest.raises(InstallError, match="update journal"):
        installer.recover()

    assert path.exists()


def test_recover_rejects_an_insecure_journal_file(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    path = _write_journal(prefix, _rollback_journal(current, lkg))
    path.chmod(0o644)

    with pytest.raises(InstallError, match="update journal is unsafe"):
        installer.recover()

    assert path.exists()


def test_doctor_reports_recovery_required_without_mutating_the_journal(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    path = _write_journal(prefix, _rollback_journal(current, lkg))
    before = path.read_bytes()

    assert installer.doctor() == {
        "installed": True,
        "developer_usable": False,
        "ga_ready": False,
        "web_url": None,
        "category": "recovery_required",
    }

    assert path.read_bytes() == before


def test_recover_cli_reports_the_completed_action(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from distribution.cli import main

    _installer, prefix, current, lkg = _installed_pair(tmp_path, wheel_pair)
    _write_journal(prefix, _rollback_journal(current, lkg))
    (prefix / "current.json").write_text(json.dumps(lkg, sort_keys=True) + "\n")

    assert main(["recover", "--prefix", str(prefix)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "result": {"action": "committed"}}


def test_returned_prepare_failure_uses_the_durable_recovery_path(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    class FailingInstaller(DistributionInstaller):
        @staticmethod
        def _pointer_transaction_checkpoint(phase: str) -> None:
            if phase == "prepared":
                raise RuntimeError("injected checkpoint failure")

    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair)

    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        FailingInstaller(prefix).install(bundle, allow_unsigned_developer=True)

    assert not (prefix / ".pointer-transaction.json").exists()
    assert not (prefix / "current.json").exists()
    assert not (prefix / "last-known-good.json").exists()
    assert not (prefix / "versions").exists()
    assert not (prefix / "bin").exists()


def test_returned_install_pointer_failure_cleans_owned_generation_and_launchers(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    class FailingInstaller(DistributionInstaller):
        @staticmethod
        def _pointer_transaction_checkpoint(phase: str) -> None:
            if phase == "last_known_good_published":
                raise RuntimeError("injected pointer failure")

    prefix = tmp_path / "distribution"
    bundle = _bundle(tmp_path / "bundle", wheel_pair)

    with pytest.raises(RuntimeError, match="injected pointer failure"):
        FailingInstaller(prefix).install(bundle, allow_unsigned_developer=True)

    assert not (prefix / ".pointer-transaction.json").exists()
    assert not (prefix / "current.json").exists()
    assert not (prefix / "last-known-good.json").exists()
    assert not (prefix / "versions").exists()
    assert not (prefix / "bin").exists()


def test_returned_upgrade_pointer_failure_cleans_owned_generation_and_snapshot(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    class FailingInstaller(DistributionInstaller):
        @staticmethod
        def _pointer_transaction_checkpoint(phase: str) -> None:
            if phase == "last_known_good_published":
                raise RuntimeError("injected pointer failure")

    prefix = tmp_path / "distribution"
    first = _bundle(tmp_path / "bundle-one", wheel_pair)
    second = _bundle(
        tmp_path / "bundle-two",
        wheel_pair,
        release_id="cortex-dev-2",
        sequence=2,
    )
    installed = DistributionInstaller(prefix).install(
        first,
        allow_unsigned_developer=True,
    )
    home = tmp_path / "home"
    paths = resolve_installed_product_paths(
        prefix / "versions" / installed.version / "runtime",
        home=home,
        environment={},
    )

    with pytest.raises(RuntimeError, match="injected pointer failure"):
        FailingInstaller(prefix).upgrade(
            second,
            runtime_root=paths.runtime_update_root,
            home=home,
            allow_unsigned_developer=True,
        )

    current = json.loads((prefix / "current.json").read_text())
    assert current["release_id"] == "cortex-dev-1"
    assert json.loads((prefix / "last-known-good.json").read_text()) == current
    assert sorted(path.name for path in (prefix / "versions").iterdir()) == [
        installed.version
    ]
    assert not (prefix / "snapshots").exists()
    assert not (prefix / ".pointer-transaction.json").exists()


@pytest.mark.parametrize(
    ("phase", "returncode"),
    [
        ("prepared", 91),
        ("last_known_good_published", 92),
        ("current_published", 93),
    ],
)
def test_new_process_retries_install_after_every_pointer_checkpoint(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    phase: str,
    returncode: int,
) -> None:
    prefix = tmp_path / "distribution"
    home = tmp_path / "home"
    bundle = _bundle(tmp_path / "bundle", wheel_pair)
    sentinel = _create_external_sentinel(tmp_path)
    sentinel_before = _sentinel_evidence(sentinel)

    crashed = _run_operation_process(
        "install",
        prefix,
        bundle=bundle,
        runtime_root=None,
        home=home,
        crash_phase=phase,
    )
    assert crashed.returncode == returncode, crashed.stderr

    retried = _run_operation_process(
        "install",
        prefix,
        bundle=bundle,
        runtime_root=None,
        home=home,
        crash_phase=None,
    )
    assert retried.returncode == 0, retried.stderr
    result = json.loads(retried.stdout)
    assert result["action"] == (
        "unchanged" if phase == "current_published" else "installed"
    )

    current = json.loads((prefix / "current.json").read_text())
    assert json.loads((prefix / "last-known-good.json").read_text()) == current
    assert sorted(path.name for path in (prefix / "versions").iterdir()) == [
        current["version"]
    ]
    assert all(
        not path.name.startswith(".candidate-")
        for path in (prefix / "versions").iterdir()
    )
    assert not (prefix / ".pointer-transaction.json").exists()
    assert DistributionInstaller(prefix).recover() == "none"
    assert _sentinel_evidence(sentinel) == sentinel_before


@pytest.mark.parametrize(
    ("phase", "returncode"),
    [
        ("prepared", 91),
        ("last_known_good_published", 92),
        ("current_published", 93),
    ],
)
def test_new_process_retries_upgrade_after_every_pointer_checkpoint(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    phase: str,
    returncode: int,
) -> None:
    prefix = tmp_path / "distribution"
    home = tmp_path / "home"
    first = _bundle(tmp_path / "bundle-one", wheel_pair)
    second = _bundle(
        tmp_path / "bundle-two",
        wheel_pair,
        release_id="cortex-dev-2",
        sequence=2,
    )
    installed = DistributionInstaller(prefix).install(
        first,
        allow_unsigned_developer=True,
    )
    paths = resolve_installed_product_paths(
        prefix / "versions" / installed.version / "runtime",
        home=home,
        environment={},
    )
    sentinel = _create_external_sentinel(tmp_path)
    sentinel_before = _sentinel_evidence(sentinel)

    crashed = _run_operation_process(
        "upgrade",
        prefix,
        bundle=second,
        runtime_root=paths.runtime_update_root,
        home=home,
        crash_phase=phase,
    )
    assert crashed.returncode == returncode, crashed.stderr

    retried = _run_operation_process(
        "upgrade",
        prefix,
        bundle=second,
        runtime_root=paths.runtime_update_root,
        home=home,
        crash_phase=None,
    )
    assert retried.returncode == 0, retried.stderr
    result = json.loads(retried.stdout)
    assert result["action"] == (
        "unchanged" if phase == "current_published" else "upgraded"
    )

    current = json.loads((prefix / "current.json").read_text())
    lkg = json.loads((prefix / "last-known-good.json").read_text())
    assert current["release_id"] == "cortex-dev-2"
    assert lkg["release_id"] == "cortex-dev-1"
    assert sorted(path.name for path in (prefix / "versions").iterdir()) == sorted(
        [str(current["version"]), str(lkg["version"])]
    )
    assert len(tuple((prefix / "snapshots").iterdir())) == 1
    assert not (prefix / ".pointer-transaction.json").exists()
    assert DistributionInstaller(prefix).recover() == "none"
    assert _sentinel_evidence(sentinel) == sentinel_before


@pytest.mark.parametrize(
    ("phase", "returncode"),
    [
        ("prepared", 91),
        ("last_known_good_published", 92),
        ("current_published", 93),
    ],
)
def test_new_process_retries_rollback_after_every_pointer_checkpoint(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    phase: str,
    returncode: int,
) -> None:
    installer, prefix, current_before, lkg_before = _installed_pair(
        tmp_path,
        wheel_pair,
    )
    home = tmp_path / "home"
    paths = resolve_installed_product_paths(
        prefix / "versions" / str(current_before["version"]) / "runtime",
        home=home,
        environment={},
    )
    sentinel = _create_external_sentinel(tmp_path)
    sentinel_before = _sentinel_evidence(sentinel)

    crashed = _run_operation_process(
        "rollback",
        prefix,
        bundle=None,
        runtime_root=paths.runtime_update_root,
        home=home,
        crash_phase=phase,
    )
    assert crashed.returncode == returncode, crashed.stderr

    retried = _run_operation_process(
        "rollback",
        prefix,
        bundle=None,
        runtime_root=paths.runtime_update_root,
        home=home,
        crash_phase=None,
    )
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout)["action"] == "rolled-back"

    assert json.loads((prefix / "current.json").read_text()) == lkg_before
    assert json.loads((prefix / "last-known-good.json").read_text()) == current_before
    assert not (prefix / ".pointer-transaction.json").exists()
    assert len(tuple((prefix / "snapshots").iterdir())) == 1
    assert installer.recover() == "none"
    assert _sentinel_evidence(sentinel) == sentinel_before
