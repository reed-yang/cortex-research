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
- Each paper evidence entry is a bounded window of one indexed chunk or one
  retained file version (its prefix or a section window), and its kind, locator
  and content hash identify that source. notes and grounding may hold
  model-written summaries. Stored v1/v2 packets keep validating unchanged.
- An Output is committed under its producing attempt. Preview and Source are
  views of the same stored bytes; UI rendering does not rewrite provenance.
- The source asset route reads only the authorized source's own `assets/`
  directory, with the binding taken from Control, and serves PNG, JPEG, GIF or
  WebP decided by signature with `Cache-Control: no-store`. Library rendering
  never rewrites stored Markdown.
- Catalog states and human pauses are preserved. No autonomous idea/exploration
  advancement or hidden model escalation is part of the current command scope.
- The engine runs out-of-tree code only through a capability slot served by an
  operator-accepted skill whose package digest still matches; uv never runs
  inside an effect. A paper that needs an unavailable capability is refused as
  `capability_unavailable` before any corpus write.
- PDF OCR stays the operator skill's. First-party image OCR for XHS carousel
  images is in-product: the research profile also hosts the XHS, OCR,
  Responses and blog provider clients, which only engine-child handlers import
  and the nine arXiv bridge modules never do. Each provider operation receives
  only its own credentials, never opens `research.db`, and writes only under
  the one asset root its caller binds, or nowhere. `cortex_platform/` itself
  imports only the standard library.
- The XHS plugin is a first-party, opt-in acquisition path. It runs only from
  the `xhs-pull` and `xhs-drain` schedule rows, which migration 21 seeds
  disabled, under the dispatch gate, with `[xhs] enabled` and both of its asset
  roots ready. Provider I/O happens in engine children outside SQLite
  transactions; each result is checked again in cortexd and recorded in one
  transaction fenced by the task revision. Daily caps bound provider calls.
- OCR text is stored verbatim and model output is stored separately as
  identified recommendations; a model failure never yields an empty list.
  cortexd re-runs the rules and the verbatim filter on its own copy instead of
  trusting a child's items. Signed image URLs stay in private task rows; raw
  provider answers stay in private `raw/` and `ocr/` files that no route serves.
- `xhs_note` and `blog` sources resolve through their latest content binding
  under an enabled `xhs-notes` or `blogs` root, outside the research corpus,
  and are never research evidence, Library search results or readings
  publications: publication requires the paper kind. Importing a recommended
  paper stages an ordinary Capture that still needs approval; a blog import
  fetches only a recommended link, through the public-address fetch policy.
- The weekly recommendation fallback runs only inside `xhs-drain` with
  `[xhs] fallback_enabled`. It never imports a paper or stages a Capture; it
  queues a blog import only after Cortex fetched the proposed page itself and
  its title matched, and never for a paper host or PDF. A model call that may
  have run is never repeated. Exclusion keeps the row, shows its reason and is
  reversible; a restored row belongs to the operator. Its Telegram digest
  carries a count, up to three titles and the Web Inbox link, goes only to the
  one bound operator chat, and never opens the transport.

Executable authorities: `product/research/`, `product/sources/`,
`product/artifacts/`, `product/skills.py`, `product/engine/bindings.py`,
`product/xhs/`, `product/control/xhs_store.py`, `apps/web/app/control/` and
their corresponding tests.

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
