"""Acquire and pin the product's third-party dependency closure.

Operator-run, once per lock. This is the second and last network step of the
embedded-runtime contract; `build_repository_bundle` consumes the cache offline.
The tool is deliberately outside `distribution/` and is never copied into a
bundle.

Contract: `docs/plans/2026-07-29-deploy-compat-embedded-python.md` §4.1, as
amended by §10 A14 — one distribution in the real lock (`peewee`) publishes no
wheel at all, so a bounded sdist fallback is required for the closure to be
acquirable at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from distribution.wheel_closure import (
    TARGET_ENVIRONMENT,
    WheelClosureError,
    evaluate_marker,
    normalize_name,
    parse_requirements_hashes,
)

REPOSITORY = Path(__file__).resolve().parents[1]
VENDOR_RELATIVE = "vendor/wheelhouse"
REQUIREMENTS_NAME = "closure.requirements.txt"
SIDE_RECORD_NAME = "built-from-sdist.json"

# AGPL. A licence boundary must fail closed on a name the resolver could pull in
# transitively, so it is named rather than inferred.
FORBIDDEN_DISTRIBUTIONS = frozenset({"backtesting"})

PLATFORM_TAGS = (
    "macosx_11_0_arm64",
    "macosx_12_0_arm64",
    "macosx_14_0_arm64",
    "macosx_26_0_arm64",
)
ABI_TAGS = ("cp314", "abi3", "none")
PYTHON_VERSION = "3.14"
IMPLEMENTATION = "cp"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)")

CommandRunner = Callable[[Sequence[str], Path], str]


class WheelhouseError(RuntimeError):
    """The dependency closure could not be acquired safely."""


def _default_runner(command: Sequence[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WheelhouseError(f"{command[0]} could not be run") from exc
    if completed.returncode != 0:
        raise WheelhouseError(f"{command[0]} failed: {completed.stderr.strip()[:400]}")
    return completed.stdout


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


# ---------------------------------------------------------------------------
# Export and filtering
# ---------------------------------------------------------------------------


def export_requirements(destination: Path, *, repository: Path, run: CommandRunner) -> Path:
    """Export the lock's universal, hash-pinned requirements."""

    run(
        [
            "uv",
            "export",
            "--frozen",
            "--offline",
            "--no-dev",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
            "-o",
            str(destination),
        ],
        repository,
    )
    if not destination.is_file():
        raise WheelhouseError("uv export produced no requirements file")
    return destination


def logical_lines(text: str) -> list[str]:
    """Join uv's backslash continuations into one line per requirement."""

    joined: list[str] = []
    buffer = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1].strip() + " "
            continue
        joined.append((buffer + stripped).strip())
        buffer = ""
    if buffer.strip():
        joined.append(buffer.strip())
    return joined


def filter_for_target(text: str) -> tuple[str, dict[str, str]]:
    """Keep the lines that apply to the target, and report their versions.

    Markers are evaluated with the same evaluator the bundle verifier uses, so
    acquisition and verification can never disagree about which requirements
    apply — the failure that would otherwise ship a closure the proof rejects.
    """

    retained: list[str] = []
    versions: dict[str, str] = {}
    for entry in logical_lines(text):
        # Split on `--hash=` FIRST, exactly as `parse_requirements_hashes`
        # does. Partitioning on `;` first puts every hash in the marker half of
        # a marker-bearing line, so the retained line silently loses the digests
        # that are the whole point of handing this file to `pip download`.
        head, *hashes = entry.split("--hash=")
        requirement, _, marker = head.partition(";")
        match = _REQUIREMENT.match(requirement.strip())
        if match is None:
            raise WheelhouseError(f"requirement line is unsupported: {entry[:80]!r}")
        marker_text = marker.strip()
        try:
            if marker_text and not evaluate_marker(marker_text, TARGET_ENVIRONMENT):
                continue
        except WheelClosureError as exc:
            raise WheelhouseError(f"unsupported requirement marker: {exc}") from exc
        name = normalize_name(match.group("name"))
        if name in FORBIDDEN_DISTRIBUTIONS:
            raise WheelhouseError(f"forbidden license boundary: {name}")
        if name in versions:
            raise WheelhouseError(f"duplicate requirement for the target: {name}")
        versions[name] = match.group("version")
        digests = " ".join(f"--hash={value.strip()}" for value in hashes)
        retained.append(f"{name}=={match.group('version')} {digests}".strip())
    if not retained:
        raise WheelhouseError("no requirement applies to the target environment")
    return "\n".join(retained) + "\n", versions


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def _download_command(requirements: Path, cache: Path) -> list[str]:
    return _wheel_download_command(["-r", str(requirements)], cache)


def _download_one_command(name: str, version: str, cache: Path) -> list[str]:
    """The same acquisition, for exactly one distribution.

    `pip download -r` resolves the whole file before fetching anything, so one
    distribution the index has no compatible wheel for fails all of them. The
    per-distribution form is what makes a failure attributable to the
    distribution that caused it.
    """

    return _wheel_download_command([f"{name}=={version}"], cache)


def _wheel_download_command(target: list[str], cache: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        *target,
        "--no-deps",
        "--only-binary=:all:",
        "--dest",
        str(cache),
        "--implementation",
        IMPLEMENTATION,
        "--python-version",
        PYTHON_VERSION,
    ]
    for tag in ABI_TAGS:
        command.extend(["--abi", tag])
    for tag in PLATFORM_TAGS:
        command.extend(["--platform", tag])
    return command


def _sdist_command(name: str, version: str, cache: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "download",
        f"{name}=={version}",
        "--no-deps",
        "--no-binary",
        ":all:",
        "--dest",
        str(cache),
    ]


def _build_command(sdist: Path, cache: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        str(sdist),
        "--no-deps",
        "--no-build-isolation",
        "--wheel-dir",
        str(cache),
    ]


def acquire_from_sdist(
    name: str,
    version: str,
    *,
    cache: Path,
    pinned: dict[str, set[str]],
    repository: Path,
    run: CommandRunner,
) -> tuple[Path, str]:
    """Build one wheel from the lock-pinned sdist, binding it to that sdist.

    Used only for a distribution the index offers no compatible wheel for. The
    lock still binds the *input*: the sdist's digest must be one the requirement
    line pins, and the derived wheel is recorded against it so the verifier can
    tell a locally built artifact from an unpinned one.
    """

    with tempfile.TemporaryDirectory(prefix="cortex-sdist-") as scratch:
        staging = Path(scratch)
        run(_sdist_command(name, version, staging), repository)
        archives = [path for path in sorted(staging.iterdir()) if path.is_file()]
        if len(archives) != 1:
            raise WheelhouseError(f"sdist acquisition for {name} was not exactly one archive")
        sdist = archives[0]
        sdist_digest = _digest(sdist)
        if sdist_digest not in pinned.get(name, set()):
            raise WheelhouseError(f"sdist digest for {name} is not pinned by the lock")
        run(_build_command(sdist, staging), repository)
        built = [
            path
            for path in sorted(staging.iterdir())
            if path.is_file() and path.name.endswith(".whl")
        ]
        if len(built) != 1:
            raise WheelhouseError(f"building {name} from its sdist produced no single wheel")
        # This is the only wheel in the whole closure whose bytes are produced
        # here rather than fetched from the index, so it is the only one whose
        # content depends on THIS machine. A pure-Python wheel is reproducible
        # from the sdist anywhere; the moment a build compiles an extension, the
        # result carries the vendor host's toolchain and is neither reproducible
        # nor covered by anything the lock pins. Refuse rather than ship it: a
        # future sdist-only dependency that genuinely needs a C extension is a
        # different and harder problem, and it should stop the build and force
        # the decision instead of silently baking in this host.
        if not built[0].name.endswith("-py3-none-any.whl"):
            raise WheelhouseError(
                f"wheel built from {name}'s sdist is not pure Python: {built[0].name}"
            )
        destination = cache / built[0].name
        shutil.copyfile(built[0], destination)
    return destination, sdist_digest


def _acquired_names(cache: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(cache.glob("*.whl")):
        fields = path.name.removesuffix(".whl").rsplit("-", 4)
        if len(fields) != 5:
            raise WheelhouseError(f"acquired wheel filename is unsupported: {path.name}")
        name = normalize_name(fields[0])
        if name in found:
            raise WheelhouseError(f"acquired two wheels for {name}")
        found[name] = path
    return found


def verify_acquisition(
    cache: Path,
    *,
    versions: dict[str, str],
    pinned: dict[str, set[str]],
    built_from_sdist: dict[str, dict[str, str]],
) -> None:
    """Bind every acquired wheel to the lock, from bytes, both directions."""

    acquired = _acquired_names(cache)
    if set(acquired) != set(versions):
        missing = sorted(set(versions) - set(acquired))
        extra = sorted(set(acquired) - set(versions))
        raise WheelhouseError(
            f"acquisition does not match the target closure; missing={missing} extra={extra}"
        )
    for name, path in acquired.items():
        digest = _digest(path)
        if digest in pinned.get(name, set()):
            continue
        built = built_from_sdist.get(name)
        # A wheel built here from a pinned sdist. The lock binds the input and
        # the record binds that input to this output — so the record must name
        # THESE bytes, or it explains some other wheel and this one is unbound.
        if built is not None and built["wheel_sha256"] == digest:
            continue
        raise WheelhouseError(f"acquired wheel for {name} is not pinned by the lock")


# ---------------------------------------------------------------------------
# Operator entry point
# ---------------------------------------------------------------------------


def vendor(
    *,
    repository: Path,
    vendor_root: Path | None = None,
    run: CommandRunner = _default_runner,
) -> dict[str, object]:
    """Acquire, verify, and cache the closure. Writes nothing on failure."""

    lock = repository / "uv.lock"
    if not lock.is_file():
        raise WheelhouseError("repository lock file is missing")
    lock_digest = _digest(lock)
    root = (vendor_root or repository / VENDOR_RELATIVE) / lock_digest
    with tempfile.TemporaryDirectory(prefix="cortex-wheelhouse-") as scratch:
        staging = Path(scratch)
        exported = export_requirements(
            staging / "universal.requirements.txt", repository=repository, run=run
        )
        # Filter BEFORE parsing hashes. The universal export legitimately lists
        # one distribution twice under complementary markers — `scipy` does, at
        # `python_full_version` < and >= '3.12' — and `parse_requirements_hashes`
        # refuses a repeated name by design, because merging two lines' digest
        # sets would accept a wheel matching either. The marker filter collapses
        # that pair to the single line which applies, which is exactly the state
        # that parser documents itself as expecting. Parsing the unfiltered
        # export instead made this tool abort on the real lock before acquiring
        # anything.
        filtered_text, versions = filter_for_target(exported.read_text(encoding="utf-8"))
        filtered = staging / REQUIREMENTS_NAME
        filtered.write_text(filtered_text)
        try:
            pinned = parse_requirements_hashes(filtered)
        except WheelClosureError as exc:
            raise WheelhouseError(f"exported requirements are unusable: {exc}") from exc

        cache = staging / "wheels"
        cache.mkdir()
        download_failure: WheelhouseError | None = None
        try:
            run(_download_command(filtered, cache), repository)
        except WheelhouseError as exc:
            # `pip download -r` resolves the ENTIRE file before fetching, so one
            # distribution the index has no compatible wheel for fails all of
            # them — measured against the real lock, `peewee` alone took the
            # other 155 with it. Retrying per distribution isolates the failure
            # to the distribution that caused it, so the sdist fallback below is
            # entered only by the ones that genuinely need it instead of by
            # everything. The batch reason is kept rather than discarded: a
            # batch that failed for an unrelated cause — a bad index, no network
            # — must not be reported as an sdist problem, and if the remainder
            # cannot be resolved either then this is the useful error.
            download_failure = exc
            for name, version in sorted(versions.items()):
                if name in _acquired_names(cache):
                    continue
                try:
                    run(_download_one_command(name, version, cache), repository)
                except WheelhouseError:
                    continue
        built_from_sdist: dict[str, dict[str, str]] = {}
        for name, version in sorted(versions.items()):
            if name in _acquired_names(cache):
                continue
            try:
                wheel, sdist_digest = acquire_from_sdist(
                    name,
                    version,
                    cache=cache,
                    pinned=pinned,
                    repository=repository,
                    run=run,
                )
            except WheelhouseError as exc:
                # The per-distribution reason is the specific one and stays the
                # message; the batch failure becomes its cause, so an operator
                # whose whole download died for an unrelated reason sees that in
                # the chain instead of losing it. Which distributions were built
                # from source is reported either way, so a batch failure that
                # silently routed everything here remains visible.
                raise exc from download_failure
            built_from_sdist[name] = {
                "sdist_sha256": sdist_digest,
                "wheel_sha256": _digest(wheel),
            }
        record = dict(sorted(built_from_sdist.items()))
        verify_acquisition(
            cache,
            versions=versions,
            pinned=pinned,
            built_from_sdist=record,
        )
        root.mkdir(parents=True, exist_ok=True)
        for wheel in sorted(cache.glob("*.whl")):
            shutil.copyfile(wheel, root / wheel.name)
        # The FILTERED file is the closure the bundle ships. `verify_bundle`
        # requires the requirements names to equal the dependency-wheel names
        # exactly, and the universal export carries names this target excludes,
        # so caching the export made every real bundle unverifiable.
        shutil.copyfile(filtered, root / REQUIREMENTS_NAME)
        ledger = root / SIDE_RECORD_NAME
        if record:
            ledger.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        else:
            # Nothing was derived here, so there is nothing to explain and the
            # honest state is this file's ABSENCE. A present-but-empty `{}`
            # claims a derivation and then names none, which
            # `distribution/bundle.py:_validate_sdist_builds` refuses on sight.
            # The removal is not hypothetical: this cache is keyed by lock
            # digest, so a record from an earlier acquisition under the same
            # lock survives a rerun and would go on naming wheels this run did
            # not build.
            ledger.unlink(missing_ok=True)
    return {
        "lock_sha256": lock_digest,
        "wheelhouse": str(root),
        "distributions": len(versions),
        "built_from_sdist": sorted(record),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--vendor-root", type=Path, default=None)
    arguments = parser.parse_args(argv)
    try:
        report = vendor(repository=arguments.repository, vendor_root=arguments.vendor_root)
    except WheelhouseError as exc:
        print(f"vendor-wheelhouse: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
