"""The pre-window transport capability assertion, as a supported command.

The P5 window procedure opens with one predicate: *outside any window, offline,
the active slot's worker answers `telegram.capabilities` with
`cortex.telegram.transport/1`*. Its whole purpose is that a release which was
never repackaged for P5.3 is found before the operator's research bot is taken
down, not after. The P6 cutover preflight already refuses to proceed without the
record this writes -- and until now nothing in the product could produce it, so
the preflight's own failure text pointed at the gap ("no CLI issues
telegram.capabilities").

Read-only by construction. The descriptor comes from `build_active_descriptor`,
which writes no attempt row; the worker is launched, asked one ungated question
and closed; and no transport window is opened, so `worker_environment` binds no
credential and the worker could not send anything even if it wanted to.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from ..transports.worker_rpc import (
    TELEGRAM_TRANSPORT_PROTOCOL,
    TransportCapabilityUnavailable,
    assert_transport_capability,
)
from .models import digest_document
from .service import RuntimeUpdateService
from .supervisor import WorkerSupervisorV2
from .worker_launch import (
    WorkerLaunchError,
    build_active_descriptor,
    descriptor_document,
)

#: The filename and location `deployment/cutover/lib.sh` derives as
#: `CUTOVER_CAPABILITY_RECORD="$CUTOVER_RECORD_DIR/capability-assertion.json"`,
#: with `CUTOVER_RECORD_DIR` defaulting to `$HOME/.local/state/cortex/cutover`.
#: Both halves are overridable there and `--output` is overridable here, so an
#: operator who moved the record directory passes the same path twice.
CAPABILITY_RECORD_RELATIVE = ".local/state/cortex/cutover/capability-assertion.json"

#: The preflight parses this with `time.strptime(..., "%Y-%m-%dT%H:%M:%SZ")`, so
#: the record is written in exactly that shape rather than in `isoformat()`,
#: which would carry an offset the preflight cannot read.
_INSTANT = "%Y-%m-%dT%H:%M:%SZ"


class CapabilityAssertionError(RuntimeError):
    """The assertion could not be attempted at all."""


def default_capability_record(home: Path) -> Path:
    return home / CAPABILITY_RECORD_RELATIVE


def assert_active_transport_capability(
    service: RuntimeUpdateService,
    *,
    output: Path,
    supervisor_factory: Callable[..., object] = WorkerSupervisorV2,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[int, dict[str, object]]:
    """Ask the ACTIVE slot the one question, record the answer, return an exit.

    A failed assertion is still recorded. The preflight reads `capability` and
    refuses anything that is not the expected protocol, so writing the refusal
    leaves the operator with evidence of *why* rather than with a stale success
    from the previous release -- which is the failure mode a record that is only
    written on success would create.
    """

    try:
        descriptor = build_active_descriptor(service)
        manifest = _slot_manifest(
            descriptor.slot_path, descriptor.expected_manifest_sha256
        )
    except (WorkerLaunchError, CapabilityAssertionError) as exc:
        # The promise above applies to these two paths as well. They run before
        # a descriptor exists, so they used to escape as exceptions and write
        # nothing -- leaving the PREVIOUS release's success on disk, which is
        # the file the P6 preflight reads as the current answer. The record
        # carries no `release_id`/`slot` here because none was resolved, which
        # is itself the honest answer.
        failure: dict[str, object] = {
            "schema_version": 1,
            "asserted_at": now().strftime(_INSTANT),
            "result": "unavailable",
            "capability": None,
            "reason": type(exc).__name__,
            "detail": str(exc),
        }
        _write_record(output, failure)
        return 1, failure
    record: dict[str, object] = {
        "schema_version": 1,
        "asserted_at": now().strftime(_INSTANT),
        "slot": descriptor.slot_id,
        "release_id": descriptor.release_id,
        "manifest_sha256": descriptor.expected_manifest_sha256,
        # ⟦D-P5-1⟧'s honest cost is a NEW release, because `worker_modules`
        # digests `cortex_worker` bytes into the manifest. Recording them here
        # names the exact bytes that answered, so a later question about which
        # worker was asserted is answered by the record and not by memory.
        "worker_modules": dict(manifest.get("worker_modules") or {}),
    }
    directory = Path(tempfile.mkdtemp(prefix="cortex-capability-"))
    try:
        descriptor_path = directory / "descriptor.json"
        descriptor_path.write_text(
            json.dumps(descriptor_document(descriptor), sort_keys=True),
            encoding="utf-8",
        )
        supervisor = supervisor_factory(descriptor_path)
        try:
            try:
                supervisor.start()
            except Exception as exc:  # noqa: BLE001 - a launch failure is an answer
                # A slot that cannot be launched at all fails the assertion
                # rather than escaping as an unrelated type: the operator asked
                # whether the active runtime can speak the transport, and "it
                # does not start" is a `no` the record should carry.
                raise TransportCapabilityUnavailable(
                    type(exc).__name__, detail=str(exc)
                ) from None
            answer = assert_transport_capability(supervisor)
        except TransportCapabilityUnavailable as exc:
            record |= {
                "result": "unavailable",
                "capability": None,
                "reason": exc.reason,
                "detail": exc.detail,
            }
            exit_code = 1
        else:
            record |= {
                "result": "ok",
                "capability": TELEGRAM_TRANSPORT_PROTOCOL,
                "capabilities": dict(answer),
            }
            exit_code = 0
        finally:
            # The release action of the whole procedure is an explicit
            # `close()`; a pre-window assertion that left a worker running would
            # be holding the very process step 5 has to prove is gone.
            supervisor.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    _write_record(output, record)
    return exit_code, record


def _slot_manifest(slot_path: Path, expected_digest: str) -> Mapping[str, object]:
    """Read the active slot's manifest and re-check it against the descriptor.

    The descriptor already carries `expected_manifest_sha256`, derived through
    the registry, so re-deriving the digest here costs one hash and makes the
    `worker_modules` map in the record attested rather than merely read.
    """

    try:
        raw = json.loads((slot_path / "manifest.json").read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityAssertionError("slot manifest is unreadable") from exc
    if not isinstance(raw, dict) or digest_document(raw) != expected_digest:
        raise CapabilityAssertionError("slot manifest digest mismatch")
    return raw


def _write_record(output: Path, record: Mapping[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.parent / f".{output.name}.tmp"
    handle = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=1)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, output)
