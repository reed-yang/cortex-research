# Cortex auto-research contributor instructions

This repository contains the single-operator research product. Start with
`README.md` for supported behavior and `distribution/release.toml` for the
candidate composition. `docs/README.md` maps current architecture, contracts,
roadmap and release records. Read the applicable runbook before changing a boundary.

## Architecture and scope

- `cortex_platform/product/` owns Control state, API, orchestration, transport,
  sources, artifacts and research contexts. Preserve transaction and command
  idempotency boundaries when extracting helpers.
- `profiles/research/src/cortex_research/` is the retained arXiv ingestion and
  indexing bridge. The directory name does not imply multi-profile support.
  Metadata uses the product-bound abs page directly; the export API is a
  fallback when that page cannot provide valid metadata. Both paths must
  identify the requested paper. Full text uses the direct HTML endpoint.
- `product/readings/` owns opt-in external publication and its separate journal.
  Keep Capture outcomes independent, preserve existing notes and require
  provenance/hash matches for generated-file updates. External writes belong
  only in the sandboxed publisher; never add deletion or mirror semantics.
- Research catalog items and adopted dossiers are durable evidence. Opening an
  item or sending `/research` does not run autonomous idea/exploration rounds.
- `cortex_platform/runtime/` implements RuntimePort through managed Hermes.
  `product/transports/managed_worker.py` owns transport authorization;
  `runtime/managed_hermes.py` owns backend lifecycle; the runtime-update
  supervisor owns process I/O; the runtime-update service owns release state.
  These responsibilities must remain distinct.
- The product's Python dependency closure excludes the separately supplied Hermes
  slot, which carries its own interpreter and dependencies. Product installation
  with runtime health `unbound` is not a successful model-turn acceptance.
- `deployment/private_access/` is required by the bundle contract. Its directory
  name is not a reason to exclude it from the product wheel.

## Repository review tooling

The generic engine lives in `reed-yang/independent-pr-review`; Cortex consumes an
immutable reusable workflow pin in `.github/workflows/auto-review.yml`. Project
policy stays in `.github/review.json` and `tools/pr_review/cortex-rules.md`. Read
`tools/pr_review/README.md` before changing activation, credentials or publishing.
These are checkout-only development tools and are not part of product runtime.

PR head content is untrusted API data and never executes. The trusted engine
collects bounded context, obtains independent Grok/native agy opinions, verifies
candidates, and updates an English PR summary plus verified inline comments. It
never approves, merges or modifies code. Signed state preserves budgets, per-lane
baselines and owned finding threads. Silence does not mean an old issue is fixed.

Provider credentials and a per-repository `REVIEW_STATE_KEY` belong to the
`pr-review` GitHub Environment restricted to the default branch. Only inference
steps receive provider credentials; they receive neither a GitHub write token nor
the state signing key. Native agy OAuth refreshes in disposable hosted HOME state.
The current lanes are Grok 4.6 xhigh/500k and Gemini 3.8 Flash Medium/1M. Context
projection is per lane; token estimates and provider capacities are distinct.
Consumer CI validates project configuration; engine tests run in its own repository.

## Release and state

Product version, installation sequence, Control schema and worker generation are
independent. Derive release identity through the existing descriptor and build
driver. Keep worker byte copies covered by the existing tests. A worker upgrade
needs separate compatibility and session-continuity evidence.

Use the official distribution lifecycle. Preserve data, credentials, sessions,
asset roots and installed predecessors. Never edit a sealed bundle or active
worker slot. Same-schema row readability, actual native-session continuation,
and end-to-end Web/Telegram delivery are different acceptance claims.

## Private operator context

If present, `.cortex-context/README.md` indexes ignored internal handoffs and
plans. Read it for operator-specific continuity, but never force-add its files
or make the build depend on that directory. Public docs must remain sufficient
for a clean clone. Original historical designs may name removed legacy modules.

## Development

Create a plan for multi-step implementation. Keep task evidence under `logs/`
(ignored), with a concise committed handoff when work is complete. Use separate
worktrees and checkout-owned virtual environments for concurrent writers.

Use the README's `uv sync --frozen --python 3.12` and import-ownership check.
Run focused tests for the changed behavior; use `npm run test:fast` for Web
unit/type/contract changes and the relevant real process/browser gates when
behavior requires them. Stage binary test inputs explicitly through the existing
`CORTEX_TEST_*` variables. Never silently use another checkout's editable install.

Keep configuration generic and secrets external. Do not add private paths,
operator data, logs, runtime artifacts or a previous repository's Git history.
This repository currently has no selected owned-code license; source publication
and binary redistribution require separate owner decisions. A local passing test
is not deployment or publication evidence.
