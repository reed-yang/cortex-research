"""Explicit inputs for the retained Hermes acceptance driver.

`hermes_acceptance.py` is the only thing that has ever proved §5 clauses 3-8,
and it was written to run from one directory on one machine. It derived six
required inputs from its own location on disk — `OUTPUT/..` sibling lookups for
the closure, the vendored interpreter and the patched fork — and it wrote its
evidence into whatever directory it happened to live in. That is fine for a file
that lives beside its own evidence and nowhere else. It is not fine for a
tracked tool: moved into `tools/`, every one of those lookups silently retargets,
and the evidence lands in the checkout.

This module is the seam. It takes the inputs the driver needs and makes each one
an argument, so the driver states its requirements instead of inferring them
from a filesystem layout nobody declared.

**Refusals, not defaults.** Every input is required. The old sibling-directory
lookups are deliberately not reproduced as fallbacks: a default that resolves to
*something* is exactly how a run silently measures the wrong closure or the
wrong fork, and the record would still say `certified: true`. The one derived
value is `--product`, which keeps the driver's original behaviour of measuring
the worktree that `cortex_platform` was imported from, because that is the thing
being certified and taking it as an argument would let the record name a
worktree the run never used.

**Fresh output roots.** The original purged WORK and artifact-set mode replaces
`EVIDENCE_ROOT / "sandbox-profiles"`. Both writable roots must now be absent or
empty, disjoint, and outside supplied inputs, the checkout and retained evidence.
Preparation creates both before the first proof writes; it never purges an
existing work directory. Filesystem and home roots remain refused.

Nothing in this module runs the acceptance, imports the product, touches a
network, or reads a credential. `--help` and every refusal exit before the
driver's first side effect, which is what makes the tool safe to validate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

#: Directories whose contents are retained certification evidence. A run must
#: never be pointed at one as its own scratch or output space: `_purge` and the
#: `sandbox-profiles` replacement would delete the record the tool exists to
#: preserve. Matched by name so a moved workspace still refuses.
RETAINED_EVIDENCE_DIRECTORY_NAMES = frozenset(
    {
        "s31b-real-acceptance-20260901",
        "s35-real-acceptance-20260902",
        "hermes-release-gen9-20260902",
    }
)


class InputError(SystemExit):
    """A refusal the operator can act on, raised before any side effect."""

    def __init__(self, message: str) -> None:
        super().__init__(f"hermes-acceptance: {message}")


@dataclass(frozen=True)
class AcceptanceInputs:
    """Every path the driver needs, resolved and checked."""

    driver: Path
    closure: Path
    runtime: Path
    archive: Path
    pin: Path
    fork: Path
    product: Path
    work: Path
    evidence_root: Path
    record_name: str
    artifacts: Path | None

    @property
    def mode(self) -> str:
        return "artifact-set" if self.artifacts is not None else "self-packaged"


def _existing_directory(value: str, what: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise InputError(f"{what} is not a directory: {path}")
    return path


def _existing_file(value: str, what: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise InputError(f"{what} is not a file: {path}")
    return path


def _resolve_vendored_interpreter(runtime: Path) -> tuple[Path, Path]:
    """Find the one archive in `runtime` and the pin committed beside it.

    Discovered rather than typed, for the same reason `package_hermes_release`
    derives `minimum_os_version`: a hand-supplied filename can name an archive
    the pin does not describe, and the mismatch only surfaces later as a link
    error. Ambiguity is refused instead of resolved by ordering.
    """
    archives = sorted(
        candidate
        for candidate in runtime.glob("*.tar.gz")
        if not candidate.name.endswith(".attestation.json")
    )
    if not archives:
        raise InputError(f"no interpreter archive (*.tar.gz) under {runtime}")
    if len(archives) > 1:
        names = ", ".join(candidate.name for candidate in archives)
        raise InputError(
            f"{runtime} carries more than one interpreter archive: {names}"
        )
    archive = archives[0]
    pin = archive.with_suffix(archive.suffix + ".pin.json")
    if not pin.is_file():
        pin = Path(str(archive).removesuffix(".tar.gz") + ".pin.json")
    if not pin.is_file():
        raise InputError(f"no pin document beside {archive.name} in {runtime}")
    return archive, pin


def _refuse_unsafe_writable(path: Path, flag: str, checkout: Path) -> None:
    """Refuse a directory the run must not be allowed to delete."""
    if path == Path(path.anchor) or path == Path.home():
        raise InputError(f"{flag} may not be a filesystem or home root: {path}")
    if path == checkout or checkout in path.parents:
        raise InputError(
            f"{flag} may not resolve inside the checkout ({checkout}): {path}"
        )
    for candidate in (path, *path.parents):
        if candidate.name in RETAINED_EVIDENCE_DIRECTORY_NAMES:
            raise InputError(
                f"{flag} may not resolve inside retained evidence "
                f"({candidate}): {path}"
            )


def _checkout_root(product: Path) -> Path:
    return product


def _require_empty_directory(path: Path, flag: str) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise InputError(f"{flag} must be absent or empty; existing contents are not purged: {path}")


def prepare_directories(inputs: AcceptanceInputs) -> None:
    """Create the run's two fresh writable roots before any proof writes."""

    for path, flag in ((inputs.work, "--work"), (inputs.evidence_root, "--evidence-root")):
        _require_empty_directory(path, flag)
    inputs.work.mkdir(mode=0o700, parents=True, exist_ok=True)
    inputs.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)


def resolve_inputs(
    argv: list[str] | None = None,
    *,
    product: Path | None = None,
) -> AcceptanceInputs:
    """Parse and validate the driver's inputs.

    `product` is supplied by the caller because the driver derives it from the
    imported `cortex_platform`, and this module deliberately imports nothing
    from the product.
    """
    parser = argparse.ArgumentParser(
        prog="hermes-acceptance",
        description=(
            "Run the real Hermes acceptance that proves §5 clauses 3-8. "
            "Every input is explicit; nothing is inferred from where this "
            "file happens to live."
        ),
    )
    parser.add_argument(
        "--driver",
        required=True,
        help="attested acceptance_driver.py entrypoint the driven release names",
    )
    parser.add_argument(
        "--closure",
        required=True,
        help="S3.1b closure directory (wheelhouse, hermes wheel, requirements)",
    )
    parser.add_argument(
        "--runtime",
        required=True,
        help="directory holding the vendored CPython archive and its pin",
    )
    parser.add_argument(
        "--fork", required=True, help="patched Hermes fork checkout to read patches from"
    )
    parser.add_argument(
        "--work",
        required=True,
        help="scratch directory this run owns; must be absent or empty",
    )
    parser.add_argument(
        "--evidence-root",
        required=True,
        help="fresh directory the acceptance record and profiles are written to",
    )
    parser.add_argument(
        "--artifacts",
        default=None,
        help=(
            "certify an artifact set that already exists on disk instead of "
            "packaging a production release for this run"
        ),
    )
    parsed = parser.parse_args(argv)

    if product is None:
        raise InputError("the caller must supply the product worktree")
    product = Path(product).expanduser().resolve()

    driver = _existing_file(parsed.driver, "--driver")
    closure = _existing_directory(parsed.closure, "--closure")
    runtime = _existing_directory(parsed.runtime, "--runtime")
    fork = _existing_directory(parsed.fork, "--fork")
    archive, pin = _resolve_vendored_interpreter(runtime)

    for required in ("wheelhouse", "closure.requirements.txt"):
        if not (closure / required).exists():
            raise InputError(f"--closure is missing {required}: {closure}")

    work = Path(parsed.work).expanduser().resolve()
    evidence_root = Path(parsed.evidence_root).expanduser().resolve()
    checkout = _checkout_root(product)
    _refuse_unsafe_writable(work, "--work", checkout)
    _refuse_unsafe_writable(evidence_root, "--evidence-root", checkout)
    _require_empty_directory(work, "--work")
    _require_empty_directory(evidence_root, "--evidence-root")
    if work == evidence_root:
        raise InputError("--work may not also be --evidence-root")
    if work in evidence_root.parents or evidence_root in work.parents:
        raise InputError("--work and --evidence-root must not contain each other")

    artifacts = None
    if parsed.artifacts is not None:
        artifacts = _existing_directory(parsed.artifacts, "--artifacts")

    for output, flag in ((work, "--work"), (evidence_root, "--evidence-root")):
        for source in (closure, runtime, fork, artifacts):
            if source is not None and (output == source or source in output.parents):
                raise InputError(f"{flag} may not write inside a supplied input: {source}")

    return AcceptanceInputs(
        driver=driver,
        closure=closure,
        runtime=runtime,
        archive=archive,
        pin=pin,
        fork=fork,
        product=product,
        work=work,
        evidence_root=evidence_root,
        record_name=(
            "acceptance.full.json" if artifacts is not None else "acceptance.json"
        ),
        artifacts=artifacts,
    )
