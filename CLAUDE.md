# Cortex auto-research contributor instructions

This repository contains the single-operator research product. Start with
`README.md` for supported behavior and `distribution/release.toml` for the
candidate composition. Read the applicable runbook before changing a boundary.

## Architecture and scope

- `cortex_platform/product/` owns Control state, API, orchestration, transport,
  sources, artifacts and research contexts. Preserve transaction and command
  idempotency boundaries when extracting helpers.
- `profiles/research/src/cortex_research/` is the retained arXiv ingestion and
  indexing bridge. The directory name does not imply multi-profile support.
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

## Release and state

Product version, installation sequence, Control schema and worker generation are
independent. Derive release identity through the existing descriptor and build
driver. Keep worker byte copies covered by the existing tests. A worker upgrade
needs separate compatibility and session-continuity evidence.

Use the official distribution lifecycle. Preserve data, credentials, sessions,
asset roots and installed predecessors. Never edit a sealed bundle or active
worker slot. Same-schema row readability, actual native-session continuation,
and end-to-end Web/Telegram delivery are different acceptance claims.

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
