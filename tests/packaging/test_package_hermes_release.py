"""The packaging command must derive the OS floor, not accept one.

S3.1a locked the door: `import_release` refuses a release whose `platform`
target the host cannot satisfy. But `minimum_os_version` had zero producer-side
references, so the floor was whatever a packager typed. A closure carrying
`macosx_14_0_arm64` wheels could be published with `minimum_os_version: null`,
import cleanly on macOS 12, and fail later as a native-wheel link error — the
exact failure the platform target exists to prevent.

These tests pin the producer half. The floor is a property of the wheels pip
actually downloaded, and nothing else — not the packaging host, and not an
argument.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import namedtuple
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import canonical_json
from cortex_platform.product.runtime_update.worker_payload import (
    ENTRYPOINT_SOURCE,
    module_sources,
)
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
    VerificationError,
)
from distribution import wheel_closure
from tools.package_hermes_release import (
    PackagedRelease,
    PackagingError,
    ReleaseIdentity,
    derive_minimum_os_version,
    expand_closure,
    package_release,
    resolve_requirements,
)

COMMIT = "9de9c25f620ff7f1ce0fd5457d596052d5159596"


def _identity(**overrides: object) -> ReleaseIdentity:
    fields: dict[str, object] = {
        "release_id": "hermes-0.18.2",
        "release_sequence": 182,
        "distribution_version": "0.18.2",
        "upstream_repository": "NousResearch/hermes-agent",
        "upstream_tag": "v2026.7.7.2",
        "upstream_commit": COMMIT,
        "publisher": "pypi:NousResearch",
        "workflow": "release.yml",
        "python_range": ">=3.11,<3.15",
        "adapter_protocol": "0.1",
        "session_schema": 13,
        "evidence_sha256": "b" * 64,
    }
    fields.update(overrides)
    return ReleaseIdentity(**fields)  # type: ignore[arg-type]


def _closure(root: Path, names: list[str], *, requirements: str = "hermes-agent==0.18.2\n") -> Path:
    """A wheelhouse is the files pip left behind, plus its requirements."""

    closure = root / "wheelhouse"
    closure.mkdir(parents=True, exist_ok=True)
    for name in names:
        (closure / name).write_bytes(name.encode())
    (closure / "closure.requirements.txt").write_text(requirements, encoding="utf-8")
    return closure


def _payload(root: Path) -> Path:
    """The tree that becomes the artifact: hermes beside the worker entrypoint."""

    payload = root / "payload"
    (payload / "hermes_agent").mkdir(parents=True, exist_ok=True)
    (payload / "hermes_agent" / "__init__.py").write_text("", encoding="utf-8")
    (payload / "runtime_worker.py").write_text(
        "def handle(method, params):\n"
        '    return {"status": "healthy", "release_id": params.get("release_id")}\n',
        encoding="utf-8",
    )
    return payload


# A stand-in for the vendored CPython archive. Packaging never opens it as a
# tarball — it copies bytes and digests them — so the properties below are
# provable without a 38 MB fixture. The real vendored archive appears once, in
# the real-pip test at the bottom of this file.
SYNTHETIC_RUNTIME = b"synthetic-cp311-runtime\n"


def _runtime(root: Path, *, payload: bytes = SYNTHETIC_RUNTIME) -> tuple[Path, Path]:
    """The two files `vendor_python_runtime.py` leaves behind, in that shape."""

    vendored = root / "vendored"
    vendored.mkdir(parents=True, exist_ok=True)
    name = "cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz"
    archive = vendored / name
    archive.write_bytes(payload)
    pin = vendored / "cpython-3.11.15-cp311-macosx_11_0_arm64.pin.json"
    pin.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "implementation": "CPython",
                "version": "3.11.15",
                "abi_tag": "cp311",
                "platform_tag": "macosx_11_0_arm64",
                "interpreter_path": "bin/python3.11",
                "archive": {
                    "name": name,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    return archive, pin


def _service(root: Path, packaged: PackagedRelease) -> RuntimeUpdateService:
    """Pin the service to the digests packaging emitted, as an operator would."""

    return RuntimeUpdateService(
        root / f"runtime-update-{packaged.pins['manifest_sha256'][:12]}",
        catalog_verifier=DigestPinVerifier(packaged.pins["catalog_payload_sha256"]),
        attestation_verifier=DigestPinVerifier(packaged.pins["attestation_sha256"]),
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )


def _package(tmp_path: Path, wheels: list[str], **overrides: object) -> PackagedRelease:
    arguments: dict[str, object] = {
        "identity": _identity(),
        "closure": _closure(tmp_path, wheels),
        "payload": _payload(tmp_path),
        "output": tmp_path / "out",
        "catalog_sequence": 182,
        "issued_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }
    if "python_runtime" not in overrides:
        # Built lazily so a test that hands in its own (mutated) pair is not
        # quietly overwritten by a pristine one.
        runtime, pin = _runtime(tmp_path)
        arguments["python_runtime"] = runtime
        arguments["python_runtime_pin"] = pin
    arguments.update(overrides)
    return package_release(**arguments)  # type: ignore[arg-type]


# --- the derivation itself -------------------------------------------------


def test_the_floor_is_the_numeric_maximum_not_the_lexical_one() -> None:
    """`10_0` sorts below `9_0` as text and above it as a version.

    Both orderings appear in a real closure: pip is handed `macosx_11_0`
    through `macosx_26_0`, and older wheels on PyPI still carry `10_x`.
    """

    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-macosx_9_0_arm64.whl", "b-1.0-cp314-abi3-macosx_10_0_arm64.whl"]
    ) == "10.0"
    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-macosx_10_16_arm64.whl", "b-1.0-cp314-abi3-macosx_11_0_arm64.whl"]
    ) == "11.0"
    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-macosx_14_9_arm64.whl", "b-1.0-cp314-abi3-macosx_14_10_arm64.whl"]
    ) == "14.10"
    # Descending order: `_closure_wheels` sorts by filename, so `av`'s 14_0
    # precedes `tokenizers`' 11_0 in a real closure — a "last wheel wins"
    # implementation would pass every ascending fixture and publish 11.0 here.
    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-macosx_14_0_arm64.whl", "b-1.0-cp314-abi3-macosx_11_0_arm64.whl"]
    ) == "14.0"


def test_a_portable_closure_declares_no_floor() -> None:
    """Pure-Python wheels run anywhere, so a floor would only refuse hosts."""

    assert derive_minimum_os_version(["a-1.0-py3-none-any.whl", "b-2.0-py3-none-any.whl"]) is None
    assert derive_minimum_os_version([]) is None


def test_every_tag_a_multi_tag_wheel_carries_counts() -> None:
    """One wheel may declare several platforms in a dot-separated field."""

    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-macosx_11_0_arm64.macosx_14_0_arm64.whl"]
    ) == "14.0"
    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-macosx_14_0_arm64.macosx_11_0_arm64.whl"]
    ) == "14.0"


def test_a_distribution_named_after_a_platform_cannot_raise_the_floor() -> None:
    """Only the platform field is read, not the whole filename.

    The contract permits a lexical scan; restricting it to the field that
    actually carries tags costs one `rsplit` and removes the whole class.
    """

    assert derive_minimum_os_version(["macosx_99_0_helper-1.0-py3-none-any.whl"]) is None


def test_non_macos_platform_tags_are_ignored() -> None:
    assert derive_minimum_os_version(
        ["a-1.0-cp314-abi3-manylinux_2_28_x86_64.whl", "b-1.0-cp314-abi3-win_amd64.whl"]
    ) is None


@pytest.mark.parametrize(
    "tag",
    [
        "macosx_11_0_arm64",
        "macosx_10_16_x86_64",
        "macosx_26_0_universal2",
        "any",
        "win_amd64",
        "manylinux_2_28_x86_64",
        "macosx_11_arm64",
        "macosx_11_0",
        "macosx_a_0_arm64",
    ],
)
def test_the_local_tag_grammar_agrees_with_the_closure_verifier(tag: str) -> None:
    """This module keeps its own copy of the macOS tag grammar rather than
    importing a private name from `distribution.wheel_closure`. Pin the two so
    a divergence is a test failure, not a silently different answer."""

    from tools.package_hermes_release import _MACOS_TAG as local

    assert (local.fullmatch(tag) is None) == (
        wheel_closure._MACOS_TAG.fullmatch(tag) is None
    )


# --- resolving the closure -------------------------------------------------


def test_markers_are_evaluated_against_the_target_not_the_running_host(
    tmp_path: Path,
) -> None:
    """"Markers intact" means evaluated, not stripped. pip would decide against
    the interpreter running the packaging command; the closure was resolved for
    `TARGET_ENVIRONMENT`."""

    requirements = tmp_path / "closure.requirements.txt"
    requirements.write_text(
        "# comment\n"
        "hermes-agent==0.18.2\n"
        'pywin32==306 ; sys_platform == "win32"\n'
        'uvloop==0.21.0 ; sys_platform == "darwin"\n'
        "\n",
        encoding="utf-8",
    )
    assert resolve_requirements(requirements) == [
        "hermes-agent==0.18.2",
        "uvloop==0.21.0",
    ]


def test_a_closure_that_applies_to_nothing_is_refused(tmp_path: Path) -> None:
    requirements = tmp_path / "closure.requirements.txt"
    requirements.write_text('pywin32==306 ; sys_platform == "win32"\n', encoding="utf-8")
    with pytest.raises(PackagingError, match="applies to the target"):
        resolve_requirements(requirements)


def test_hash_pins_survive_resolution_and_cover_sdist_built_wheels(
    tmp_path: Path,
) -> None:
    """The documented input producer always emits `--hash=` pins, which put
    pip into --require-hashes mode. They are a real binding and are kept — but
    a wheel `vendor_wheelhouse` legitimately built from a pinned sdist has a
    digest the lock does not know, so its recorded `wheel_sha256` must ride
    along or pip refuses the closure's own blessed wheel.

    The input is produced by the real `filter_for_target`, not hand-written,
    so the two tools cannot drift on the line format.
    """

    from tools.vendor_wheelhouse import filter_for_target

    lock_hash = "a" * 64
    text, versions = filter_for_target(f"idna==3.7 --hash=sha256:{lock_hash}\n")
    assert versions == {"idna": "3.7"}
    closure = tmp_path / "closure"
    closure.mkdir()
    requirements = closure / "closure.requirements.txt"
    requirements.write_text(text, encoding="utf-8")
    assert resolve_requirements(requirements) == [
        f"idna==3.7 --hash=sha256:{lock_hash}"
    ]

    wheel_hash = "d" * 64
    (closure / "built-from-sdist.json").write_text(
        json.dumps({"idna": {"sdist_sha256": "c" * 64, "wheel_sha256": wheel_hash}}),
        encoding="utf-8",
    )
    assert resolve_requirements(requirements) == [
        f"idna==3.7 --hash=sha256:{lock_hash} --hash=sha256:{wheel_hash}"
    ]


def test_expansion_pins_the_closure_and_stays_offline(tmp_path: Path) -> None:
    """`--no-deps` is not an optimisation. The closure is already complete and
    already pinned, so a pip re-resolve would replace what was certified."""

    recorded: list[list[str]] = []
    destination = expand_closure(
        closure=_closure(tmp_path, ["idna-3.7-py3-none-any.whl"]),
        destination=tmp_path / "tree",
        extra_files={"runtime_worker.py": _payload(tmp_path) / "runtime_worker.py"},
        runner=lambda command: recorded.append(list(command)),
    )
    assert len(recorded) == 1
    command = recorded[0]
    assert command[1:4] == ["-m", "pip", "install"]
    assert "--no-index" in command and "--no-deps" in command
    # Without --no-compile pip writes __pycache__ into the target, and each
    # cached code object embeds the absolute staging path — the artifact then
    # digests differently on every run.
    assert "--no-compile" in command
    # The entrypoint lands beside the closure, at the tree root.
    assert (destination / "runtime_worker.py").is_file()


def test_a_dirty_destination_is_refused_before_pip_runs(tmp_path: Path) -> None:
    """Re-running the command into a reused --output must refuse, not overlay.

    Real pip skips an existing directory with a stderr warning and exit 0, so
    expanding version 2.0 over 1.0's tree would emit a manifest attesting 2.0's
    identity over 1.0's bytes — every digest self-consistent over the wrong
    payload, undetectable by any consumer check. The refusal must come before
    pip is reached, because pip is the thing that cannot be trusted here.
    """

    recorded: list[list[str]] = []
    destination = tmp_path / "tree"
    destination.mkdir()
    (destination / "stale.py").write_text("", encoding="utf-8")
    with pytest.raises(PackagingError, match="destination is not empty"):
        expand_closure(
            closure=_closure(tmp_path, ["idna-3.7-py3-none-any.whl"]),
            destination=destination,
            runner=lambda command: recorded.append(list(command)),
        )
    assert recorded == []


# --- the packaging command -------------------------------------------------


def test_the_floor_comes_from_the_wheels_not_from_the_packaging_host(
    tmp_path: Path,
) -> None:
    """This is the defect S3.1b exists to close.

    The packaging host's own OS version is not the closure's floor. A machine
    on macOS 26 packaging a closure of `macosx_14_0` wheels must publish 14.0,
    or every host between 14 and 26 is refused a release that would have run.
    """

    result = _package(
        tmp_path,
        ["av-1.0-cp314-abi3-macosx_14_0_arm64.whl", "idna-3.7-py3-none-any.whl"],
    )
    assert result.manifest["platform"]["minimum_os_version"] == "14.0"


def test_the_floor_cannot_be_supplied_by_the_caller() -> None:
    """There is no argument to pass and no field to set."""

    assert "minimum_os_version" not in ReleaseIdentity.__dataclass_fields__


def test_packaged_documents_are_accepted_by_the_real_import_path(
    tmp_path: Path,
) -> None:
    """Prod-consistent: the four documents go through `import_release` itself.

    A packaging command that emits documents its own consumer rejects is worth
    nothing, and every cross-field rule (`patch_set_sha256` binds the ledger,
    the catalog entry binds the manifest digest, the attestation is an exact
    projection of the manifest) is only checked there.
    """

    # A closure with an in-range floor, so a derived non-null value is what
    # travels through `satisfied_by` — a pure-Python closure would satisfy at
    # the first branch and leave the comparison unexercised.
    result = _package(
        tmp_path,
        ["av-1.0-cp314-abi3-macosx_11_0_arm64.whl", "idna-3.7-py3-none-any.whl"],
    )
    assert result.manifest["platform"]["minimum_os_version"] == "11.0"
    # The identity fields packaging carries must land bound to their sources.
    assert result.manifest["evidence_sha256"] == _identity().evidence_sha256
    assert result.manifest["dependency_lock_sha256"] == hashlib.sha256(
        (tmp_path / "wheelhouse" / "closure.requirements.txt").read_bytes()
    ).hexdigest()
    slot = _service(tmp_path, result).import_release(
        catalog=result.catalog,
        manifest=result.manifest,
        attestation=result.attestation,
        patch_ledger=result.patch_ledger,
        artifact=result.artifact,
    )
    # Prove the import did the work rather than returning early.
    assert (slot / "content" / result.manifest["worker_entrypoint"]).is_file()
    assert (slot / "content" / "hermes_agent" / "__init__.py").is_file()
    assert (slot / "manifest.json").is_file()
    assert (slot / "attestation.json").is_file()


def test_a_derived_floor_the_host_cannot_meet_is_actually_refused(
    tmp_path: Path,
) -> None:
    """The end-to-end proof that the slice closed the gap it was written for.

    Everything above shows the floor is computed. This shows the computed value
    reaches the refusal — package a closure the running host cannot satisfy and
    `import_release` says so, with no packager cooperation required and no way
    for one to opt out.
    """

    result = _package(tmp_path, ["av-1.0-cp314-abi3-macosx_99_0_arm64.whl"])
    assert result.manifest["platform"]["minimum_os_version"] == "99.0"
    with pytest.raises(VerificationError, match="platform is not supported"):
        _service(tmp_path, result).import_release(
            catalog=result.catalog,
            manifest=result.manifest,
            attestation=result.attestation,
            patch_ledger=result.patch_ledger,
            artifact=result.artifact,
        )


def test_a_catalog_the_consumer_would_refuse_is_refused_at_packaging_time(
    tmp_path: Path,
) -> None:
    """The catalog goes through the consumer's own model like the manifest and
    the ledger. `--valid-days 0` makes `issued_at == expires_at`, which
    `CatalogPayload` refuses — a producer must not emit a document set whose
    only possible consumer verdict is refusal."""

    issued = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with pytest.raises(PackagingError, match="catalog"):
        _package(
            tmp_path,
            ["idna-3.7-py3-none-any.whl"],
            issued_at=issued,
            expires_at=issued,
        )


def test_the_pins_are_emitted_rather_than_left_to_be_computed_by_hand(
    tmp_path: Path,
) -> None:
    """`import` takes two trusted digests. Making the operator derive them is
    the same hand-filled-key defect one layer up."""

    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])
    assert result.pins["catalog_payload_sha256"] == hashlib.sha256(
        canonical_json(result.catalog["payload"])
    ).hexdigest()
    assert result.pins["attestation_sha256"] == hashlib.sha256(
        canonical_json(result.attestation)
    ).hexdigest()
    assert (result.paths["pins"]).is_file()


def test_the_artifact_carries_the_entrypoint_at_its_root(tmp_path: Path) -> None:
    """`import_release` extracts the archive at `content/` and looks for
    `content/<worker_entrypoint>`, so the entrypoint must not be nested."""

    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])
    with zipfile.ZipFile(result.artifact) as archive:
        names = set(archive.namelist())
    assert result.manifest["worker_entrypoint"] in names
    assert "hermes_agent/__init__.py" in names


def test_a_payload_without_the_entrypoint_is_refused_at_packaging_time(
    tmp_path: Path,
) -> None:
    """Fail where the reason is still legible. `import_release` would say
    'candidate worker entrypoint is missing' on another machine, later."""

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "hermes_agent.py").write_text("", encoding="utf-8")
    with pytest.raises(PackagingError, match="entrypoint"):
        _package(tmp_path, ["idna-3.7-py3-none-any.whl"], payload=bare)


def test_a_symlink_in_the_payload_is_refused_at_packaging_time(
    tmp_path: Path,
) -> None:
    """The extractor forbids archived symlinks. Refusing here turns a remote
    'artifact archive is invalid' into a local message naming the file."""

    payload = _payload(tmp_path)
    (payload / "escape").symlink_to(tmp_path)
    with pytest.raises(PackagingError, match="symlink"):
        _package(tmp_path, ["idna-3.7-py3-none-any.whl"], payload=payload)


def test_an_empty_closure_is_refused(tmp_path: Path) -> None:
    """A wheelhouse with no wheels means the closure was never resolved, and a
    derived `None` floor would be indistinguishable from a portable one."""

    # "contains no wheels" exactly: a bare match of "closure" also matches the
    # missing-requirements and missing-directory refusals.
    with pytest.raises(PackagingError, match="contains no wheels"):
        _package(tmp_path, [])


def test_the_zip_is_deterministic_for_identical_payload_trees(tmp_path: Path) -> None:
    """Same payload bytes, same digest. The manifest pins `artifact_sha256`,
    so a zip that varies by mtime makes every document unstable for no reason.

    This pins only the archiver. Whether two full runs produce identical
    payload *trees* is the real-pip test below — this one's previous name
    claimed that property while never calling `expand_closure`.
    """

    issued = datetime(2026, 8, 5, tzinfo=timezone.utc)
    expires = datetime(2026, 9, 5, tzinfo=timezone.utc)
    wheels = ["av-1.0-cp314-abi3-macosx_14_0_arm64.whl"]
    # Two separate payload trees with identical contents, not one tree packaged
    # twice: packaging now copies the runtime into the payload, and re-running
    # over its own output is a refusal rather than a repeat.
    common: dict[str, object] = {"issued_at": issued, "expires_at": expires}
    first = _package(
        tmp_path, wheels, output=tmp_path / "a", payload=_payload(tmp_path / "a"), **common
    )
    second = _package(
        tmp_path, wheels, output=tmp_path / "b", payload=_payload(tmp_path / "b"), **common
    )
    assert first.manifest["artifact_sha256"] == second.manifest["artifact_sha256"]
    assert first.pins == second.pins


@pytest.fixture(scope="session")
def real_pip_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A Python that can actually run pip.

    The repository venv is uv-managed and ships no pip — which is exactly how
    the first real run of this tool found pip missing after a green suite.
    Seed a scratch venv so real-pip properties are proven through real pip,
    and skip only when even that is impossible.
    """

    if shutil.which("uv") is None:
        pytest.skip("uv is not available to seed a pip venv")
    venv = tmp_path_factory.mktemp("pip-venv") / "venv"
    seeded = subprocess.run(
        ["uv", "venv", "--seed", str(venv)], capture_output=True, text=True
    )
    if seeded.returncode != 0:
        pytest.skip(f"could not seed a pip venv: {seeded.stderr.strip()[:200]}")
    python = venv / "bin" / "python"
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.skip("seeded venv has no working pip")
    return python


def _real_wheel(closure: Path, *, name: str = "idna", version: str = "3.7") -> Path:
    """A minimal installable wheel — real pip refuses the zero-byte stand-ins."""

    dist_info = f"{name}-{version}.dist-info"
    wheel = closure / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{name}/__init__.py", f'VERSION = "{version}"\n')
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{name}/__init__.py,,\n"
            f"{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n{dist_info}/RECORD,,\n",
        )
    return wheel


def _console_script_wheel(
    closure: Path, *, name: str = "consoler", version: str = "1.2.3"
) -> Path:
    """A real wheel declaring `[console_scripts]`, so pip writes into `bin/`.

    ⟦S32-05⟧ The existing reproducibility wheel is `idna`, which declares no
    entry points — so pip never wrote a `bin/` script and the test was blind to
    the one member class that carries the builder's absolute path.
    """

    dist_info = f"{name}-{version}.dist-info"
    wheel = closure / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "def main():\n    return 0\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            f"[console_scripts]\n{name} = {name}:main\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{name}/__init__.py,,\n"
            f"{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n"
            f"{dist_info}/entry_points.txt,,\n{dist_info}/RECORD,,\n",
        )
    return wheel


@pytest.fixture(scope="session")
def second_pip_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A second pip venv at a different absolute path.

    The reproducibility test reused one interpreter for both rounds, so a digest
    that depended on the interpreter's path could not be observed. Two roots is
    the whole point.
    """

    if shutil.which("uv") is None:
        pytest.skip("uv is not available to seed a pip venv")
    venv = tmp_path_factory.mktemp("pip-venv-second") / "venv"
    seeded = subprocess.run(
        ["uv", "venv", "--seed", str(venv)], capture_output=True, text=True
    )
    if seeded.returncode != 0:
        pytest.skip(f"could not seed a pip venv: {seeded.stderr.strip()[:200]}")
    python = venv / "bin" / "python"
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.skip("seeded venv has no working pip")
    return python


def test_console_script_shebangs_do_not_carry_the_build_root(
    tmp_path: Path, real_pip_python: Path, second_pip_python: Path
) -> None:
    """⟦S32-05⟧ Two interpreters, two build roots, one artifact digest.

    Measured on the real acceptance artifact before the fix: 14 members under
    `bin/` began `#!/tmp/s32-real-acceptance/cp311/bin/python3.11`, and that
    zip's sha256 is exactly the descriptor's `expected_artifact_digest` and its
    `slot_id`. The release identity was a function of the directory it was built
    in.
    """

    # The venv paths, not what they resolve to: pip writes the shebang from the
    # path it was invoked as, and both venvs share one uv-managed interpreter.
    assert real_pip_python != second_pip_python
    assert real_pip_python.parent.parent != second_pip_python.parent.parent
    issued = datetime(2026, 8, 5, tzinfo=timezone.utc)
    expires = datetime(2026, 9, 5, tzinfo=timezone.utc)
    results: list[PackagedRelease] = []
    roots: list[Path] = []
    for run, interpreter in (("a", real_pip_python), ("b", second_pip_python)):
        root = tmp_path / run
        roots.append(root)
        closure = root / "wheelhouse"
        closure.mkdir(parents=True)
        _console_script_wheel(closure)
        (closure / "closure.requirements.txt").write_text(
            "consoler==1.2.3\n", encoding="utf-8"
        )
        entrypoint = root / "runtime_worker.py"
        entrypoint.write_text(
            "def handle(method, params):\n    return {}\n", encoding="utf-8"
        )
        payload = expand_closure(
            closure=closure,
            destination=root / "payload",
            extra_files={"runtime_worker.py": entrypoint},
            python_executable=interpreter,
        )
        script = payload / "bin" / "consoler"
        assert script.is_file(), "pip did not write the console script"
        runtime, pin = _runtime(root)
        results.append(
            package_release(
                identity=_identity(),
                closure=closure,
                payload=payload,
                output=root / "out",
                catalog_sequence=182,
                issued_at=issued,
                expires_at=expires,
                python_runtime=runtime,
                python_runtime_pin=pin,
            )
        )
        # Rewritten by `package_release`, which is where the release's own pin
        # is parsed and therefore the only place the honest version exists.
        first_line = script.read_bytes().split(b"\n", 1)[0]
        assert first_line.startswith(b"#!python"), first_line[:120]
        assert b"/" not in first_line, first_line[:120]
    first, second = results
    assert first.manifest["artifact_sha256"] == second.manifest["artifact_sha256"]
    assert first.manifest == second.manifest
    assert first.pins == second.pins
    # And no member anywhere carries either build root.
    for result, root in zip(results, roots):
        with zipfile.ZipFile(result.artifact) as archive:
            for name in archive.namelist():
                payload_bytes = archive.read(name)
                for other in roots:
                    assert str(other).encode() not in payload_bytes, (name, other)


def test_the_record_still_describes_the_rewritten_script(
    tmp_path: Path, real_pip_python: Path
) -> None:
    """Rewriting rather than deleting only holds if RECORD is rewritten too."""

    root = tmp_path / "record"
    closure = root / "wheelhouse"
    closure.mkdir(parents=True)
    _console_script_wheel(closure)
    (closure / "closure.requirements.txt").write_text(
        "consoler==1.2.3\n", encoding="utf-8"
    )
    entrypoint = root / "runtime_worker.py"
    entrypoint.write_text(
        "def handle(method, params):\n    return {}\n", encoding="utf-8"
    )
    payload = expand_closure(
        closure=closure,
        destination=root / "payload",
        extra_files={"runtime_worker.py": entrypoint},
        python_executable=real_pip_python,
    )
    runtime, pin = _runtime(root)
    package_release(
        identity=_identity(),
        closure=closure,
        payload=payload,
        output=root / "out",
        catalog_sequence=182,
        issued_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        python_runtime=runtime,
        python_runtime_pin=pin,
    )
    script = payload / "bin" / "consoler"
    body = script.read_bytes()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(body).digest())
        .decode("ascii")
        .rstrip("=")
    )
    records = sorted(payload.glob("*.dist-info/RECORD"))
    assert records
    entries = [
        line
        for record in records
        for line in record.read_text(encoding="utf-8").splitlines()
        if line.split(",")[0].endswith("bin/consoler")
    ]
    assert entries, "RECORD does not mention the console script"
    for line in entries:
        _path, digest, size = line.split(",")
        assert digest == f"sha256={expected}"
        assert int(size) == len(body)


# --- ADJ-19: the shebang version is the release's, not the builder's --------


_BuilderVersion = namedtuple(
    "_BuilderVersion", "major minor micro releaselevel serial"
)


@contextlib.contextmanager
def _builder_python(major: int, minor: int) -> Iterator[None]:
    """Stand the packaging host on a different CPython for the duration.

    `sys.version_info` was the one channel through which the *builder's* own
    version reached the payload, so moving it is the honest way to prove that it
    no longer does. Two real interpreters cannot prove it here: the fix's own
    regression test seeds two venvs from one uv-managed interpreter, so its two
    "different builder Pythons" report the same `major.minor`.
    """

    original = sys.version_info
    # `sys.version_info` is a structseq and refuses instantiation, so the stand-in
    # is a namedtuple with the same fields — every read this module ever made was
    # `.major` and `.minor`.
    sys.version_info = _BuilderVersion(major, minor, 0, "final", 0)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.version_info = original  # type: ignore[assignment]


def _console_script_payload(root: Path, *, shebang: str) -> Path:
    """A payload in the shape pip leaves: a `bin/` script and a RECORD for it.

    Hand-built rather than pip-built because what is under test is *which*
    version the rewrite reads. That pip writes an absolute build-root shebang at
    all is measured on real pip above.
    """

    payload = _payload(root)
    scripts = payload / "bin"
    scripts.mkdir(parents=True, exist_ok=True)
    body = f"{shebang}\nfrom consoler import main\nmain()\n".encode()
    (scripts / "consoler").write_bytes(body)
    dist_info = payload / "consoler-1.2.3.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "RECORD").write_text(
        "../../bin/consoler,sha256=stale,7\n"
        "consoler-1.2.3.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    return payload


def _packaged_shebang(result: PackagedRelease) -> bytes:
    with zipfile.ZipFile(result.artifact) as archive:
        return archive.read("bin/consoler").split(b"\n", 1)[0]


def _package_console_script(root: Path, **overrides: object) -> PackagedRelease:
    payload = _console_script_payload(
        root, shebang="#!/tmp/builder/venv/bin/python3.14"
    )
    arguments: dict[str, object] = {
        "identity": _identity(),
        "closure": _closure(root, ["idna-3.7-py3-none-any.whl"]),
        "payload": payload,
        "output": root / "out",
        "catalog_sequence": 182,
        "issued_at": datetime(2026, 8, 5, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 9, 5, tzinfo=timezone.utc),
    }
    if "python_runtime" not in overrides:
        # Built lazily, like `_package`: a caller handing in its own mutated pin
        # must not be quietly overwritten by a pristine one.
        runtime, pin = _runtime(root)
        arguments["python_runtime"] = runtime
        arguments["python_runtime_pin"] = pin
    arguments.update(overrides)
    return package_release(**arguments)  # type: ignore[arg-type]


def test_the_shebang_version_is_read_from_the_release_pin(tmp_path: Path) -> None:
    """⟦ADJ-19⟧ The shebang is payload bytes, so it is `artifact_sha256`.

    It was written from `_interpreter_version`, which returned the *builder's*
    `sys.version_info` whenever `--python-executable` was absent. The pin the
    release already ships carries `"version": "3.11.15"`, and that is the only
    interpreter version a release can honestly claim about itself.
    """

    result = _package_console_script(tmp_path / "pinned")

    assert _packaged_shebang(result) == b"#!python3.11"
    assert result.manifest["worker_runtime"]["python_version"] == "3.11.15"


def test_the_artifact_digest_does_not_move_with_the_builders_python(
    tmp_path: Path,
) -> None:
    """Byte-identical inputs on two builder CPythons, one `artifact_sha256`.

    This is the finding's requested test — the same pinned closure and the same
    vendored archive packaged on a 3.12 host and a 3.13 host — with the builder
    varied through `sys.version_info` rather than through two installed
    interpreters, because the shebang read that value and nothing else about the
    host.
    """

    digests: list[str] = []
    for run, (major, minor) in (("a", (3, 12)), ("b", (3, 13))):
        with _builder_python(major, minor):
            result = _package_console_script(tmp_path / run)
        assert _packaged_shebang(result) == b"#!python3.11"
        digests.append(str(result.manifest["artifact_sha256"]))
    first, second = digests
    assert first == second


def test_the_shebang_follows_the_pin_when_the_pin_moves(tmp_path: Path) -> None:
    """The other direction: a different pinned interpreter is a different release."""

    root = tmp_path / "moved"
    runtime, pin = _runtime(root)
    document = json.loads(pin.read_text(encoding="utf-8"))
    document["version"] = "3.12.9"
    pin.write_text(json.dumps(document), encoding="utf-8")

    moved = _package_console_script(root, python_runtime=runtime, python_runtime_pin=pin)
    pinned = _package_console_script(tmp_path / "unmoved")

    assert _packaged_shebang(moved) == b"#!python3.12"
    assert moved.manifest["artifact_sha256"] != pinned.manifest["artifact_sha256"]


def test_a_builder_interpreter_that_does_not_exist_is_a_packaging_error(
    tmp_path: Path,
) -> None:
    """⟦ADJ-19⟧ A typo'd `--python-executable` escaped as a bare OSError.

    `subprocess.run` raises `FileNotFoundError` before pip ever reports
    anything, so the operator saw a traceback from the standard library rather
    than this module's own typed refusal.
    """

    closure = _closure(tmp_path, ["idna-3.7-py3-none-any.whl"])

    with pytest.raises(PackagingError, match="closure expansion could not start"):
        expand_closure(
            closure=closure,
            destination=tmp_path / "payload",
            python_executable=tmp_path / "no" / "such" / "python3.11",
        )


def test_the_artifact_is_reproducible_under_real_pip(
    tmp_path: Path, real_pip_python: Path
) -> None:
    """Two full runs — real pip expansion, then packaging — agree on every pin.

    This is the property the manifest's `artifact_sha256` claims, proven on
    the production path. Without `--no-compile`, pip writes `__pycache__/*.pyc`
    into the target and the two runs digest differently (reproduced in the
    S3.1b adversarial review); an injected runner cannot observe that.
    """

    issued = datetime(2026, 8, 5, tzinfo=timezone.utc)
    expires = datetime(2026, 9, 5, tzinfo=timezone.utc)
    results: list[PackagedRelease] = []
    for run in ("a", "b"):
        root = tmp_path / run
        closure = root / "wheelhouse"
        closure.mkdir(parents=True)
        _real_wheel(closure)
        (closure / "closure.requirements.txt").write_text(
            "idna==3.7\n", encoding="utf-8"
        )
        entrypoint = root / "runtime_worker.py"
        entrypoint.write_text(
            "def handle(method, params):\n    return {}\n", encoding="utf-8"
        )
        payload = expand_closure(
            closure=closure,
            destination=root / "payload",
            extra_files={"runtime_worker.py": entrypoint},
            python_executable=real_pip_python,
        )
        assert (payload / "idna" / "__init__.py").is_file()
        runtime, pin = _runtime(root)
        results.append(
            package_release(
                identity=_identity(),
                closure=closure,
                payload=payload,
                output=root / "out",
                catalog_sequence=182,
                issued_at=issued,
                expires_at=expires,
                python_runtime=runtime,
                python_runtime_pin=pin,
            )
        )
    first, second = results
    assert first.pins == second.pins
    with zipfile.ZipFile(first.artifact) as archive:
        compiled = [item for item in archive.namelist() if item.endswith(".pyc")]
    assert compiled == []


# --- S3.2: the interpreter rides inside the release ------------------------
#
# The same rule as the OS floor, applied to a second field. `worker_runtime` is
# derived from the bytes packaged and from the vendor tool's committed pin;
# nothing about it can be typed, and packaging refuses inputs that disagree.


def test_the_runtime_archive_and_pin_ride_inside_the_artifact(tmp_path: Path) -> None:
    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])

    runtime = result.manifest["worker_runtime"]
    assert runtime == {
        "archive": "runtime/cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz",
        "archive_sha256": hashlib.sha256(SYNTHETIC_RUNTIME).hexdigest(),
        "interpreter_relative": "bin/python3.11",
        "python_version": "3.11.15",
    }
    assert set(result.manifest["worker_modules"]) == set(module_sources())
    with zipfile.ZipFile(result.artifact) as archive:
        names = set(archive.namelist())
        assert archive.read(runtime["archive"]) == SYNTHETIC_RUNTIME
        pin = json.loads(archive.read("runtime/pin.json"))
    # The pin travels with the archive so a slot can be audited without the
    # producer's workstation.
    assert names >= {runtime["archive"], "runtime/pin.json"}
    assert pin["archive"]["sha256"] == runtime["archive_sha256"]


def test_the_archive_digest_is_derived_from_the_bytes_packaged(tmp_path: Path) -> None:
    """Not from the source file, and not from the pin.

    The pin is checked against the source archive, but the manifest describes
    what the *artifact* carries. Digesting the zip member closes the gap between
    the two: no copy step can leave the manifest describing bytes the release
    does not contain.
    """

    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])
    relative = result.manifest["worker_runtime"]["archive"]
    with zipfile.ZipFile(result.artifact) as archive:
        packaged = archive.read(relative)

    assert result.manifest["worker_runtime"]["archive_sha256"] == (
        hashlib.sha256(packaged).hexdigest()
    )


def test_a_pin_that_names_other_bytes_is_refused(tmp_path: Path) -> None:
    archive, pin = _runtime(tmp_path)
    archive.write_bytes(b"different-runtime-bytes\n")

    with pytest.raises(PackagingError, match="does not match its pin"):
        _package(
            tmp_path,
            ["idna-3.7-py3-none-any.whl"],
            python_runtime=archive,
            python_runtime_pin=pin,
        )


def test_a_pin_for_a_different_archive_is_refused(tmp_path: Path) -> None:
    """A pin and an archive that merely sit in the same directory are not a pair."""

    archive, pin = _runtime(tmp_path)
    renamed = archive.with_name("cpython-3.11.15-cp311-macosx_11_0_x86_64.tar.gz")
    archive.rename(renamed)

    with pytest.raises(PackagingError, match="does not name the archive"):
        _package(
            tmp_path,
            ["idna-3.7-py3-none-any.whl"],
            python_runtime=renamed,
            python_runtime_pin=pin,
        )


@pytest.mark.parametrize("field", ["interpreter_path", "version", "archive"])
def test_an_incomplete_pin_is_refused(tmp_path: Path, field: str) -> None:
    archive, pin = _runtime(tmp_path)
    document = json.loads(pin.read_text())
    del document[field]
    pin.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PackagingError, match="does not describe an archive"):
        _package(
            tmp_path,
            ["idna-3.7-py3-none-any.whl"],
            python_runtime=archive,
            python_runtime_pin=pin,
        )


def test_a_payload_that_already_carries_a_runtime_directory_is_refused(
    tmp_path: Path,
) -> None:
    """Packaging places the runtime; it never merges into one already there.

    A payload arriving with its own `runtime/` is either a re-run over a previous
    output or a tree carrying something the vendor tool did not produce. Both are
    answered the same way, because packaging cannot tell them apart.
    """

    payload = _payload(tmp_path)
    (payload / "runtime").mkdir()
    (payload / "runtime" / "smuggled.tar.gz").write_bytes(b"not from the vendor tool\n")

    with pytest.raises(PackagingError, match="already carries a runtime/ directory"):
        _package(tmp_path, ["idna-3.7-py3-none-any.whl"], payload=payload)


def test_the_carried_runtime_survives_the_real_import_path(tmp_path: Path) -> None:
    """The archive reaches `content/` and the slot's own digest covers it.

    This is the whole identity argument of D-S3.2-2 in one assertion: the
    interpreter is an ordinary payload file, so `_tree_digest(content)` binds it
    with no new schema and no second trust root.
    """

    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])
    slot = _service(tmp_path, result).import_release(
        catalog=result.catalog,
        manifest=result.manifest,
        attestation=result.attestation,
        patch_ledger=result.patch_ledger,
        artifact=result.artifact,
    )

    carried = slot / "content" / result.manifest["worker_runtime"]["archive"]
    assert carried.is_file()
    assert hashlib.sha256(carried.read_bytes()).hexdigest() == (
        result.manifest["worker_runtime"]["archive_sha256"]
    )
    assert (slot / "content" / "runtime" / "pin.json").is_file()


def test_the_cli_threads_the_chosen_interpreter_into_the_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1 finding 1: `expand_closure` had the parameter and `main` had no flag.

    Without the flag the closure is expanded by whichever pip the packaging host
    happens to run, so a cp311 release could be built with a cp314 pip and only
    fail at launch. The wiring is pinned here; the real-pip test below proves the
    coupling matters.
    """

    import tools.package_hermes_release as tool

    recorded: dict[str, object] = {}

    def fake_expand(**kwargs: object) -> Path:
        recorded.update(kwargs)
        return _payload(tmp_path)

    def fake_package(**kwargs: object) -> PackagedRelease:
        recorded["packaged"] = kwargs
        return PackagedRelease(
            artifact=tmp_path / "artifact.zip",
            manifest={},
            catalog={},
            attestation={},
            patch_ledger={},
            pins={"manifest_sha256": "a" * 64},
        )

    monkeypatch.setattr(tool, "expand_closure", fake_expand)
    monkeypatch.setattr(tool, "package_release", fake_package)
    archive, pin = _runtime(tmp_path)
    entrypoint = tmp_path / "runtime_worker.py"
    entrypoint.write_text("def handle(method, params):\n    return {}\n", encoding="utf-8")
    patches = tmp_path / "patches.json"
    patches.write_text(json.dumps([_patch_entry()]), encoding="utf-8")

    assert (
        tool.main(
            [
                "--closure", str(_closure(tmp_path, ["idna-3.7-py3-none-any.whl"])),
                "--worker-entrypoint-source", str(entrypoint),
                "--output", str(tmp_path / "out"),
                "--release-id", "hermes-0.18.2",
                "--release-sequence", "182",
                "--distribution-version", "0.18.2",
                "--upstream-repository", "NousResearch/hermes-agent",
                "--upstream-tag", "v2026.7.7.2",
                "--upstream-commit", COMMIT,
                "--publisher", "pypi:NousResearch",
                "--workflow", "release.yml",
                "--python-range", ">=3.11,<3.12",
                "--adapter-protocol", "0.1",
                "--session-schema", "13",
                "--evidence-sha256", "b" * 64,
                "--catalog-sequence", "182",
                "--python-runtime", str(archive),
                "--python-runtime-pin", str(pin),
                "--python-executable", "/opt/cp311/bin/python3.11",
                "--patches", str(patches),
            ]
        )
        == 0
    )

    assert recorded["python_executable"] == Path("/opt/cp311/bin/python3.11")
    assert recorded["packaged"]["python_runtime"] == archive
    assert recorded["packaged"]["python_runtime_pin"] == pin


def _patch_entry(**overrides: object) -> dict[str, object]:
    """One ledger entry in the shape `PatchEntry.from_dict` accepts."""

    entry: dict[str, object] = {
        "patch_id": "cortex-131bea608a4a-managed-worker",
        "source_commit": COMMIT,
        "patch_sha256": "c" * 64,
        "disposition": "required",
    }
    entry.update(overrides)
    return entry


def _cli_arguments(tmp_path: Path, *, patches: Path, output: Path) -> list[str]:
    """Every required flag, with the one under test parameterised.

    `--python-executable` stays explicit here for the same reason it is explicit
    on a real build: the closure is cp311 and the packaging host is not.
    """

    entrypoint = tmp_path / "runtime_worker.py"
    entrypoint.write_text("def handle(method, params):\n    return {}\n", encoding="utf-8")
    archive, pin = _runtime(tmp_path)
    return [
        "--closure", str(_closure(tmp_path, ["idna-3.7-py3-none-any.whl"])),
        "--worker-entrypoint-source", str(entrypoint),
        "--output", str(output),
        "--release-id", "hermes-0.18.2",
        "--release-sequence", "182",
        "--distribution-version", "0.18.2",
        "--upstream-repository", "NousResearch/hermes-agent",
        "--upstream-tag", "v2026.7.7.2",
        "--upstream-commit", COMMIT,
        "--publisher", "pypi:NousResearch",
        "--workflow", "release.yml",
        "--python-range", ">=3.11,<3.12",
        "--adapter-protocol", "cortex-worker/2",
        "--session-schema", "13",
        "--evidence-sha256", "b" * 64,
        "--catalog-sequence", "182",
        "--python-runtime", str(archive),
        "--python-runtime-pin", str(pin),
        "--python-executable", "/opt/cp311/bin/python3.11",
        "--patches", str(patches),
    ]


def test_the_cli_builds_a_release_whose_ledger_satisfies_clause_nine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main` registered no way to pass `patches`, so no CLI build could certify.

    `package_release` defaults `patches` to `()` and
    `prove_patch_ledger_dispositions` passes only on `bool(patches)`, so every
    release built through the command failed §5.9 — which is why the gen 9 build
    had to drive the packaging API directly instead. The producer runs for real
    here (only the pip expansion is stubbed) and the emitted directory is read
    back through the consumer's own proof rather than through this test's idea
    of the file.
    """

    import tools.package_hermes_release as tool
    from cortex_platform.product.runtime_update.certification import (
        ArtifactSet,
        prove_artifact_set_is_consistent,
        prove_patch_ledger_dispositions,
    )

    monkeypatch.setattr(
        tool, "expand_closure", lambda **kwargs: _payload(tmp_path)
    )
    document = tmp_path / "patches.json"
    document.write_text(
        json.dumps(
            [
                _patch_entry(),
                _patch_entry(
                    patch_id="cortex-0c859a1c044c-upstreamed-framing",
                    patch_sha256="d" * 64,
                    disposition="upstreamed",
                ),
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    assert tool.main(_cli_arguments(tmp_path, patches=document, output=output)) == 0

    artifacts = ArtifactSet.load(output)
    proof, divergence = prove_patch_ledger_dispositions(artifacts)
    assert proof.passed, proof.detail
    assert proof.evidence["patches"] == 2
    assert proof.evidence["dispositions"] == {"required": 1, "upstreamed": 1}
    assert proof.evidence["upstream_commit"] == COMMIT
    assert divergence == 1
    # §5.1 is what binds the ledger to the manifest: `patch_set_sha256` is
    # derived from the entries, so a release whose ledger is swapped afterwards
    # fails there rather than passing quietly here.
    assert prove_artifact_set_is_consistent(artifacts).passed


def test_the_cli_accepts_a_ledger_document_a_previous_run_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-package is handed the ledger of the release it supersedes.

    `patch_ledger.json` is the shape that already exists beside every packaged
    release, so `--patches` reads the whole document as readily as a bare list.
    """

    import tools.package_hermes_release as tool

    monkeypatch.setattr(tool, "expand_closure", lambda **kwargs: _payload(tmp_path))
    document = tmp_path / "patch_ledger.json"
    document.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "hermes-0.18.1",
                "upstream_commit": COMMIT,
                "patches": [_patch_entry()],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    assert tool.main(_cli_arguments(tmp_path, patches=document, output=output)) == 0

    ledger = json.loads((output / "patch_ledger.json").read_text(encoding="utf-8"))
    assert [entry["patch_id"] for entry in ledger["patches"]] == [
        "cortex-131bea608a4a-managed-worker"
    ]
    # The ledger the release carries is this release's, not the superseded one's.
    assert ledger["release_id"] == "hermes-0.18.2"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param([], "no entries", id="empty"),
        pytest.param(
            [_patch_entry(disposition="backported")],
            "invalid patch entry",
            id="disposition the consumer refuses",
        ),
        pytest.param(
            [_patch_entry(source_commit="131bea608a4a")],
            "invalid patch entry",
            id="abbreviated commit",
        ),
        pytest.param({"patches": "cortex-1"}, "list of patch entries", id="not a list"),
    ],
)
def test_the_cli_refuses_a_patch_document_before_it_runs_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: object,
    message: str,
) -> None:
    """A ledger the consumer would reject costs a parse, not a closure install.

    Validation through `PatchEntry` is the same rule `package_release` follows
    for the manifest and the catalog: the producer may not emit a document its
    consumer refuses. Running it before the expansion is what keeps a typo from
    costing several minutes of pip.
    """

    import tools.package_hermes_release as tool

    def refuse_expansion(**kwargs: object) -> Path:
        raise AssertionError("the expansion ran despite an invalid patch document")

    monkeypatch.setattr(tool, "expand_closure", refuse_expansion)
    patches = tmp_path / "patches.json"
    patches.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "out"

    with pytest.raises(PackagingError, match=message):
        tool.main(_cli_arguments(tmp_path, patches=patches, output=output))
    assert not output.exists()


@pytest.fixture(scope="session")
def vendored_cp311(
    vendored_worker_runtime: tuple[Path, Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """The real vendored cp311 tree, expanded once and reused.

    Plain `tarfile` rather than `stage_python_runtime`: what this suite needs is
    a working pip on 3.11, and the staging kernel's own guarantees are proven in
    `tests/product/runtime_update` where they belong.
    """

    archive, _pin = vendored_worker_runtime
    tree = tmp_path_factory.mktemp("cp311") / "tree"
    with tarfile.open(archive, "r:gz") as unpacked:
        unpacked.extractall(tree, filter="data")
    interpreter = tree / "bin" / "python3.11"
    probe = subprocess.run(
        [str(interpreter), "-m", "pip", "--version"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.fail(f"the vendored cp311 runtime cannot run pip: {probe.stderr[:300]}")
    return interpreter


def _cp311_only_wheel(closure: Path) -> Path:
    """A wheel only a 3.11 pip will install.

    `Requires-Python` is the cheapest honest coupling: no fabricated extension
    module, and pip enforces it before it touches the target directory.
    """

    dist_info = "cp311only-1.0.dist-info"
    wheel = closure / "cp311only-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("cp311only/__init__.py", 'VERSION = "1.0"\n')
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: cp311only\nVersion: 1.0\n"
            "Requires-Python: >=3.11,<3.12\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"cp311only/__init__.py,,\n{dist_info}/METADATA,,\n"
            f"{dist_info}/WHEEL,,\n{dist_info}/RECORD,,\n",
        )
    return wheel


def test_a_cp311_closure_is_expanded_by_the_vendored_cp311_pip(
    tmp_path: Path, vendored_cp311: Path, vendored_worker_runtime: tuple[Path, Path]
) -> None:
    """The acceptance finding, closed on real artifacts rather than argued.

    The closure declares `Requires-Python: >=3.11,<3.12`, so the running
    interpreter — 3.14 in this repository's own venv — cannot expand it and the
    vendored 3.11 can. Then the real vendored archive is packaged and imported,
    and the slot's `content/` carries the exact interpreter the manifest names.
    """

    archive, pin = vendored_worker_runtime
    closure = tmp_path / "wheelhouse"
    closure.mkdir()
    _cp311_only_wheel(closure)
    (closure / "closure.requirements.txt").write_text("cp311only==1.0\n", encoding="utf-8")
    entrypoint = tmp_path / "runtime_worker.py"
    entrypoint.write_text("def handle(method, params):\n    return {}\n", encoding="utf-8")

    with pytest.raises(PackagingError, match="closure expansion failed"):
        expand_closure(
            closure=closure,
            destination=tmp_path / "wrong-payload",
            extra_files={"runtime_worker.py": entrypoint},
        )

    payload = expand_closure(
        closure=closure,
        destination=tmp_path / "payload",
        extra_files={"runtime_worker.py": entrypoint},
        python_executable=vendored_cp311,
    )
    assert (payload / "cp311only" / "__init__.py").is_file()

    result = package_release(
        identity=_identity(python_range=">=3.11,<3.12"),
        closure=closure,
        payload=payload,
        output=tmp_path / "out",
        catalog_sequence=182,
        issued_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        python_runtime=archive,
        python_runtime_pin=pin,
    )
    assert result.manifest["worker_runtime"]["python_version"] == "3.11.15"

    slot = _service(tmp_path, result).import_release(
        catalog=result.catalog,
        manifest=result.manifest,
        attestation=result.attestation,
        patch_ledger=result.patch_ledger,
        artifact=result.artifact,
    )
    carried = slot / "content" / result.manifest["worker_runtime"]["archive"]
    assert carried.read_bytes() == archive.read_bytes()


# --- S3.2: the worker-side modules ride inside the release too --------------


def test_the_worker_modules_are_copied_from_the_product_sources(
    tmp_path: Path,
) -> None:
    """The S3.5 residual, asserted rather than assumed.

    The worker protocol is the product's contract. If a fork could ship its own
    copy of these modules it could answer its own identity questions, so the
    packaged bytes are required to equal the product's sources exactly — and the
    digests the manifest records are re-read out of the artifact, so the map
    describes what the release carries rather than what the copy started from.
    """

    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])

    recorded = result.manifest["worker_modules"]
    assert set(recorded) == set(module_sources())
    with zipfile.ZipFile(result.artifact) as archive:
        for relative, source in module_sources().items():
            packaged = archive.read(relative)
            assert packaged == source.read_bytes()
            assert recorded[relative] == hashlib.sha256(packaged).hexdigest()


def test_a_payload_that_already_carries_a_worker_package_is_refused(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    (payload / "cortex_worker").mkdir()
    (payload / "cortex_worker" / "serve.py").write_text("# smuggled\n", encoding="utf-8")

    with pytest.raises(PackagingError, match="already carries a cortex_worker/ directory"):
        _package(tmp_path, ["idna-3.7-py3-none-any.whl"], payload=payload)


def test_the_worker_modules_survive_into_the_slot(tmp_path: Path) -> None:
    """`import_release` puts them beside the entrypoint that imports them.

    ⟦AMD-6⟧'s bootstrap resolves the entrypoint's own directory, so "beside" is
    the whole contract: the modules must land in `content/`, under the same
    content-tree digest the slot attests.
    """

    result = _package(tmp_path, ["idna-3.7-py3-none-any.whl"])
    slot = _service(tmp_path, result).import_release(
        catalog=result.catalog,
        manifest=result.manifest,
        attestation=result.attestation,
        patch_ledger=result.patch_ledger,
        artifact=result.artifact,
    )

    for relative, digest in result.manifest["worker_modules"].items():
        carried = slot / "content" / relative
        assert carried.is_file()
        assert hashlib.sha256(carried.read_bytes()).hexdigest() == digest


def test_the_product_entrypoint_is_a_usable_worker_entrypoint(tmp_path: Path) -> None:
    """The attested entrypoint the launch contract executes.

    Packaging does not own the entrypoint — an operator names its source — but
    the product ships one, and it has to satisfy both halves of the contract at
    once: `handle` for the updater's v1 activation probe, and a `__main__` that
    bootstraps `sys.path` from its own resolved directory before importing
    `cortex_worker`.
    """

    source = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
    result = _package(
        tmp_path,
        ["idna-3.7-py3-none-any.whl"],
        payload=_entrypoint_payload(tmp_path / "product-entrypoint"),
    )

    with zipfile.ZipFile(result.artifact) as archive:
        assert archive.read("runtime_worker.py").decode("utf-8") == source
    assert "def handle(" in source
    assert "sys.path.insert(0, directory)" in source
    assert "os.path.realpath(__file__)" in source
    assert "PYTHONPATH" not in source.split('"""')[2]


def _entrypoint_payload(root: Path) -> Path:
    payload = root / "payload"
    (payload / "hermes_agent").mkdir(parents=True)
    (payload / "hermes_agent" / "__init__.py").write_text("", encoding="utf-8")
    (payload / "runtime_worker.py").write_bytes(ENTRYPOINT_SOURCE.read_bytes())
    return payload


def test_the_certification_harness_reads_the_ledger_this_producer_writes() -> None:
    """The two halves of D5 name the same file.

    Packaging lives in `tools/` and certification lives in the wheel, so the
    only place they can be compared is a test directory that sees both. Until
    the lanes merged they disagreed — the harness looked for a hyphenated
    `patch-ledger.json` — and no suite noticed, because the harness's own tests
    synthesize the artifact directory and the acceptance driver builds the
    `ArtifactSet` in process. A real packaged release read as having no ledger.
    """

    from cortex_platform.product.runtime_update.certification import (
        PATCH_LEDGER_NAME,
    )
    from tools.package_hermes_release import PATCH_LEDGER_DOCUMENT

    assert PATCH_LEDGER_NAME == f"{PATCH_LEDGER_DOCUMENT}.json"
