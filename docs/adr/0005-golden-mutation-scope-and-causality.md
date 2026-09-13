# ADR 0005: Separate Control Audit from Research Side Effects

**Status:** Accepted
**Date:** 2026-07-15
**Decision IDs:** `PROD-AUDIT-002`, `PROD-EVENT-CAUSALITY-001`
**Clarifies:** ADR 0004 `PROD-GOLD-001`, `PROD-AUDIT-001`

## Context

The v0.1 contract requires a decision to be durable before a run enters
`waiting_for_decision`. ADR 0004 also says that no persistent mutation occurs
before source resolution. Read literally, those requirements conflict: the run,
decision, and audit events must be persisted in order to wait safely, but they
are themselves persistent mutations.

The first P1 fixture exposed a second ambiguity. Its mutation manifest mixed
research side effects with Cortex control entities, while durable creation
events could appear before the manifest's declared transaction commit. Such a
fixture cannot prove crash-safe event causality.

## Decision

### PROD-AUDIT-002

"No mutation before source resolution" means no externally meaningful research
side effect before resolution. This includes source import, research database
rows or chunks, lineage changes, filesystem materialization, and artifact
publication.

Cortex control-plane state is different. The logical run, attempt, source
intent, pending decision, and durable audit events must be committed before the
runtime waits. Checkpoints are also control-plane state and must be committed
before recovery refers to them.

The golden fixture therefore labels its side-effect manifest scope explicitly.
It is not a complete row-by-row log of the Cortex event store. Control-plane
durability is verified through the event, decision, run, attempt, and checkpoint
fixture state; research and materialization deltas are verified through the
scoped side-effect manifest.

### PROD-EVENT-CAUSALITY-001

A durable event that states an entity was created, updated, or committed must
not precede the corresponding state commit. The event and mutation may share
one atomic commit boundary, represented by the same sequence, or the event may
follow an already committed mutation. A recovery event may reference only a
previously committed checkpoint.

Event replay remains at-least-once and idempotent. The fixture must prove that
replaying from a checkpoint does not duplicate a control event or a research
side effect.

## Motivation

- Persisting the decision before waiting is necessary for restart safety and
  prevents a lost in-memory prompt from becoming an implicit approval.
- Blocking research side effects protects the corpus and filesystem while the
  source identity remains unresolved.
- Explicit scopes prevent a test from claiming full mutation coverage while
  silently omitting control-store rows.
- Commit/event causality makes the deterministic fixture useful for later
  crash-recovery implementation instead of only validating a happy-path JSON
  snapshot.

## Alternatives considered

- **Forbid every persistent write before resolution:** rejected because the
  pending decision and waiting state would disappear after a crash.
- **Put every run event and event-store row in the side-effect manifest:**
  rejected because it duplicates the event log and can become recursively
  defined. Control durability still receives direct structural assertions.
- **Allow events before state commit as intentions:** rejected for `created`,
  `updated`, and `committed` event types because their names assert completed
  state. A future intent event must use an explicit requested/planned type.
- **Leave manifest scope implicit:** rejected because different work packages
  would count incompatible mutation sets.

## Consequences

- G0 contains durable control state but an empty research-side-effect manifest.
- P2 needs separate assertions for control-store transitions and research/file
  deltas.
- Mutation manifests require a declared scope and must not be described as a
  complete database change log.
- Event producers and state writers need a shared transaction boundary or an
  outbox-style ordering rule.

## Verification

- G0 asserts that its run, attempt, pending decision, and waiting events are
  durable while its research-side-effect manifest is empty.
- G1 includes an explicit existing-source binding/reuse and a second-source
  import only after source resolution.
- Every created/updated/committed event is aligned with its declared commit
  sequence.
- A durable checkpoint event precedes recovery, and replay from that checkpoint
  creates no duplicate event, decision, source, artifact, or materialization.
- P2 repeats the fixture against temporary control/research databases and
  filesystem roots before any live gate.

## Revisit triggers

- The control store adopts a transactional outbox whose audit format can replace
  the fixture's separate control assertions.
- Operators need row-level forensic coverage beyond the scoped side-effect
  manifest.
- A runtime requires speculative imports before resolution and can prove a safe,
  isolated rollback model.
- Crash testing shows that equal-sequence event/state commits are ambiguous in
  the physical store.

A replacement ADR must preserve pending-decision recovery, identify the new
audit scopes, and migrate historical event/manifest interpretation explicitly.
