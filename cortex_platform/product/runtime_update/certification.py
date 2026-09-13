"""D5's harness: the nine things §5 says a certified Hermes must demonstrate.

One module, nine numbered proofs, each a function that returns a `Proof` rather
than raising. A harness that stopped at the first failure would report the first
problem and hide the rest, and the operator's question is "is this build
certifiable", not "what is the earliest thing wrong with it".

Two rules from the contract shape the code more than anything else.

**No fake Hermes stands in for the acceptance** (§7). The proofs that need a
running worker take a real staged slot and the interpreter that release carries.
They *are* injectable: `run_harness` takes a `runtime_proofs` callable and
extends its proof list with whatever that returns, and this module's own tests
drive it with stubs. What §7 is enforced by is not the absence of that seam but
the fact that nothing shipped reaches it — `certify` never passes the callable,
so the installed command proves §5.1, §5.2 and §5.9 and reports the other six
clauses as missing. Today the only caller that supplies real ones is an
out-of-tree acceptance driver. See `docs/plans/2026-09-02-s35-certification-notes.md`.

**Every refusal is independent of its accept test** (§7). Proof 2 imports each
mutation into its own runtime-update root, so a refusal can never be inherited
from the state a passing import left behind.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .approval import ReleaseApprovalGate
from .models import ReleaseManifest, ValidationError, canonical_json, digest_document
from .service import (
    CatalogReplayError,
    DigestPinVerifier,
    RuntimeUpdateService,
    VerificationError,
    _remove_tree,
)

#: The nine §5 clauses, in the contract's order, as the harness names them.
PROOF_NAMES = {
    1: "artifact_set_is_internally_consistent",
    2: "import_accepts_and_refuses_each_mutation",
    3: "worker_launches_on_the_slots_own_interpreter",
    4: "dedup_is_demonstrated_across_a_real_sigkill",
    5: "sandbox_denies_and_permits_exactly_what_d3_declares",
    6: "activation_gate_off_refuses_without_contacting_the_runtime",
    7: "one_attempt_dispatches_under_a_bounded_window",
    8: "the_secret_reaches_the_effect_process_and_nowhere_else",
    9: "patch_ledger_records_a_disposition_per_patch",
}

#: Bumped from 1 for ⟦ADJ-09⟧: the record gained `sandbox_profiles`,
#: `interpreter_sha256`, `binding_failures` and `workspace_cleanup_error`, so
#: a version-1 document and a version-2 document are not comparable records.
CERTIFICATION_SCHEMA_VERSION = 2

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

#: The evidence key any proof uses to name the sandbox profile it ran under.
PROFILE_EVIDENCE_KEY = "sandbox_profile_sha256"

#: The evidence key proof 5 uses to name the profile *production* runs under,
#: which is not always the profile the probe ran under: binding 443 needs root,
#: so an acceptance run generates the egress rule for a bindable port through
#: the same interpolation. That substitution is honest only if the record
#: discloses it, so when the two digests differ both disclosures are required.
PRODUCTION_PROFILE_EVIDENCE_KEY = "production_profile_sha256"
SUBSTITUTION_DISCLOSURE_KEYS = ("mapping_note", "production_profile_egress_rule")

#: Proof 5 is the §5.5 sandbox proof. A sandbox proof that names no profile
#: says only that *some* policy denied something: the refuter replaced the
#: profile with `(allow default)` and left the record byte-identical.
PROFILE_BOUND_PROOFS = (5,)

#: Proof 7 dispatches one attempt, but through a descriptor-shaped pin producer
#: rather than `RuntimeUpdateService.pin_attempt`, so D6's dispatch gate is
#: proved separately. The record must say so rather than let a reader assume
#: the dispatch path carried it.
SEPARATION_DISCLOSURES = {7: "dispatch_gate_proved_separately"}

#: The clauses that need a running worker on the slot's own interpreter. A
#: record carrying any of them must name which interpreter that was.
RUNTIME_PROOF_NUMBERS = frozenset({3, 4, 5, 6, 7, 8})


class CertificationError(RuntimeError):
    """The harness could not run, as distinct from a proof that failed."""


@dataclass(frozen=True)
class Proof:
    """One §5 clause, and what was observed for it."""

    number: int
    passed: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def name(self) -> str:
        return PROOF_NAMES[self.number]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proof": self.number,
            "name": self.name,
            "passed": self.passed,
            "evidence": dict(self.evidence),
            "detail": self.detail,
        }


def _named_profile_digests(evidence: Any) -> list[str]:
    """Every sandbox profile digest a proof's evidence names, however nested.

    The walk is recursive because a proof's evidence is whatever the caller
    observed: the dispatch proof carries its digest inside a nested result
    mapping, and a digest that escaped this scan would escape the binding check
    that follows it.
    """

    found: list[str] = []
    if isinstance(evidence, Mapping):
        for key, value in evidence.items():
            if key == PROFILE_EVIDENCE_KEY and isinstance(value, str) and value:
                found.append(value)
            elif key == PRODUCTION_PROFILE_EVIDENCE_KEY and isinstance(value, str) and value:
                found.append(value)
            else:
                found.extend(_named_profile_digests(value))
    elif isinstance(evidence, (list, tuple)):
        for item in evidence:
            found.extend(_named_profile_digests(item))
    return found


@dataclass(frozen=True)
class CertificationReport:
    """Every proof, and the identity of the release they were run against."""

    schema_version: int
    release_id: str
    manifest_sha256: str
    artifact_sha256: str
    proofs: tuple[Proof, ...]
    divergence_count: int
    ran_at: str
    #: ⟦ADJ-09⟧ digest → the profile text itself, for every sandbox profile the
    #: run generated. The text rather than a path, because the point is that
    #: `evidence_sha256` covers the bytes: with only a path in the record, the
    #: refuter replaced `profile.sb` with `(allow default)` and left the digest
    #: byte-identical with `certified: true` standing.
    sandbox_profiles: Mapping[str, str] = field(default_factory=dict)
    #: The interpreter the worker proofs ran on. Required once the record
    #: carries any of them: §5.3 is about the slot's *own* interpreter, and the
    #: record named no interpreter anywhere.
    interpreter_sha256: str | None = None
    #: ⟦ADJ-08⟧ Set when the harness could not remove the expansion it created.
    #: Surfaced rather than raised, so a cleanup fault cannot discard a report
    #: that is otherwise complete.
    workspace_cleanup_error: str | None = None

    @property
    def certified(self) -> bool:
        """Every clause proved, every proof passed, and every claim bound.

        A missing clause is not a neutral absence: it is the shape a synthetic
        run has, and §7 forbids one standing in for the acceptance. So a report
        that never ran the worker proofs is not certified, however green its
        document checks were.

        Nor is an unbound claim. A proof that names a profile the record does
        not carry is a proof about a file nobody can produce, which is the gap
        the refuter walked through.
        """

        return (
            not self.missing
            and not self.binding_failures
            and all(proof.passed for proof in self.proofs)
        )

    @property
    def missing(self) -> tuple[int, ...]:
        return tuple(sorted(set(PROOF_NAMES) - {proof.number for proof in self.proofs}))

    @property
    def binding_failures(self) -> tuple[str, ...]:
        """Every claim in this record that its own digest does not cover.

        `evidence_sha256` only binds what the record contains. Each check here
        closes one way a proof could assert something about a file, a profile or
        an interpreter that the record never names — which is exactly a claim
        that can be changed after the fact without disturbing the digest.
        """

        failures: list[str] = []
        profiles = dict(self.sandbox_profiles)
        for digest, text in sorted(profiles.items()):
            observed = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if observed != digest:
                failures.append(
                    f"sandbox_profiles[{digest}] holds text that hashes to {observed}"
                )
        by_number = {proof.number: proof for proof in self.proofs}
        for proof in self.proofs:
            for digest in sorted(set(_named_profile_digests(proof.evidence))):
                if digest not in profiles:
                    failures.append(
                        f"proof {proof.number} names sandbox profile {digest}, "
                        "which the record does not carry"
                    )
        for number in PROFILE_BOUND_PROOFS:
            proof = by_number.get(number)
            if proof is None:
                continue
            probed = str(proof.evidence.get(PROFILE_EVIDENCE_KEY, "") or "")
            produced = str(proof.evidence.get(PRODUCTION_PROFILE_EVIDENCE_KEY, "") or "")
            if not probed:
                failures.append(f"proof {number} names no {PROFILE_EVIDENCE_KEY}")
            if not produced:
                failures.append(
                    f"proof {number} names no {PRODUCTION_PROFILE_EVIDENCE_KEY}"
                )
            if probed and produced and probed != produced:
                for key in SUBSTITUTION_DISCLOSURE_KEYS:
                    if not str(proof.evidence.get(key, "") or "").strip():
                        failures.append(
                            f"proof {number} probed a profile that is not the "
                            f"production one and does not disclose {key}"
                        )
        for number, key in sorted(SEPARATION_DISCLOSURES.items()):
            proof = by_number.get(number)
            if proof is None:
                continue
            if not str(proof.evidence.get(key, "") or "").strip():
                failures.append(f"proof {number} does not record {key}")
        if RUNTIME_PROOF_NUMBERS & set(by_number) and not _SHA256_HEX.fullmatch(
            self.interpreter_sha256 or ""
        ):
            failures.append(
                "a record carrying the worker proofs names no interpreter_sha256"
            )
        return tuple(failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "manifest_sha256": self.manifest_sha256,
            "artifact_sha256": self.artifact_sha256,
            "certified": self.certified,
            "divergence_count": self.divergence_count,
            "ran_at": self.ran_at,
            "proofs": [proof.to_dict() for proof in self.proofs],
            "sandbox_profiles": dict(self.sandbox_profiles),
            "interpreter_sha256": self.interpreter_sha256,
            "binding_failures": list(self.binding_failures),
            "workspace_cleanup_error": self.workspace_cleanup_error,
        }

    @property
    def evidence_sha256(self) -> str:
        """The digest a manifest's `evidence_sha256` may finally name.

        S3.1b's accepted release digested a note saying the harness had not run.
        This is what replaces it: a digest over the report, so the field names
        evidence rather than an apology for its absence.
        """

        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


# ---------------------------------------------------------------------------
# Proof 1 — the artifact set agrees with itself
# ---------------------------------------------------------------------------


#: The ledger file `package_release` writes, named here so the reader and the
#: writer cannot drift again. They did: the loader looked for a hyphenated
#: `patch-ledger.json` and nothing caught it, because this harness's own tests
#: synthesize the directory and the S3.5 acceptance driver hands `run_harness`
#: an `ArtifactSet` it built in process. Against a real packaged release the
#: ledger read as absent, so §5.9 reported "release carries no patch ledger"
#: and §5.2 refused every import on a `patch_set_sha256` it could not match.
#: `tests/packaging` pins this against the producer, which lives in `tools/`
#: and cannot be imported from here.
PATCH_LEDGER_NAME = "patch_ledger.json"


@dataclass(frozen=True)
class ArtifactSet:
    """The four documents and the zip, exactly as `package_release` emits them."""

    catalog: Mapping[str, Any]
    manifest: Mapping[str, Any]
    attestation: Mapping[str, Any]
    patch_ledger: Mapping[str, Any] | None
    artifact: Path

    @classmethod
    def load(cls, directory: Path) -> "ArtifactSet":
        def document(name: str) -> Mapping[str, Any]:
            path = directory / name
            if not path.is_file():
                raise CertificationError(f"artifact set has no {name}")
            return json.loads(path.read_text(encoding="utf-8"))

        manifest = document("manifest.json")
        # ⟦ADJ-23⟧ Validate before joining. `--artifacts` is the bound on what
        # this command may read, and `artifact_filename` is the only value in
        # the set that becomes a path here. `models` already applies the
        # one-safe-name rule to it; taking the name from the validated object
        # is what makes that rule run on this path too, so a traversing value
        # cannot be opened, digested, and published as the release's artifact.
        try:
            release = ReleaseManifest.from_dict(dict(manifest))
        except ValidationError as exc:
            raise CertificationError(f"artifact set manifest is invalid: {exc}") from exc
        artifact = directory / release.artifact_filename
        if not artifact.is_file():
            raise CertificationError("artifact set has no artifact archive")
        ledger_path = directory / PATCH_LEDGER_NAME
        return cls(
            catalog=document("catalog.json"),
            manifest=manifest,
            attestation=document("attestation.json"),
            patch_ledger=(
                json.loads(ledger_path.read_text(encoding="utf-8"))
                if ledger_path.is_file()
                else None
            ),
            artifact=artifact,
        )


def prove_artifact_set_is_consistent(artifacts: ArtifactSet) -> Proof:
    """§5.1 — catalog ↔ manifest ↔ attestation ↔ patch ledger, and the zip hash."""

    evidence: dict[str, Any] = {}
    failures: list[str] = []
    try:
        release = ReleaseManifest.from_dict(dict(artifacts.manifest))
    except ValidationError as exc:
        return Proof(1, False, {}, f"manifest is invalid: {exc}")

    manifest_digest = digest_document(dict(artifacts.manifest))
    evidence["manifest_sha256"] = manifest_digest
    if manifest_digest != release.digest:
        failures.append("manifest digest disagrees with its own canonical form")

    entries = [
        entry
        for entry in artifacts.catalog.get("payload", {}).get("entries", [])
        if entry.get("release_id") == release.release_id
    ]
    evidence["catalog_entries"] = len(entries)
    if len(entries) != 1:
        failures.append("catalog does not name this release exactly once")
    else:
        evidence["catalog_status"] = entries[0].get("status")
        if entries[0].get("manifest_sha256") != manifest_digest:
            failures.append("catalog manifest digest does not match the manifest")
        if entries[0].get("status") != "certified":
            failures.append("catalog does not certify this release")

    observed = hashlib.sha256(artifacts.artifact.read_bytes()).hexdigest()
    evidence["artifact_sha256"] = observed
    if observed != release.artifact_sha256:
        failures.append("artifact hash does not match the manifest")

    expected_provenance = {
        "schema_version": 1,
        "artifact_sha256": release.artifact_sha256,
        "repository": release.upstream_repository,
        "tag": release.upstream_tag,
        "commit": release.upstream_commit,
        "publisher": release.publisher,
        "workflow": release.workflow,
    }
    if dict(artifacts.attestation) != expected_provenance:
        failures.append("provenance does not match the manifest")

    if artifacts.patch_ledger is not None:
        ledger_digest = hashlib.sha256(
            canonical_json(dict(artifacts.patch_ledger))
        ).hexdigest()
        evidence["patch_set_sha256"] = ledger_digest
        if ledger_digest != release.patch_set_sha256:
            failures.append("patch ledger digest does not match the manifest")
    elif release.patch_set_sha256 != hashlib.sha256(b"").hexdigest():
        evidence["patch_set_sha256"] = release.patch_set_sha256

    return Proof(1, not failures, evidence, "; ".join(failures))


# ---------------------------------------------------------------------------
# Proof 2 — import accepts it, and refuses every mutation
# ---------------------------------------------------------------------------


def _service_for(
    root: Path, artifacts: ArtifactSet, approvals: ReleaseApprovalGate | None
) -> RuntimeUpdateService:
    payload = artifacts.catalog.get("payload", {})
    return RuntimeUpdateService(
        root,
        catalog_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(dict(payload))).hexdigest()
        ),
        attestation_verifier=DigestPinVerifier(
            hashlib.sha256(canonical_json(dict(artifacts.attestation))).hexdigest()
        ),
        approvals=approvals,
    )


def _mutations(artifacts: ArtifactSet) -> list[tuple[str, ArtifactSet]]:
    """One mutated artifact set per refusal §5.2 names.

    Every mutation is a deep copy: a refusal that shared a document with the
    accept case could pass by mutating the thing it was supposed to leave alone.
    """

    cases: list[tuple[str, ArtifactSet]] = []

    def variant(name: str, **changes: Any) -> None:
        cases.append(
            (
                name,
                ArtifactSet(
                    catalog=changes.get("catalog", copy.deepcopy(dict(artifacts.catalog))),
                    manifest=changes.get(
                        "manifest", copy.deepcopy(dict(artifacts.manifest))
                    ),
                    attestation=changes.get(
                        "attestation", copy.deepcopy(dict(artifacts.attestation))
                    ),
                    patch_ledger=changes.get(
                        "patch_ledger",
                        copy.deepcopy(artifacts.patch_ledger)
                        if artifacts.patch_ledger is not None
                        else None,
                    ),
                    artifact=changes.get("artifact", artifacts.artifact),
                ),
            )
        )

    manifest = copy.deepcopy(dict(artifacts.manifest))
    manifest["artifact_sha256"] = "0" * 64
    variant("wrong_artifact_hash", manifest=manifest)

    catalog = copy.deepcopy(dict(artifacts.catalog))
    for entry in catalog["payload"]["entries"]:
        entry["status"] = "revoked"
    variant("uncertified_status", catalog=catalog)

    catalog = copy.deepcopy(dict(artifacts.catalog))
    for entry in catalog["payload"]["entries"]:
        entry["manifest_sha256"] = "1" * 64
    variant("manifest_catalog_mismatch", catalog=catalog)

    attestation = copy.deepcopy(dict(artifacts.attestation))
    attestation["publisher"] = "attacker"
    variant("provenance_mismatch", attestation=attestation)

    if artifacts.patch_ledger is not None:
        ledger = copy.deepcopy(dict(artifacts.patch_ledger))
        ledger["upstream_commit"] = "0" * 40
        variant("patch_ledger_mismatch", patch_ledger=ledger)

    return cases


def prove_import_accepts_and_refuses(
    artifacts: ArtifactSet, workspace: Path, *, approvals: ReleaseApprovalGate | None
) -> Proof:
    """§5.2 — the accept, then each refusal, each in its own runtime root."""

    evidence: dict[str, Any] = {}
    failures: list[str] = []
    accept_root = workspace / "import-accept"
    try:
        _service_for(accept_root, artifacts, approvals).import_release(
            catalog=dict(artifacts.catalog),
            manifest=dict(artifacts.manifest),
            attestation=dict(artifacts.attestation),
            patch_ledger=(
                dict(artifacts.patch_ledger)
                if artifacts.patch_ledger is not None
                else None
            ),
            artifact=artifacts.artifact,
        )
        evidence["accepted"] = True
    except Exception as exc:  # noqa: BLE001 - the proof is what it refused with
        evidence["accepted"] = False
        failures.append(f"import refused the unmutated release: {exc}")

    refusals: dict[str, str] = {}
    for index, (name, mutated) in enumerate(_mutations(artifacts)):
        root = workspace / f"import-refuse-{index}-{name}"
        try:
            _service_for(root, mutated, approvals).import_release(
                catalog=dict(mutated.catalog),
                manifest=dict(mutated.manifest),
                attestation=dict(mutated.attestation),
                patch_ledger=(
                    dict(mutated.patch_ledger)
                    if mutated.patch_ledger is not None
                    else None
                ),
                artifact=mutated.artifact,
            )
        except (VerificationError, ValidationError, CatalogReplayError) as exc:
            refusals[name] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            refusals[name] = f"unexpected:{type(exc).__name__}"
            failures.append(f"{name} refused with an unexpected type: {exc}")
        else:
            refusals[name] = "accepted"
            failures.append(f"{name} was accepted")

    refusals.update(
        _catalog_sequence_refusals(artifacts, workspace, approvals, failures, evidence)
    )
    evidence["refusals"] = refusals
    return Proof(2, not failures, evidence, "; ".join(failures))


def _catalog_sequence_refusals(
    artifacts: ArtifactSet,
    workspace: Path,
    approvals: ReleaseApprovalGate | None,
    failures: list[str],
    evidence: dict[str, Any],
) -> dict[str, str]:
    """Replay and equivocation, which need a root that already accepted once.

    Not a shared-state exception to §7: a replay is only expressible against a
    catalog sequence the same root has already seen, so the prior import IS the
    precondition rather than leftover state.
    """

    observed: dict[str, str] = {}
    root = workspace / "import-sequence"
    service = _service_for(root, artifacts, approvals)
    try:
        service.import_release(
            catalog=dict(artifacts.catalog),
            manifest=dict(artifacts.manifest),
            attestation=dict(artifacts.attestation),
            patch_ledger=(
                dict(artifacts.patch_ledger)
                if artifacts.patch_ledger is not None
                else None
            ),
            artifact=artifacts.artifact,
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"sequence precondition import failed: {exc}")
        evidence["replay_shape"] = "unavailable"
        return {"replayed_sequence": "unavailable", "equivocating_sequence": "unavailable"}

    replayed = copy.deepcopy(dict(artifacts.catalog))
    # ⟦ADJ-18⟧ Zero is a legal catalog sequence (`_positive_int(..., zero=True)`)
    # and the manager's high-water mark starts at -1, so a release published at
    # sequence 0 is well-formed. `sequence - 1` is then -1, which no catalog may
    # carry: `import_release` refused it as a `ValidationError` before the replay
    # check ever ran, and the harness failed the proof for a good release.
    #
    # At zero the backward move is not expressible, so the replay takes the only
    # shape that exists there: the same sequence carrying different bytes, which
    # the manager refuses through the equivocation branch of the same
    # `CatalogReplayError`. Which shape ran is recorded, because "refused" means
    # a different thing in each.
    published = int(replayed["payload"]["sequence"])
    candidate = max(published - 1, 0)
    replayed["payload"]["sequence"] = candidate
    if candidate == published:
        replayed["payload"]["issued_at"] = _shifted(
            str(replayed["payload"]["issued_at"])
        )
        evidence["replay_shape"] = "same_sequence_distinct_digest"
    else:
        evidence["replay_shape"] = "lower_sequence"
    evidence["published_catalog_sequence"] = published
    replayed["signature"] = hashlib.sha256(
        canonical_json(replayed["payload"])
    ).hexdigest()
    replay_service = _service_for(root, artifacts, approvals)
    replay_service._catalog_verifier = DigestPinVerifier(
        hashlib.sha256(canonical_json(replayed["payload"])).hexdigest()
    )
    try:
        replay_service.import_release(
            catalog=replayed,
            manifest=dict(artifacts.manifest),
            attestation=dict(artifacts.attestation),
            patch_ledger=(
                dict(artifacts.patch_ledger)
                if artifacts.patch_ledger is not None
                else None
            ),
            artifact=artifacts.artifact,
        )
    except CatalogReplayError:
        observed["replayed_sequence"] = "CatalogReplayError"
    except Exception as exc:  # noqa: BLE001
        observed["replayed_sequence"] = f"unexpected:{type(exc).__name__}"
        failures.append(f"replayed catalog refused with an unexpected type: {exc}")
    else:
        observed["replayed_sequence"] = "accepted"
        failures.append("a replayed catalog sequence was accepted")

    equivocating = copy.deepcopy(dict(artifacts.catalog))
    equivocating["payload"]["issued_at"] = _shifted(
        str(equivocating["payload"]["issued_at"])
    )
    equivocating["signature"] = hashlib.sha256(
        canonical_json(equivocating["payload"])
    ).hexdigest()
    equivocation_service = _service_for(root, artifacts, approvals)
    equivocation_service._catalog_verifier = DigestPinVerifier(
        hashlib.sha256(canonical_json(equivocating["payload"])).hexdigest()
    )
    try:
        equivocation_service.import_release(
            catalog=equivocating,
            manifest=dict(artifacts.manifest),
            attestation=dict(artifacts.attestation),
            patch_ledger=(
                dict(artifacts.patch_ledger)
                if artifacts.patch_ledger is not None
                else None
            ),
            artifact=artifacts.artifact,
        )
    except CatalogReplayError:
        observed["equivocating_sequence"] = "CatalogReplayError"
    except Exception as exc:  # noqa: BLE001
        observed["equivocating_sequence"] = f"unexpected:{type(exc).__name__}"
        failures.append(f"equivocating catalog refused with an unexpected type: {exc}")
    else:
        observed["equivocating_sequence"] = "accepted"
        failures.append("an equivocating catalog sequence was accepted")
    return observed


def _shifted(moment: str) -> str:
    parsed = datetime.fromisoformat(moment.removesuffix("Z") + "+00:00")
    return parsed.replace(microsecond=(parsed.microsecond + 1) % 1_000_000).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Proof 9 — the patch ledger
# ---------------------------------------------------------------------------


def prove_patch_ledger_dispositions(artifacts: ArtifactSet) -> tuple[Proof, int]:
    """§5.9 — a disposition per patch, and the divergence count A2 drives to zero."""

    ledger = artifacts.patch_ledger
    if ledger is None:
        return Proof(9, False, {}, "release carries no patch ledger"), 0
    patches = list(ledger.get("patches", []))
    dispositions: dict[str, int] = {}
    failures: list[str] = []
    for patch in patches:
        disposition = patch.get("disposition")
        if not isinstance(disposition, str) or not disposition:
            failures.append(f"patch {patch.get('patch_id')} has no disposition")
            continue
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
    # "Divergence" is what the fork still carries over upstream: every patch the
    # release records as required or carried. A2's goal drives this to zero, and
    # the harness prints it rather than judging it — a non-zero count is a fact
    # about the fork, not a failure of the build.
    divergence = sum(
        count
        for disposition, count in dispositions.items()
        if disposition not in {"upstreamed", "dropped"}
    )
    evidence = {
        "patches": len(patches),
        "dispositions": dict(sorted(dispositions.items())),
        "divergence_count": divergence,
        "upstream_commit": ledger.get("upstream_commit"),
    }
    return Proof(9, not failures and bool(patches), evidence, "; ".join(failures)), divergence


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


#: Proofs 3–8 need a running worker, a control store and a real interpreter, so
#: they are supplied by the caller that owns those. Nothing shipped supplies
#: them: `certify` never passes this callable, so the installed command's report
#: says so through `missing`, which is why `certified` requires a proof for
#: every clause. The only caller that passes real ones today is an out-of-tree
#: acceptance driver.
RuntimeProofs = Callable[[ArtifactSet, Path], Sequence[Proof]]


def _remove_run_workspace(run_root: Path, owned_root: Path | None) -> str | None:
    """Remove exactly what this run created, and report rather than raise.

    ⟦ADJ-08⟧ Two things were wrong with `shutil.rmtree(..., ignore_errors=True)`
    here. It ran only when the harness owned the workspace, so the command —
    which always supplied one — never cleaned up at all and every run left two
    full expansions behind. And it could not have cleaned them anyway: import
    seals its slots 0o500/0o400, which `rmtree` cannot unlink and
    `ignore_errors` then silently swallows. `_remove_tree` is the chmod-walk
    that already exists for sealed managed trees.

    What is removed is only the per-run subdirectory the harness made. The
    caller's `--workspace` may be an ordinary operator directory that already
    holds data — `run_harness` accepts a pre-existing non-empty one — so
    removing that root is never correct, however tempting the symmetry.

    A failure is returned. `_remove_tree` raises on a symlink and does not
    swallow OSError, and a cleanup fault must not throw away a report whose
    proofs all ran.
    """

    for path in (run_root, owned_root):
        if path is None:
            continue
        try:
            _remove_tree(path)
        except Exception as exc:  # noqa: BLE001 - reported, never raised over a report
            return f"{path}: {type(exc).__name__}: {exc}"
    return None


def run_harness(
    artifacts: ArtifactSet,
    *,
    workspace: Path | None = None,
    approvals: ReleaseApprovalGate | None,
    runtime_proofs: RuntimeProofs | None = None,
    sandbox_profiles: Mapping[str, str] | None = None,
    interpreter_sha256: str | None = None,
) -> CertificationReport:
    """Run every proof the inputs make expressible, and report all of them.

    `sandbox_profiles` maps a profile digest to the profile text itself, for
    every profile the run generated, and `interpreter_sha256` names the
    interpreter the worker proofs ran on. Both are required to be consistent
    with what the proofs claim; see `CertificationReport.binding_failures`.
    """

    owned = workspace is None
    root = Path(workspace) if workspace is not None else Path(
        tempfile.mkdtemp(prefix="cortex-certify-")
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Everything this run writes goes under one directory the harness made, so
    # cleanup can name exactly its own bytes and never the caller's.
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    try:
        proofs: list[Proof] = [
            prove_artifact_set_is_consistent(artifacts),
            prove_import_accepts_and_refuses(artifacts, run_root, approvals=approvals),
        ]
        ledger_proof, divergence = prove_patch_ledger_dispositions(artifacts)
        if runtime_proofs is not None:
            proofs.extend(runtime_proofs(artifacts, run_root))
        proofs.append(ledger_proof)
    finally:
        cleanup_error = _remove_run_workspace(run_root, root if owned else None)
    return CertificationReport(
        schema_version=CERTIFICATION_SCHEMA_VERSION,
        release_id=str(artifacts.manifest.get("release_id", "")),
        manifest_sha256=digest_document(dict(artifacts.manifest)),
        artifact_sha256=hashlib.sha256(artifacts.artifact.read_bytes()).hexdigest(),
        proofs=tuple(sorted(proofs, key=lambda proof: proof.number)),
        divergence_count=divergence,
        ran_at=datetime.now(timezone.utc).isoformat(),
        sandbox_profiles=dict(sandbox_profiles or {}),
        interpreter_sha256=interpreter_sha256,
        workspace_cleanup_error=cleanup_error,
    )
