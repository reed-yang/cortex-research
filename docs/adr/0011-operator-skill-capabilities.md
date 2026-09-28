# ADR 0011: Serve engine capabilities from digest-accepted operator skills

**Status:** Accepted
**Date:** 2026-09-27
**Decision IDs:** `PROD-SKILL-001`, `PROD-SKILL-002`

## Context

A Capture of an arXiv paper without LaTeXML HTML always failed as
`materialization_failed` within seconds. Strict ingestion refuses the degraded
plain-text body, and its OCR fallback was unreachable: the binding table denied
the paper-ingestion skill path and uv, and the bound `HOME` held no skill. No
product-side OCR had ever succeeded. The operator already maintains a working
OCR skill outside the product.

While diagnosing this, a second failure appeared. On 2026-09-14 a profile sync
tool replaced that skill's entry file with an older copy. The skill directory
was a git checkout whose HEAD had the newer backend; its working file did not.
Every OCR call then asked for an engine the file no longer knew. Nothing
reported the substitution.

## Evidence and constraints

- The product's root distribution imports only the standard library, so it
  cannot parse arbitrary YAML.
- Agent Skills `metadata` is a string-to-string map reserved for client
  properties, so a declaration there keeps `SKILL.md` valid for Claude Code and
  Codex.
- uv 0.11.3: `uv run --no-sync` without a `.venv` downloads CPython and creates
  `.venv` inside the project. With a prepared environment it writes only its
  cache under `HOME`.
- The effect child's hard timeout is 600 s (`LeasePlan`); the OCR budget was
  1000 s, so OCR would have been killed before its own deadline.
- The child's write audit covers only its own interpreter. Subprocesses rely on
  the bound environment and, when readings publication is configured, the
  inherited write-denying sandbox.

## Decision

### PROD-SKILL-001

The engine runs out-of-tree code only through a named capability slot. The
operator names a skills root (`[skills] root`); a skill declares
`cortex-capability`, `cortex-entry` and `cortex-interpreter` in its `metadata`.
`ocr` is the only slot with a consumer. A clean installation has no OCR and
behaves as before, except that the refusal names its cause.

### PROD-SKILL-002

A capability is served only by the exact package the operator accepted.
`cortex skills accept` records a sha256 over the package tree, excluding
environments, caches, sync backups and `.env`. The supervisor recomputes it
before every effect and binds the entry and interpreter only on a match. uv is
never an effect-time runner. A paper that needs an unavailable capability is
refused as `capability_unavailable` before any corpus write.

## Alternatives considered

- **One explicit path per capability in configuration.** Most explicit, but
  every new skill needs a configuration edit and it still pins no identity.
- **A Control-state registry with API and Web management.** Revisioned and
  visible, but skill registration is a fact about this machine rather than
  business state needing cross-session arbitration, and it needs a schema
  migration.
- **Intersect the skill's supported engines with the product's chain.** This
  treats the 2026-09-14 symptom and would have silently accepted the degraded
  file. Rejected in favour of identity.
- **Bundle an OCR implementation.** Enlarges the wheel closure, duplicates the
  operator's mature skill, and conflicts with the operator's choice that OCR is
  operator-provided.
- **Trust on first use.** Removes the accept step, but the first registration
  after a silent substitution would adopt the substitute.

## Consequences

Positive: PDF-only papers ingest through the operator's skill. A changed skill
stops being run and is reported by `cortex doctor`, `cortex skills status` and
the capture category. The inbox distinguishes "OCR is not ready" from a paper
that failed.

Negative: every legitimate skill update needs a review and `accept`. The digest
does not cover the prepared environment. Verification and execution are
separate steps. The skill runs with the OCR credentials. The child's stderr is
still not persisted, so an OCR failure keeps only its category and message.

## Contracts and signals

- `cortex_platform/tests/product/test_skills.py`: declaration parsing, digest
  exclusions, symlink handling, containment, acceptance and every state.
- `cortex_platform/tests/product/engine/test_ocr_capability.py`: a real effect
  child OCRs a PDF-only fixture through an accepted stand-in skill with `-B`,
  without the embedding credential, and refuses a changed one.
- `cortex_platform/tests/product/engine/test_bindings.py`: capability rows,
  whole-or-nothing binding, uv denied, OCR budget inside the child timeout.
- `profiles/research/tests/`: interpreter runner mode and the distinction
  between OCR unavailable and OCR attempted.
- `apps/web/tests/shell/inbox.test.tsx`: the capability sentence.

## Revisit and rollback

Revisit when a second capability gains a consumer, when model-side skills are
designed, or if acceptance friction leads operators to accept without review.
Rollback: remove `[skills]` or the acceptance record; the engine then refuses
PDF-only papers as `capability_unavailable`. Product 0.1.26 ignores the section.
