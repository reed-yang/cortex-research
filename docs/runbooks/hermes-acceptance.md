# The real Hermes acceptance driver

`tools/hermes_acceptance.py` is the only thing that has ever produced
`certified: true` for a Hermes release. It proves §5 clauses 3-8; the shipped
`cortex-hermes certify` proves clauses 1, 2 and 9 and therefore exits 1 for
every release, by design. See
[the S3.5 notes](../plans/2026-09-02-s35-certification-notes.md) for the clause
split and the two rules that shaped it.

The tool is checkout-only. The wheel allowlist contains `cortex_platform` and
`deployment/private_access`, so nothing under `tools/` reaches a wheel, and the product gains no
proof-injection flag from this file being tracked.

## Why it is tracked

It was an ignored file under `output/`, outside the repository, uncovered by any
suite. Routine cleanup could have destroyed the ability to investigate or repeat
a certification. It is now tracked, and the retention rule covers the **pair** —
the harness and the attested entrypoint it packages into the `driven` release:

| Tracked | Retained original | sha256 | bytes |
|---|---|---|---|
| `tools/hermes_acceptance.py` | `output/s35-real-acceptance-20260902/run_acceptance.py` | `23d90307…24cc` | 58499 |
| `tools/hermes_acceptance_driver.py` | `output/s35-real-acceptance-20260902/acceptance_driver.py` | `8c638428…5e8b` | 11657 |

`output/hermes-release-gen9-20260902/` holds byte-identical copies of both under
`.gen9.py` names. The harness was imported verbatim first, then made portable, so
the portability diff is reviewable against the original bytes.
`tests/packaging/test_hermes_acceptance_inputs.py` pins that the tracked
entrypoint still hashes to the copy that produced the retained evidence.

Neither original has been modified, and neither may be deleted as disposable
`output/` data.

## Inputs

Every input is an argument. The original derived them from its own location on
disk; tracked under `tools/`, those lookups resolve elsewhere, so they are
refusals now rather than defaults.

| Flag | What it is | Retained location today | Size |
|---|---|---|---|
| `--driver` | attested `acceptance_driver.py` the `driven` release names | `output/s35-real-acceptance-20260902/acceptance_driver.py` | 12K |
| `--closure` | S3.1b closure: wheelhouse, hermes wheel, requirements | `output/s31b-real-acceptance-20260901` | 60M |
| `--runtime` | directory holding the vendored CPython archive and its pin | `output/hermes-runtime-cp311` | 36M |
| `--fork` | patched Hermes fork checkout the patch ledger reads | `output/hermes-fork-131bea608` | 993M |
| `--work` | fresh scratch tree this run owns | supply a fresh path | — |
| `--evidence-root` | where the record and profiles are written | supply a fresh path | — |
| `--artifacts` | optional: certify an artifact set that already exists | `output/hermes-release-gen9-20260902` | — |

`--product` is not a flag. It is derived from the imported `cortex_platform`,
because the record names the commit it measured and a dirty worktree is refused
outright; accepting it would let the record name a worktree the run never used.

The interpreter archive and its pin are discovered from `--runtime` and ambiguity
is refused, on the same derive-never-accept rule as
`package_hermes_release`'s `minimum_os_version`.

### Refusals

Artifact-set mode replaces its generated `sandbox-profiles` tree. To keep that
operation inside a fresh run:

- Both `--work` and `--evidence-root` must be absent or empty and must not contain
  each other. Preparation creates them before any proof writes, rechecking
  freshness first; prior work/evidence is never purged to make a path usable.
- Neither writable root may be inside the supplied closure, runtime, fork or
  artifact set, regardless of that input directory's name.
- neither writable path may resolve inside the checkout, inside a retained
  evidence directory (`s31b-real-acceptance-20260901`,
  `s35-real-acceptance-20260902`, `hermes-release-gen9-20260902`), or onto a
  home or filesystem root.

Reading a retained artifact set through `--artifacts` is unchanged and expected.
Only writing into one is refused. This is a deliberate change from the original,
which wrote `acceptance.full.json` beside the artifact set it certified.

`--help` and input refusals exit before the acceptance starts a process, resolves
a credential or creates a directory. The CLI imports its product dependencies
before parsing; the standalone input-validation module does not.

## Recipe

From this checkout's root, with its venv, as a module so the repository is
importable:

```bash
python -m tools.hermes_acceptance \
  --driver        <workspace>/output/s35-real-acceptance-20260902/acceptance_driver.py \
  --closure       <workspace>/output/s31b-real-acceptance-20260901 \
  --runtime       <workspace>/output/hermes-runtime-cp311 \
  --fork          <workspace>/output/hermes-fork-131bea608 \
  --work          <fresh scratch dir> \
  --evidence-root <fresh output dir>
```

Add `--artifacts <workspace>/output/hermes-release-gen9-20260902` to certify the
shipped gen9 artifact set instead of packaging a production release for the run.
In that mode the record is named `acceptance.full.json` and proofs 3-8 are
observations of the shipped artifact.

The run needs a clean product worktree, macOS with the seatbelt, and the arm64
vendored CPython. It makes no provider call: the base URL is
`https://runtime.invalid/v1` and the credential is a self-labelled fake whose
only job is to prove clause 8's allowlist.

## What must be re-evaluated before trusting a fresh run

The driver was written on 2026-09-02 against the code of that day. These modules
it imports directly have changed since, so a fresh run is a new measurement, not
a replay of the retained record:

`runtime_update/{supervisor,service,sandbox,approval,certification,certify_cli,
capability,egress_probe}.py`, `transports/managed_worker.py`,
`tools/package_hermes_release.py`, and the sealed worker payload's
`cortex_worker/{serve,turn,runtime,telegram}.py`.

Two consequences worth separating:

- **Artifact-set mode is the safer comparison.** It stages and probes the
  existing gen9 zip, so payload source drift does not change what is measured.
- **Self-packaged mode packages from current source.** Changed worker payload
  bytes mean the release under test is not gen9, and a green record there is not
  evidence about the certified gen9 slot.

Clause 6 and 7 read the dispatch gate and the supervisor's refusal path, both of
which moved; clause 5 reads sandbox denials from inside the fork process and
`sandbox.py` moved. Re-derive those three before quoting a fresh result.

## Status

Tracked, reviewed and portable. Its input handling and isolation are covered by
`tests/packaging/test_hermes_acceptance_inputs.py`.

**No re-certification has been performed.** Retaining and versioning the driver
is not a new certification, and it does not change that `cortex-hermes certify`
alone cannot certify. A real run needs a staged slot and a running worker; the
certified slot remains `hermes-0.15.0-gen9` and no upgrade or replacement is
proposed here.
