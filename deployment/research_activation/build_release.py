#!/usr/bin/env python3
"""Build the release the checked-in descriptor selects; default is preflight.

This is the reusable form of the outer `build16.py`, which hard-coded sequence
16, schema 17 and the gen15 predecessor. Every one of those is an input here:
the composition comes from `distribution/release.toml`, the supplies are named
explicitly, and the predecessor is the previous release's own record rather than
a pair of constants.

It drives the existing public tools -- the Web release supply CLI, `python -m
distribution build|verify`, and the candidate bundle's own `cortex-dist verify`.
It re-implements none of them and copies no deployment tree. The descriptor is
reconciled against the source and the produced artifacts by
`distribution.release_record`; a disagreement is a refusal, not a recorded value.

Nothing is built without `--apply`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


#: The side record `tools/vendor_wheelhouse.py` writes beside the cached
#: closure. Named here rather than imported: this driver runs against a source
#: checkout it does not import `tools` from.
SDIST_LEDGER_NAME = "built-from-sdist.json"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_sdist_ledger(wheelhouse):
    """The distributions the wheelhouse built from source, or `[]` for none.

    The acquisition tool writes this record only when it derived a dependency
    wheel from that distribution's own pinned sdist. A closure whose every
    distribution publishes a usable wheel derives nothing and writes no file, so
    absence is a supply STATE and not a missing supply -- demanding the file
    made a pure-wheel acquisition unbuildable.

    A present-but-empty record is the opposite and is refused: it claims a
    derivation and then names none, which is also the shape a stale file from an
    earlier acquisition leaves behind. `distribution/bundle.py` refuses exactly
    the same shape inside a bundle, and this raises rather than inventing a
    provenance the acquisition never recorded.
    """

    path = wheelhouse / SDIST_LEDGER_NAME
    if not path.is_file():
        return []
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"{path} is not a readable JSON object") from error
    if not isinstance(record, dict) or not record:
        raise ValueError(f"{path} is present but explains no wheel")
    return sorted(record)


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, default=None,
                        help="default: <source>/distribution/release.toml")
    parser.add_argument("--previous-release-json", type=Path, default=None,
                        help="the deployed predecessor's release record; omit only for a first release")
    parser.add_argument("--allocation-root", type=Path, action="append", default=[],
                        help="directory of existing <build>/release.json records to check the sequence against")
    # Supplies. No defaults that quietly reach into another worktree.
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--build-node", type=Path, required=True)
    parser.add_argument("--npm-cli", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--python-runtime-pin", type=Path, required=True)
    parser.add_argument("--python-runtime-sha256", default=None)
    parser.add_argument("--hermes", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    output = args.output.absolute()
    if output.exists() or output.is_relative_to(source):
        parser.error("output must be absent and outside the source checkout")

    # The descriptor parser is the repository's own, read from the source being
    # released rather than from whatever happens to be importable.
    sys.path.insert(0, str(source))
    from distribution import release_record

    descriptor_path = args.descriptor or source / "distribution/release.toml"
    descriptor = release_record.load_descriptor(descriptor_path)
    package_descriptor = release_record.load_descriptor(source / "distribution/release.toml")
    if descriptor.product_version != package_descriptor.product_version:
        parser.error("descriptor product version does not match the source package version")

    head = git(source, "rev-parse", "HEAD")
    if head != git(source, "rev-parse", args.commit) or git(source, "status", "--porcelain"):
        parser.error("source must be clean and exactly at --commit")

    schema_version, declared = release_record.read_source_control_schema(source)
    if schema_version != descriptor.target_schema:
        parser.error(
            f"descriptor targets control schema {descriptor.target_schema} but the "
            f"source declares {schema_version}"
        )
    if max(declared) != descriptor.target_schema:
        parser.error("descriptor target schema is not the source's highest migration")
    undeclared = [v for v in descriptor.upgrade_from_schemas if v not in declared]
    if undeclared:
        parser.error(f"upgrade_from_schemas names undeclared migrations: {undeclared}")

    worker = release_record.read_worker_manifest(args.hermes)
    if worker["release_id"] != descriptor.worker_release_id:
        parser.error("worker manifest release identifier does not match the descriptor")
    if worker["adapter_protocol"] != descriptor.adapter_protocol:
        parser.error("worker manifest adapter protocol does not match the descriptor")
    matched = release_record.verify_worker_payload(source, head, worker)

    previous = (
        release_record.read_release_record(args.previous_release_json)
        if args.previous_release_json is not None
        else None
    )
    if previous is not None:
        release_record.validate_predecessor(descriptor, previous)
        git(source, "merge-base", "--is-ancestor", previous["source_commit"], head)

    # Report a collision rather than guessing a different installed identity.
    roots = [path.resolve() for path in args.allocation_root]
    if previous is not None:
        roots.append(args.previous_release_json.resolve().parent.parent)
    seen = set()
    roots = [path for path in roots if not (path in seen or seen.add(path))]
    records = release_record.find_release_records(roots)
    release_record.check_sequence_available(descriptor, records)

    supplies = [
        args.python, args.node, args.build_node, args.npm_cli,
        args.python_runtime, args.python_runtime_pin,
        args.wheelhouse / "closure.requirements.txt",
        args.hermes / "manifest.json",
    ]
    missing = [str(path) for path in supplies if not path.is_file()]
    if missing:
        parser.error("missing build supplies: " + ", ".join(missing))
    # Not a required supply: see `read_sdist_ledger`. Read here anyway, so a
    # ledger that explains nothing is refused before the build spends any work,
    # and so preflight states which wheels the wheelhouse says it derived.
    try:
        sdist_built = read_sdist_ledger(args.wheelhouse)
    except ValueError as error:
        parser.error(str(error))
    if args.python_runtime_sha256 and digest(args.python_runtime) != args.python_runtime_sha256:
        parser.error("embedded Python runtime does not match --python-runtime-sha256")
    if args.python_runtime.stat().st_nlink != 1:
        parser.error("embedded Python runtime archive must not be hard linked")

    preflight = {
        "preflight": "passed",
        "descriptor": str(descriptor_path),
        "product_version": descriptor.product_version,
        "source_commit": head,
        "control_schema": schema_version,
        "upgrade_from_schemas": list(descriptor.upgrade_from_schemas),
        "release_id": descriptor.release_id,
        "release_sequence": descriptor.release_sequence,
        "worker_release_id": worker["release_id"],
        "worker_release_sequence": worker["release_sequence"],
        "worker_session_schema": worker["session_schema"],
        "worker_modules_matched": matched,
        "sdist_built": sdist_built,
        "previous_release_id": previous["release_id"] if previous else None,
        "previous_control_schema": previous["control_schema"] if previous else None,
        "release_records_checked": len(records),
        "applied": args.apply,
    }
    print(json.dumps(preflight, sort_keys=True))
    if not args.apply:
        return 0

    output.mkdir(mode=0o700, parents=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    for name in ("PYTHON", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)

    def run(label, command, cwd=source, child_env=env):
        completed = subprocess.run([str(v) for v in command], cwd=cwd, env=child_env,
                                   capture_output=True, text=True)
        (output / f"{label}.stdout").write_text(completed.stdout)
        (output / f"{label}.stderr").write_text(completed.stderr)
        completed.check_returncode()
        return json.loads(completed.stdout)

    bundle_name = f"bundle-{descriptor.release_id}"
    supply, payload, bundle = (output / n for n in ("web-supply", "web-payload", bundle_name))
    # The Web payload's build id, which `release-supply.mjs:2490` requires to
    # match `^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$`. Derived from the descriptor's
    # own release id rather than a literal: it used to name one operator's
    # machine, which made every payload's recorded provenance claim a builder
    # the descriptor never mentioned.
    build_id = f"{descriptor.release_id}-build-{head[:7]}"
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}", build_id), build_id
    acquired = run("01-acquire", [args.build_node, "scripts/release-supply-cli.mjs", "acquire",
                                  "--source", source / "apps/web", "--destination", supply,
                                  "--npm-cli", args.npm_cli], source / "apps/web")
    assert acquired["release_eligible"] is True
    run("02-web", [args.build_node, "scripts/release-supply-cli.mjs", "build", "--build-id", build_id,
                   "--lock-digest", acquired["lock_sha256"], "--supply", supply,
                   "--supply-digest", acquired["supply_sha256"], "--output", payload], source / "apps/web")
    assert git(source, "rev-parse", "HEAD") == head and not git(source, "status", "--porcelain")
    built = run("03-bundle", [args.python, "-B", "-m", "distribution", "build", "--repository", source,
                              "--output", bundle, "--release-id", descriptor.release_id,
                              "--release-sequence", str(descriptor.release_sequence),
                              "--web-payload-root", payload / "payload",
                              "--web-payload-ledger", payload / "release-payload.sha256",
                              "--web-build-id", build_id, "--web-lock-sha256", acquired["lock_sha256"],
                              "--node-adapter-version", "1", "--python-runtime", args.python_runtime,
                              "--python-runtime-pin", args.python_runtime_pin,
                              "--requirements", args.wheelhouse / "closure.requirements.txt",
                              "--wheelhouse", args.wheelhouse, "--node-executable", args.node])
    verified = run("04-verify-source", [args.python, "-B", "-m", "distribution", "verify",
                                        "--bundle", bundle, "--node-executable", args.node])
    independent = run("05-verify-bundle", [bundle / "cortex-dist", "verify", "--bundle", bundle,
                                           "--node-executable", args.node],
                      child_env=dict(env, PYTHON=str(args.python)))
    expected = built["result"]["bundle_digest"]
    assert verified["result"]["bundle_digest"] == independent["result"]["bundle_digest"] == expected
    assert json.loads((bundle / "provenance-inputs.json").read_text())["source_commit"] == head
    assert not list(bundle.rglob("*.pyc"))

    manifest = json.loads((bundle / "manifest.json").read_text())
    record = release_record.build_release_record(
        descriptor,
        source_commit=head,
        source_schema_version=schema_version,
        declared_migrations=declared,
        bundle_path=bundle,
        bundle_manifest=manifest,
        bundle_digest=expected,
        web_build_id=build_id,
        worker=worker,
        previous=previous,
    )
    (output / "release.json").write_bytes(release_record.record_bytes(record))
    return 0


def _run():
    # A refused composition is an operator-facing message, not a traceback: the
    # descriptor reconciliation is the point of this driver, so its refusals are
    # the expected output when something disagrees.
    try:
        return main()
    except Exception as error:  # noqa: BLE001 - the message is the deliverable
        if type(error).__name__ != "ReleaseRecordError":
            raise
        print(f"build_release.py: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_run())
