"""A release must say which host it can run on, and import must refuse others.

Schema 1 carried `python_range` and nothing about the platform, so a closure of
`macosx_14_0_arm64` wheels verified identically to a portable one and failed
later, at stage or launch, where the failure says least. Schema 2 records the
target the packaging command resolved against.

The check deliberately does not reimplement PEP 425 tag matching — this project
has already paid for a hand-written packaging layer once. It compares what
`distribution/bundle.py` already compares (`platform.system()` and
`platform.machine()`, exact equality) plus a minimum OS version, which is the
one thing the bundle's own check misses and F7 named concretely.
"""

from __future__ import annotations

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.models import (
    ReleaseManifest,
    ValidationError,
    host_platform,
)


def _manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": 3,
        "release_id": "hermes-0.18.2",
        "release_sequence": 182,
        "distribution_name": "hermes-agent",
        "distribution_version": "0.18.2",
        "upstream_repository": "NousResearch/hermes-agent",
        "upstream_tag": "v2026.7.7.2",
        "upstream_commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
        "artifact_filename": "hermes.zip",
        "artifact_sha256": "a" * 64,
        "publisher": "pypi:NousResearch",
        "workflow": "release.yml",
        "python_range": ">=3.11",
        "dependency_lock_sha256": "b" * 64,
        "adapter_protocol": "0.1",
        "session_schema": 13,
        "patch_set_sha256": "c" * 64,
        "evidence_sha256": "d" * 64,
        "worker_entrypoint": "runtime_worker.py",
        "worker_runtime": {
            "archive": "runtime/cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz",
            "archive_sha256": "e" * 64,
            "interpreter_relative": "bin/python3.11",
            "python_version": "3.11.15",
        },
        "worker_modules": {},
        "platform": {
            "system": "Darwin",
            "machine": "arm64",
            "minimum_os_version": "14.0",
        },
    }
    manifest.update(overrides)
    return manifest


def test_schema_1_is_no_longer_accepted() -> None:
    """Zero releases have ever been imported, so there is nothing to migrate.

    Verified on the production instance: its runtime-update root holds no
    slots, no registry and no manifest. Paying the bump now costs code; paying
    it after the first certified artifact costs a migration of recorded
    manifest digests, catalog entries and pinned attempts.
    """

    legacy = _manifest()
    legacy["schema_version"] = 1
    del legacy["platform"]
    with pytest.raises(ValidationError):
        ReleaseManifest.from_dict(legacy)


def test_a_platform_target_round_trips_through_the_digest() -> None:
    release = ReleaseManifest.from_dict(_manifest())
    assert release.platform.machine == "arm64"
    assert release.platform.minimum_os_version == "14.0"
    assert ReleaseManifest.from_dict(release.to_dict()).digest == release.digest


def test_a_portable_release_declares_no_minimum() -> None:
    release = ReleaseManifest.from_dict(
        _manifest(
            platform={
                "system": "Darwin",
                "machine": "arm64",
                "minimum_os_version": None,
            }
        )
    )
    assert release.platform.minimum_os_version is None


@pytest.mark.parametrize(
    "platform_value",
    [
        {"system": "Darwin", "machine": "arm64"},
        {"system": "Darwin", "machine": "arm64", "minimum_os_version": "14.0", "x": 1},
        {"system": "", "machine": "arm64", "minimum_os_version": None},
        {"system": "Darwin", "machine": "", "minimum_os_version": None},
        {"system": "Darwin", "machine": "arm64", "minimum_os_version": "14.0.x"},
        {"system": "Darwin", "machine": "arm64", "minimum_os_version": ""},
        {"system": "Darwin", "machine": "arm64", "minimum_os_version": 14},
        "Darwin-arm64",
    ],
)
def test_a_malformed_platform_target_is_refused(platform_value: object) -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest.from_dict(_manifest(platform=platform_value))


@pytest.mark.parametrize(
    "required,host,satisfied",
    [
        ("14.0", "14", True),
        ("14", "14.0", True),
        ("14.0", "14.0.0", True),
        ("14.0.0", "14.0", True),
        ("14.0", "14.1", True),
        ("14.0", "13.9", False),
        ("14.0", "14.0.1", True),
        ("14.1", "14.0.9", False),
        ("9.0", "10.0", True),
    ],
)
def test_version_components_compare_by_value_not_by_tuple_length(
    required: str, host: str, satisfied: bool
) -> None:
    """`(14,) < (14, 0)` in Python, so a bare major must not read as older.

    `platform.mac_ver()` normally reports major.minor, but a host that reports
    just "14" is at macOS 14 and satisfies a "14.0" floor. Comparing the raw
    tuples would refuse it.
    """

    from cortex_platform.product.runtime_update.models import PlatformTarget

    target = PlatformTarget("Darwin", "arm64", required)
    assert target.satisfied_by(
        {"system": "Darwin", "machine": "arm64", "minimum_os_version": host}
    ) is satisfied


def _service(root, catalog, attestation):
    import hashlib

    from cortex_platform.product.runtime_update.models import canonical_json
    from cortex_platform.product.runtime_update.service import (
        DigestPinVerifier,
        RuntimeUpdateService,
    )

    return RuntimeUpdateService(
        root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=FakePythonStager(),
        # S3.4/D6 gates activate and pin; these tests exercise
        # everything else, and say so rather than defaulting to it.
        approvals=AllowUnapprovedReleases(),
    )


def _foreign(kind: str) -> dict:
    """A target this host cannot satisfy, derived from this host.

    Hard-coding `x86_64` or `Linux` would make the case pass vacuously on the
    very machine it names — an Intel mac would *satisfy* the "foreign" x86_64
    target and the test would report a refusal that never happened.
    """

    host = host_platform()
    if kind == "system":
        return {**host, "system": "Linux" if host["system"] != "Linux" else "Darwin"}
    if kind == "machine":
        return {
            **host,
            "machine": "x86_64" if host["machine"] != "x86_64" else "arm64",
        }
    return {**host, "minimum_os_version": "999.0"}


@pytest.mark.parametrize("kind", ["system", "machine", "os_version"])
def test_import_refuses_a_release_this_host_cannot_run(
    tmp_path, release_factory, kind: str
) -> None:
    """Refuse at verification, where the reason is still legible.

    Without this the artifact imports, and the mismatch surfaces at stage or
    launch as a link error from a native wheel.
    """

    from cortex_platform.product.runtime_update.service import VerificationError

    artifact, manifest, catalog, attestation = release_factory(
        platform=_foreign(kind)
    )
    service = _service(tmp_path / "updates", catalog, attestation)
    with pytest.raises(VerificationError, match="platform"):
        service.import_release(
            catalog=catalog,
            manifest=manifest,
            attestation=attestation,
            artifact=artifact,
        )


def test_activation_rechecks_the_platform_of_an_already_imported_slot(
    tmp_path, release_factory, monkeypatch
) -> None:
    """Import is not the only door once state can move between machines.

    A runtime-update root restored from backup onto different hardware — which
    is exactly what this deployment's restic mirror makes possible — carries
    slots that were verified against the *old* host. Checking only at import
    would let such a slot activate with no refusal at all.
    """

    from cortex_platform.product.runtime_update import service as service_module
    from cortex_platform.product.runtime_update.service import ActivationError

    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])

    # The slot is on disk and verified; now the machine underneath it changes.
    moved = {**host_platform(), "machine": "totally-other-arch"}
    monkeypatch.setattr(service_module, "host_platform", lambda: moved)

    with pytest.raises(ActivationError, match="platform"):
        service.activate(manifest["release_id"], probe=lambda candidate: True)


def test_import_accepts_a_release_built_for_this_host(
    tmp_path, release_factory
) -> None:
    """The same path, with the only difference being the declared target."""

    artifact, manifest, catalog, attestation = release_factory()
    service = _service(tmp_path / "updates", catalog, attestation)
    slot = service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    assert slot.is_dir()


def test_host_platform_reports_this_machine() -> None:
    """The shape the manifest is compared against, read from the host."""

    observed = host_platform()
    assert set(observed) == {"system", "machine", "minimum_os_version"}
    assert observed["system"] and observed["machine"]


# --- schema 3: the interpreter the release carries -------------------------
#
# S3.2 (design D-S3.2-1, ⟦AMD-1⟧/⟦AMD-5⟧). `worker_runtime` names the vendored
# CPython archive that rides inside `content/`, and its two path fields are the
# only fields in this document that a stage-time join uses to build a filesystem
# path. Everything below is the refusal half: the grammar exists so that stage
# never joins a path the schema already accepted.


def _runtime(**overrides: object) -> dict[str, object]:
    runtime: dict[str, object] = {
        "archive": "runtime/cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz",
        "archive_sha256": "e" * 64,
        "interpreter_relative": "bin/python3.11",
        "python_version": "3.11.15",
    }
    runtime.update(overrides)
    return runtime


def test_schema_2_is_refused_rather_than_migrated() -> None:
    """The field a schema-2 release lacks is exactly the one that cannot be
    invented: nothing in a schema-2 document says which interpreter it needs.

    Two refusals, deliberately different. A genuine schema-2 document is caught
    by the closed field set before the version is ever read, because it does not
    carry `worker_runtime` at all; only a schema-3-shaped document wearing a
    schema-2 number reaches the version check. Both are refusals — the point of
    pinning both is that the first message names the missing evidence rather
    than the number, which is the more useful thing for an operator to read.
    """

    genuine = _manifest()
    genuine["schema_version"] = 2
    del genuine["worker_runtime"]
    del genuine["worker_modules"]
    with pytest.raises(ValidationError, match="release manifest fields"):
        ReleaseManifest.from_dict(genuine)

    with pytest.raises(ValidationError, match="unsupported release manifest schema"):
        ReleaseManifest.from_dict(_manifest(schema_version=2))


def test_a_schema_3_release_round_trips_through_the_digest() -> None:
    release = ReleaseManifest.from_dict(_manifest())

    assert release.schema_version == 3
    assert release.worker_runtime.python_version == "3.11.15"
    assert release.worker_runtime.interpreter_relative == "bin/python3.11"
    assert release.worker_modules == ()
    assert release.to_dict()["worker_modules"] == {}
    assert ReleaseManifest.from_dict(release.to_dict()).digest == release.digest


def test_the_worker_runtime_object_is_closed() -> None:
    for value in (
        {**_runtime(), "size": 1},
        {key: value for key, value in _runtime().items() if key != "archive"},
        "runtime/python.tar.gz",
        None,
    ):
        with pytest.raises(ValidationError, match="worker runtime fields"):
            ReleaseManifest.from_dict(_manifest(worker_runtime=value))


@pytest.mark.parametrize(
    "field", ["archive", "interpreter_relative"]
)
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("/etc/passwd", "must be a relative path"),
        ("/", "must be a relative path"),
        ("..", "empty, current, or parent component"),
        ("../outside/python.tar.gz", "empty, current, or parent component"),
        ("runtime/../../outside", "empty, current, or parent component"),
        (".", "empty, current, or parent component"),
        ("./runtime/python.tar.gz", "empty, current, or parent component"),
        ("runtime//python.tar.gz", "empty, current, or parent component"),
        ("runtime/", "empty, current, or parent component"),
        ("runtime\\python.tar.gz", "must not contain a backslash"),
        ("C:\\python.tar.gz", "must not contain a backslash"),
        ("runtime/py\0thon.tar.gz", "must be a non-empty string"),
        ("", "must be a non-empty string"),
    ],
)
def test_the_path_grammar_refuses_every_escape(
    field: str, value: str, message: str
) -> None:
    """One case per refusal the AMD-5 grammar owes, on both path fields.

    `..` inside a longer path matters as much as a bare one: `posixpath.normpath`
    would happily collapse `runtime/../../outside` into a path that leaves
    `content/` entirely, and normalizing rather than refusing is how a traversal
    becomes a legal-looking manifest.
    """

    with pytest.raises(ValidationError, match=message):
        ReleaseManifest.from_dict(_manifest(worker_runtime=_runtime(**{field: value})))


@pytest.mark.parametrize(
    "version", ["3.11", "3", "3.11.15rc1", "3.11.x", "v3.11.15", "", "3.11.15.1"]
)
def test_an_unusable_python_version_is_refused(version: str) -> None:
    """The version selects the staging profile, so it must be a release the
    profile registry can be keyed by rather than free text."""

    with pytest.raises(ValidationError, match="python_version"):
        ReleaseManifest.from_dict(
            _manifest(worker_runtime=_runtime(python_version=version))
        )


def test_worker_modules_may_be_empty_but_never_absent() -> None:
    """S3.2 ships no worker modules; S3.3 fills the map. An absent key is still
    a refusal, so a release that forgot to declare its modules cannot pass for
    one that legitimately carries none."""

    absent = _manifest()
    del absent["worker_modules"]
    with pytest.raises(ValidationError, match="release manifest fields"):
        ReleaseManifest.from_dict(absent)

    release = ReleaseManifest.from_dict(_manifest(worker_modules={}))
    assert release.worker_modules == ()


def test_worker_modules_are_a_closed_path_to_digest_map() -> None:
    release = ReleaseManifest.from_dict(
        _manifest(
            worker_modules={
                "cortex_worker/ledger.py": "b" * 64,
                "cortex_worker/__init__.py": "a" * 64,
            }
        )
    )

    assert release.worker_modules == (
        ("cortex_worker/__init__.py", "a" * 64),
        ("cortex_worker/ledger.py", "b" * 64),
    )
    assert ReleaseManifest.from_dict(release.to_dict()).digest == release.digest


@pytest.mark.parametrize(
    ("modules", "message"),
    [
        ([], "worker_modules must be an object"),
        ("cortex_worker/ledger.py", "worker_modules must be an object"),
        ({"../ledger.py": "a" * 64}, "empty, current, or parent component"),
        ({"/ledger.py": "a" * 64}, "must be a relative path"),
        ({"pkg\\ledger.py": "a" * 64}, "must not contain a backslash"),
        ({"": "a" * 64}, "must be a non-empty string"),
        ({"ledger.py": "A" * 64}, "lowercase SHA-256"),
        ({"ledger.py": "abc"}, "lowercase SHA-256"),
        ({"ledger.py": 1}, "non-empty string"),
    ],
)
def test_worker_modules_refuse_an_unsafe_entry(modules: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ReleaseManifest.from_dict(_manifest(worker_modules=modules))
