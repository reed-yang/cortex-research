# Cortex Research review policy

This policy describes the extracted, single-operator research product. Current
code and public contracts take precedence over historical design documents.

- Control is the durable authority for runs, attempts, claims, events and commands.
  Preserve transaction boundaries, idempotency, cancellation and delivery identity.
- Transport authorization, managed Hermes backend lifecycle, worker process I/O,
  and release lifecycle have distinct owners. Do not merge them based on names.
- Research catalog items and adopted dossiers are durable evidence. Selecting an
  item or sending `/research` does not run autonomous idea/exploration rounds.
  The retained engine supports ingestion, checkpoint, reconciliation and self-check.
  Investment and multi-profile behavior are excluded, not missing features.
- Sources, artifacts and evidence packets preserve immutable versions, provenance
  and selection authority. Valid citation labels do not establish a correct claim.
- Web/PWA and Telegram use the Control boundary. Credentials, private paths and
  daemon authority must not enter public browser output or source archives.
- Product version, installation sequence, Control schema and Hermes generation
  are independent. An unbound runtime is not a successful model-turn acceptance.
  Preserve installed predecessors and sessions through the official lifecycle.
- `deployment/private_access` is required by the bundle contract; its name is
  not grounds for removing it. `tools/pr_review` is checkout-only developer tooling.
- For Web changes, check client/server DTO alignment, authorization, event replay,
  rendered Markdown/math and Preview/Source behavior. Do not change a fixture just
  to make a failing workflow pass without confirming the actual contract.
- Hosted fast-checks cover provider-free Control/artifact contracts, Web test:fast
  and review consumer configuration. Passing them does not prove installed, browser,
  macOS, real-provider or production behavior. This review tool executes no tests.
- Report introduced, demonstrable P1/P2 bugs with triggers and packet evidence.
  Avoid formatting, speculative refactors and generic test requests. Identify
  missing caller context instead of inventing it. Preserve independent findings.

References: README.md, CLAUDE.md, docs/architecture.md,
docs/specs/product-boundaries.md, docs/runbooks/web-verification.md,
.github/workflows/fast-checks.yml.
