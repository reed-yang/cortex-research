"""The pre-window capability assertion, against a stub worker.

The real-worker case -- a repackaged release answering over a real framed pipe,
and an S3.3-only release refusing -- is proved by the P5.3 transport acceptance,
because it needs a packaged release, a staged interpreter and a real `Popen`.
What is proved here is everything around that one frame: what gets recorded,
where, that the worker is closed, and that nothing is written into the updater.
"""

from __future__ import annotations

import hashlib
import json
import stat
import time
from pathlib import Path

import pytest

from .fake_python_stager import FakePythonStager

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.capability import (
    CAPABILITY_RECORD_RELATIVE,
    assert_active_transport_capability,
    default_capability_record,
)
from cortex_platform.product.runtime_update.models import canonical_json, digest_document
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
)
from cortex_platform.product.runtime_update.supervisor import WorkerProtocolError
from cortex_platform.product.runtime_update.worker_protocol import (
    PROTOCOL_V2,
    SlotInterpreterDescriptor,
)


class StubSupervisor:
    """A supervisor that loads a real descriptor and answers one method."""

    def __init__(self, descriptor_path: Path) -> None:
        # The descriptor is loaded exactly as `WorkerSupervisorV2` loads it, so
        # a document this command wrote wrong fails here too.
        self.descriptor = SlotInterpreterDescriptor.load(descriptor_path)
        self.started = False
        self.closed = False
        self.answer: object = {"protocol": "cortex.telegram.transport/1"}
        self.raises: BaseException | None = None
        self.start_raises: BaseException | None = None
        self.methods: list[str] = []

    def start(self) -> None:
        if self.start_raises is not None:
            raise self.start_raises
        self.started = True

    def request(self, method: str, params, *, timeout: float | None = None) -> object:
        self.methods.append(method)
        if self.raises is not None:
            raise self.raises
        return self.answer

    def close(self) -> None:
        self.closed = True


def _staged_service(tmp_path: Path, release_factory):
    # The real worker payload, not a fabricated `worker_modules` map: ⟦ADJ-20⟧
    # re-derives every declared module against the bytes beside it in the slot
    # and refuses any file the map does not name, so a release whose map does
    # not describe its own content never reaches `activate`.
    artifact, manifest, catalog, attestation = release_factory(
        real_worker_payload=True
    )
    manifest["adapter_protocol"] = PROTOCOL_V2
    catalog["payload"]["entries"][0]["manifest_sha256"] = digest_document(manifest)
    service = RuntimeUpdateService(
        tmp_path / "updates",
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(catalog["payload"])).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(attestation)).hexdigest()
        ),
        python_stager=FakePythonStager(),
        # ⟦ADJ-17⟧ Named explicitly: this file exercises the capability
        # assertion, not D6, and "no gate configured" stays fail-closed.
        approvals=AllowUnapprovedReleases(),
    )
    service.import_release(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        artifact=artifact,
    )
    service.stage(manifest["release_id"])
    service.activate(manifest["release_id"], probe=lambda _: True)
    return service, manifest


def test_a_repackaged_release_records_the_asserted_capability(
    tmp_path: Path,
    release_factory,
) -> None:
    service, manifest = _staged_service(tmp_path, release_factory)
    output = tmp_path / "cutover" / "capability-assertion.json"
    supervisors: list[StubSupervisor] = []

    def factory(descriptor_path: Path) -> StubSupervisor:
        supervisors.append(StubSupervisor(descriptor_path))
        return supervisors[-1]

    exit_code, record = assert_active_transport_capability(
        service, output=output, supervisor_factory=factory
    )

    assert exit_code == 0
    assert record["result"] == "ok"
    assert record["capability"] == "cortex.telegram.transport/1"
    assert record["release_id"] == manifest["release_id"]
    assert record["worker_modules"] == manifest["worker_modules"]
    assert record["manifest_sha256"] == digest_document(manifest)
    assert supervisors[0].methods == ["telegram.capabilities"]
    # The release action of the window procedure is an explicit close, and an
    # assertion that left a worker running would be holding the process step 5
    # has to prove is gone.
    assert supervisors[0].closed is True
    assert json.loads(output.read_text()) == record


def test_the_record_is_where_the_p6_preflight_reads_it(
    tmp_path: Path,
    release_factory,
) -> None:
    """`deployment/cutover/lib.sh` derives one path; this writes that path.

    The preflight parses `asserted_at` with `strptime("%Y-%m-%dT%H:%M:%SZ")` and
    refuses anything else as "not an ISO-8601 UTC instant", so the format is a
    contract with a shell script rather than a stylistic choice.
    """

    service, _manifest = _staged_service(tmp_path, release_factory)
    home = tmp_path / "home"
    output = default_capability_record(home)
    assert output == home / CAPABILITY_RECORD_RELATIVE

    _exit_code, record = assert_active_transport_capability(
        service, output=output, supervisor_factory=StubSupervisor
    )

    time.strptime(str(record["asserted_at"]), "%Y-%m-%dT%H:%M:%SZ")
    assert output.is_file()
    assert stat.S_IMODE(output.lstat().st_mode) == 0o600
    assert list(output.parent.iterdir()) == [output]


def test_an_s33_only_worker_is_recorded_as_unavailable(
    tmp_path: Path,
    release_factory,
) -> None:
    """The packaging gap, found offline instead of with the bot already down.

    An S3.3-era worker has no `telegram.capabilities` in its closed method set,
    raises `ProtocolViolation("worker method is invalid")` inside its own
    process, and answers a `protocol_violation` frame the product sees as a
    `WorkerProtocolError`. The refusal is recorded rather than dropped, because
    the alternative leaves the previous release's success on disk for the
    preflight to read.
    """

    service, _manifest = _staged_service(tmp_path, release_factory)
    output = tmp_path / "capability-assertion.json"
    output.write_text(json.dumps({"capability": "cortex.telegram.transport/1"}))
    supervisors: list[StubSupervisor] = []

    def factory(descriptor_path: Path) -> StubSupervisor:
        supervisor = StubSupervisor(descriptor_path)
        supervisor.raises = WorkerProtocolError("worker replied to an unknown request")
        supervisors.append(supervisor)
        return supervisor

    exit_code, record = assert_active_transport_capability(
        service, output=output, supervisor_factory=factory
    )

    assert exit_code == 1
    assert record["result"] == "unavailable"
    assert record["capability"] is None
    assert record["reason"] == "WorkerProtocolError"
    assert record["detail"] == "worker replied to an unknown request"
    assert supervisors[0].closed is True
    assert json.loads(output.read_text())["capability"] is None


def test_a_worker_answering_another_protocol_is_recorded_as_unavailable(
    tmp_path: Path,
    release_factory,
) -> None:
    service, _manifest = _staged_service(tmp_path, release_factory)

    def factory(descriptor_path: Path) -> StubSupervisor:
        supervisor = StubSupervisor(descriptor_path)
        supervisor.answer = {"protocol": "cortex.telegram.transport/2"}
        return supervisor

    exit_code, record = assert_active_transport_capability(
        service,
        output=tmp_path / "record.json",
        supervisor_factory=factory,
    )

    assert exit_code == 1
    assert record["reason"] == "protocol_mismatch"
    assert record["detail"] == "cortex.telegram.transport/2"


def test_a_slot_that_cannot_launch_is_an_answer_not_a_traceback(
    tmp_path: Path,
    release_factory,
) -> None:
    service, _manifest = _staged_service(tmp_path, release_factory)

    def factory(descriptor_path: Path) -> StubSupervisor:
        supervisor = StubSupervisor(descriptor_path)
        supervisor.start_raises = OSError("interpreter is unavailable")
        return supervisor

    exit_code, record = assert_active_transport_capability(
        service,
        output=tmp_path / "record.json",
        supervisor_factory=factory,
    )

    assert exit_code == 1
    assert record["reason"] == "OSError"
    assert record["detail"] == "interpreter is unavailable"


def test_the_assertion_writes_nothing_into_the_updater(
    tmp_path: Path,
    release_factory,
) -> None:
    """Read-only is the point: no attempt row, no pin, no journal entry."""

    service, _manifest = _staged_service(tmp_path, release_factory)
    before = {
        path.relative_to(service.paths.root).as_posix(): path.read_bytes()
        for path in sorted(service.paths.root.rglob("*"))
        if path.is_file()
    }

    assert_active_transport_capability(
        service,
        output=tmp_path / "record.json",
        supervisor_factory=StubSupervisor,
    )

    after = {
        path.relative_to(service.paths.root).as_posix(): path.read_bytes()
        for path in sorted(service.paths.root.rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert not service.paths.attempts.exists() or not list(
        service.paths.attempts.iterdir()
    )


def test_the_descriptor_never_survives_the_assertion(
    tmp_path: Path,
    release_factory,
) -> None:
    """The temporary descriptor is the only file this writes outside the record."""

    service, _manifest = _staged_service(tmp_path, release_factory)
    seen: list[Path] = []

    def factory(descriptor_path: Path) -> StubSupervisor:
        seen.append(descriptor_path)
        assert descriptor_path.is_file()
        return StubSupervisor(descriptor_path)

    assert_active_transport_capability(
        service,
        output=tmp_path / "record.json",
        supervisor_factory=factory,
    )

    assert not seen[0].exists()
    assert not seen[0].parent.exists()


def test_the_record_carries_no_credential_shaped_field(
    tmp_path: Path,
    release_factory,
) -> None:
    """D-P5-4: no credential reaches any file the product writes."""

    service, _manifest = _staged_service(tmp_path, release_factory)
    output = tmp_path / "record.json"

    _exit_code, _record = assert_active_transport_capability(
        service, output=output, supervisor_factory=StubSupervisor
    )

    text = output.read_text().lower()
    for forbidden in ("token", "secret", "credential", "api_key", "apikey"):
        assert forbidden not in text


@pytest.mark.parametrize("tamper", ["removed", "rewritten"])
def test_a_tampered_active_slot_is_refused_before_any_worker_is_launched(
    tmp_path: Path,
    release_factory,
    tamper: str,
) -> None:
    """The manifest is read twice, and the first read already refuses.

    `build_active_descriptor` resolves the active identity through the slot's
    own manifest, so a tampered slot fails there -- before a descriptor exists,
    before a process is spawned. `capability._slot_manifest` re-derives the
    digest afterwards anyway, which is what makes the `worker_modules` map in
    the record attested rather than merely read.
    """

    service, _manifest = _staged_service(tmp_path, release_factory)
    pointer = json.loads((service.paths.pointers / "active.json").read_bytes())
    manifest_path = service.paths.slots / pointer["slot_digest"] / "manifest.json"
    # `import_release` seals the slot, so tampering with it needs the write bit
    # back on the directory as well as on the file.
    manifest_path.parent.chmod(0o700)
    manifest_path.chmod(0o600)
    if tamper == "removed":
        manifest_path.unlink()
    else:
        manifest_path.write_text('{"schema_version": 3}')
    launched: list[Path] = []

    exit_code, record = assert_active_transport_capability(
        service,
        output=tmp_path / "record.json",
        supervisor_factory=lambda path: launched.append(path),  # type: ignore[arg-type,return-value]
    )

    assert launched == []
    # A failure BEFORE a descriptor exists still has to be recorded. Escaping
    # as an exception left the PREVIOUS release's success on disk, and the P6
    # preflight reads that file as the current answer.
    assert exit_code == 1
    assert record["result"] == "unavailable"
    assert record["capability"] is None
    written = json.loads((tmp_path / "record.json").read_text())
    assert written == record
    assert "active runtime is unavailable" in written["detail"]
