"""`stage` expands the interpreter the release carries (S3.2, D-S3.2-2).

The archive rides inside `content/`, so the slot's own content-tree digest binds
it. What this module pins is everything that happens after that: the digest-keyed
root beside the slots, the three-way reuse rule ⟦AMD-4⟧, the containment
assertions ⟦AMD-5⟧, the pin `_verify_candidate` re-reads at every door, and the
refcount pruning.

Almost every test injects a fake staging kernel, because the state machine is
independent of what CPython an archive holds and a 38 MB unpack per case would
make it untestable. The last test injects nothing: it stages the real vendored
cp311 archive through the real wheel-shipped kernel and requires the probe to
have run on `bin/python3.11`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import canonical_json
from cortex_platform.product.runtime_update.service import (
    ActivationError,
    DigestPinVerifier,
    RuntimeUpdateService,
    VerificationError,
    _contained,
    _verify_worker_modules,
)
from cortex_platform.product.runtime_update.worker_payload import (
    WORKER_PACKAGE,
    module_sources,
)

from .fake_python_stager import FakePythonStager


def _service(
    root: Path,
    catalog: dict,
    attestation: dict,
    stager: FakePythonStager | None = None,
) -> RuntimeUpdateService:
    return RuntimeUpdateService(
        root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=stager,
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )


def _imported(
    tmp_path: Path,
    release_factory,
    stager: FakePythonStager | None = None,
    **release: object,
):
    """One imported release and the service that imported it."""

    artifact, manifest, catalog, attestation = release_factory(**release)
    service = _service(tmp_path / "updates", catalog, attestation, stager)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    return service, manifest


def _digest(manifest: dict) -> str:
    return manifest["worker_runtime"]["archive_sha256"]


# --- expansion -------------------------------------------------------------


def test_stage_expands_the_carried_archive_into_a_digest_named_root(
    tmp_path: Path, release_factory
) -> None:
    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)

    candidate = service.stage(manifest["release_id"])

    root = service.paths.interpreters / _digest(manifest)
    assert root.is_dir()
    assert (root / "bin" / "python3.11").is_file()
    # Beside the slots, not inside one: the slot is import-immutable and exec
    # bits cannot ride a zip.
    assert root.parent == service.paths.interpreters
    assert not (candidate.slot_dir / "interpreters").exists()
    [call] = stager.expansions
    assert call["archive"] == candidate.slot_dir / "content" / (
        manifest["worker_runtime"]["archive"]
    )
    assert call["stage_root"] == service.paths.interpreters
    assert call["profile"] == "cp311"
    assert call["expected"]["sha256"] == _digest(manifest)
    assert call["expected"]["version"] == "3.11.15"
    assert call["expected"]["abi_tag"] == "cp311"


def test_the_pin_records_the_stage_measured_interpreter_digest(
    tmp_path: Path, release_factory
) -> None:
    """⟦AMD-3⟧'s second identity: the archive digest names the root, the
    interpreter digest is what a descriptor will later be bound to."""

    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)

    service.stage(manifest["release_id"])

    pin = json.loads(
        (service.paths.interpreters / f"{_digest(manifest)}.pin.json").read_text()
    )
    assert pin == {
        "archive_sha256": _digest(manifest),
        "interpreter_sha256": hashlib.sha256(stager.interpreter_body).hexdigest(),
    }


def test_the_slot_metadata_stays_a_closed_four_key_set(
    tmp_path: Path, release_factory
) -> None:
    """The interpreter changed nothing about slot identity. `slot.json` is
    compared for exact equality at stage, activate and rollback simultaneously,
    so a fifth key would break all three at once."""

    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())

    candidate = service.stage(manifest["release_id"])

    assert set(json.loads((candidate.slot_dir / "slot.json").read_text())) == {
        "schema_version",
        "artifact_sha256",
        "manifest_sha256",
        "content_tree_sha256",
    }


def test_a_release_naming_an_unknown_python_is_refused(
    tmp_path: Path, release_factory
) -> None:
    """A typed refusal, never a fallback to the product's own profile: staging a
    3.9 tree under the 3.14 profile would check paths neither tree carries."""

    service, manifest = _imported(
        tmp_path, release_factory, FakePythonStager(), python_version="3.9.19"
    )

    with pytest.raises(ActivationError, match="unsupported Python runtime profile"):
        service.stage(manifest["release_id"])


def test_a_failed_expansion_leaves_no_state_generation_behind(
    tmp_path: Path, release_factory
) -> None:
    stager = FakePythonStager(fail_expansion=True)
    service, manifest = _imported(tmp_path, release_factory, stager)

    with pytest.raises(ActivationError, match="could not be staged"):
        service.stage(manifest["release_id"])

    assert list((service.paths.generations / manifest["release_id"]).iterdir()) == []


# --- ⟦AMD-4⟧ three-way reuse ----------------------------------------------


def test_an_already_staged_interpreter_is_reverified_and_reused(
    tmp_path: Path, release_factory
) -> None:
    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)

    service.stage(manifest["release_id"])
    service.stage(manifest["release_id"])

    # Expanded once; the second stage finds the published root and re-runs the
    # cheap seal and content assertions instead — corruption detection, not
    # tamper-proofing.
    assert len(stager.expansions) == 1
    assert stager.verifications == [service.paths.interpreters / _digest(manifest)]


def test_two_releases_carrying_one_interpreter_share_its_root(
    tmp_path: Path, release_factory
) -> None:
    stager = FakePythonStager()
    first_service, first = _imported(tmp_path, release_factory, stager)
    second_service, second = _imported(
        tmp_path,
        release_factory,
        stager,
        release_id="hermes-0.18.3",
        sequence=183,
    )

    first_service.stage(first["release_id"])
    second_service.stage(second["release_id"])

    assert _digest(first) == _digest(second)
    assert len(stager.expansions) == 1
    assert sorted(
        item.name for item in service_interpreters(first_service)
    ) == [_digest(first), f"{_digest(first)}.pin.json"]


def service_interpreters(service: RuntimeUpdateService) -> list[Path]:
    return sorted(service.paths.interpreters.iterdir())


def test_a_corrupted_interpreter_root_refuses_rather_than_being_reused(
    tmp_path: Path, release_factory
) -> None:
    """Present and unusable is a refusal, not a silent re-stage: the tree was
    published once and something removed part of it afterwards."""

    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)
    service.stage(manifest["release_id"])
    root = service.paths.interpreters / _digest(manifest)
    os.chmod(root, 0o700)
    interpreter = root / "bin" / "python3.11"
    os.chmod(interpreter.parent, 0o700)
    interpreter.unlink()

    with pytest.raises(ActivationError, match="staged interpreter verification failed"):
        service.stage(manifest["release_id"])


def test_a_pin_naming_another_archive_is_refused(
    tmp_path: Path, release_factory
) -> None:
    """Two different interpreters claiming one digest is not a state this
    service resolves for itself."""

    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    service.stage(manifest["release_id"])
    pin = service.paths.interpreters / f"{_digest(manifest)}.pin.json"
    pin.write_text(
        json.dumps({"archive_sha256": "f" * 64, "interpreter_sha256": "e" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(ActivationError, match="does not match the manifest"):
        service.stage(manifest["release_id"])


def test_a_crash_orphaned_root_is_restaged_rather_than_poisoned(
    tmp_path: Path, release_factory
) -> None:
    """The R0-C sealed-leftover class, closed by construction.

    R0-C's acceptance found that a refused upgrade left a sealed interpreter
    behind and thereby blocked that upgrade from ever succeeding. A tree whose
    pin never landed is exactly that residue, so it is removed and re-staged
    rather than treated as a permanent refusal.
    """

    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)
    service.stage(manifest["release_id"])
    (service.paths.interpreters / f"{_digest(manifest)}.pin.json").unlink()

    service.stage(manifest["release_id"])

    assert len(stager.expansions) == 2
    assert (service.paths.interpreters / f"{_digest(manifest)}.pin.json").is_file()


def test_a_pin_without_its_tree_is_restaged(tmp_path: Path, release_factory) -> None:
    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)
    service.stage(manifest["release_id"])
    root = service.paths.interpreters / _digest(manifest)
    os.chmod(root, 0o700)
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path.parent, 0o700)
        path.unlink() if path.is_file() else path.rmdir()
    root.rmdir()

    service.stage(manifest["release_id"])

    assert len(stager.expansions) == 2
    assert (root / "bin" / "python3.11").is_file()


def test_an_unreadable_pin_document_is_refused(
    tmp_path: Path, release_factory
) -> None:
    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    service.stage(manifest["release_id"])
    (service.paths.interpreters / f"{_digest(manifest)}.pin.json").write_text(
        json.dumps({"archive_sha256": _digest(manifest)}), encoding="utf-8"
    )

    with pytest.raises(ActivationError, match="interpreter pin schema is invalid"):
        service.stage(manifest["release_id"])


# --- ⟦AMD-5⟧ containment ---------------------------------------------------


def test_tampering_with_the_carried_archive_is_caught_before_containment(
    tmp_path: Path, release_factory
) -> None:
    """The layers, in the order they actually fire.

    The archive is an ordinary file inside `content/`, so replacing it — with a
    symlink, with other bytes, or with nothing — changes the content-tree digest
    and `_verify_candidate` refuses before `stage` ever resolves a path. That is
    the point of D-S3.2-2: slot identity covers the interpreter for free. The
    containment assertion below is the second line, and by construction it is not
    reachable through a slot this service imported.
    """

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "runtime.tar.gz").write_bytes(b"elsewhere\n")
    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    slot = service.paths.slots / manifest["artifact_sha256"]
    content = slot / "content"
    for directory in (slot, content, content / "runtime"):
        os.chmod(directory, 0o700)
    archive = content / manifest["worker_runtime"]["archive"]
    archive.unlink()
    archive.symlink_to(outside / "runtime.tar.gz")

    with pytest.raises(ActivationError, match="slot content verification failed"):
        service.stage(manifest["release_id"])


@pytest.mark.parametrize(
    "relative",
    ["../escape", "runtime/../../escape", "..", "runtime/../..", "."],
)
def test_the_containment_assertion_refuses_every_escape(
    tmp_path: Path, relative: str
) -> None:
    """Asserted directly, because nothing can reach it through the public path.

    ⟦AMD-5⟧ asks for the check regardless: the manifest grammar decides about a
    string, and this decides about the filesystem. Testing it through `stage`
    would only re-prove that the content digest fires first, so the unit is
    exercised where it lives — including the case the grammar cannot see, a root
    that resolves to itself.
    """

    root = tmp_path / "content"
    (root / "runtime").mkdir(parents=True)

    with pytest.raises(ActivationError, match="resolves outside its root"):
        _contained(root, relative, "release runtime archive")


def test_the_containment_assertion_accepts_a_nested_path(tmp_path: Path) -> None:
    root = tmp_path / "content"
    (root / "runtime").mkdir(parents=True)

    assert _contained(root, "runtime/python.tar.gz", "release runtime archive") == (
        root / "runtime" / "python.tar.gz"
    )


# --- the pin every door re-reads ------------------------------------------


def test_activation_refuses_a_candidate_whose_interpreter_is_gone(
    tmp_path: Path, release_factory
) -> None:
    """`_verify_candidate` asks the question at stage, activate and rollback,
    because a runtime-update root restored onto other hardware carries slots
    verified somewhere else."""

    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    service.stage(manifest["release_id"])
    (service.paths.interpreters / f"{_digest(manifest)}.pin.json").unlink()

    with pytest.raises(ActivationError, match="candidate interpreter is not staged"):
        service.activate(manifest["release_id"], probe=lambda _candidate: True)


def test_activation_refuses_a_pin_that_names_another_archive(
    tmp_path: Path, release_factory
) -> None:
    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    service.stage(manifest["release_id"])
    (service.paths.interpreters / f"{_digest(manifest)}.pin.json").write_text(
        json.dumps({"archive_sha256": "f" * 64, "interpreter_sha256": "e" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(ActivationError, match="pin does not match the manifest"):
        service.activate(manifest["release_id"], probe=lambda _candidate: True)


# --- pruning ---------------------------------------------------------------


def test_prune_removes_an_interpreter_no_surviving_release_names(
    tmp_path: Path, release_factory
) -> None:
    stager = FakePythonStager()
    keeper_service, keeper = _imported(tmp_path, release_factory, stager)
    doomed_service, doomed = _imported(
        tmp_path,
        release_factory,
        stager,
        release_id="hermes-0.18.3",
        sequence=183,
        runtime_archive=b"a second synthetic cp311 runtime\n",
    )
    keeper_service.stage(keeper["release_id"])
    doomed_service.stage(doomed["release_id"])
    assert _digest(keeper) != _digest(doomed)

    removed = keeper_service.prune(retain_candidates=0)

    assert removed == (keeper["release_id"], doomed["release_id"])
    assert service_interpreters(keeper_service) == []


def test_prune_keeps_the_interpreter_a_retained_release_still_names(
    tmp_path: Path, release_factory
) -> None:
    stager = FakePythonStager()
    service, manifest = _imported(tmp_path, release_factory, stager)
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda _candidate: True)

    assert service.prune(retain_candidates=0) == ()
    assert sorted(item.name for item in service_interpreters(service)) == [
        _digest(manifest),
        f"{_digest(manifest)}.pin.json",
    ]


def test_prune_leaves_an_unrecognized_entry_alone(
    tmp_path: Path, release_factory
) -> None:
    """Refcount by scan deletes only what it can name. An entry that is not a
    digest is not this service's garbage to collect."""

    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda _candidate: True)
    stray = service.paths.interpreters / "not-a-digest"
    stray.mkdir()

    service.prune(retain_candidates=0)

    assert stray.is_dir()


# --- the real thing --------------------------------------------------------


def test_the_real_vendored_cp311_archive_stages_and_probes(
    tmp_path: Path, release_factory, vendored_worker_runtime
) -> None:
    """No fake anywhere: the wheel-shipped kernel expands the vendored archive.

    Everything the fake drops is proven here — the fd-bound unpack, the prefix
    substitution, the 0o500/0o400 seal, the linkage check, and the probe, which
    executes `bin/python3.11` out of the staged tree and measures the bytes it
    ran from. `interpreter_sha256` in the pin is that measurement, so comparing
    it to the file on disk is what proves the probe ran on the right file.
    """

    archive, pin_document = vendored_worker_runtime
    service, manifest = _imported(
        tmp_path, release_factory, None, runtime_archive=archive.read_bytes()
    )

    service.stage(manifest["release_id"])

    root = service.paths.interpreters / _digest(manifest)
    interpreter = root / "bin" / "python3.11"
    assert interpreter.is_file()
    assert stat.S_IMODE(root.lstat().st_mode) == 0o500
    assert stat.S_IMODE(interpreter.lstat().st_mode) == 0o500
    assert stat.S_IMODE((root / "lib" / "python3.11" / "os.py").lstat().st_mode) == 0o400
    pin = json.loads((service.paths.interpreters / f"{_digest(manifest)}.pin.json").read_text())
    assert pin["archive_sha256"] == _digest(manifest)
    assert pin["interpreter_sha256"] == hashlib.sha256(interpreter.read_bytes()).hexdigest()
    # The vendored pin says which interpreter the archive holds; the staged tree
    # is asked the same question by running it.
    assert json.loads(pin_document.read_text())["interpreter_path"] == "bin/python3.11"

    # Re-staging finds the published root, re-verifies it, and reuses it.
    service.stage(manifest["release_id"])
    assert json.loads(
        (service.paths.interpreters / f"{_digest(manifest)}.pin.json").read_text()
    ) == pin


# --- S3.2 review fixes -----------------------------------------------------


def test_an_unreadable_surviving_manifest_neither_aborts_prune_nor_deletes(
    tmp_path: Path, release_factory
) -> None:
    """⟦S32-R-01⟧ `_read_json` raises its own `ActivationError`.

    It used to sit above the `try`, so an unreadable surviving slot manifest
    escaped `prune()` *after* `releases.pop()` and `_remove_tree` had run and
    before the registry was committed — leaving the registry naming releases
    whose slots were gone, and every re-run hitting the same read.
    """

    service, first = _imported(tmp_path, release_factory, FakePythonStager())
    service.stage(first["release_id"])
    service.activate(first["release_id"], probe=lambda _candidate: True)

    artifact, second, catalog, attestation = release_factory(
        release_id="hermes-0.18.3", sequence=183
    )
    service._catalog_verifier = DigestPinVerifier(
        hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
    )
    service._attestation_verifier = DigestPinVerifier(
        hashlib.sha256(canonical_json(attestation)).hexdigest()
    )
    service.import_release(
        catalog=catalog,
        manifest=second,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(second["release_id"])
    service.activate(second["release_id"], probe=lambda _candidate: True)

    # Make the ACTIVE release's manifest unreadable, so the surviving record is
    # the one whose read fails.
    slot = service.paths.slots / second["artifact_sha256"]
    manifest_path = slot / "manifest.json"
    manifest_path.chmod(0o600)
    manifest_path.write_text("{ not json", encoding="utf-8")

    removed = service.prune(retain_candidates=0)

    # prune completed rather than escaping mid-mutation, and the registry it
    # committed agrees with the slots that survived.
    registry = service._registry()
    for release_id in removed:
        assert release_id not in registry["releases"]
    for release_id, record in registry["releases"].items():
        assert (service.paths.slots / record["slot_digest"]).is_dir(), release_id


def test_a_crash_orphaned_expansion_tree_is_swept_by_the_next_stage(
    tmp_path: Path, release_factory
) -> None:
    """⟦S32-R-02⟧ The orphan `_prune_interpreters` structurally cannot see.

    `runtime_staging` names its in-progress tree `.{archive_sha256}.{32 hex}`
    and removes it only from its own `except BaseException`, so a SIGKILL or a
    plain SIGTERM leaves it; prune skips every name failing `_SHA256.fullmatch`,
    which a dot-prefixed name always does.
    """

    service, manifest = _imported(tmp_path, release_factory, FakePythonStager())
    interpreters = service.paths.interpreters
    interpreters.mkdir(parents=True, exist_ok=True, mode=0o700)
    orphan = interpreters / f".{'a' * 64}.{'b' * 32}"
    (orphan / "bin").mkdir(parents=True)
    (orphan / "bin" / "python3.11").write_bytes(b"orphaned interpreter\n")
    unrelated = interpreters / ".not-an-orphan"
    unrelated.mkdir()

    service.stage(manifest["release_id"])

    assert not orphan.exists()
    # Matched exactly, so an operator's own dot-prefixed entry survives — the
    # same posture `test_prune_leaves_an_unrecognized_entry_alone` pins.
    assert unrelated.is_dir()
    assert (interpreters / _digest(manifest)).is_dir()


def test_a_declared_worker_module_that_does_not_match_is_refused_at_import(
    tmp_path: Path, release_factory
) -> None:
    """⟦S32-R-07⟧ `worker_modules` had no consumer at all.

    The field is closed, refuses absence and carries digests, and nothing ever
    compared one to `content/<path>` — so a release could declare a module
    digest that did not describe the bytes it shipped, and the slot it produced
    was self-consistent about everything else. Mutating the shipped file instead
    would be caught by `content_tree_sha256` and would prove nothing about this
    field, so the declaration is what diverges here.
    """

    artifact, manifest, catalog, attestation = release_factory(
        real_worker_payload=True,
        worker_modules={"cortex_worker/serve.py": "0" * 64},
    )
    service = _service(tmp_path / "updates", catalog, attestation, FakePythonStager())

    with pytest.raises(VerificationError, match="worker module digest mismatch"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )


def test_a_declared_worker_module_that_is_absent_is_refused(
    tmp_path: Path, release_factory
) -> None:
    artifact, manifest, catalog, attestation = release_factory(
        real_worker_payload=True,
        worker_modules={"cortex_worker/ghost.py": "0" * 64},
    )
    service = _service(tmp_path / "updates", catalog, attestation, FakePythonStager())

    with pytest.raises(VerificationError, match="worker module is missing"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )


def test_a_worker_module_the_slot_carries_but_does_not_declare_is_refused(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-20⟧ The loop only ever walked the DECLARED set.

    5368dcf's own commit message and this function's docstring both state the
    gap as "a release could declare modules it did not carry, or carry modules
    it did not declare, and every gate passed" — but iterating
    `manifest.worker_modules` closes exactly one of those two. A slot carrying
    `cortex_worker/<undeclared>.py` was accepted at import and at every later
    door, and the entrypoint inserts its own resolved directory onto
    `sys.path`, so that file was importable for the life of the release.

    Built by declaring a strict subset of what the real payload ships, so the
    undeclared module is a genuine product source rather than a fabricated one.
    """

    declared = {
        relative: hashlib.sha256(source.read_bytes()).hexdigest()
        for relative, source in module_sources().items()
    }
    assert len(declared) > 1, "the worker package ships too little to subset"
    undeclared = sorted(declared)[0]
    del declared[undeclared]
    artifact, manifest, catalog, attestation = release_factory(
        real_worker_payload=True, worker_modules=declared
    )
    service = _service(tmp_path / "updates", catalog, attestation, FakePythonStager())

    with pytest.raises(VerificationError, match="undeclared worker module"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )


def _declared_tree(root: Path, files: dict[str, bytes]) -> tuple[Path, object]:
    """A `content/` tree and a manifest stand-in declaring exactly `files`."""

    content = root / "content"
    for relative, payload in files.items():
        target = content / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    content.mkdir(parents=True, exist_ok=True)

    class _Manifest:
        worker_modules = tuple(
            (relative, hashlib.sha256(payload).hexdigest())
            for relative, payload in sorted(files.items())
        )

    return content, _Manifest()


def test_no_compiled_artifact_is_exempt_from_the_declaration(tmp_path: Path) -> None:
    """The `__pycache__` rule, decided rather than inherited.

    "Every regular file under the worker package is declared, or the candidate
    is refused" admits no exception for bytecode. It costs nothing: the producer
    expands with `--no-compile`, `_place_worker_modules` copies sources only,
    production slots are sealed `0o555`, and the entrypoint sets
    `sys.dont_write_bytecode` before it imports anything — so a `.pyc` in a slot
    is never something the honest pipeline put there. Exempting the directory
    would have left a sourceless `cortex_worker/<name>.pyc` importable, which is
    the same hole one file extension over.
    """

    source = b"VALUE = 1\n"
    content, manifest = _declared_tree(
        tmp_path / "slot", {f"{WORKER_PACKAGE}/serve.py": source}
    )
    _verify_worker_modules(content, manifest, VerificationError)

    cache = content / WORKER_PACKAGE / "__pycache__"
    cache.mkdir()
    (cache / "serve.cpython-311.pyc").write_bytes(b"\x00compiled")

    with pytest.raises(VerificationError, match="undeclared worker module"):
        _verify_worker_modules(content, manifest, VerificationError)


def test_every_later_door_re_asks_the_worker_module_question(
    tmp_path: Path, release_factory
) -> None:
    """A runtime-update root can be restored onto a machine that did not import it."""

    service, manifest = _imported(
        tmp_path, release_factory, FakePythonStager(), real_worker_payload=True
    )
    assert manifest["worker_modules"], "fixture declares no worker modules"
    relative = sorted(manifest["worker_modules"])[0]
    slot = service.paths.slots / manifest["artifact_sha256"]
    module = slot / "content" / relative
    module.parent.chmod(0o700)
    module.unlink()

    with pytest.raises(ActivationError) as raised:
        service.stage(manifest["release_id"])
    # The tree digest notices too; what matters is that the release is refused
    # by a door that re-derives rather than by one that trusts the import.
    assert "verification failed" in str(raised.value) or "worker module" in str(
        raised.value
    )
