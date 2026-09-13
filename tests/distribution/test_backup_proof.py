"""`cortex-dist record-proof`, against the real ControlStore it has to satisfy.

R0-C §2 excluded producing a proof and left the runbook with a prose
placeholder where a command should be. This suite is the other half of that
contract: the proof this producer records must be one the coordinator in the
same module accepts, and the only way to know is to record one with the real
store and then ask the real port.

Nothing here fabricates control state with raw `sqlite3` -- that is the shape of
defect this program has found by running real input eight times over.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.schema import SCHEMA_VERSION
from distribution.state_safety import (
    BackupBackedSafetyPort,
    BackupProofError,
    RESTORE_FRESHNESS_SECONDS,
    UpgradeRequest,
    record_backup_proof,
)


class _Installation:
    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.data.mkdir(mode=0o700)
        self.runtime = tmp_path / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.backups = tmp_path / "backups"
        self.backups.mkdir(mode=0o700)
        self.database = self.data / "control.db"
        self.companion = self.data / ".control.db.transport.key"
        self.store = ControlStore(self.database)
        self.store.initialize()

    def register_root(self, root_id: str, files: dict[str, bytes]) -> object:
        private = self.data / "assets" / root_id
        private.mkdir(parents=True, mode=0o700)
        for name, payload in files.items():
            path = private / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(payload)
        return self.store.register_asset_root(
            root_id=root_id,
            private_path=private,
            max_bytes=1_000_000,
            enabled=True,
            actor_id="operator",
            idempotency_key=f"register-{root_id}".ljust(16, "x"),
        )

    def record(self, **kwargs):
        # The real subprocess boundary, on this interpreter: the production
        # command runs the INSTALLED generation's own `bin/python`, and the only
        # thing that changes here is which interpreter owns `cortex_platform`.
        return record_backup_proof(
            database=self.database,
            companion=self.companion,
            backup_root=self.backups,
            python_executable=Path(sys.executable),
            environment={
                "HOME": str(self.data.parent),
                "PATH": os.defpath,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            **kwargs,
        )

    def request(self) -> UpgradeRequest:
        return UpgradeRequest(
            current_bundle_digest="1" * 64,
            candidate_bundle_digest="2" * 64,
            current_version="cortex-dev-1-1111111111111111",
            candidate_version="cortex-dev-2-2222222222222222",
            candidate_control_schema=SCHEMA_VERSION,
            control_database=self.database,
            identity_companion=self.companion,
            runtime_root=self.runtime,
        )


def test_a_recorded_proof_authorizes_the_upgrade_the_gate_was_refusing(
    tmp_path: Path,
) -> None:
    """The whole point: `verified backup authorization is required`, satisfied.

    Before, the coordinator refuses with the message the runbook quotes. After
    one `record-proof`, the same request over the same state is authorized --
    and the manifest that authorizes it was built by the product's own store,
    not by this test.
    """

    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one", "nested/b.md": b"two"})
    port = BackupBackedSafetyPort()
    with pytest.raises(Exception, match="verified backup authorization is required"):
        port.inspect(installation.request())

    recorded = installation.record()

    binding = port.inspect(installation.request())
    assert binding is not None
    assert binding.proof_id == recorded.proof_id
    assert binding.enabled_roots == (("sources", 0),)
    assert recorded.freshness_seconds == RESTORE_FRESHNESS_SECONDS


def test_the_backup_set_is_paired_restored_and_byte_equal_to_the_source(
    tmp_path: Path,
) -> None:
    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one"})

    recorded = installation.record()

    import hashlib

    root = recorded.backup_root
    # Compared against the digest of the bytes that WERE copied, not against the
    # live file: recording the proof writes into the database the proof
    # describes, so the two necessarily differ by the time this reads them.
    for copy in ("primary", "independent"):
        assert (
            hashlib.sha256((root / copy / "control.db").read_bytes()).hexdigest()
            == recorded.source_digest
        )
    assert (root / "primary" / "assets" / "sources" / "a.md").read_bytes() == b"one"
    assert (root / "independent" / "identity-companion").read_bytes() == (
        installation.companion.read_bytes()
    )
    # A backup nobody restored is a hope: both copies are restored and read
    # back through the same `immutable=1` URI the coordinator uses.
    assert (root / "restore" / "0" / "control.db").is_file()
    assert (root / "restore" / "1" / "control.db").is_file()
    assert recorded.restored_database_count == 2
    assert recorded.verified_sample_count > 0
    # database + companion + one asset root, per copy.
    assert recorded.copied_snapshot_count == 3


def test_the_recorded_source_digest_is_the_live_database(tmp_path: Path) -> None:
    """`source_digest` names the bytes that were copied, not the bytes now.

    Recording the proof writes into the very database the proof describes, so
    the live file necessarily differs afterwards. The printed digest is
    therefore evidence about the backup, and the test says so explicitly rather
    than leaving a reader to assume the two still match.
    """

    import hashlib

    installation = _Installation(tmp_path)
    before = hashlib.sha256(installation.database.read_bytes()).hexdigest()

    recorded = installation.record()

    assert recorded.source_digest == before
    assert (
        hashlib.sha256(installation.database.read_bytes()).hexdigest()
        != recorded.source_digest
    )


def test_recording_leaves_the_state_checkpointed_for_the_upgrade_that_follows(
    tmp_path: Path,
) -> None:
    """The next step in the runbook is `upgrade`, which refuses a live WAL.

    A rollback once left a `-wal` behind and the next backup step correctly
    refused the state as un-checkpointed (R0-C A?). Recording a proof writes to
    the database, so the same trap is one step earlier here.
    """

    installation = _Installation(tmp_path)

    installation.record()

    for suffix in ("-wal", "-journal"):
        sidecar = installation.database.with_name(installation.database.name + suffix)
        assert not sidecar.exists() or sidecar.stat().st_size == 0


def test_an_uncheckpointed_database_is_refused_before_anything_is_copied(
    tmp_path: Path,
) -> None:
    installation = _Installation(tmp_path)
    wal = installation.database.with_name(installation.database.name + "-wal")
    wal.write_bytes(b"x" * 32)

    with pytest.raises(BackupProofError, match="not checkpointed"):
        installation.record()

    assert list(installation.backups.iterdir()) == []


def test_the_newest_proof_is_the_one_that_decides(tmp_path: Path) -> None:
    """Newest-wins, by `restore_completed_at` -- the coordinator's own ordering."""

    installation = _Installation(tmp_path)
    first = installation.record(proof_id="backup-proof-older")
    second = installation.record(proof_id="backup-proof-newer")

    proofs = installation.store.list_paired_backup_proofs()
    assert proofs[0].id == second.proof_id
    assert first.proof_id in {proof.id for proof in proofs}
    binding = BackupBackedSafetyPort().inspect(installation.request())
    assert binding is not None and binding.proof_id == second.proof_id


def test_a_stale_proof_still_refuses(tmp_path: Path) -> None:
    """The producer cannot mint permanence: the gate re-checks freshness."""

    installation = _Installation(tmp_path)
    stale = datetime.now(UTC) - timedelta(days=8)
    installation.record(now=lambda: stale)

    port = BackupBackedSafetyPort()
    with pytest.raises(Exception, match="verified restore is stale"):
        port.inspect(installation.request())


def test_a_root_enabled_after_the_proof_breaks_coverage(tmp_path: Path) -> None:
    """Coverage is about now, so a new root invalidates yesterday's proof."""

    installation = _Installation(tmp_path)
    installation.record()
    installation.register_root("late", {"c.md": b"three"})

    port = BackupBackedSafetyPort()
    with pytest.raises(Exception, match="does not cover the current control state"):
        port.inspect(installation.request())

    installation.record(proof_id="backup-proof-recovered")

    assert port.inspect(installation.request()) is not None


def test_a_second_backup_set_never_overwrites_an_earlier_one(tmp_path: Path) -> None:
    """One backup set per proof, and a re-run of the same proof id refuses."""

    installation = _Installation(tmp_path)
    recorded = installation.record(proof_id="backup-proof-once")
    assert recorded.backup_root.name == "backup-proof-once"

    with pytest.raises(BackupProofError, match="backup set already exists"):
        installation.record(proof_id="backup-proof-once")


def test_the_restored_copy_reads_through_an_immutable_uri(tmp_path: Path) -> None:
    """`immutable=1` creates no companion files, which is why the digest holds."""

    installation = _Installation(tmp_path)
    recorded = installation.record()
    restored = recorded.backup_root / "restore" / "0"

    assert sorted(path.name for path in restored.iterdir()) == ["control.db"]
    connection = sqlite3.connect(f"file:{restored / 'control.db'}?mode=ro&immutable=1", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


class _StubInstaller:
    """Stands in for the distribution pointer, not for the proof recorder.

    The composed generations these suites build carry a synthetic `cortex`
    wheel, so nothing in an installed generation here can import
    `cortex_platform.product.control` -- which is exactly what the recorder
    subprocess needs. The child-process boundary is therefore proved above
    against the real store, and what is proved here is the wiring around it:
    which database, which backup directory, and what the command refuses.
    """

    generation = Path("/nonexistent/generation")

    def __init__(self, prefix) -> None:
        self.prefix = prefix

    @contextlib.contextmanager
    def current_generation_binding(self):
        yield self.generation


def _cli_doubles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, state: str):
    import distribution.cli as cli

    data = tmp_path / "Data"
    data.mkdir(mode=0o700)
    database = data / "control.db"
    ControlStore(database).initialize()
    state_dir = tmp_path / "State"
    state_dir.mkdir(mode=0o700)
    recorded: dict[str, object] = {}

    class StubLifecycle:
        def __init__(
            self, generation, runtime_root, *, home, environment=None, pin_tools=True
        ):
            recorded["generation"] = generation
            recorded["runtime_root"] = runtime_root
            recorded["home"] = home
            recorded["pin_tools"] = pin_tools
            self.product_paths = SimpleNamespace(
                control_database_file=database, state_dir=state_dir
            )
            self.supervisor_python = Path(sys.executable)
            self.generation = SimpleNamespace(
                control_environment=lambda *, home: {
                    "HOME": str(home),
                    "PATH": os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )

        def status(self):
            return SimpleNamespace(state=state)

    monkeypatch.setattr(cli, "DistributionInstaller", _StubInstaller)
    monkeypatch.setattr(cli, "LifecycleManager", StubLifecycle)
    return cli, database, state_dir, recorded


def test_record_proof_refuses_while_the_lifecycle_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A copy taken from a live database describes a state that never existed."""

    cli, _database, state_dir, _recorded = _cli_doubles(
        tmp_path, monkeypatch, state="running"
    )

    result = cli.main(
        ["record-proof", "--runtime-root", str(tmp_path / "runtime"), "--home", str(tmp_path)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["ok"] is False
    assert payload["error"] == "operation requires the lifecycle to be stopped"
    assert payload["category"] == "record_proof_requires_stop"
    assert not (state_dir / "backups").exists()


def test_record_proof_prints_the_proof_id_and_source_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The two values an operator has to carry into the next runbook step."""

    import hashlib

    cli, database, state_dir, recorded = _cli_doubles(
        tmp_path, monkeypatch, state="stopped"
    )
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    result = cli.main(
        [
            "record-proof",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--home",
            str(tmp_path),
            "--proof-id",
            "backup-proof-cli",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["ok"] is True
    assert payload["result"]["proof_id"] == "backup-proof-cli"
    assert payload["result"]["source_digest"] == before
    assert payload["result"]["authorizes_for_seconds"] == RESTORE_FRESHNESS_SECONDS
    # Defaulted, not invented: the backup set lands under the product's own
    # state root, named by the proof it belongs to.
    assert payload["result"]["backup_root"] == str(
        state_dir / "backups" / "backup-proof-cli"
    )
    assert recorded["generation"] == _StubInstaller.generation
    # DIST-1: the generation this measures is the PREVIOUS one, so its
    # `tools/**` cannot be pinned to this verifier's copies.
    assert recorded["pin_tools"] is False
    assert ControlStore(database).list_paired_backup_proofs()[0].id == "backup-proof-cli"


def test_record_proof_binds_a_generation_whose_tools_precede_the_verifier(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """DIST-1: the command exists ONLY for the cross-generation moment.

    `record-proof` is run by the release being installed against the
    generation already on disk, so its `LifecycleManager` necessarily measures
    a previous generation's `tools/**`. With the bundled-tools pin left on, the
    release that introduces the command can never succeed against any prior
    generation -- and the prior generation has no `record-proof` of its own to
    fall back to, so the stateful upgrade cannot be performed at all.

    The twelve tests above monkeypatch `cli.LifecycleManager` and
    `cli.DistributionInstaller` wholesale, so none of them executes this chain.
    This one drives the real binding against a real installed generation whose
    tools this verifier rejects, and asserts the refusal it used to produce is
    gone -- the command now reaches the generation's own interpreter, which is
    where a composed test generation (synthetic `cortex` wheel) legitimately
    stops.
    """

    from test_installer import _installed_previous_generation, _upgrade_context
    import distribution.cli as cli
    from distribution.lifecycle import LifecycleManager
    from distribution.product_paths import allowed_product_path_environment

    state = _installed_previous_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, monkeypatch
    )
    _paths, runtime_root, home = _upgrade_context(tmp_path)
    runtime_root.mkdir(parents=True, mode=0o700)

    # The pin fires at LifecycleManager construction, before anything is
    # copied: with it on, this is the whole of what the operator sees.
    with pytest.raises(Exception, match="do not match the verifier"):
        LifecycleManager(
            state.version_dir,
            runtime_root,
            home=home,
            environment=allowed_product_path_environment(os.environ),
        )
    lifecycle = LifecycleManager(
        state.version_dir,
        runtime_root,
        home=home,
        environment=allowed_product_path_environment(os.environ),
        pin_tools=False,
    )
    database = lifecycle.product_paths.control_database_file
    ControlStore(database).initialize()

    result = cli.main(
        [
            "record-proof",
            "--prefix",
            str(state.prefix),
            "--runtime-root",
            str(runtime_root),
            "--home",
            str(home),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert "do not match the verifier" not in payload["error"]
    # Past the binding and into the generation's own interpreter: the recorder
    # is the first thing a composed test generation cannot satisfy.
    assert payload["error"].startswith("control store inspect failed")


def test_the_proof_id_class_is_the_control_store_s_own(tmp_path: Path) -> None:
    """DIST-5: the parent validates against the class the store will apply.

    `distribution/` is stdlib-only and cannot import `cortex_platform`, so the
    character class has to be written down twice. This is the only place the
    two can be compared, and a drift here means the parent copies four
    directories the store then refuses.
    """

    from cortex_platform.product.control.store import _REGISTRY_RESOURCE_ID_RE
    from distribution.state_safety import _PROOF_ID_RE

    assert _PROOF_ID_RE.pattern == _REGISTRY_RESOURCE_ID_RE.pattern


@pytest.mark.parametrize(
    "identifier",
    ["../escape", "nested/child", "/absolute", "Upper-Case", "", ".", "..", "a" * 101],
)
def test_a_proof_id_outside_the_backup_root_is_refused_before_any_copy(
    tmp_path: Path, identifier: str
) -> None:
    """DIST-5: the id is operator-supplied and reaches the parent first.

    `_write_backup_set` copies control.db, the identity companion and every
    enabled asset root twice over before `_run_recorder` hands the id to the
    store. A value carrying `/` or `..` therefore wrote a full copy of control
    state outside `backup_root` and left it there -- no try/finally, and no
    command that prunes it.
    """

    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one"})

    with pytest.raises(BackupProofError, match="backup proof id is invalid"):
        installation.record(proof_id=identifier)

    assert list(installation.backups.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_a_failure_midway_removes_the_partial_backup_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written backup set is control state nobody accounted for."""

    from distribution import state_safety

    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one", "nested/b.md": b"two"})

    def fail(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise BackupProofError("injected restore failure")

    monkeypatch.setattr(state_safety, "_verify_restore", fail)

    with pytest.raises(BackupProofError, match="injected restore failure"):
        installation.record(proof_id="backup-proof-partial")

    assert list(installation.backups.iterdir()) == []


def _recorded_manifest(installation: "_Installation") -> dict:
    conn = sqlite3.connect(installation.database)
    try:
        raw = conn.execute(
            "SELECT protected_set_manifest_json FROM paired_backup_proofs "
            "ORDER BY restore_completed_at DESC, id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw)


def _rederive(directory: Path) -> dict:
    """The same content manifest, re-derived here from a directory on disk."""

    import hashlib

    entries = []
    total = 0
    for item in sorted(directory.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        size = item.stat().st_size
        total += size
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        entries.append(f"{digest} {size} {item.relative_to(directory).as_posix()}")
    payload = "\n".join(entries).encode("utf-8")
    return {
        "content_manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "file_count": len(entries),
        "byte_length": total,
    }


def test_an_asset_root_that_is_a_symlink_is_refused_with_nothing_recorded(
    tmp_path: Path,
) -> None:
    """DIV-1: a root that is not a directory is a refusal, not a silent skip.

    `_write_backup_set` skipped anything that was not a real directory and the
    manifest was measured from the same path, so the proof recorded
    `file_count: 0` for a root whose bytes it never held -- and the coverage
    check compares only `(root_id, revision)`, so the upgrade gate authorized
    it. A symlinked root is a state the store cannot have produced by copying.
    """

    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one"})
    private = installation.data / "assets" / "sources"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.md").write_bytes(b"one")
    shutil.rmtree(private)
    private.symlink_to(elsewhere)

    with pytest.raises(BackupProofError, match="is not a directory this proof"):
        installation.record()

    assert installation.store.list_paired_backup_proofs() == ()
    assert list(installation.backups.iterdir()) == []


def test_an_asset_root_whose_directory_is_a_file_is_refused(tmp_path: Path) -> None:
    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one"})
    private = installation.data / "assets" / "sources"
    shutil.rmtree(private)
    private.write_bytes(b"not a directory")

    with pytest.raises(BackupProofError, match="is not a directory this proof"):
        installation.record()


def test_an_absent_asset_root_is_recorded_as_unreadable_not_as_empty(
    tmp_path: Path,
) -> None:
    """An enabled root whose directory does not exist is a LEGAL registration.

    `register_asset_root` performs no existence check, so refusing here would
    make `record-proof` -- and therefore the whole R0-C gate -- unreachable on
    an installation that has done nothing wrong. It is recorded, but it is not
    recorded as an empty directory: the two must not produce the same manifest.
    """

    installation = _Installation(tmp_path)
    installation.register_root("present", {"a.md": b"one"})
    installation.register_root("absent", {})
    shutil.rmtree(installation.data / "assets" / "absent")
    installation.register_root("empty", {})

    recorded = installation.record()

    manifest = _recorded_manifest(installation)
    roots = {entry["root_id"]: entry for entry in manifest["asset_roots"]}
    assert roots["absent"]["file_count"] == 0
    assert roots["empty"]["file_count"] == 0
    # The distinguishing bit: an absent root is not the empty-directory digest.
    assert (
        roots["absent"]["content_manifest_sha256"]
        != roots["empty"]["content_manifest_sha256"]
    )
    # control.db + companion + the two roots that were actually copied.
    assert recorded.copied_snapshot_count == 4


def test_the_recorded_manifest_describes_the_backup_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is a claim about the backup, by construction.

    It was measured from the LIVE directory, so a root that changed between the
    copy and the measurement produced a manifest describing bytes the backup
    does not hold. The corpus this protects is written to by the research agent
    and the copy is not instantaneous, so this is the ordinary case, not a
    contrived one -- the write below lands where `_verify_restore` runs, which
    is exactly the gap between the two.
    """

    from distribution import state_safety

    installation = _Installation(tmp_path)
    installation.register_root("sources", {"a.md": b"one", "nested/b.md": b"two"})
    live = installation.data / "assets" / "sources"
    verify = state_safety._verify_restore

    def write_between(*args: object, **kwargs: object) -> tuple[int, int]:
        (live / "arrived-late.md").write_bytes(b"three")
        return verify(*args, **kwargs)

    monkeypatch.setattr(state_safety, "_verify_restore", write_between)

    recorded = installation.record(proof_id="backup-proof-manifest")

    manifest = _recorded_manifest(installation)
    entry = next(
        item for item in manifest["asset_roots"] if item["root_id"] == "sources"
    )
    copy = recorded.backup_root / "primary" / "assets" / "sources"
    rederived = _rederive(copy)
    assert entry["content_manifest_sha256"] == rederived["content_manifest_sha256"]
    assert entry["file_count"] == rederived["file_count"] == 2
    assert entry["byte_length"] == rederived["byte_length"]


# ---------------------------------------------------------------------------
# ⟦P5.6⟧ Pruning. Every R0-C proof writes ~12 GB on the mini and nothing
# ever removed a superseded set; five in one day filled the disk and the
# sixth proof failed with ENOSPC while the product was stopped.
# ---------------------------------------------------------------------------


def _fake_set(root: Path, name: str, *, age: int) -> Path:
    """A directory carrying the backup-set marker `record_backup_proof`
    leaves (both copies, the primary's `control.db`), `age` seconds old."""

    path = root / name
    for copy in ("primary", "independent"):
        (path / copy).mkdir(parents=True, mode=0o700)
    (path / "primary" / "control.db").write_bytes(b"x")
    stamp = 1_800_000_000 - age
    os.utime(path, (stamp, stamp))
    return path


def _aged(path: Path, *, age: int) -> Path:
    stamp = 1_800_000_000 - age
    os.utime(path, (stamp, stamp))
    return path


def test_prune_keeps_the_newest_n_sets_and_deletes_the_rest(tmp_path: Path) -> None:
    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    oldest = _fake_set(root, "backup-proof-20260903t005511z", age=300)
    middle = _fake_set(root, "backup-proof-20260903t090911z", age=200)
    newest = _fake_set(root, "backup-proof-20260903t220610z", age=100)

    report = prune_backup_sets(root, keep=2, protect=newest)

    assert report.pruned == (oldest,)
    assert set(report.kept) == {newest, middle}
    assert report.failed == ()
    assert not oldest.exists() and middle.exists() and newest.exists()


def test_prune_never_removes_the_set_just_written_even_at_keep_one(
    tmp_path: Path,
) -> None:
    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    older = _fake_set(root, "backup-proof-20260903t005511z", age=300)
    # The protected set carries an OLDER mtime than another set (a restored
    # copy, a clock that went backwards): it is still never pruned.
    just_written = _fake_set(root, "backup-proof-20260903t220610z", age=500)

    report = prune_backup_sets(root, keep=1, protect=just_written)

    assert just_written.exists()
    assert just_written in report.kept
    assert report.pruned == (older,)


def test_prune_touches_only_proof_shaped_directories_and_never_a_symlink(
    tmp_path: Path,
) -> None:
    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    newest = _fake_set(root, "backup-proof-20260903t220610z", age=10)
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    (victim / "keep-me").write_bytes(b"precious")
    # A symlink named like a set, pointing outside the root.
    (root / "backup-proof-20260901t000000z").symlink_to(victim)
    # A directory whose name is not a proof id, a stray file, and a symlink
    # INSIDE an old set that points outside the root.
    (root / "Not-A-Proof").mkdir()
    (root / "notes.txt").write_text("operator notes")
    old = _fake_set(root, "backup-proof-20260902t000000z", age=900)
    (old / "link-out").symlink_to(victim)

    report = prune_backup_sets(root, keep=1, protect=newest)

    assert report.pruned == (old,)
    assert not old.exists()
    assert (victim / "keep-me").read_bytes() == b"precious"
    assert (root / "backup-proof-20260901t000000z").is_symlink()
    assert (root / "Not-A-Proof").is_dir()
    assert (root / "notes.txt").is_file()
    assert newest.exists()


def test_prune_leaves_a_lowercase_directory_that_is_not_a_backup_set(
    tmp_path: Path,
) -> None:
    """⟦Batch F P56-OBS-1⟧ `--proof-id` is operator-supplied, so any lowercase
    id is a legal set name and the name filter alone admits an archive the
    operator parked beside the sets. The marker is what `record_backup_proof`
    writes, and only a directory carrying it is ever deleted."""

    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    newest = _fake_set(root, "backup-proof-20260903t220610z", age=10)
    old = _fake_set(root, "backup-proof-20260902t000000z", age=900)
    archive = root / "corpus-archive"
    archive.mkdir()
    (archive / "papers.tar").write_bytes(b"irreplaceable")
    _aged(archive, age=5000)
    # Name-shaped and even partly set-shaped, but not the marker: one copy
    # missing, the database a symlink, the copy a symlink.
    half = root / "backup-proof-half"
    (half / "primary").mkdir(parents=True)
    (half / "primary" / "control.db").write_bytes(b"x")
    _aged(half, age=6000)
    linked = root / "backup-proof-linked-db"
    for copy in ("primary", "independent"):
        (linked / copy).mkdir(parents=True)
    (linked / "primary" / "control.db").symlink_to(archive / "papers.tar")
    _aged(linked, age=7000)
    linked_copy = root / "backup-proof-linked-copy"
    (linked_copy / "primary").mkdir(parents=True)
    (linked_copy / "primary" / "control.db").write_bytes(b"x")
    (linked_copy / "independent").symlink_to(archive)
    _aged(linked_copy, age=8000)

    report = prune_backup_sets(root, keep=1, protect=newest)

    assert report.pruned == (old,)
    assert set(report.kept) == {newest}
    assert (archive / "papers.tar").read_bytes() == b"irreplaceable"
    assert half.is_dir() and linked.is_dir() and linked_copy.is_dir()
    assert (linked / "primary" / "control.db").is_symlink()
    assert (linked_copy / "independent").is_symlink()


def test_a_directory_without_the_marker_cannot_take_a_keep_slot(
    tmp_path: Path,
) -> None:
    """The non-set directory is NEWER than a genuine superseded set: it must
    not be counted toward `keep`, or the real set would be pruned in its
    place while the stranger stays."""

    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    just_written = _fake_set(root, "backup-proof-20260903t220610z", age=10)
    superseded = _fake_set(root, "backup-proof-20260903t005511z", age=900)
    stranger = root / "corpus-archive"
    stranger.mkdir()
    (stranger / "note").write_text("newer than the superseded set")
    _aged(stranger, age=100)

    report = prune_backup_sets(root, keep=2, protect=just_written)

    assert report.pruned == ()
    assert set(report.kept) == {just_written, superseded}
    assert superseded.is_dir() and stranger.is_dir()


def test_prune_orders_operator_named_sets_by_mtime_not_by_name(
    tmp_path: Path,
) -> None:
    """⟦P56-06⟧ Operator `--proof-id` names carry no stamp; their lexical
    order here is the REVERSE of their age, and mtime must win."""

    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    # Lexically "zulu" > "mike" > "alpha", but alpha is the newest by mtime.
    alpha = _fake_set(root, "alpha-before-the-mini-move", age=100)
    mike = _fake_set(root, "mike-after-restore", age=500)
    zulu = _fake_set(root, "zulu-first-ever", age=900)
    just_written = _fake_set(root, "backup-proof-p56-first", age=10)

    report = prune_backup_sets(root, keep=2, protect=just_written)

    assert set(report.kept) == {just_written, alpha}
    assert set(report.pruned) == {mike, zulu}
    assert alpha.is_dir() and not mike.exists() and not zulu.exists()


def test_prune_refuses_a_keep_below_one(tmp_path: Path) -> None:
    from distribution.state_safety import prune_backup_sets

    root = tmp_path / "backups"
    root.mkdir()
    with pytest.raises(BackupProofError, match="keep"):
        prune_backup_sets(root, keep=0)


def test_record_proof_prunes_superseded_sets_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Two real proofs, `--keep 1`: exactly the older set is gone, and the
    payload names it. The set just written survives by construction."""

    cli, database, state_dir, _recorded = _cli_doubles(
        tmp_path, monkeypatch, state="stopped"
    )
    common = ["record-proof", "--runtime-root", str(tmp_path / "runtime"), "--home", str(tmp_path)]
    assert cli.main([*common, "--proof-id", "backup-proof-first"]) == 0
    first = json.loads(capsys.readouterr().out)["result"]
    assert first["pruned_backup_sets"] == []
    first_set = state_dir / "backups" / "backup-proof-first"
    assert first_set.is_dir()

    assert cli.main([*common, "--proof-id", "backup-proof-second", "--keep", "1"]) == 0
    second = json.loads(capsys.readouterr().out)["result"]
    second_set = state_dir / "backups" / "backup-proof-second"
    assert second["pruned_backup_sets"] == [str(first_set)]
    assert second["kept_backup_sets"] == [str(second_set)]
    assert second["prune_failures"] == []
    assert not first_set.exists() and second_set.is_dir()
    # The proof rows are untouched: pruning is about bytes on disk, not
    # about what the store recorded.
    assert [proof.id for proof in ControlStore(database).list_paired_backup_proofs()] == [
        "backup-proof-second",
        "backup-proof-first",
    ]


def test_keep_below_one_is_a_usage_error_before_anything_is_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """⟦Batch F P56-02⟧ The pruner refuses `keep < 1` -- after the ~12 GB copy.
    The parser refuses it first."""

    cli, _database, state_dir, _recorded = _cli_doubles(
        tmp_path, monkeypatch, state="stopped"
    )
    common = ["record-proof", "--runtime-root", str(tmp_path / "runtime"), "--home", str(tmp_path)]
    for bad in ("0", "-1", "two"):
        with pytest.raises(SystemExit) as refused:
            cli.main([*common, "--keep", bad])
        assert refused.value.code == 2
        assert "--keep" in capsys.readouterr().err
    assert not (state_dir / "backups").exists()


def test_a_prune_refusal_never_discards_the_receipt_of_a_recorded_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The proof is recorded and the row exists; a pruner that cannot run
    (here: a refusal) rides along on the receipt instead of replacing it."""

    from distribution import cli as cli_module

    cli, database, state_dir, _recorded = _cli_doubles(
        tmp_path, monkeypatch, state="stopped"
    )

    def refusing(*args: object, **kwargs: object):
        raise BackupProofError("backup root is not a directory")

    monkeypatch.setattr(cli_module, "prune_backup_sets", refusing)
    common = ["record-proof", "--runtime-root", str(tmp_path / "runtime"), "--home", str(tmp_path)]
    assert cli.main([*common, "--proof-id", "backup-proof-kept"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True
    result = receipt["result"]
    assert result["proof_id"] == "backup-proof-kept"
    assert result["backup_root"] == str(state_dir / "backups" / "backup-proof-kept")
    assert result["prune_error"] == {
        "error": "BackupProofError",
        "detail": "backup root is not a directory",
    }
    assert result["kept_backup_sets"] == [] and result["pruned_backup_sets"] == []
    assert (state_dir / "backups" / "backup-proof-kept").is_dir()
    assert [proof.id for proof in ControlStore(database).list_paired_backup_proofs()] == [
        "backup-proof-kept"
    ]


def test_record_proof_default_keep_is_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from distribution.state_safety import DEFAULT_KEEP_BACKUP_SETS

    assert DEFAULT_KEEP_BACKUP_SETS == 2
    cli, _database, state_dir, _recorded = _cli_doubles(
        tmp_path, monkeypatch, state="stopped"
    )
    common = ["record-proof", "--runtime-root", str(tmp_path / "runtime"), "--home", str(tmp_path)]
    for name in ("backup-proof-a", "backup-proof-b", "backup-proof-c"):
        assert cli.main([*common, "--proof-id", name]) == 0
        capsys.readouterr()
    remaining = sorted(path.name for path in (state_dir / "backups").iterdir())
    assert remaining == ["backup-proof-b", "backup-proof-c"]
