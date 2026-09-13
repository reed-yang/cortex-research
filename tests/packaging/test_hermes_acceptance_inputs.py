"""The retained acceptance driver must state its inputs, and refuse unsafe ones.

`tools/hermes_acceptance.py` proves §5 clauses 3-8 and nothing else does. It was
an ignored file under `output/`, written to run from one directory: it found its
closure, its vendored interpreter and its patched fork by walking up from its own
location, and it wrote its evidence into whatever directory it was stored in.
Tracked under `tools/`, every one of those lookups resolves somewhere else, and
the run would still write a record saying `certified: true`.

These tests cover the seam that fixes that — not the acceptance itself, which
needs a real staged slot and a real worker. They pin two things:

* each input is an argument, and a missing or ambiguous one is a refusal rather
  than a guess;
* both writable roots must be fresh. The original purged WORK and artifact-set
  mode replaces sandbox profiles; neither may destroy prior evidence. Preparation
  now creates both roots before the first proof writes into them.

The input module imports no product code. Its refusal tests read no credential
and start no acceptance process; they do not execute the full harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.hermes_acceptance_inputs import (
    AcceptanceInputs,
    InputError,
    prepare_directories,
    resolve_inputs,
)

REPOSITORY = Path(__file__).resolve().parents[2]

#: The attested entrypoint the driven release names. Retained beside the harness
#: in both original evidence directories; identical in each.
RETAINED_DRIVER_SHA256 = (
    "8c63842893b2a1080304b7a48f3741eeaededc3138e5331406ff8c1ac6b15e8b"
)

#: The retained S3.5 evidence, named by the environment or found in THIS
#: checkout -- never searched for from the checkout upwards. The ancestor walk
#: this replaces left the checkout and cross-checked against a directory the
#: checkout never declared, so a worktree silently consumed a supply belonging
#: to some parent directory while the same test skipped on every other machine.
#: That is the defect `tests/conftest.py` removed for the worker runtime.
S35_EVIDENCE_VARIABLE = "CORTEX_TEST_S35_EVIDENCE"
S35_EVIDENCE_RETAINED = "output/s35-real-acceptance-20260902"


def _retained_s35_driver() -> Path | None:
    """The retained `acceptance_driver.py`, or None when it is not on this host.

    Absence is a skip rather than a failure, unlike the vendored cp311 archive
    in `tests/conftest.py`: `CORTEX_REQUIRE_REAL_RUNTIME` is that fixture's own
    gate (`tests/test_real_runtime_gate.py` pins its message and its scope), and
    the S3.5 evidence is real operator material that no staging manifest
    declares -- the same class as `CORTEX_TEST_PRE_GEN7_CONTROL_DB`,
    `CORTEX_TEST_VINEXT_PAYLOAD` and `CORTEX_TEST_HERMES_FORK`, none of which
    escalate either. Making it required is a supply decision, not a test one.
    """

    configured = os.environ.get(S35_EVIDENCE_VARIABLE)
    # A mistyped override is held to the same test as the retained directory,
    # so it reports the missing input rather than a digest mismatch.
    directory = Path(configured) if configured else REPOSITORY / S35_EVIDENCE_RETAINED
    driver = directory / "acceptance_driver.py"
    return driver if driver.is_file() else None


def _closure(root: Path) -> Path:
    closure = root / "closure"
    (closure / "wheelhouse").mkdir(parents=True)
    (closure / "closure.requirements.txt").write_text("hermes-agent==0.15.0\n")
    return closure


def _runtime(root: Path, *, archives: int = 1, pin: bool = True) -> Path:
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    for index in range(archives):
        archive = runtime / f"cpython-3.11.15-cp311-macosx_11_0_arm64.{index}.tar.gz"
        archive.write_bytes(b"not a real interpreter")
        if pin:
            archive.with_suffix(archive.suffix + ".pin.json").write_text("{}")
    return runtime


def _inputs(root: Path, **overrides: object) -> list[str]:
    driver = root / "acceptance_driver.py"
    driver.write_text("# stand-in for the attested entrypoint\n")
    fork = root / "fork"
    fork.mkdir()
    argv = {
        "--driver": str(driver),
        "--closure": str(_closure(root)),
        "--runtime": str(_runtime(root)),
        "--fork": str(fork),
        "--work": str(root / "work"),
        "--evidence-root": str(root / "evidence"),
    }
    for flag, value in overrides.items():
        argv["--" + flag.replace("_", "-")] = str(value)
    return [item for pair in argv.items() for item in pair]


@pytest.fixture()
def product(tmp_path: Path) -> Path:
    worktree = tmp_path / "product"
    worktree.mkdir()
    return worktree


def test_every_input_resolves_and_the_interpreter_is_discovered(
    tmp_path: Path, product: Path
) -> None:
    resolved = resolve_inputs(_inputs(tmp_path), product=product)

    assert isinstance(resolved, AcceptanceInputs)
    assert resolved.closure == (tmp_path / "closure").resolve()
    assert resolved.fork == (tmp_path / "fork").resolve()
    assert resolved.product == product.resolve()
    # Derived from the runtime directory, never typed: a supplied filename can
    # name an archive the pin beside it does not describe.
    assert resolved.archive.parent == resolved.runtime
    assert resolved.pin.is_file()
    assert resolved.mode == "self-packaged"
    assert resolved.record_name == "acceptance.json"


def test_artifact_set_mode_names_the_full_record(tmp_path: Path, product: Path) -> None:
    artifacts = tmp_path / "artifact-set"
    artifacts.mkdir()

    resolved = resolve_inputs(
        _inputs(tmp_path, artifacts=artifacts), product=product
    )

    assert resolved.artifacts == artifacts.resolve()
    assert resolved.mode == "artifact-set"
    # Must not collide with the record of the run that certified that directory.
    assert resolved.record_name == "acceptance.full.json"


@pytest.mark.parametrize(
    "flag", ["--driver", "--closure", "--runtime", "--fork", "--work", "--evidence-root"]
)
def test_a_missing_input_is_refused_rather_than_inferred(
    tmp_path: Path, product: Path, flag: str
) -> None:
    argv = _inputs(tmp_path)
    index = argv.index(flag)
    del argv[index : index + 2]

    with pytest.raises(SystemExit) as refusal:
        resolve_inputs(argv, product=product)

    assert refusal.value.code != 0


def test_an_ambiguous_interpreter_is_refused(tmp_path: Path, product: Path) -> None:
    argv = _inputs(tmp_path)
    runtime = Path(argv[argv.index("--runtime") + 1])
    extra = runtime / "cpython-3.11.15-cp311-macosx_11_0_arm64.9.tar.gz"
    extra.write_bytes(b"a second archive")
    extra.with_suffix(extra.suffix + ".pin.json").write_text("{}")

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "more than one interpreter archive" in str(refusal.value)


def test_an_unpinned_interpreter_is_refused(tmp_path: Path, product: Path) -> None:
    argv = _inputs(tmp_path)
    runtime = Path(argv[argv.index("--runtime") + 1])
    for pin in runtime.glob("*.pin.json"):
        pin.unlink()

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "no pin document" in str(refusal.value)


def test_a_closure_missing_its_wheelhouse_is_refused(
    tmp_path: Path, product: Path
) -> None:
    argv = _inputs(tmp_path)
    closure = Path(argv[argv.index("--closure") + 1])
    (closure / "wheelhouse").rmdir()

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "wheelhouse" in str(refusal.value)


def test_a_populated_work_directory_is_refused_because_it_is_purged(
    tmp_path: Path, product: Path
) -> None:
    argv = _inputs(tmp_path)
    work = Path(argv[argv.index("--work") + 1])
    work.mkdir()
    (work / "someone-elses-evidence.json").write_text("{}")

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "--work must be absent or empty" in str(refusal.value)


def test_the_purged_directory_may_not_also_hold_the_evidence(
    tmp_path: Path, product: Path
) -> None:
    shared = tmp_path / "shared"
    argv = _inputs(tmp_path, work=shared, evidence_root=shared)

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "may not also be --evidence-root" in str(refusal.value)


@pytest.mark.parametrize("flag", ["work", "evidence_root"])
def test_a_writable_directory_inside_the_checkout_is_refused(
    tmp_path: Path, product: Path, flag: str
) -> None:
    argv = _inputs(tmp_path, **{flag: product / "inside"})

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "inside the checkout" in str(refusal.value)


@pytest.mark.parametrize("flag", ["work", "evidence_root"])
@pytest.mark.parametrize(
    "retained",
    ["s31b-real-acceptance-20260901", "s35-real-acceptance-20260902",
     "hermes-release-gen9-20260902"],
)
def test_retained_evidence_is_never_a_writable_target(
    tmp_path: Path, product: Path, flag: str, retained: str
) -> None:
    # The run replaces `sandbox-profiles/` wholesale and purges its work tree.
    # Either one, aimed at a retained directory, destroys the only record that
    # has ever carried `certified: true`.
    argv = _inputs(tmp_path, **{flag: tmp_path / retained / "nested"})

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "retained evidence" in str(refusal.value)


@pytest.mark.parametrize("flag", ["work", "evidence_root"])
def test_a_home_or_filesystem_root_is_refused(
    tmp_path: Path, product: Path, flag: str
) -> None:
    argv = _inputs(tmp_path, **{flag: Path.home()})

    with pytest.raises(InputError) as refusal:
        resolve_inputs(argv, product=product)

    assert "filesystem or home root" in str(refusal.value)


def test_the_product_worktree_is_supplied_by_the_caller(tmp_path: Path) -> None:
    # Derived from the imported `cortex_platform`, never an argument, so the
    # record cannot name a worktree the run did not measure.
    with pytest.raises(InputError) as refusal:
        resolve_inputs(_inputs(tmp_path), product=None)

    assert "product worktree" in str(refusal.value)


def test_resolving_inputs_touches_nothing_on_disk(
    tmp_path: Path, product: Path
) -> None:
    argv = _inputs(tmp_path)
    work = Path(argv[argv.index("--work") + 1])
    evidence = Path(argv[argv.index("--evidence-root") + 1])

    resolve_inputs(argv, product=product)

    assert not work.exists()
    assert not evidence.exists()


def test_preparation_creates_roots_for_the_first_proof_write(
    tmp_path: Path, product: Path
) -> None:
    inputs = resolve_inputs(_inputs(tmp_path), product=product)
    prepare_directories(inputs)
    assert inputs.work.is_dir()
    profile = inputs.evidence_root / "profile.sb"
    profile.write_text("(version 1)")
    assert profile.read_text() == "(version 1)"


def test_populated_evidence_is_refused_and_preserved(
    tmp_path: Path, product: Path
) -> None:
    argv = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = evidence / "certification.full.json"
    record.write_text("retained")
    with pytest.raises(InputError, match="--evidence-root must be absent or empty"):
        resolve_inputs(argv, product=product)
    assert record.read_text() == "retained"


def test_preparation_rechecks_freshness_before_creating_work(
    tmp_path: Path, product: Path
) -> None:
    inputs = resolve_inputs(_inputs(tmp_path), product=product)
    inputs.evidence_root.mkdir()
    record = inputs.evidence_root / "previous.json"
    record.write_text("keep")
    with pytest.raises(InputError, match="--evidence-root must be absent or empty"):
        prepare_directories(inputs)
    assert record.read_text() == "keep"
    assert not inputs.work.exists()


@pytest.mark.parametrize("nested", ["work", "evidence_root"])
def test_writable_roots_must_not_contain_each_other(
    tmp_path: Path, product: Path, nested: str
) -> None:
    parent = tmp_path / "fresh"
    paths = {"work": parent, "evidence_root": parent}
    paths[nested] = parent / "nested"
    with pytest.raises(InputError, match="must not contain each other"):
        resolve_inputs(_inputs(tmp_path, **paths), product=product)


@pytest.mark.parametrize("source", ["closure", "runtime", "fork", "artifacts"])
def test_evidence_output_cannot_mutate_supplied_inputs(
    tmp_path: Path, product: Path, source: str
) -> None:
    if source == "artifacts":
        (tmp_path / "artifacts").mkdir()
    overrides = {"evidence_root": tmp_path / source / "fresh"}
    if source == "artifacts":
        overrides["artifacts"] = tmp_path / source
    with pytest.raises(InputError, match="inside a supplied input"):
        resolve_inputs(_inputs(tmp_path, **overrides), product=product)


def test_the_input_seam_does_not_import_the_product() -> None:
    # Validation must stay usable without a configured product environment,
    # and must not be able to reach a credential resolver on the way.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import tools.hermes_acceptance_inputs as m; "
            "print('cortex_platform' in sys.modules)",
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "False"


def test_the_tracked_attested_entrypoint_still_matches_the_retained_original() -> None:
    """The driven release's entrypoint must not drift from the retained copy.

    Skipped where the retained evidence is absent, like the vendored cp311
    archive in `tests/conftest.py`: `output/` is host supply, not source. The
    evidence is named by $CORTEX_TEST_S35_EVIDENCE or read from this checkout's
    own `output/`, so the cross-check can no longer resolve into an ancestor
    directory the checkout never declared.
    """
    import hashlib

    tracked = REPOSITORY / "tools" / "hermes_acceptance_driver.py"
    digest = hashlib.sha256(tracked.read_bytes()).hexdigest()
    assert digest == RETAINED_DRIVER_SHA256, (
        "tools/hermes_acceptance_driver.py no longer matches the attested "
        "entrypoint that produced the retained certification evidence"
    )

    original = _retained_s35_driver()
    if original is None:  # pragma: no cover - staged supply
        pytest.skip(
            "the retained S3.5 acceptance_driver.py is required: set "
            f"${S35_EVIDENCE_VARIABLE} to the evidence directory, or hold it at "
            f"{REPOSITORY / S35_EVIDENCE_RETAINED}"
        )
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
