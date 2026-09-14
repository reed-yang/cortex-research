"""The release descriptor refuses a composition the artifacts do not support."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from distribution.release_record import (
    DESCRIPTOR_FORMAT_VERSION,
    RECORD_SCHEMA,
    ReleaseRecordError,
    build_release_record,
    check_sequence_available,
    find_release_records,
    load_descriptor,
    parse_descriptor,
    read_release_record,
    read_source_control_schema,
    read_worker_manifest,
    release_pointer_version,
    verify_worker_payload,
)


REPOSITORY = Path(__file__).resolve().parents[2]

DESCRIPTOR = {
    "format_version": DESCRIPTOR_FORMAT_VERSION,
    "product": {
        "version": "0.1.18",
        "release_id": "cortex-personal-17",
        "release_sequence": 17,
    },
    "control": {"target_schema": 18, "upgrade_from_schemas": [16, 17]},
    "worker": {
        "release_id": "hermes-0.15.0-gen9",
        "adapter_protocol": "cortex-worker/2",
    },
}

COMMIT = "b" * 40
DIGEST = "c" * 64

WORKER = {
    "release_id": "hermes-0.15.0-gen9",
    "release_sequence": 9,
    "distribution_name": "hermes-agent",
    "distribution_version": "0.15.0",
    "adapter_protocol": "cortex-worker/2",
    "session_schema": 13,
    "artifact_sha256": "a" * 64,
    "worker_modules": {"cortex_worker/__init__.py": "d" * 64},
}

PREVIOUS = {
    "release_id": "cortex-personal-16",
    "release_sequence": 16,
    "control_schema": 17,
    "bundle_digest": "e" * 64,
}


def _manifest(**overrides: object) -> dict[str, object]:
    manifest = {
        "release_id": "cortex-personal-17",
        "release_sequence": 17,
        "source": {"commit": COMMIT, "lock_sha256": "f" * 64},
    }
    manifest.update(overrides)
    return manifest


def _record(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "source_commit": COMMIT,
        "source_schema_version": 18,
        "declared_migrations": tuple(range(1, 19)),
        "bundle_path": Path("/tmp/bundle"),
        "bundle_manifest": _manifest(),
        "bundle_digest": DIGEST,
        "web_build_id": "cortex-personal-build-b940e09",
        "worker": WORKER,
        "previous": PREVIOUS,
    }
    arguments.update(overrides)
    return build_release_record(parse_descriptor(DESCRIPTOR), **arguments)


# -- the descriptor -------------------------------------------------------


def test_the_checked_in_descriptor_parses_and_names_this_release() -> None:
    """The one handwritten input, read as shipped.

    Product version, installation sequence, Control schema and worker release
    remain independently selected by the release descriptor.
    """

    descriptor = load_descriptor(REPOSITORY / "distribution" / "release.toml")

    assert descriptor.product_version == "0.1.20"
    assert descriptor.release_id == "cortex-research-20"
    assert descriptor.release_sequence == 20
    assert descriptor.target_schema == 19
    assert descriptor.upgrade_from_schemas == (16, 17, 18)
    assert descriptor.worker_release_id == "hermes-0.15.0-gen9"
    assert descriptor.adapter_protocol == "cortex-worker/2"


def test_the_descriptor_schema_is_closed() -> None:
    """An extra or missing key is a refusal, not a silently ignored field."""

    for section in ("product", "control", "worker"):
        extra = json.loads(json.dumps(DESCRIPTOR))
        extra[section]["unexpected"] = True
        with pytest.raises(ReleaseRecordError, match="schema is invalid"):
            parse_descriptor(extra)

    missing = json.loads(json.dumps(DESCRIPTOR))
    del missing["worker"]
    with pytest.raises(ReleaseRecordError, match="schema is invalid"):
        parse_descriptor(missing)


def test_an_unsupported_descriptor_format_is_refused() -> None:
    with pytest.raises(ReleaseRecordError, match="format is unsupported"):
        parse_descriptor({**DESCRIPTOR, "format_version": 2})


def test_upgrade_paths_must_sit_below_the_target_and_not_repeat() -> None:
    for upgrades, message in (
        ([16, 18], "below the target"),
        ([16, 19], "below the target"),
        ([16, 16], "repeats a schema"),
        ([], "is invalid"),
    ):
        with pytest.raises(ReleaseRecordError, match=message):
            parse_descriptor(
                {**DESCRIPTOR, "control": {**DESCRIPTOR["control"], "upgrade_from_schemas": upgrades}}
            )


def test_the_public_version_may_not_wear_the_installer_pointer_shape() -> None:
    """`0.1.18` and `cortex-personal-17-<digest16>` are different things.

    `distribution/install.py` derives the pointer's `version` from the release
    identifier and the bundle digest and refuses anything else, so a descriptor
    that spelled a pointer identity into the public version would be recording a
    generation directory as a product release.
    """

    pointer = release_pointer_version("cortex-personal-17", DIGEST)
    assert pointer == f"cortex-personal-17-{DIGEST[:16]}"

    with pytest.raises(ReleaseRecordError, match="public product version"):
        parse_descriptor(
            {**DESCRIPTOR, "product": {**DESCRIPTOR["product"], "version": pointer}}
        )


# -- derivation from the real source and artifacts ------------------------


def test_the_source_control_schema_is_read_without_importing_it() -> None:
    """The tree being released cannot be the authority on its own schema."""

    schema_version, declared = read_source_control_schema(REPOSITORY)

    from cortex_platform.product.control import schema

    assert schema_version == schema.SCHEMA_VERSION
    assert declared == schema.MIGRATION_VERSIONS


def test_the_worker_identity_comes_out_of_the_certified_manifest(
    tmp_path: Path,
) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "release_id": "hermes-0.15.0-gen9",
                "release_sequence": 9,
                "distribution_name": "hermes-agent",
                "distribution_version": "0.15.0",
                "adapter_protocol": "cortex-worker/2",
                "session_schema": 13,
                "artifact_sha256": "a" * 64,
                "worker_modules": {"cortex_worker/turn.py": "b" * 64},
                "ignored": "extra manifest keys are not this module's business",
            }
        )
    )

    worker = read_worker_manifest(tmp_path)

    assert worker["release_sequence"] == 9
    assert worker["session_schema"] == 13
    assert worker["distribution_version"] == "0.15.0"
    assert worker["worker_modules"] == {"cortex_worker/turn.py": "b" * 64}


def test_the_certified_worker_payload_is_checked_against_the_source(
    tmp_path: Path,
) -> None:
    """Every declared module, at the exact commit -- and the count is derived."""

    repository = tmp_path / "repository"
    payload = repository / "cortex_platform/product/runtime_update/worker_payload"
    payload.mkdir(parents=True)
    body = b"# certified\n"
    (payload / "turn.py").write_bytes(body)
    for command in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "payload"),
    ):
        subprocess.run(["git", "-C", str(repository), *command], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()

    worker = {
        **WORKER,
        "worker_modules": {"turn.py": hashlib.sha256(body).hexdigest()},
    }
    assert verify_worker_payload(repository, commit, worker) == 1

    tampered = {**worker, "worker_modules": {"turn.py": "0" * 64}}
    with pytest.raises(ReleaseRecordError, match="does not match the manifest"):
        verify_worker_payload(repository, commit, tampered)

    absent = {**worker, "worker_modules": {"missing.py": "0" * 64}}
    with pytest.raises(ReleaseRecordError, match="absent from the source"):
        verify_worker_payload(repository, commit, absent)


# -- the reconciliation ---------------------------------------------------


def test_the_record_carries_the_public_version_beside_the_pointer_identity() -> None:
    record = _record()

    assert record["schema"] == RECORD_SCHEMA
    assert record["product_version"] == "0.1.18"
    assert record["version"] == f"cortex-personal-17-{DIGEST[:16]}"
    assert record["product_version"] != record["version"]
    assert record["control_schema"] == 18
    assert record["upgrade_from_schemas"] == [16, 17]
    assert record["worker"] == {
        "release_id": "hermes-0.15.0-gen9",
        "release_sequence": 9,
        "distribution_name": "hermes-agent",
        "distribution_version": "0.15.0",
        "adapter_protocol": "cortex-worker/2",
        "session_schema": 13,
        "artifact_sha256": "a" * 64,
        "worker_module_count": 1,
    }
    assert record["previous_release_id"] == "cortex-personal-16"
    assert record["previous_control_schema"] == 17


def test_a_descriptor_that_disagrees_with_the_source_schema_is_refused() -> None:
    with pytest.raises(ReleaseRecordError, match="but the source declares 17"):
        _record(source_schema_version=17, declared_migrations=tuple(range(1, 18)))


def test_a_target_that_is_not_the_highest_migration_is_refused() -> None:
    with pytest.raises(ReleaseRecordError, match="highest migration"):
        _record(declared_migrations=tuple(range(1, 20)))


def test_an_upgrade_path_the_source_never_declared_is_refused() -> None:
    declared = tuple(version for version in range(1, 19) if version != 16)
    with pytest.raises(ReleaseRecordError, match="undeclared migrations"):
        _record(declared_migrations=declared)


def test_a_bundle_built_under_another_identity_is_refused() -> None:
    with pytest.raises(ReleaseRecordError, match="release identifier does not match"):
        _record(bundle_manifest=_manifest(release_id="cortex-personal-16"))
    with pytest.raises(ReleaseRecordError, match="release sequence does not match"):
        _record(bundle_manifest=_manifest(release_sequence=16))
    with pytest.raises(ReleaseRecordError, match="source commit does not match"):
        _record(bundle_manifest=_manifest(source={"commit": "a" * 40, "lock_sha256": "f" * 64}))


def test_a_different_worker_release_or_protocol_is_refused() -> None:
    with pytest.raises(ReleaseRecordError, match="worker manifest release identifier"):
        _record(worker={**WORKER, "release_id": "hermes-0.16.0-gen10"})
    with pytest.raises(ReleaseRecordError, match="adapter protocol"):
        _record(worker={**WORKER, "adapter_protocol": "cortex-worker/3"})


def test_a_predecessor_must_be_below_this_release_on_both_counters() -> None:
    with pytest.raises(ReleaseRecordError, match="sequence is not below"):
        _record(previous={**PREVIOUS, "release_sequence": 17})
    with pytest.raises(ReleaseRecordError, match="schema is not below"):
        _record(previous={**PREVIOUS, "control_schema": 19})
    with pytest.raises(ReleaseRecordError, match="not a declared upgrade path"):
        _record(previous={**PREVIOUS, "control_schema": 15})


def test_a_first_release_records_an_absent_predecessor_explicitly() -> None:
    record = _record(previous=None)

    assert record["previous_bundle_digest"] is None
    assert record["previous_release_sequence"] is None


# -- allocation -----------------------------------------------------------


def test_an_allocated_sequence_is_reported_not_reallocated(tmp_path: Path) -> None:
    """Report the collision; never guess a different installed identity."""

    build = tmp_path / "personal16-build"
    build.mkdir()
    (build / "release.json").write_text(
        json.dumps(
            {
                "source_commit": "0" * 40,
                "release_id": "cortex-personal-16",
                "release_sequence": 16,
                "control_schema": 17,
                "bundle": "/tmp/b",
                "bundle_digest": "1" * 64,
                "version": f"cortex-personal-16-{'1' * 16}",
                "web_build_id": "cortex-personal-build-0000000",
                "previous_bundle_digest": "2" * 64,
            }
        )
    )
    records = find_release_records([tmp_path])
    assert len(records) == 1

    check_sequence_available(parse_descriptor(DESCRIPTOR), records)

    taken = parse_descriptor(
        {**DESCRIPTOR, "product": {**DESCRIPTOR["product"], "release_sequence": 16}}
    )
    with pytest.raises(ReleaseRecordError, match="already allocated"):
        check_sequence_available(taken, records)

    renamed = parse_descriptor(
        {
            **DESCRIPTOR,
            "product": {**DESCRIPTOR["product"], "release_id": "cortex-personal-16"},
        }
    )
    with pytest.raises(ReleaseRecordError, match="already exists at sequence"):
        check_sequence_available(renamed, records)


def test_the_legacy_gen16_record_shape_stays_readable(tmp_path: Path) -> None:
    """The predecessor was written before this module existed."""

    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            {
                "source_commit": "631532dffaf4ab005eaeec2bd33c99775425f5f4",
                "release_id": "cortex-personal-16",
                "release_sequence": 16,
                "control_schema": 17,
                "bundle": "/tmp/bundle-personal-16",
                "bundle_digest": "5360380789e985a0ce2ab0b957d9793eb6c73b2b42b3214b04ad94b8bcce21a3",
                "version": "cortex-personal-16-5360380789e985a0",
                "web_build_id": "cortex-personal-build-631532d",
                "previous_bundle_digest": "114fda8fa890f5156bc36184c479c7f9c1519b24c912e24c65b48db8535b595e",
            }
        )
    )

    record = read_release_record(path)

    assert record["release_sequence"] == 16
    assert "schema" not in record
