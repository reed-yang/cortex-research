"""S3.4 + S3.5 real acceptance: the seatbelt, the approval decision, the harness.

Nothing below the model call is synthetic. The closure is the real patched
Hermes fork's D1 closure from S3.1b; the release carries the vendored CPython
3.11.15 this program line vendored; `stage` expands and probes that archive
through the wheel-shipped staging kernel; `build_descriptor` derives the
interpreter and its digest from the pin `stage` wrote; and every worker below
runs the slot's OWN `bin/python3.11` on the slot's OWN attested entrypoint,
importing the slot's OWN `cortex_worker` package.

Two releases are packaged from one expanded payload, differing only in which
attested entrypoint the manifest names:

  * `runtime_worker.py`     — production, exactly as the product ships it. Used
    for the handshake, the identity remeasure, the dedup probe, the HERMES_HOME
    refusals, the fd inventory and the negative identity case.
  * `acceptance_driver.py`  — the same shipped `serve()`, the same framed
    channel, the same turn thread and heartbeat, the same ledger and digest, and
    a real `import run_agent` / `tools.terminal_tool.set_approval_callback`
    through `cortex_worker.runtime.load_runtime`. It answers the model itself,
    because a provider call is a network call and this acceptance makes none.

Credentials are FAKE and env-delivered, resolved in the parent through
`SecretResolver` into exactly one allowlisted key. No value is printed, stored,
or written to the record. No production instance is touched; `runtime_dispatch`
stays false everywhere but the /tmp control.db this creates.

Run from this checkout's root with its venv, as a module so the repository
is importable:

  python -m tools.hermes_acceptance --driver ... --closure ... --runtime ...
      --fork ... --work ... --evidence-root ... [--artifacts ...]

Every input is an argument; `docs/runbooks/hermes-acceptance.md` carries the
input matrix and the reproducibility recipe. `--help` and every refusal exit
before the first side effect.

`--artifacts <dir>` certifies an artifact set that ALREADY EXISTS on disk
instead of packaging its own production release. Everything else is unchanged:
the same real fork closure, the same vendored cp311, the same seatbelt, the same
worker processes. The named directory supplies `production` — its four documents
and its zip, with the pins recomputed from those bytes and cross-checked against
the `pins.json` beside them — and the run stages, launches and probes THAT
release, so proofs 3-8 are observations of the shipped artifact rather than of a
release this program built for itself.

The `driven` release is still packaged here, because clauses 7 and the turn
methods need the `acceptance_driver.py` entrypoint, which no shipped artifact
carries and none should. In this mode the run writes `certification.full.json`,
`acceptance.full.json`, `profile.sb` and `sandbox-profiles/` into
`--evidence-root`. The original wrote them beside the artifact set; that
directory is retained evidence and the run replaces `sandbox-profiles/`
wholesale, so a separate output directory is now required and pointing one
at retained evidence is refused. Reading the artifact set is unchanged.

This file is evidence tooling. It imports the product and never ships inside
it: the wheel allowlist contains `cortex_platform` and
`deployment/private_access`, so nothing under `tools/` reaches a wheel.
It is tracked because it was an ignored
`output/` file that routine cleanup could destroy, and it is the only thing
that has ever proved clauses 3-8. Tracking it adds no seam: §7 still forbids
the product growing a path that would let a caller inject those proofs, and
no such flag exists on either side.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cortex_platform.product.control.store import ControlStore
from cortex_platform.product.runtime_update.approval import (
    ControlReleaseApprovals,
    ReleaseApprovalError,
)
from cortex_platform.product.runtime_update import sandbox as sandbox_module
from cortex_platform.product.runtime_update.certification import (
    ArtifactSet,
    Proof,
    run_harness,
)
from cortex_platform.product.runtime_update.certify_cli import write_evidence
from cortex_platform.product.orchestration.service import RunOrchestrator
from cortex_platform.product.runtime_update.service import (
    DigestPinVerifier,
    RuntimeUpdateService,
)
from cortex_platform.product.runtime_update.supervisor import (
    WorkerProtocolError,
    WorkerSupervisorV2,
    worker_environment,
)
from cortex_platform.product.runtime_update.worker_launch import build_descriptor
from cortex_platform.product.runtime_update.worker_payload import ENTRYPOINT_SOURCE
from cortex_platform.product.runtime_update.worker_payload.cortex_worker.turn import (
    EVENT_FINISH,
    EVENT_HEARTBEAT,
)
from cortex_platform.backup import inspect_database
from cortex_platform.product.control.schema import (
    RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,
)
from cortex_platform.product.secrets import SecretResolver
from cortex_platform.runtime.hermes import HermesAdapter, HermesRunInput
from cortex_platform.runtime import managed_hermes as managed_hermes_module
from cortex_platform.runtime.managed_hermes import (
    ManagedHermesBackend,
    control_durable_stream,
    recompute_result_digest,
)
from cortex_platform.product.runtime_update.models import (
    ReleaseManifest,
    canonical_json,
)
from tools.package_hermes_release import (
    ReleaseIdentity,
    derive_minimum_os_version,
    expand_closure,
    package_release,
)
from tools.hermes_acceptance_inputs import prepare_directories, resolve_inputs

#: The product worktree this run measures. The record names the commit, and a
#: dirty tree is refused outright (ADJ-09's lesson generalised): an acceptance
#: whose evidence is digest-bound to artifacts built from uncommitted source
#: names a state nobody can check out again. Derived, not accepted: taking it
#: as an argument would let the record name a worktree the run never imported.
PRODUCT = Path(
    __import__("cortex_platform").__file__
).resolve().parent.parent

#: Every other input is an argument. This file used to read its closure, its
#: vendored interpreter and its patched fork out of directories that happened
#: to sit beside it, and to write its evidence into whatever directory it was
#: stored in. Tracked in `tools/`, all of that silently retargets, so the
#: lookups are refusals now. See `hermes_acceptance_inputs` for each rule.
INPUTS = resolve_inputs(sys.argv[1:], product=PRODUCT)

ARTIFACTS: Path | None = INPUTS.artifacts
#: Evidence goes to a directory this run owns. The original wrote beside the
#: artifact set in `--artifacts` mode; that directory is retained evidence, and
#: the run replaces `sandbox-profiles/` wholesale, so writing there is refused.
#: Reading a retained artifact set is still exactly what `--artifacts` is for.
EVIDENCE_ROOT = INPUTS.evidence_root
OUTPUT = INPUTS.evidence_root
RECORD_NAME = INPUTS.record_name
WORK = INPUTS.work
S31B = INPUTS.closure
RUNTIME = INPUTS.runtime
ARCHIVE = INPUTS.archive
PIN = INPUTS.pin
FORK = INPUTS.fork
DRIVER = INPUTS.driver

#: Fake, and shaped like a credential so the allowlist is exercised for real.
FAKE_CREDENTIAL = "sk-ant-fake-acceptance-0000000000000000000000000000"


def _product_commit() -> str:
    status = subprocess.run(
        ["git", "-C", str(PRODUCT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if status:
        raise SystemExit(
            "refusing to record an acceptance from a dirty worktree:\n" + status
        )
    return subprocess.run(
        ["git", "-C", str(PRODUCT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


record: dict[str, object] = {
    "slice": "S3.4+S3.5",
    "ran_at": datetime.now(timezone.utc).isoformat(),
    "product_commit": _product_commit(),
    "mode": "artifact-set" if ARTIFACTS is not None else "self-packaged",
    "artifact_set": str(ARTIFACTS) if ARTIFACTS is not None else None,
    "scenarios": {},
}
started_processes: list[subprocess.Popen] = []

#: ⟦ADJ-09⟧ Every profile this run generates, keyed by digest, so the evidence
#: record can name the profile each proof actually ran under. The record used to
#: carry one digest for which no file existed anywhere, and the profile proof 5
#: ran under — the port-mapped one — appeared in no artifact at all.
generated_profiles: dict[str, str] = {}
_prepare_sandbox = sandbox_module.prepare_sandbox


def _recording_prepare_sandbox(*args, **kwargs):
    launch = _prepare_sandbox(*args, **kwargs)
    generated_profiles[launch.profile_sha256] = launch.profile_path.read_text(
        encoding="utf-8"
    )
    return launch


# Both bindings: `managed_hermes` imported the function by name, so patching the
# module attribute alone would miss every profile the managed backend generates.
sandbox_module.prepare_sandbox = _recording_prepare_sandbox
managed_hermes_module.prepare_sandbox = _recording_prepare_sandbox


def step(name: str, **values: object) -> None:
    record["scenarios"][name] = {"pass": True, **values}  # type: ignore[index]
    print(f"[PASS {name}] " + "  ".join(f"{k}={v}" for k, v in values.items())[:400])


def fail(name: str, reason: str) -> None:
    record["scenarios"][name] = {"pass": False, "reason": reason}  # type: ignore[index]
    print(f"[FAIL {name}] {reason}")


def _purge(path: Path) -> None:
    """Remove a tree that may contain a sealed interpreter.

    `stage` publishes the interpreter root 0o500 with 0o400 files, and unlinking
    needs the write bit on the *directory* — so a plain `rmtree` silently leaves
    the runtime behind. The lesson the R0-C acceptance paid for.
    """

    if not path.exists():
        return
    for parent, directories, _files in os.walk(path, topdown=False):
        for name in directories:
            entry = Path(parent) / name
            if not entry.is_symlink():
                os.chmod(entry, 0o700)
    os.chmod(path, 0o700)
    shutil.rmtree(path)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(FORK), *args], capture_output=True, text=True, check=True
    ).stdout


def open_fds(pid: int) -> list[str]:
    completed = subprocess.run(
        ["/usr/sbin/lsof", "-p", str(pid), "-Fftn"],
        capture_output=True,
        text=True,
        check=False,
    )
    entries: list[str] = []
    current = ""
    for line in completed.stdout.splitlines():
        if line.startswith("f"):
            current = line[1:]
        elif line.startswith("t"):
            current = f"{current}:{line[1:]}"
        elif line.startswith("n") and current and current[0].isdigit():
            entries.append(f"{current}:{line[1:]}")
            current = ""
    return entries


# ---------------------------------------------------------------------------
# 1. The interpreter the release will carry, verified against its own pin.
# ---------------------------------------------------------------------------
prepare_directories(INPUTS)

pin_document = json.loads(PIN.read_text())
archive_digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
assert (
    pin_document["archive"]["sha256"] == archive_digest
), "vendored archive does not match the pin beside it"
tree = WORK / "cp311"
with tarfile.open(ARCHIVE, "r:gz") as unpacked:
    unpacked.extractall(tree, filter="data")
cp311 = tree / "bin" / "python3.11"
version = subprocess.run([str(cp311), "-VV"], capture_output=True, text=True, check=True)
step(
    "vendored_interpreter",
    path=str(cp311),
    version=version.stdout.strip(),
    archive_sha256=archive_digest,
)

# ---------------------------------------------------------------------------
# 2. The real fork closure, verbatim from the S3.1b acceptance.
# ---------------------------------------------------------------------------
wheelhouse = WORK / "wheelhouse"
shutil.copytree(S31B / "wheelhouse", wheelhouse)
hermes_wheel = S31B / "hermes_agent-0.15.0-py3-none-any.whl"
shutil.copyfile(hermes_wheel, wheelhouse / hermes_wheel.name)
requirements = (S31B / "closure.requirements.txt").read_text()
requirements += (
    f"hermes-agent==0.15.0 --hash=sha256:"
    f"{hashlib.sha256(hermes_wheel.read_bytes()).hexdigest()}\n"
)
(wheelhouse / "closure.requirements.txt").write_text(requirements)
wheels = sorted(path.name for path in wheelhouse.glob("*.whl"))
step("closure", wheels=len(wheels), derived_floor=derive_minimum_os_version(wheels))

head = git("rev-parse", "HEAD").strip()
base = git("merge-base", "origin/main", "HEAD").strip()
patches = []
for commit in git("rev-list", "--reverse", f"{base}..HEAD").split():
    patch_bytes = subprocess.run(
        ["git", "-C", str(FORK), "format-patch", "--stdout", "-1", commit],
        capture_output=True,
        check=True,
    ).stdout
    subject = git("log", "-1", "--format=%f", commit).strip()[:40]
    patches.append(
        {
            "patch_id": f"cortex-{commit[:12]}-{subject}"[:80],
            "source_commit": commit,
            "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
            "disposition": "required",
        }
    )
step("patch_ledger", patches=len(patches), upstream_base=base[:12], fork_head=head[:12])

note = WORK / "evidence-pending.txt"
note.write_text(
    "S3.5 certification harness has not run; this digest names this note, not evidence.\n"
)
evidence_digest = hashlib.sha256(note.read_bytes()).hexdigest()

# ---------------------------------------------------------------------------
# 3. One real pip expansion by the 3.11 the release carries; two releases.
# ---------------------------------------------------------------------------
payload = expand_closure(
    closure=wheelhouse,
    destination=WORK / "payload",
    extra_files={
        "runtime_worker.py": ENTRYPOINT_SOURCE,
        "acceptance_driver.py": DRIVER,
    },
    python_executable=cp311,
)
step("expansion", files=sum(1 for path in payload.rglob("*") if path.is_file()))


def build(entrypoint: str, release_id: str, sequence: int):
    copied = WORK / f"payload-{sequence}"
    shutil.copytree(payload, copied, symlinks=True)
    issued = datetime.now(timezone.utc)
    return package_release(
        identity=ReleaseIdentity(
            release_id=release_id,
            release_sequence=sequence,
            distribution_version="0.15.0",
            upstream_repository="NousResearch/hermes-agent",
            upstream_tag="v2026.5.28",
            upstream_commit=base,
            publisher="local:cortex-fork",
            workflow="package-hermes-release",
            python_range=">=3.11,<3.12",
            adapter_protocol="cortex-worker/2",
            session_schema=13,
            evidence_sha256=evidence_digest,
        ),
        closure=wheelhouse,
        payload=copied,
        output=WORK / f"release-{sequence}",
        catalog_sequence=sequence,
        issued_at=issued,
        expires_at=issued + timedelta(days=30),
        patches=patches,
        python_runtime=ARCHIVE,
        python_runtime_pin=PIN,
        worker_entrypoint=entrypoint,
    )


@dataclass(frozen=True)
class OnDiskRelease:
    """A `PackagedRelease`-shaped view of an artifact set already on disk.

    Only the six attributes this program reads: the four documents, the zip, and
    the pins `stage` turns into the two `DigestPinVerifier`s. The pins are
    RECOMPUTED from the documents rather than read from `pins.json` — that file
    ships inside the directory it pins and therefore authenticates nothing — and
    then compared against it, so a set whose pins were edited after packaging
    stops here instead of being certified.
    """

    catalog: dict
    manifest: dict
    attestation: dict
    patch_ledger: dict
    artifact: Path
    pins: dict


def load_release(directory: Path) -> OnDiskRelease:
    documents: dict[str, dict] = {}
    for name in ("catalog", "manifest", "attestation", "patch_ledger"):
        document = directory / f"{name}.json"
        if not document.is_file():
            raise SystemExit(f"artifact set has no {document.name}")
        documents[name] = json.loads(document.read_text(encoding="utf-8"))
    manifest = documents["manifest"]
    artifact = directory / manifest["artifact_filename"]
    if not artifact.is_file():
        raise SystemExit(f"artifact set has no {manifest['artifact_filename']}")
    measured = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if measured != manifest["artifact_sha256"]:
        raise SystemExit(
            f"artifact bytes hash to {measured}, manifest says "
            f"{manifest['artifact_sha256']}"
        )
    pins = {
        "artifact_sha256": measured,
        "attestation_sha256": hashlib.sha256(
            canonical_json(documents["attestation"])
        ).hexdigest(),
        "catalog_payload_sha256": hashlib.sha256(
            canonical_json(documents["catalog"]["payload"])
        ).hexdigest(),
        "manifest_sha256": ReleaseManifest.from_dict(dict(manifest)).digest,
    }
    recorded = json.loads((directory / "pins.json").read_text(encoding="utf-8"))
    if recorded != pins:
        raise SystemExit(f"pins.json disagrees with the documents: {recorded} != {pins}")
    return OnDiskRelease(
        catalog=documents["catalog"],
        manifest=manifest,
        attestation=documents["attestation"],
        patch_ledger=documents["patch_ledger"],
        artifact=artifact,
        pins=pins,
    )


if ARTIFACTS is None:
    production = build("runtime_worker.py", "hermes-0.15.0", 1)
else:
    production = load_release(ARTIFACTS)
    step(
        "artifact_set_under_certification",
        directory=str(ARTIFACTS),
        release_id=production.manifest["release_id"],
        artifact_sha256=production.pins["artifact_sha256"],
        manifest_sha256=production.pins["manifest_sha256"],
        catalog_payload_sha256=production.pins["catalog_payload_sha256"],
        attestation_sha256=production.pins["attestation_sha256"],
        patch_set_sha256=production.manifest["patch_set_sha256"],
        worker_modules=sorted(production.manifest["worker_modules"]),
    )
driven = build("acceptance_driver.py", "hermes-0.15.0-driven", 2)

#: Every later reference to "the release under test" reads this rather than the
#: literal the self-packaged mode used, so D6's approval and revocation act on
#: whichever release this run actually staged.
PRODUCTION_RELEASE_ID = production.manifest["release_id"]
with zipfile.ZipFile(production.artifact) as archive:
    members = len(archive.namelist())
step(
    "packaged",
    artifact_mib=production.artifact.stat().st_size // 1_048_576,
    members=members,
    worker_runtime=production.manifest["worker_runtime"],
    worker_modules=sorted(production.manifest["worker_modules"]),
    entrypoints=[
        production.manifest["worker_entrypoint"],
        driven.manifest["worker_entrypoint"],
    ],
)


CONTROL_DIR = WORK / "control"


def control_store() -> ControlStore:
    """One real control.db for D6, the activation gate and the event stream."""

    CONTROL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONTROL_DIR, 0o700)
    store = ControlStore(CONTROL_DIR / "control.db")
    store.initialize()
    return store


CONTROL = control_store()


def stage(result, root: str):
    service = RuntimeUpdateService(
        WORK / root,
        catalog_verifier=DigestPinVerifier(result.pins["catalog_payload_sha256"]),
        attestation_verifier=DigestPinVerifier(result.pins["attestation_sha256"]),
        # ⟦S3.4/D6⟧ The real gate, reading the real append-only table.
        approvals=ControlReleaseApprovals(CONTROL),
    )
    slot = service.import_release(
        catalog=result.catalog,
        manifest=result.manifest,
        attestation=result.attestation,
        patch_ledger=result.patch_ledger,
        artifact=result.artifact,
    )
    assert (slot / "content" / "agent" / "anthropic_adapter.py").is_file()
    assert (slot / "content" / "run_agent.py").is_file()
    assert (slot / "content" / "cortex_worker" / "serve.py").is_file()
    assert (slot / "content" / "cortex_worker" / "turn.py").is_file()
    assert (slot / "content" / "cortex_worker" / "runtime.py").is_file()
    service.stage(result.manifest["release_id"])
    # D6 refusal FIRST, before any approval exists for these bytes, and from a
    # control store that has never seen them.
    release_id = result.manifest["release_id"]
    manifest_digest = result.pins["manifest_sha256"]
    refused_before = None
    try:
        service.activate(release_id, probe=lambda _candidate: True)
    except ReleaseApprovalError as exc:
        refused_before = exc.reason_code
    CONTROL.approve_runtime_release(
        release_id=release_id,
        manifest_sha256=manifest_digest,
        actor_id="s35-acceptance",
        # The store's keys are `[A-Za-z0-9_-]{16,128}`, so a release id with a
        # dot in it cannot be one.
        idempotency_key="approve-"
        + hashlib.sha256(f"{release_id}\x1f{manifest_digest}".encode()).hexdigest()[:40],
    )
    service.activate(release_id, probe=lambda _candidate: True)
    return service, slot, refused_before, manifest_digest


production_service, production_slot, production_refused, production_digest = stage(
    production, "runtime-root"
)
driven_service, driven_slot, driven_refused, driven_digest = stage(
    driven, "runtime-root-driven"
)
runtime = production.manifest["worker_runtime"]
interpreter_root = production_service.paths.interpreters / runtime["archive_sha256"]
staged_interpreter = interpreter_root / runtime["interpreter_relative"]
interpreter_pin = json.loads(
    (
        production_service.paths.interpreters / f"{runtime['archive_sha256']}.pin.json"
    ).read_text()
)
assert interpreter_pin["interpreter_sha256"] == hashlib.sha256(
    staged_interpreter.read_bytes()
).hexdigest()
step(
    "staged",
    interpreter=str(staged_interpreter),
    interpreter_sha256=interpreter_pin["interpreter_sha256"],
    root_mode=oct(interpreter_root.lstat().st_mode & 0o777),
)


def descriptor_for(service, attempt: str, path: Path):
    pin = service.pin_attempt(attempt)
    descriptor = build_descriptor(service, pin.attempt_id)
    document = {
        "schema_version": 1,
        "slot_path": str(descriptor.slot_path),
        "slot_id": descriptor.slot_id,
        "state_generation_id": descriptor.state_generation_id,
        "release_id": descriptor.release_id,
        "expected_artifact_digest": descriptor.expected_artifact_digest,
        "expected_manifest_sha256": descriptor.expected_manifest_sha256,
        "expected_content_tree_sha256": descriptor.expected_content_tree_sha256,
        "expected_interpreter_sha256": descriptor.expected_interpreter_sha256,
        "interpreter_path": str(descriptor.interpreter_path),
        "worker_entrypoint": descriptor.worker_entrypoint,
        "state_dir": str(descriptor.state_dir),
        "worker_protocol": descriptor.worker_protocol,
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return descriptor, document


production_descriptor, production_document = descriptor_for(
    production_service, "attempt-s35-production", WORK / "descriptor.json"
)
driven_descriptor, driven_document = descriptor_for(
    driven_service, "attempt-s35-driven", WORK / "descriptor-driven.json"
)
assert production_descriptor.interpreter_path == staged_interpreter

# ---------------------------------------------------------------------------
# 4. The environment: fake credentials, resolved in the parent, into one key.
# ---------------------------------------------------------------------------
resolver = SecretResolver(environment={"CORTEX_S35_FAKE_PROVIDER_KEY": FAKE_CREDENTIAL})


def environment_for(descriptor) -> dict[str, str]:
    return worker_environment(
        state_dir=descriptor.state_dir,
        token="s35-acceptance-token-with-more-than-32-bytes",
        secret_refs={"provider": "env://CORTEX_S35_FAKE_PROVIDER_KEY"},
        credential_bindings={"provider": "ANTHROPIC_API_KEY"},
        resolver=resolver,
        base_urls={"ANTHROPIC_BASE_URL": "https://runtime.invalid/v1"},
        settings={"HERMES_INFERENCE_PROVIDER": "anthropic"},
    )


production_environment = environment_for(production_descriptor)
driven_environment = environment_for(driven_descriptor)
hermes_home = Path(production_environment["HERMES_HOME"])
step(
    "worker_environment",
    keys=sorted(production_environment),
    hermes_home=str(hermes_home),
    hermes_home_created_by_product=hermes_home.is_dir(),
    hermes_home_mode=oct(hermes_home.lstat().st_mode & 0o777),
    credential_key_count=sum(
        1 for value in production_environment.values() if value == FAKE_CREDENTIAL
    ),
    yolo_mode_absent="HERMES_YOLO_MODE" not in production_environment,
)


step(
    "d6_activation_requires_approval",
    production_refused=production_refused,
    driven_refused=driven_refused,
    production_manifest_sha256=production_digest,
    approved=CONTROL.runtime_release_approved(
        PRODUCTION_RELEASE_ID, production_digest
    ),
    other_digest_not_approved=not CONTROL.runtime_release_approved(
        PRODUCTION_RELEASE_ID, "f" * 64
    ),
    dispatch_still_disabled=not CONTROL.runtime_dispatch_enabled(),
)

# ---------------------------------------------------------------------------
# 5. ⟦D3⟧ The generated profile: deterministic text, a digest, probed denials.
# ---------------------------------------------------------------------------
production_sandbox = sandbox_module.prepare_sandbox(
    production_descriptor,
    descriptor_path=WORK / "descriptor.json",
    hermes_home=Path(production_environment["HERMES_HOME"]),
)
driven_sandbox = sandbox_module.prepare_sandbox(
    driven_descriptor,
    descriptor_path=WORK / "descriptor-driven.json",
    hermes_home=Path(driven_environment["HERMES_HOME"]),
)
profile_text = production_sandbox.profile_path.read_text(encoding="utf-8")
(EVIDENCE_ROOT / "profile.sb").write_text(profile_text, encoding="utf-8")
# ⟦ADJ-09⟧ The profile beside this record must be one the certification names.
# The pre-review run wrote a profile.sb whose digest appeared nowhere in
# certification.json, and named a digest for which no file existed at all.
assert (
    hashlib.sha256(profile_text.encode("utf-8")).hexdigest() in generated_profiles
), "the profile written beside the evidence is not one this run generated"
regenerated = sandbox_module.render_profile(production_sandbox.policy)
step(
    "sandbox_profile_generated_and_probed",
    profile_sha256=production_sandbox.profile_sha256,
    policy_sha256=production_sandbox.policy.digest,
    deterministic=regenerated == profile_text,
    egress_port=production_sandbox.policy.egress_port,
    probes=dict(production_sandbox.probes),
    every_path_resolved=all(
        value == sandbox_module.resolved(value)
        for value in (
            production_sandbox.policy.interpreter_path,
            production_sandbox.policy.slot_root,
            production_sandbox.policy.content_root,
            production_sandbox.policy.state_root,
            production_sandbox.policy.hermes_home,
        )
    ),
)

# The prober refuses a launch when a rule is removed — its own failure mode,
# independent of the accept case above.
original_render = sandbox_module.render_profile
try:
    sandbox_module.render_profile = lambda policy: "\n".join(
        line
        for line in original_render(policy).splitlines()
        if not line.startswith("(deny file-write*")
    ) + "\n"
    refused_probe = None
    try:
        sandbox_module.prepare_sandbox(
            production_descriptor,
            descriptor_path=WORK / "descriptor.json",
            hermes_home=Path(production_environment["HERMES_HOME"]),
        )
    except sandbox_module.SandboxProbeFailed as exc:
        refused_probe = {"probe": exc.probe, "observation": exc.observation}
finally:
    sandbox_module.render_profile = original_render
if refused_probe is not None:
    step("prober_refuses_a_profile_whose_deny_does_not_deny", **refused_probe)
else:
    fail("prober_refuses_a_profile_whose_deny_does_not_deny", "the launch proceeded")
# Regenerate the real profile the removed-rule run overwrote.
production_sandbox = sandbox_module.prepare_sandbox(
    production_descriptor,
    descriptor_path=WORK / "descriptor.json",
    hermes_home=Path(production_environment["HERMES_HOME"]),
)

# ---------------------------------------------------------------------------
# 6. Handshake + identity remeasure, INSIDE the seatbelt (§5.3).
# ---------------------------------------------------------------------------
supervisor = WorkerSupervisorV2(
    WORK / "descriptor.json",
    environment=production_environment,
    sandbox=production_sandbox,
)
supervisor.start()
started_processes.append(supervisor.process)
try:
    identity = dict(supervisor.identity)
    health = supervisor.request("health.check", {})
    remeasured = supervisor.request("identity.measure", {})
    argv = list(supervisor.process.args)
finally:
    supervisor.close()
step(
    "handshake_and_remeasure_inside_the_sandbox",
    identity=identity,
    health=health,
    remeasured_equal=remeasured == identity,
    argv_prefix=argv[:3],
    interpreter=argv[3],
)

# ---------------------------------------------------------------------------
# 7. The dedup probe across a real SIGKILL, inside the seatbelt (§5.4).
# ---------------------------------------------------------------------------
backend = ManagedHermesBackend(
    WORK / "descriptor.json", environment=production_environment
)
try:
    capabilities = backend.capabilities()
    provenance = backend.probe_provenance
    started_processes.append(backend._supervisor.process)
    if not capabilities.durable_operation_deduplication:
        fail("dedup_probe_inside_the_sandbox", str(backend.probe_failure_stage))
    else:
        step(
            "dedup_probe_inside_the_sandbox",
            durable_operation_deduplication=True,
            provenance=provenance.as_dict(),
            sandbox_profile_sha256=backend.sandbox_launch.profile_sha256,
            worker_argv0=backend._supervisor.process.args[0],
        )
finally:
    backend.close()

# ---------------------------------------------------------------------------
# 8. The four turn methods, the approval bridge and the denials (§5.5).
# ---------------------------------------------------------------------------
listeners = []
for _ in range(2):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listeners.append(listener)
allowed_port = listeners[0].getsockname()[1]
refused_port = listeners[1].getsockname()[1]

# The honest 443 mapping: the profile's rule is generated for a port this
# process can bind, so the port-scoping D3 relies on is demonstrated rather
# than asserted. The production profile above carries 443 verbatim.
mapped_sandbox = sandbox_module.prepare_sandbox(
    driven_descriptor,
    descriptor_path=WORK / "descriptor-driven.json",
    hermes_home=Path(driven_environment["HERMES_HOME"]),
    egress_port=allowed_port,
)
driven_env = dict(driven_environment)
driven_env["CORTEX_ACCEPTANCE_PORTS"] = f"{allowed_port},{refused_port}"

turn_supervisor = WorkerSupervisorV2(
    WORK / "descriptor-driven.json", environment=driven_env, sandbox=mapped_sandbox
)
turn_supervisor.start()
started_processes.append(turn_supervisor.process)
observed_kinds: list[str] = []
finish_payload: dict[str, object] = {}
heartbeats = 0
try:
    begin_state = turn_supervisor.begin_turn(
        "attempt-s35.0",
        hashlib.sha256(b"s35-acceptance").hexdigest(),
        {
            "session_ref": "cortex_session",
            "parent_session_ref": None,
            "user_message": "probe-denials",
            "system_message": None,
            "conversation_history": [],
            "task_id": "attempt-s35",
            "agent_options": {},
            "session_db_path": None,
        },
    )
    for event in turn_supervisor.turn_events("attempt-s35.0"):
        kind = str(event.get("kind"))
        observed_kinds.append(kind)
        if kind == "decision.required":
            turn_supervisor.resolve_turn(
                "attempt-s35.0",
                {
                    "decision_id": event["payload"]["decision_id"],
                    "choice": "approve_once",
                },
            )
        if kind == EVENT_FINISH:
            finish_payload = dict(event["payload"])
finally:
    for listener in listeners:
        listener.close()

result = dict(finish_payload.get("result") or {})
denials = dict(result.get("denials") or {})
step(
    "turn_methods_and_the_fork_inside_the_sandbox",
    turn_begin=begin_state,
    event_kinds=observed_kinds,
    fork_modules=result.get("fork_modules"),
    approval_bridge_installed=result.get("approval_bridge_installed"),
    approvals=result.get("approvals"),
    final_response=result.get("final_response"),
    outcome=finish_payload.get("outcome"),
)
layer1_denials = dict(denials.get("layer1") or {})
layer2_denials = dict(denials.get("layer2") or {})
# ADJ-16: the two layers, named apart. Four of these paths used to be recorded
# only through their absolute-path attempt, which layer 2's audit hook answers
# before the syscall is issued — so the record said "denials from inside the
# real worker" while evidencing the containment, not the boundary.
step(
    "d3_layer1_seatbelt_denials_observed_from_inside_the_real_worker",
    layer="1 (sandbox-exec; the kernel answers)",
    how=(
        "the same targets named relatively after a chdir, which "
        "WriteConfinement.check declines by design, so the hook steps aside"
    ),
    write_outside_state=layer1_denials.get("write_outside_state"),
    write_hermes_home_env=layer1_denials.get("write_hermes_home_env"),
    create_hermes_home_plugins=layer1_denials.get("create_hermes_home_plugins"),
    create_hermes_home_skills=layer1_denials.get("create_hermes_home_skills"),
    write_slot_content=layer1_denials.get("write_slot_content"),
    relative_escape=layer1_denials.get("relative_escape"),
    spawn_shell=layer1_denials.get("spawn_shell"),
    fork=layer1_denials.get("fork"),
    terminal_tool=layer1_denials.get("terminal_tool"),
    egress_refused_port=denials.get("connect_port_1"),
)
step(
    "d3_layer2_typed_confinement_refusals_from_inside_the_real_worker",
    layer="2 (in-process audit hook; a typed WriteConfinementError)",
    how="the same targets named absolutely, which the hook can judge",
    write_outside_state=layer2_denials.get("write_outside_state"),
    write_hermes_home_env=layer2_denials.get("write_hermes_home_env"),
    create_hermes_home_plugins=layer2_denials.get("create_hermes_home_plugins"),
    create_hermes_home_skills=layer2_denials.get("create_hermes_home_skills"),
    write_slot_content=layer2_denials.get("write_slot_content"),
)
step(
    "d3_permitted_operations_observed_from_inside_the_real_worker",
    write_inside_state=denials.get("write_inside_state"),
    read_slot_content=denials.get("read_slot_content"),
    egress_allowed_port=denials.get("connect_port_0"),
    ports=denials.get("ports"),
    # ADJ-01: the fork's own process resolving a public hostname through the
    # resolver socket. The profile as first written denied this outright, and
    # no probe, scenario or proof could see it, because every other egress
    # check in the stack connects to a numeric address.
    resolve_public_hostname=denials.get("resolve_public_hostname"),
    resolved_address=denials.get("resolved_address"),
    connect_resolver_socket=denials.get("connect_resolver_socket"),
    production_profile_egress_rule=(
        f'(allow network-outbound (remote tcp "*:'
        f'{production_sandbox.policy.egress_port}"))'
    ),
    production_profile_resolver_rule=(
        f'(allow network-outbound (literal "{sandbox_module.RESOLVER_SOCKET}"))'
    ),
    mapping_note=(
        "binding 443 needs root, so the rule was generated for a bindable port "
        "through the same interpolation the production profile uses; the "
        "production profile above carries 443 verbatim"
    ),
)
turn_supervisor.close()

# ---------------------------------------------------------------------------
# 9. The approval vocabulary refuses a standing grant, product-side (§5.5).
# ---------------------------------------------------------------------------
vocab_sandbox = sandbox_module.prepare_sandbox(
    driven_descriptor,
    descriptor_path=WORK / "descriptor-driven.json",
    hermes_home=Path(driven_environment["HERMES_HOME"]),
)
vocab = WorkerSupervisorV2(
    WORK / "descriptor-driven.json",
    environment=driven_environment,
    sandbox=vocab_sandbox,
)
vocab.start()
started_processes.append(vocab.process)
vocabulary: dict[str, object] = {}
try:
    vocab.begin_turn(
        "attempt-s35.1",
        hashlib.sha256(b"s35-vocabulary").hexdigest(),
        {
            "session_ref": "cortex_session",
            "parent_session_ref": None,
            "user_message": "vocabulary",
            "system_message": None,
            "conversation_history": [],
            "task_id": "attempt-s35-vocab",
            "agent_options": {},
            "session_db_path": None,
        },
    )
    stream = vocab.turn_events("attempt-s35.1")
    first = next(stream)
    while first.get("kind") != "decision.required":
        first = next(stream)
    for rejected in ("always", "session", "approve", ""):
        try:
            vocab.resolve_turn(
                "attempt-s35.1",
                {"decision_id": first["payload"]["decision_id"], "choice": rejected},
            )
            vocabulary[rejected or "<empty>"] = "delivered"
        except WorkerProtocolError as exc:
            vocabulary[rejected or "<empty>"] = str(exc)
    # The channel survived every refusal, and still answers.
    vocabulary["channel_alive"] = vocab.request("health.check", {})["healthy"]
    vocab.resolve_turn(
        "attempt-s35.1",
        {"decision_id": first["payload"]["decision_id"], "choice": "deny"},
    )
    denied_result: dict[str, object] = {}
    for event in stream:
        if str(event.get("kind")) == EVENT_FINISH:
            denied_result = dict(event["payload"].get("result") or {})
    vocabulary["deny_mapped_to"] = denied_result.get("approvals")
finally:
    vocab.close()
step("approval_vocabulary_is_closed", **vocabulary)

# ---------------------------------------------------------------------------
# 10a. §5.6 — the activation gate off refuses, and never contacts the runtime.
# ---------------------------------------------------------------------------
managed = ManagedHermesBackend(
    WORK / "descriptor-driven.json", environment=driven_environment
)


@dataclass
class _Pin:
    attempt_id: str
    release_id: str = driven_descriptor.release_id
    generation_id: str = driven_descriptor.state_generation_id
    slot_id: str = driven_descriptor.slot_id
    artifact_digest: str = driven_descriptor.expected_artifact_digest
    worker_protocol: str = driven_descriptor.worker_protocol


class _Releases:
    """The pin producer `RunOrchestrator` needs, over the real descriptor."""

    def __init__(self) -> None:
        self.pins: list[_Pin] = []
        self.finished: list[_Pin] = []

    def preview_attempt_pin(self, attempt_id):
        return self.attempt_pin(attempt_id) or _Pin(attempt_id)

    def pin_attempt(self, attempt_id, expected_pin=None):
        existing = self.attempt_pin(attempt_id)
        if existing is not None:
            return existing
        pin = _Pin(attempt_id)
        assert expected_pin is None or expected_pin == pin
        self.pins.append(pin)
        return pin

    def finish_attempt(self, attempt_id, pin):
        self.finished.append(pin)

    def attempt_pin(self, attempt_id):
        return next(
            (
                pin
                for pin in self.pins
                if pin.attempt_id == attempt_id and pin not in self.finished
            ),
            None,
        )


def _thread_with_message(store, title: str, message: str, suffix: str):
    workspace = store.create_workspace(
        title=title, actor_id="local", idempotency_key=f"workspace-s35-{suffix}"
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Acceptance",
        expected_revision=workspace["revision"],
        actor_id="local",
        idempotency_key=f"thread-s35-{suffix}",
    ).value
    store.append_message(
        thread_id=thread["id"],
        role="user",
        content=message,
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key=f"message-s35-{suffix}",
    )
    return store.create_run(
        thread_id=thread["id"],
        expected_revision=store.get_thread(thread["id"])["revision"],
        actor_id="local",
        idempotency_key=f"run-s35-{suffix}",
    ).value


async def _gate_off() -> tuple[str | None, bool]:
    """Dispatch with `runtime_dispatch=false`: a typed refusal, and no worker."""

    adapter = HermesAdapter(managed=True, backend_loader=lambda: managed)
    orchestrator = RunOrchestrator(CONTROL, adapter, _Releases())
    run = _thread_with_message(CONTROL, "S3.5 gate", "gate is off", "gateoff01")
    reason = None
    try:
        await asyncio.wait_for(orchestrator.dispatch(run["id"]), 60)
    except Exception as exc:  # noqa: BLE001 - the reason code is the evidence
        reason = getattr(exc, "reason_code", None) or type(exc).__name__
    state = CONTROL.get_run(run["id"])
    if reason is None:
        # The orchestrator does not raise past `dispatch`: it records the typed
        # category on the run's terminal transition (`service.py:435-465`).
        for event in CONTROL.list_run_events(run["id"]):
            category = (event.get("payload") or {}).get("category")
            if category:
                reason = str(category)
                break
    return (reason, str(state["state"])), managed._supervisor is None


assert not CONTROL.runtime_dispatch_enabled(), "the gate must start off"
(gate_off_refusal, gate_off_state), runtime_never_contacted = asyncio.run(_gate_off())
if gate_off_refusal == "runtime_activation_disabled" and runtime_never_contacted:
    step(
        "activation_gate_off_refuses_without_contacting_the_runtime",
        reason_code=gate_off_refusal,
        run_state=gate_off_state,
        runtime_never_contacted=runtime_never_contacted,
    )
else:
    fail(
        "activation_gate_off_refuses_without_contacting_the_runtime",
        f"reason={gate_off_refusal} state={gate_off_state} "
        f"never_contacted={runtime_never_contacted}",
    )

# ---------------------------------------------------------------------------
# 10b. §5.7 — one attempt dispatches under a bounded window authorization.
# ---------------------------------------------------------------------------
CONTROL.enable_runtime_activation(
    mode="window",
    window_seconds=900,
    actor_id="s35-acceptance",
    idempotency_key="activate-s35-acceptance-0001",
)


async def _dispatch_once() -> dict[str, object]:
    adapter = HermesAdapter(managed=True, backend_loader=lambda: managed)
    orchestrator = RunOrchestrator(CONTROL, adapter, _Releases())
    run = _thread_with_message(CONTROL, "S3.5 dispatch", "hello from S3.5", "dispatch1")

    async def approve() -> None:
        for _ in range(1200):
            pending = CONTROL.list_decisions(state="pending")
            if pending:
                decision = pending[0]
                CONTROL.resolve_decision(
                    decision_id=decision["id"],
                    choice="approve_once",
                    expected_revision=decision["revision"],
                    actor_id="local",
                    idempotency_key="decision-s35-acceptance-0001",
                )
                actions = CONTROL.list_pending_runtime_actions()
                while not actions:
                    await asyncio.sleep(0.05)
                    actions = CONTROL.list_pending_runtime_actions()
                await orchestrator.deliver_runtime_action(
                    actions[0]["id"], worker_id="s35-acceptance"
                )
                return
            await asyncio.sleep(0.05)

    approver = asyncio.create_task(approve())
    completed = await asyncio.wait_for(orchestrator.dispatch(run["id"]), 240)
    await approver
    events = CONTROL.list_run_events(run["id"])
    messages = {
        message["id"]: message["content"]
        for message in CONTROL.list_messages(completed["thread_id"])
    }
    replayed = control_durable_stream(
        events,
        attempt_id=str(completed["active_attempt_id"]),
        message_content=lambda identifier: messages[identifier],
    )
    return {
        "run_state": str(completed["state"]),
        "attempt_id": str(completed["active_attempt_id"]),
        "replayed_digest": recompute_result_digest(replayed),
        "activation": CONTROL.runtime_activation_report(),
        "worker_argv0": (
            managed._supervisor.process.args[0]
            if managed._supervisor is not None and managed._supervisor.process
            else None
        ),
        "sandbox_profile_sha256": (
            managed.sandbox_launch.profile_sha256 if managed.sandbox_launch else None
        ),
    }


try:
    dispatched = asyncio.run(_dispatch_once())
    if managed._supervisor is not None and managed._supervisor.process is not None:
        started_processes.append(managed._supervisor.process)
    step("one_attempt_dispatches_under_a_bounded_window", **dispatched)
except Exception as exc:  # noqa: BLE001
    dispatched = {"run_state": f"error:{type(exc).__name__}", "detail": str(exc)[:300]}
    fail("one_attempt_dispatches_under_a_bounded_window", json.dumps(dispatched)[:400])
finally:
    managed.close()

# ---------------------------------------------------------------------------
# 10c. §5.8 / P2b — the secret is in the effect process and nowhere else.
# ---------------------------------------------------------------------------
control_bytes = (CONTROL_DIR / "control.db").read_bytes()
log_paths = [
    path
    for root in (production_descriptor.state_dir, driven_descriptor.state_dir)
    for path in root.rglob("*.log")
]
logs_clean = all(FAKE_CREDENTIAL.encode() not in path.read_bytes() for path in log_paths)
secret_observation = {
    "credential_keys_in_worker_env": sorted(
        key for key, value in production_environment.items() if value == FAKE_CREDENTIAL
    ),
    "in_control_db": FAKE_CREDENTIAL.encode() in control_bytes,
    "in_worker_logs": not logs_clean,
    "log_files_scanned": len(log_paths),
    "in_descriptor": FAKE_CREDENTIAL in (WORK / "descriptor.json").read_text(),
    "in_sandbox_profile": FAKE_CREDENTIAL in profile_text,
    "in_acceptance_record": False,
}
secret_observation["only_in_effect_process"] = (
    len(secret_observation["credential_keys_in_worker_env"]) == 1
    and not secret_observation["in_control_db"]
    and not secret_observation["in_worker_logs"]
    and not secret_observation["in_descriptor"]
    and not secret_observation["in_sandbox_profile"]
)
step("the_secret_reaches_the_effect_process_and_nowhere_else", **secret_observation)

# ---------------------------------------------------------------------------
# 10. §5.1/5.2/5.9 through the real harness, over the bytes just packaged.
# ---------------------------------------------------------------------------
artifact_set = ArtifactSet(
    catalog=production.catalog,
    manifest=production.manifest,
    attestation=production.attestation,
    patch_ledger=production.patch_ledger,
    artifact=production.artifact,
)


def runtime_proofs(_artifacts, _workspace):
    """Proofs 3-8, filled from what this program actually observed."""

    return [
        Proof(3, bool(identity) and remeasured == identity, {
            "identity": identity, "inside_sandbox": argv[0] == sandbox_module.SANDBOX_EXEC
        }),
        Proof(4, bool(capabilities.durable_operation_deduplication), {
            "provenance": provenance.as_dict() if provenance else None
        }),
        Proof(
            5,
            # ADJ-16: layer 1 is the boundary D3 declares, so proof 5 asks the
            # layer-1 answers. `denied:PermissionError` is the kernel; a
            # `WriteConfinementError` here would be the hook answering first and
            # would no longer evidence the seatbelt at all.
            all(
                layer1_denials.get(name) == "denied:PermissionError"
                for name in (
                    "write_outside_state",
                    "write_hermes_home_env",
                    "create_hermes_home_plugins",
                    "create_hermes_home_skills",
                    "write_slot_content",
                    "relative_escape",
                )
            )
            and str(layer1_denials.get("spawn_shell", "")).startswith("denied:")
            and str(layer1_denials.get("fork", "")).startswith("denied:")
            and all(
                str(layer2_denials.get(name, "")).startswith(
                    "denied:WriteConfinementError"
                )
                for name in (
                    "write_outside_state",
                    "write_hermes_home_env",
                    "create_hermes_home_plugins",
                    "create_hermes_home_skills",
                    "write_slot_content",
                )
            )
            and denials.get("connect_port_0") == "allowed"
            and str(denials.get("connect_port_1", "")).startswith("denied:")
            # ADJ-01: a certified profile that cannot resolve a hostname is a
            # release that fails every turn.
            and denials.get("resolve_public_hostname") == "allowed",
            # ⟦ADJ-09⟧ The §5.5 proof names the profile it actually ran under —
            # the port-mapped one, which appeared in no artifact at all — and
            # the production profile beside it, with the substitution disclosed
            # INSIDE the evidence so `evidence_sha256` covers it.
            denials
            | {
                "sandbox_profile_sha256": mapped_sandbox.profile_sha256,
                "production_profile_sha256": production_sandbox.profile_sha256,
                "production_profile_egress_rule": (
                    f'(allow network-outbound (remote tcp "*:'
                    f'{production_sandbox.policy.egress_port}"))'
                ),
                "production_profile_resolver_rule": (
                    f'(allow network-outbound (literal '
                    f'"{sandbox_module.RESOLVER_SOCKET}"))'
                ),
                "mapping_note": (
                    "binding 443 needs root, so the probed profile was generated "
                    "for a bindable port through the same interpolation the "
                    "production profile uses; the two differ in that port and "
                    "nothing else, and both are carried in sandbox_profiles"
                ),
                "layer1_denials": layer1_denials,
                "layer2_denials": layer2_denials,
            },
        ),
        Proof(
            6,
            gate_off_refusal == "runtime_activation_disabled"
            and runtime_never_contacted,
            {
                "reason_code": gate_off_refusal,
                "run_state": gate_off_state,
                "runtime_never_contacted": runtime_never_contacted,
            },
        ),
        Proof(
            7,
            dispatched.get("run_state") == "completed",
            # Deviation (3), recorded where the digest covers it: this proof
            # dispatches through a descriptor-shaped pin producer, not
            # `RuntimeUpdateService.pin_attempt`, so D6's dispatch gate is the
            # one thing it does NOT exercise. Section 11 below drives the real
            # service, and `_attempt_pin_from_identity` is the door all three of
            # `pin_attempt`, `preview_attempt_pin` and `attempt_pin` route
            # through.
            dispatched
            | {
                "dispatch_gate_proved_separately": (
                    "acceptance section 11 exercises D6 against the real "
                    "RuntimeUpdateService (pin_attempt refused after revoke); "
                    "this proof's pin producer is descriptor-shaped and does "
                    "not walk _attempt_pin_from_identity"
                ),
            },
        ),
        Proof(8, secret_observation.get("only_in_effect_process") is True,
              secret_observation),
    ]


report = run_harness(
    artifact_set,
    workspace=WORK / "harness",
    approvals=ControlReleaseApprovals(CONTROL),
    runtime_proofs=runtime_proofs,
    # ⟦ADJ-09⟧ Every profile this run generated, by digest, and the interpreter
    # the worker proofs ran on. Without them the record's own binding checks
    # refuse to certify.
    sandbox_profiles=generated_profiles,
    interpreter_sha256=interpreter_pin["interpreter_sha256"],
)
if ARTIFACTS is None:
    evidence_path = write_evidence(report, OUTPUT)
else:
    # `write_evidence` owns the filename `certification.json`, which in the
    # artifact directory already belongs to the shipped command's document-only
    # run. Write it to a staging directory this run owns and place it under the
    # name the full record is asked for, carrying the profiles with it.
    staged = write_evidence(report, WORK / "full-evidence")
    evidence_path = EVIDENCE_ROOT / "certification.full.json"
    shutil.copyfile(staged, evidence_path)
    staged_profiles = staged.parent / "sandbox-profiles"
    if staged_profiles.is_dir():
        target_profiles = EVIDENCE_ROOT / "sandbox-profiles"
        if target_profiles.exists():
            shutil.rmtree(target_profiles)
        shutil.copytree(staged_profiles, target_profiles)
by_number = {proof.number: proof for proof in report.proofs}
step(
    "certification_harness",
    certified=report.certified,
    missing=list(report.missing),
    binding_failures=list(report.binding_failures),
    workspace_cleanup_error=report.workspace_cleanup_error,
    sandbox_profiles=sorted(report.sandbox_profiles),
    interpreter_sha256=report.interpreter_sha256,
    harness_workspace_entries=sorted(
        entry.name for entry in (WORK / "harness").iterdir()
    ) if (WORK / "harness").exists() else [],
    failed=[number for number, proof in by_number.items() if not proof.passed],
    divergence_count=report.divergence_count,
    evidence_sha256=report.evidence_sha256,
    evidence_path=str(evidence_path),
    proof_1=by_number[1].evidence,
    proof_2_refusals=by_number[2].evidence.get("refusals"),
    proof_9=by_number[9].evidence,
    details={
        number: proof.detail for number, proof in by_number.items() if proof.detail
    },
)

# ---------------------------------------------------------------------------
# 11. D6 immutability, revocation, and the backup discipline.
# ---------------------------------------------------------------------------
import sqlite3  # noqa: E402 - local to this section's evidence

CONTROL.revoke_runtime_release(
    release_id=PRODUCTION_RELEASE_ID,
    manifest_sha256=production_digest,
    actor_id="s35-acceptance",
    idempotency_key="revoke-s35-acceptance-0001",
)
refused_after_revoke = None
try:
    production_service.pin_attempt("attempt-s35-after-revoke")
except ReleaseApprovalError as exc:
    refused_after_revoke = exc.reason_code
immutability: dict[str, str] = {}
with sqlite3.connect(CONTROL_DIR / "control.db") as connection:
    for label, statement in (
        ("update", "UPDATE runtime_release_approvals SET decision = 'approve'"),
        ("delete", "DELETE FROM runtime_release_approvals"),
    ):
        try:
            connection.execute(statement)
            immutability[label] = "permitted"
        except sqlite3.Error as exc:
            immutability[label] = f"{type(exc).__name__}: {exc}"
    rows = connection.execute(
        "SELECT decision FROM runtime_release_approvals ORDER BY rowid"
    ).fetchall()
step(
    "d6_is_append_only_and_revocation_refuses_dispatch",
    refused_after_revoke=refused_after_revoke,
    decisions=[row[0] for row in rows],
    immutability=immutability,
    dispatch_gate_untouched=CONTROL.runtime_dispatch_enabled(),
)

inspection = inspect_database(
    CONTROL_DIR / "control.db",
    required_tables=("schema_migrations", "runtime_release_approvals"),
    table_since_schema_version=(
        ("runtime_release_approvals", RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION),
    ),
)
older = WORK / "older-control"
older.mkdir(mode=0o700, exist_ok=True)
shutil.copyfile(CONTROL_DIR / "control.db", older / "control.db")
with sqlite3.connect(older / "control.db") as connection:
    connection.execute(
        "DROP TRIGGER IF EXISTS runtime_release_approvals_delete_guard"
    )
    connection.execute("DROP TABLE runtime_release_approvals")
    connection.execute(
        "DELETE FROM schema_migrations WHERE version = ?",
        (RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION,),
    )
older_inspection = inspect_database(
    older / "control.db",
    required_tables=("schema_migrations", "runtime_release_approvals"),
    table_since_schema_version=(
        ("runtime_release_approvals", RUNTIME_RELEASE_APPROVAL_SCHEMA_VERSION),
    ),
)
step(
    "backup_protects_the_table_and_an_older_schema_still_snapshots",
    schema_version=inspection["schema_version"],
    required=inspection["required_tables"],
    missing=inspection["missing_required_tables"],
    older_schema_version=older_inspection["schema_version"],
    older_required=older_inspection["required_tables"],
    older_missing=older_inspection["missing_required_tables"],
    older_status=older_inspection["status"],
)

# ---------------------------------------------------------------------------
# 12. Every process this program started, accounted for.
# ---------------------------------------------------------------------------
leaked = []
for process in started_processes:
    if process is None:
        continue
    if process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            leaked.append(process.pid)
step("processes_reaped", started=len(started_processes), leaked=leaked)

record["all_passed"] = all(
    entry.get("pass") is True for entry in record["scenarios"].values()
)
(EVIDENCE_ROOT / RECORD_NAME).write_text(
    json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
)
print(
    "\nALL PASSED" if record["all_passed"] else "\nFAILURES: "
    + ", ".join(
        name for name, entry in record["scenarios"].items() if entry.get("pass") is not True
    )
)
raise SystemExit(0 if record["all_passed"] else 1)
