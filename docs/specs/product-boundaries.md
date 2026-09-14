# Current product boundaries

This is the research-only product's maintenance baseline. It preserves the
implemented core of the earlier Control contract without importing its
historical rollout status or promising unimplemented client surfaces.

## Authority, transactions and identity

- Control owns logical workspace/thread/message/run/attempt identities. Native
  worker references and external transport scopes remain adapter data.
- Mutating API commands require their idempotency contract; revisioned commands
  also require the expected revision. A conflict performs no runtime effect.
- A thread has at most one active logical run; a run has at most one active
  attempt. Cross-domain transaction boundaries must not be split into separate
  commits during store extraction.
- Run-scoped durable transitions and events commit together. Capture inbox
  transitions instead use the audit trail because captures are not run-scoped.
- Runtime results are applied only to the owning attempt, binding and release.
  Cancellation and late results must keep their established ordering.

Executable authorities: `cortex_platform/product/control/`,
`cortex_platform/tests/product/control/`, `product/orchestration/` and their tests.
Paths abbreviated `product/` are relative to `cortex_platform/`.

## Research and artifacts

- Adoption records an immutable document version; a title match is not an
  identity replacement. Source records and raw captures are different resources.
- Evidence labels distinguish retained documents from papers. Successful label
  validation does not prove the model's scientific claims.
- An Output is committed under its producing attempt. Preview and Source are
  views of the same stored bytes; UI rendering does not rewrite provenance.
- Catalog states and human pauses are preserved. No autonomous idea/exploration
  advancement or hidden model escalation is part of the current command scope.

Executable authorities: `product/research/`, `product/sources/`,
`product/artifacts/`, `apps/web/app/control/` and their corresponding tests.

## Runtime, transport and exposure

- The worker generation is qualified separately. Slot bytes, protocol identity
  and native-session compatibility are not implied by a product version.
- Telegram uses the product's existing command/delivery ledger. A successful
  run or receipt alone is not proof of provider delivery. Ambiguous sends keep
  the established manual-resolution semantics.
- A deep link can select a workspace/thread but does not substitute for
  authorization or approve a mutation.
- The browser proxy preserves route allowlists, Origin/identity checks and
  private DTO filtering. Model output and imported content remain untrusted.

Executable authorities: `runtime/managed_hermes.py`, `product/runtime_update/`,
`product/transports/`, `apps/web/app/api/cortex/` and their tests.

## Changes requiring explicit design

New engine writes, model-role escalation, worker upgrades, additional profiles,
multi-user access and changes to transaction/idempotency/privacy boundaries
need a scoped plan and acceptance appropriate to the changed behavior. Allocate
schema changes centrally; do not reserve a number in a speculative document.
