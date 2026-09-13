# Architecture Decision Records

This directory is the durable history of Cortex product decisions. ADRs explain
why a boundary was chosen, how it is verified, and what evidence should trigger
reconsideration. They are not progress reports.

## Decision index

- [ADR 0001](0001-cortex-state-ownership.md): Cortex owns product state.
- [ADR 0002](0002-local-first-storage-and-exposure.md): Native local storage
  and loopback-first exposure.
- [ADR 0003](0003-runtime-control-and-transport.md): Cortex runtime control
  over HTTP and SSE.
- [ADR 0004](0004-golden-case-resolution-and-lineage.md): Golden-case conflict,
  reuse, and successor behavior.
- [ADR 0005](0005-golden-mutation-scope-and-causality.md): Control audit is
  durable before research side effects.
- [ADR 0006](0006-package-dependency-and-training-boundary.md): Keep NumPy 2
  and withdraw the deferred training extra.
- [ADR 0007](0007-managed-hermes-runtime-updates.md): Manage Hermes as a
  provenance-verified, replaceable runtime rather than an in-place dependency.
- [ADR 0008](0008-single-control-plane-multi-surface-clients.md): One Cortex
  control plane for Web, PWA, and Telegram.
- [ADR 0009](0009-capture-inbox-staging-identity.md): The capture inbox is a
  non-run-scoped staging identity gated on explicit approval.
- [ADR 0010](0010-product-manifest-contract-per-schema.md): The product
  manifest contract is frozen per declared schema version, with byte pins.

## Status lifecycle

```text
proposed -> accepted -> deprecated -> superseded
```

Accepted ADRs are immutable historical records except for typo corrections and
links to later evidence. A changed decision is recorded in a new ADR whose
header names the ADR it supersedes. The old ADR remains in place.

## Required decision path

Every accepted decision records:

1. Context and user problem.
2. Evidence and constraints available at decision time.
3. The selected option and motivation.
4. Alternatives considered and why they were rejected.
5. Positive and negative consequences.
6. Automated contracts, fixtures, and human signals that test the decision.
7. Explicit revisit triggers and a safe migration/rollback path.

## Changing an accepted decision

1. Record the new observation in the owning work package handoff and local
   `logs/findings.md`.
2. Reproduce it with a deterministic fixture, shadow-data test, usability
   session, or operational measurement.
3. Draft a new ADR that references the affected decision IDs and explains the
   compatibility/migration impact.
4. Update the public contract and fixtures before parallel consumers change.
5. Run contract tests, relevant regressions, and the applicable shadow or live
   gate.
6. Mark the old ADR superseded only after the replacement is integrated.

## Evidence layers

- **Contract tests:** identity, state transition, idempotency, replay, and API
  compatibility.
- **Deterministic golden fixtures:** G0 source conflict and G1 lineage/artifact
  workflow without network or production writes.
- **Shadow integration:** copied databases and read-only asset roots.
- **Usability evidence:** time to understandable status, decision clarity,
  artifact readability, and recovery after interruption.
- **Live dogfood:** only after backup and production-write gates.
