"""The harness's own tests, which §7 permits to use a synthetic release.

What they must not do is stand in for the acceptance: the real fork proofs are
supplied by `certify`, and `certified` is false while any clause has no proof at
all — so a synthetic run can never report a certified release.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

import pytest

from approval_gate import AllowUnapprovedReleases
from cortex_platform.product.runtime_update.certification import (
    PROOF_NAMES,
    ArtifactSet,
    CertificationError,
    Proof,
    prove_artifact_set_is_consistent,
    prove_import_accepts_and_refuses,
    prove_patch_ledger_dispositions,
    run_harness,
)
from cortex_platform.product.runtime_update.models import canonical_json


def _artifacts(tmp_path: Path, release_factory, **overrides) -> ArtifactSet:
    artifact, manifest, catalog, attestation = release_factory(**overrides)
    ledger = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "upstream_commit": manifest["upstream_commit"],
        "patches": [],
    }
    return ArtifactSet(
        catalog=catalog,
        manifest=manifest,
        attestation=attestation,
        patch_ledger=ledger,
        artifact=artifact,
    )


def _with_patches(artifacts: ArtifactSet, patches: list[dict]) -> ArtifactSet:
    """Rebuild the set so the ledger digest, manifest and catalog all agree."""

    ledger = dict(artifacts.patch_ledger or {})
    ledger["patches"] = patches
    manifest = dict(artifacts.manifest)
    manifest["patch_set_sha256"] = hashlib.sha256(canonical_json(ledger)).hexdigest()
    catalog = json.loads(json.dumps(dict(artifacts.catalog)))
    digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
    for entry in catalog["payload"]["entries"]:
        entry["manifest_sha256"] = digest
    catalog["signature"] = hashlib.sha256(
        canonical_json(catalog["payload"])
    ).hexdigest()
    return ArtifactSet(
        catalog=catalog,
        manifest=manifest,
        attestation=artifacts.attestation,
        patch_ledger=ledger,
        artifact=artifacts.artifact,
    )


def test_proof_one_reads_the_set_against_itself(tmp_path: Path, release_factory) -> None:
    artifacts = _with_patches(_artifacts(tmp_path, release_factory), [])

    proof = prove_artifact_set_is_consistent(artifacts)

    assert proof.number == 1 and proof.name == PROOF_NAMES[1]
    assert proof.passed, proof.detail
    assert proof.evidence["catalog_status"] == "certified"
    assert proof.evidence["artifact_sha256"] == artifacts.manifest["artifact_sha256"]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("artifact_sha256", "0" * 64, "artifact hash"),
        ("upstream_tag", "v0.0.0", "provenance"),
    ],
)
def test_proof_one_names_each_disagreement(
    tmp_path: Path, release_factory, field: str, value: str, expected: str
) -> None:
    artifacts = _with_patches(_artifacts(tmp_path, release_factory), [])
    manifest = dict(artifacts.manifest)
    manifest[field] = value
    broken = ArtifactSet(
        catalog=artifacts.catalog,
        manifest=manifest,
        attestation=artifacts.attestation,
        patch_ledger=artifacts.patch_ledger,
        artifact=artifacts.artifact,
    )

    proof = prove_artifact_set_is_consistent(broken)

    assert proof.passed is False
    assert expected in proof.detail


def test_proof_two_accepts_once_and_refuses_every_mutation(
    tmp_path: Path, release_factory
) -> None:
    artifacts = _with_patches(_artifacts(tmp_path, release_factory), [])

    proof = prove_import_accepts_and_refuses(
        artifacts, tmp_path / "workspace", approvals=AllowUnapprovedReleases()
    )

    assert proof.passed, proof.detail
    assert proof.evidence["accepted"] is True
    refusals = proof.evidence["refusals"]
    assert set(refusals) == {
        "wrong_artifact_hash",
        "uncertified_status",
        "manifest_catalog_mismatch",
        "provenance_mismatch",
        "patch_ledger_mismatch",
        "replayed_sequence",
        "equivocating_sequence",
    }
    assert all(outcome != "accepted" for outcome in refusals.values()), refusals


def _at_catalog_sequence(artifacts: ArtifactSet, sequence: int) -> ArtifactSet:
    """The same release, published under a different catalog sequence.

    Only the catalog moves. The manifest's own `release_sequence` must stay
    >= 1 to remain valid, so it is deliberately left alone: what proof 2 is
    parameterised over is the trusted high-water mark, not the release number.
    """

    catalog = json.loads(json.dumps(dict(artifacts.catalog)))
    catalog["payload"]["sequence"] = sequence
    catalog["signature"] = hashlib.sha256(
        canonical_json(catalog["payload"])
    ).hexdigest()
    return ArtifactSet(
        catalog=catalog,
        manifest=artifacts.manifest,
        attestation=artifacts.attestation,
        patch_ledger=artifacts.patch_ledger,
        artifact=artifacts.artifact,
    )


@pytest.mark.parametrize("sequence", [0, 1, 182])
def test_proof_two_holds_at_every_legal_catalog_sequence(
    tmp_path: Path, release_factory, sequence: int
) -> None:
    """⟦ADJ-18⟧ Zero is a legal catalog sequence, so it must not fail the proof.

    `sequence - 1` is -1 at zero, which no catalog may carry, so `import_release`
    refused it with `ValidationError` before it ever reached the replay check —
    and the harness recorded `unexpected:ValidationError` and failed a
    well-formed release. The replay must be expressed in a shape that exists at
    every sequence the catalog schema permits.
    """

    artifacts = _at_catalog_sequence(
        _with_patches(_artifacts(tmp_path, release_factory), []), sequence
    )

    proof = prove_import_accepts_and_refuses(
        artifacts, tmp_path / f"workspace-{sequence}", approvals=None
    )

    assert proof.passed, proof.detail
    assert proof.evidence["refusals"]["replayed_sequence"] == "CatalogReplayError"
    assert proof.evidence["replay_shape"] in {
        "lower_sequence",
        "same_sequence_distinct_digest",
    }


def test_each_refusal_imports_into_its_own_runtime_root(
    tmp_path: Path, release_factory
) -> None:
    """§7: a refusal that shared state with the accept case proves nothing."""

    artifacts = _with_patches(_artifacts(tmp_path, release_factory), [])
    workspace = tmp_path / "workspace"

    prove_import_accepts_and_refuses(
        artifacts, workspace, approvals=AllowUnapprovedReleases()
    )

    # The accept case produced a slot; no refusal root did, and none of them is
    # the accept root. A refusal that shared a root could be inherited from the
    # state the passing import left behind.
    accepted = workspace / "import-accept"
    assert sorted(item.name for item in (accepted / "slots" / "sha256").iterdir())
    refusal_roots = [
        item
        for item in workspace.iterdir()
        if item.is_dir() and item.name.startswith("import-refuse-")
    ]
    for root in refusal_roots:
        assert root != accepted
        assert not (root / "slots" / "sha256").exists() or not sorted(
            (root / "slots" / "sha256").iterdir()
        )
    # One shared root for the two sequence cases, because a replay is only
    # expressible against a sequence that root has already seen.
    assert (workspace / "import-sequence" / "slots" / "sha256").is_dir()


def test_proof_nine_counts_divergence_without_judging_it(
    tmp_path: Path, release_factory
) -> None:
    artifacts = _with_patches(
        _artifacts(tmp_path, release_factory),
        [
            {
                "patch_id": "cortex-aaaa-one",
                "source_commit": "a" * 40,
                "patch_sha256": "b" * 64,
                "disposition": "required",
            },
            {
                "patch_id": "cortex-bbbb-two",
                "source_commit": "c" * 40,
                "patch_sha256": "d" * 64,
                "disposition": "upstreamed",
            },
        ],
    )

    proof, divergence = prove_patch_ledger_dispositions(artifacts)

    assert proof.passed, proof.detail
    assert proof.evidence["patches"] == 2
    assert proof.evidence["dispositions"] == {"required": 1, "upstreamed": 1}
    # A2's number: what the fork still carries over upstream.
    assert divergence == 1 and proof.evidence["divergence_count"] == 1


def test_a_patch_without_a_disposition_fails_the_proof(
    tmp_path: Path, release_factory
) -> None:
    artifacts = _with_patches(
        _artifacts(tmp_path, release_factory),
        [{"patch_id": "cortex-aaaa-one", "source_commit": "a" * 40,
          "patch_sha256": "b" * 64}],
    )

    proof, _divergence = prove_patch_ledger_dispositions(artifacts)

    assert proof.passed is False
    assert "no disposition" in proof.detail


def test_a_report_without_the_runtime_proofs_is_not_certified(
    tmp_path: Path, release_factory
) -> None:
    """The rule that stops a synthetic run standing in for the acceptance."""

    artifacts = _with_patches(
        _artifacts(tmp_path, release_factory),
        [{"patch_id": "cortex-aaaa-one", "source_commit": "a" * 40,
          "patch_sha256": "b" * 64, "disposition": "required"}],
    )

    report = run_harness(
        artifacts,
        workspace=tmp_path / "workspace",
        approvals=AllowUnapprovedReleases(),
    )

    assert [proof.number for proof in report.proofs] == [1, 2, 9]
    assert report.missing == (3, 4, 5, 6, 7, 8)
    assert report.certified is False
    assert report.divergence_count == 1


PROFILE_KEY = "sandbox_profile_sha256"

# The shape a real acceptance run must hand `run_harness`. Two profiles because
# binding 443 needs root: the probe runs under a bindable port generated through
# the same interpolation, and the record must carry both and disclose why.
PRODUCTION_PROFILE = (
    '(version 1)\n(deny default)\n(allow network-outbound (remote tcp "*:443"))\n'
)
PROBED_PROFILE = (
    '(version 1)\n(deny default)\n(allow network-outbound (remote tcp "*:51234"))\n'
)
PRODUCTION_DIGEST = hashlib.sha256(PRODUCTION_PROFILE.encode("utf-8")).hexdigest()
PROBED_DIGEST = hashlib.sha256(PROBED_PROFILE.encode("utf-8")).hexdigest()
INTERPRETER_DIGEST = "e" * 64


def _bound_runtime_proofs() -> list[Proof]:
    """Proofs 3-8 shaped so every claim they make is one the record binds."""

    return [
        Proof(3, True, {"identity": "hermes", PROFILE_KEY: PRODUCTION_DIGEST}),
        Proof(4, True, {"durable_operation_deduplication": True,
                        PROFILE_KEY: PRODUCTION_DIGEST}),
        Proof(5, True, {
            PROFILE_KEY: PROBED_DIGEST,
            "production_profile_sha256": PRODUCTION_DIGEST,
            "production_profile_egress_rule":
                '(allow network-outbound (remote tcp "*:443"))',
            "mapping_note":
                "binding 443 needs root, so the rule was generated for a bindable "
                "port through the same interpolation the production profile uses",
            "connect_port_0": "allowed",
            "connect_port_1": "denied:sandbox",
        }),
        Proof(6, True, {"reason_code": "runtime_activation_disabled"}),
        Proof(7, True, {
            "run_state": "completed",
            PROFILE_KEY: PRODUCTION_DIGEST,
            "dispatch_gate_proved_separately":
                "acceptance §11 — proof 7 drives RunOrchestrator with a "
                "descriptor-shaped pin producer, not RuntimeUpdateService."
                "pin_attempt, so D6's dispatch gate is proved outside this path",
        }),
        Proof(8, True, {"only_in_effect_process": True}),
    ]


SANDBOX_PROFILES = {
    PRODUCTION_DIGEST: PRODUCTION_PROFILE,
    PROBED_DIGEST: PROBED_PROFILE,
}


def _certified_report(tmp_path: Path, release_factory, **overrides):
    artifacts = _with_patches(
        _artifacts(tmp_path, release_factory),
        [{"patch_id": "cortex-aaaa-one", "source_commit": "a" * 40,
          "patch_sha256": "b" * 64, "disposition": "required"}],
    )
    keywords = {
        "workspace": tmp_path / "workspace",
        "approvals": None,
        "runtime_proofs": lambda _artifacts, _workspace: _bound_runtime_proofs(),
        "sandbox_profiles": dict(SANDBOX_PROFILES),
        "interpreter_sha256": INTERPRETER_DIGEST,
    }
    keywords.update(overrides)
    return run_harness(artifacts, **keywords)


def test_a_report_with_every_proof_is_certified_and_digests_itself(
    tmp_path: Path, release_factory
) -> None:
    report = _certified_report(tmp_path, release_factory)

    assert report.missing == ()
    assert report.binding_failures == ()
    assert report.certified is True
    assert [proof.number for proof in report.proofs] == list(range(1, 10))
    # The digest a manifest's `evidence_sha256` may finally name.
    assert len(report.evidence_sha256) == 64
    assert report.evidence_sha256 == run_harness.__globals__["hashlib"].sha256(
        canonical_json(report.to_dict())
    ).hexdigest()


def test_stub_proofs_cannot_report_a_certified_release(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-09⟧ The seam the harness's own tests used to drive to green.

    `Proof(n, True, {})` asserted six clauses while naming no profile, no
    production profile and no interpreter — nothing a reader could check and
    nothing the digest covered. A record that claims the sandbox proof without
    saying which policy was loaded is the shape the refuter exploited.
    """

    report = _certified_report(
        tmp_path,
        release_factory,
        runtime_proofs=lambda _a, _w: [
            Proof(number, True, {"stub": True}) for number in (3, 4, 5, 6, 7, 8)
        ],
        sandbox_profiles={},
        interpreter_sha256=None,
    )

    assert report.missing == ()
    assert report.certified is False
    joined = "; ".join(report.binding_failures)
    assert "proof 5 names no sandbox_profile_sha256" in joined
    assert "proof 5 names no production_profile_sha256" in joined
    assert "proof 7 does not record dispatch_gate_proved_separately" in joined
    assert "names no interpreter_sha256" in joined


def test_a_profile_a_proof_names_but_the_record_omits_refuses_certification(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-09⟧ The exact gap: proof 7's digest belonged to no file anywhere."""

    report = _certified_report(
        tmp_path,
        release_factory,
        sandbox_profiles={PROBED_DIGEST: PROBED_PROFILE},
    )

    assert report.certified is False
    assert any(
        f"names sandbox profile {PRODUCTION_DIGEST}" in failure
        for failure in report.binding_failures
    ), report.binding_failures


def test_a_profile_whose_text_does_not_hash_to_its_key_refuses_certification(
    tmp_path: Path, release_factory
) -> None:
    report = _certified_report(
        tmp_path,
        release_factory,
        sandbox_profiles=dict(SANDBOX_PROFILES) | {PROBED_DIGEST: "(allow default)\n"},
    )

    assert report.certified is False
    assert any("hashes to" in failure for failure in report.binding_failures)


def test_a_substituted_profile_must_be_disclosed_inside_the_proof(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-09⟧ The disclosure lived in acceptance.json, outside the digest.

    Deleting the mapping note and rewriting the egress rule left
    `evidence_sha256` byte-identical. Both now sit inside proof 5's evidence,
    and a probe under a profile that is not the production one is a binding
    failure without them.
    """

    stripped = [
        Proof(5, True, {
            PROFILE_KEY: PROBED_DIGEST,
            "production_profile_sha256": PRODUCTION_DIGEST,
        })
        if proof.number == 5
        else proof
        for proof in _bound_runtime_proofs()
    ]

    report = _certified_report(
        tmp_path, release_factory, runtime_proofs=lambda _a, _w: stripped
    )

    assert report.certified is False
    joined = "; ".join(report.binding_failures)
    assert "does not disclose mapping_note" in joined
    assert "does not disclose production_profile_egress_rule" in joined


def test_rewriting_a_profile_moves_the_evidence_digest(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-09⟧ The refuter's demonstration, now impossible.

    The profile text is inside the record, so `(allow default)` cannot be
    swapped in behind a digest that stays the same.
    """

    honest = _certified_report(tmp_path, release_factory)
    permissive = "(version 1)\n(allow default)\n"
    swapped = _certified_report(
        tmp_path,
        release_factory,
        sandbox_profiles=dict(SANDBOX_PROFILES)
        | {hashlib.sha256(permissive.encode("utf-8")).hexdigest(): permissive},
    )

    assert honest.evidence_sha256 != swapped.evidence_sha256




# --- the command ------------------------------------------------------------


def test_the_permissive_gate_has_no_importer_in_the_shipped_package() -> None:
    """⟦ADJ-17⟧ D6 stays fail-closed only while nothing production ships opts out.

    `AllowUnapprovedReleases` approves every release by construction. It exists
    so that "no gate configured" can stay fail-closed in tests and tools, and
    the wheel ships `cortex_platform`, so a production importer would put a
    working approval bypass inside the installed product. `certify` was that
    importer; the harness only ever calls `import_release`, which does not
    consult the gate at all, so the honest gate for a certification run is no
    gate — which refuses at every path that does consult one. The class has
    since moved to `tests/conftest.py`, so the shipped package no longer even
    defines it.
    """

    package = Path(__file__).resolve().parents[3] / "cortex_platform"
    assert package.is_dir()
    mentions = sorted(
        str(source.relative_to(package))
        for source in package.rglob("*.py")
        if "AllowUnapprovedReleases" in source.read_text(encoding="utf-8")
    )

    # Zero mentions, not just zero importers. The definition itself lives in
    # `tests/conftest.py`, which is not packaged, so the escape hatch cannot
    # leave this repository at all.
    assert mentions == [], mentions


def _artifact_directory(tmp_path: Path, release_factory) -> Path:
    artifacts = _with_patches(
        _artifacts(tmp_path, release_factory),
        [{"patch_id": "cortex-aaaa-one", "source_commit": "a" * 40,
          "patch_sha256": "b" * 64, "disposition": "required"}],
    )
    directory = tmp_path / "release"
    directory.mkdir()
    (directory / "manifest.json").write_bytes(canonical_json(dict(artifacts.manifest)))
    (directory / "catalog.json").write_bytes(canonical_json(dict(artifacts.catalog)))
    (directory / "attestation.json").write_bytes(
        canonical_json(dict(artifacts.attestation))
    )
    (directory / "patch_ledger.json").write_bytes(
        canonical_json(dict(artifacts.patch_ledger))
    )
    target = directory / str(artifacts.manifest["artifact_filename"])
    target.write_bytes(artifacts.artifact.read_bytes())
    return directory


def test_the_command_reports_an_incomplete_run_as_uncertified(
    tmp_path: Path, release_factory, capsys
) -> None:
    """§7 enforced by the exit code, not by a reader noticing."""

    from cortex_platform.product.runtime_update.certify_cli import main

    directory = _artifact_directory(tmp_path, release_factory)

    code = main(
        [
            "certify",
            "--artifacts",
            str(directory),
            "--workspace",
            str(tmp_path / "workspace"),
            "--evidence-output",
            str(tmp_path / "evidence"),
        ]
    )

    assert code == 1
    reported = json.loads(capsys.readouterr().out)
    assert reported["certified"] is False
    assert reported["missing_proofs"] == [3, 4, 5, 6, 7, 8]
    assert reported["failed_proofs"] == []
    assert reported["divergence_count"] == 1
    evidence = json.loads(
        (tmp_path / "evidence" / "certification.json").read_text(encoding="utf-8")
    )
    assert evidence["evidence_sha256"] == reported["evidence_sha256"]
    assert [proof["proof"] for proof in evidence["proofs"]] == [1, 2, 9]


def _flags(text: str) -> set[str]:
    return set(re.findall(r"--[a-z0-9][a-z0-9-]*", text))


def _accepted_flags(parser) -> set[str]:
    accepted = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    for action in parser._actions:
        for child in (getattr(action, "choices", None) or {}).values():
            accepted |= _accepted_flags(child)
    return accepted


def _help_texts(parser) -> list[str]:
    texts = [parser.format_help()]
    for action in parser._actions:
        for child in (getattr(action, "choices", None) or {}).values():
            texts.extend(_help_texts(child))
    return texts


def test_the_help_text_advertises_only_flags_the_parser_accepts() -> None:
    """⟦ADJ-07⟧ `description=__doc__` prints the module docstring verbatim.

    The shipped help advertised `--runtime-proofs` as "what turns a document
    check into a certification". argparse had never been told about it, so the
    flag that was supposed to make the command able to certify did not exist,
    and the command exited 1 for every release. Help text and parser must not
    be able to disagree again.
    """

    from cortex_platform.product.runtime_update import certify_cli

    parser = certify_cli._build_parser()
    accepted = _accepted_flags(parser)
    advertised = _flags(certify_cli.__doc__ or "")
    for text in _help_texts(parser):
        advertised |= _flags(text)

    assert advertised <= accepted, sorted(advertised - accepted)

    # And the check is live rather than vacuous: put the sentence that used to
    # ship back into the description and it must be caught.
    parser.description = (certify_cli.__doc__ or "") + (
        "\n`--runtime-proofs` is what turns a document check into a certification."
    )
    regressed = set()
    for text in _help_texts(parser):
        regressed |= _flags(text)
    assert "--runtime-proofs" in regressed - accepted


def test_the_command_removes_its_expansion_without_touching_the_workspace(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-08⟧ Every run leaked a sealed expansion `rm -rf` cannot remove.

    `--workspace` was required, so the command was always on the non-owning
    path and never cleaned up at all; the owning path's `ignore_errors=True`
    could not have unlinked the 0o500 directories and 0o400 files import seals
    anyway. Two full expansions per run, and a 263 MB leak on the real host.

    The other half matters just as much: `run_harness` accepts a pre-existing
    non-empty directory, so the operator's own files must survive.
    """

    from cortex_platform.product.runtime_update.certify_cli import main

    directory = _artifact_directory(tmp_path, release_factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    operator = workspace / "operator-notes.txt"
    operator.write_bytes(b"the operator was here first")

    code = main(
        ["certify", "--artifacts", str(directory), "--workspace", str(workspace)]
    )

    assert code == 1
    assert list(workspace.rglob("import-*")) == []
    assert operator.read_bytes() == b"the operator was here first"
    assert sorted(item.name for item in workspace.iterdir()) == ["operator-notes.txt"]


def test_the_command_can_own_and_remove_its_own_workspace(
    tmp_path: Path, release_factory, capsys, monkeypatch
) -> None:
    """⟦ADJ-08⟧ The owning path was unreachable: `--workspace` was required."""

    from cortex_platform.product.runtime_update.certify_cli import main

    directory = _artifact_directory(tmp_path, release_factory)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    code = main(["certify", "--artifacts", str(directory)])

    assert code == 1
    reported = json.loads(capsys.readouterr().out)
    assert reported["workspace_cleanup_error"] is None
    assert list(scratch.glob("cortex-certify-*")) == []


def test_every_profile_a_proof_names_is_written_beside_the_record(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-09⟧ The record named a profile digest no file in the run matched."""

    from cortex_platform.product.runtime_update.certify_cli import (
        PROFILE_DIRECTORY,
        write_evidence,
    )
    from cortex_platform.product.runtime_update.certification import (
        _named_profile_digests,
    )

    report = _certified_report(tmp_path, release_factory)
    destination = tmp_path / "evidence"

    path = write_evidence(report, destination)

    document = json.loads(path.read_text(encoding="utf-8"))
    named = {
        digest
        for proof in document["proofs"]
        for digest in _named_profile_digests(proof["evidence"])
    }
    assert named
    for digest in named:
        written = destination / PROFILE_DIRECTORY / f"{digest}.sb"
        assert written.is_file(), digest
        assert hashlib.sha256(written.read_bytes()).hexdigest() == digest
        # And the text is inside the digested document, not merely beside it.
        assert document["sandbox_profiles"][digest] == written.read_text(
            encoding="utf-8"
        )
    assert document["interpreter_sha256"] == INTERPRETER_DIGEST
    # Proof 5, the §5.5 sandbox proof, carried no profile digest at all.
    proof_five = next(proof for proof in document["proofs"] if proof["proof"] == 5)
    assert proof_five["evidence"][PROFILE_KEY] in named
    assert proof_five["evidence"]["production_profile_egress_rule"]
    assert proof_five["evidence"]["mapping_note"]


def test_the_command_refuses_an_artifact_set_it_cannot_read(
    tmp_path: Path, capsys
) -> None:
    from cortex_platform.product.runtime_update.certify_cli import main

    empty = tmp_path / "empty"
    empty.mkdir()

    assert main(
        ["certify", "--artifacts", str(empty), "--workspace", str(tmp_path / "w")]
    ) == 2
    assert "error:" in capsys.readouterr().err


def test_loading_an_incomplete_artifact_set_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CertificationError):
        ArtifactSet.load(tmp_path)


def test_loading_refuses_an_artifact_filename_that_leaves_the_directory(
    tmp_path: Path, release_factory
) -> None:
    """⟦ADJ-23⟧ `--artifacts` bounds the load, so the join must be validated.

    The planted file exists and is readable, so without the manifest passing
    through `ReleaseManifest.from_dict` the traversal resolves, is opened, and
    its digest is published on stdout and in `certification.json`.
    """

    directory = _artifact_directory(tmp_path, release_factory)
    (tmp_path / "outside.bin").write_bytes(b"bytes the operator never packaged")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifact_filename"] = "../outside.bin"
    (directory / "manifest.json").write_bytes(canonical_json(manifest))

    with pytest.raises(CertificationError):
        ArtifactSet.load(directory)
