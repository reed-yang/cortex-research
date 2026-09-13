"""D5: `cortex-hermes certify` — the §5 harness over an artifact set on disk.

Deviation from D5, recorded rather than hidden: this is the harness half only.
Producing the artifact set stays in `tools/package_hermes_release.py`, which the
wheel does not ship — putting pip-invoking packaging code inside the installed
product would give it a capability it has no reason to have. Certification of a
build is therefore two steps in a checkout.

Second deviation, recorded ⟦ADJ-07⟧: this command cannot certify anything, and
said otherwise for three paragraphs. Clauses §5.3 to §5.8 need a running worker
on the slot's own interpreter, and nothing here supplies one — `certify` never
passes `run_harness` its `runtime_proofs` callable. What ships proves §5.1, §5.2
and §5.9, reports the other six clauses as missing, and because
`CertificationReport.certified` requires a proof for every clause it exits 1 for
every release, unconditionally. Those six are proved today only by an
out-of-tree acceptance driver that is not in this repository.

There is deliberately no flag that changes this. A selector resolving runtime
proofs to an import target would put the stub seam §7 exists to forbid on the
command line: `run_harness` extends its proof list with whatever the callable
returns and validates nothing. Closing the gap means implementing those clauses
here against a real staged slot, not accepting them from a caller.

See `docs/plans/2026-09-02-s35-certification-notes.md`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .certification import (
    ArtifactSet,
    CertificationError,
    CertificationReport,
    run_harness,
)

EVIDENCE_NAME = "certification.json"

#: Where `write_evidence` drops a readable copy of each profile in the record.
PROFILE_DIRECTORY = "sandbox-profiles"


def certify(*, artifacts: Path, workspace: Path | None = None) -> CertificationReport:
    """Run the harness over an artifact set and return its report.

    ⟦ADJ-07⟧ There is no `runtime_proofs` parameter. The one that used to be
    here was dead — `main` never passed it — while three docstrings described
    the command as though it were reachable from the command line.

    A `workspace` of None means the harness makes, and removes, its own.
    """

    return run_harness(
        ArtifactSet.load(artifacts),
        workspace=workspace,
        # ⟦ADJ-17⟧ No gate, rather than a permissive one. The gate is D6's, and
        # certification is not an operator approval: a release is certifiable
        # before anyone has decided to run it. The harness's only gate-bearing
        # path is `import_release`, which does not consult one — activation and
        # attempt pinning do — so passing none is both sufficient here and
        # fail-closed everywhere else. Naming the approve-everything gate here
        # would have shipped a working approval bypass inside the installed
        # wheel; the harness never needed one.
        approvals=None,
    )


def write_evidence(report: CertificationReport, destination: Path) -> Path:
    """Write `certification.json`, and every sandbox profile it names beside it.

    ⟦ADJ-09⟧ The profile text is inside the document, so `evidence_sha256`
    covers the bytes; the files here are the readable copy. Before this, the
    only profile digest in the record belonged to no file in the evidence
    directory, and the file that was there appeared in the record nowhere — so
    replacing it with `(allow default)` left the digest untouched.
    """

    destination.mkdir(parents=True, exist_ok=True)
    if report.sandbox_profiles:
        profiles = destination / PROFILE_DIRECTORY
        profiles.mkdir(parents=True, exist_ok=True)
        for digest, text in sorted(report.sandbox_profiles.items()):
            (profiles / f"{digest}.sb").write_text(text, encoding="utf-8")
    document = report.to_dict() | {"evidence_sha256": report.evidence_sha256}
    path = destination / EVIDENCE_NAME
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _build_parser() -> argparse.ArgumentParser:
    """The parser, extracted so a test can compare it against the help text.

    ⟦ADJ-07⟧ `description=__doc__` prints the module docstring verbatim under
    `--help`, which is how it came to advertise a `--runtime-proofs` flag that
    argparse had never been told about.
    """

    parser = argparse.ArgumentParser(prog="cortex-hermes", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    certify_parser = commands.add_parser(
        "certify",
        help="run the §5.1/5.2/5.9 document harness over a packaged artifact set",
    )
    certify_parser.add_argument(
        "--artifacts",
        type=Path,
        required=True,
        help="directory holding manifest.json, catalog.json, attestation.json and the zip",
    )
    certify_parser.add_argument(
        "--workspace",
        type=Path,
        help=(
            "directory to expand into; the command creates and removes a private "
            "subdirectory inside it and never touches anything else there. "
            "Omit it to use a temporary directory the command owns outright."
        ),
    )
    certify_parser.add_argument("--evidence-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        report = certify(
            artifacts=arguments.artifacts, workspace=arguments.workspace
        )
    except CertificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if arguments.evidence_output is not None:
        write_evidence(report, arguments.evidence_output)
    print(
        json.dumps(
            {
                "release_id": report.release_id,
                "certified": report.certified,
                "divergence_count": report.divergence_count,
                "missing_proofs": list(report.missing),
                "binding_failures": list(report.binding_failures),
                "workspace_cleanup_error": report.workspace_cleanup_error,
                "failed_proofs": [
                    proof.number for proof in report.proofs if not proof.passed
                ],
                "evidence_sha256": report.evidence_sha256,
            },
            sort_keys=True,
        )
    )
    return 0 if report.certified else 1


if __name__ == "__main__":
    raise SystemExit(main())
