# Cortex independent PR review

The reusable engine is now hosted in
[reed-yang/independent-pr-review](https://github.com/reed-yang/independent-pr-review).
Cortex keeps its project rules, configuration and protected credentials; generic
Python adapters, tests, OAuth utilities and orchestration live in that repository.

## Consumer files

- `.github/workflows/auto-review.yml`: immutable reusable workflow pin, automatic
  PR events, maintainer commands and manual dry-run/review/full modes.
- `.github/review.json`: bounded context, per-PR run/token limits and rules paths.
- `tools/pr_review/cortex-rules.md`: Cortex invariants; not generic product policy.
- `.github/workflows/agy-oauth-check.yml`: two fresh hosted native OAuth sessions
  using the pinned standalone composite Action.
- `.github/workflows/fast-checks.yml`: validates this consumer's configuration;
  provider-free engine tests run in the standalone repository's CI.

These files are development tooling, not product runtime, distribution payloads,
production state, or a new background service on the mini.

## Deployed behavior

The pipeline keeps Grok `grok-4.6` on the explicitly selected gateway and Gemini
`gemini-3.1-pro-high` on official agy 1.2.2 with personal Google OAuth. It obtains
independent opinions and performs an extra cross-family verification pass when
candidates or unresolved issues need examination. It fetches bounded related source
through GitHub; it never executes PR code or follows PR-supplied instructions.

One English summary shows actual coverage, reviewed SHA, status and budget. Verified
P1/P2 findings with exact diff anchors can receive inline comments. Uncertain or
ambiguous evidence stays in the summary. Only owned threads with demonstrated fixes
are resolved. Reviews never approve, request changes, merge or write product code.

An identical successful snapshot reuses the result. Incremental work uses separate
successful model baselines and falls back to full review on changed base/config or
non-ancestor history. `/review`, `/review full`, `/review pause`, `/review resume`
and `/review verify <finding-id>` require current repository write access. Commands
cannot bypass fork/draft/default-branch restrictions or configured budgets.

## Credentials and activation

The `pr-review` Environment allows only `main`. It stores `GROK_API_KEY`, native
`AGY_OAUTH_JSON`, and the unique state-signing `REVIEW_STATE_KEY`. No credential is
stored in the public engine repository. Preserve the caller's explicit secret name
mappings and the reusable workflow declarations: Environment binding alone did
not expose Secrets in hosted acceptance. Values still come from the protected
consumer Environment, with no blanket inheritance or repository-level copies.
Repository Variables select models and
routes; `AUTO_REVIEW_ENABLED=true` and `AUTO_REVIEW_PUBLISH=true` activate automatic
review and persistent state/comments. A manual `dry-run` performs no provider calls
or PR writes. Normal reviews require publication because they reserve durable budget
before inference.

The mini is needed for initial interactive Google consent or later reauthorization,
not as a continuously running review server. Native agy refreshes a disposable copy
of the encrypted OAuth document on GitHub-hosted Ubuntu. Revocation/rotation still
requires reprovisioning; repeated refresh success does not guarantee permanent login.

For OAuth provisioning commands, key storage and recovery, use the standalone
[authentication guide](https://github.com/reed-yang/independent-pr-review/blob/main/docs/authentication.md)
and [operations guide](https://github.com/reed-yang/independent-pr-review/blob/main/docs/operations.md).
Run OAuth utilities from a reviewed checkout of that repository. The old local
`tools/pr_review/*.py` commands have been removed to avoid maintaining two engines.

## Boundaries and evidence

Preparation, inference and publication have separate job/process credentials. The
provider process receives no GitHub write token or state-signing key. Code-only job
handoff artifacts last one day; normalized reports last three days. OAuth state,
conversation databases and raw CLI diagnostics are never artifacts. The HMAC state
is authenticated, not encrypted and not an administrator-proof audit log.

Run caps are hard per PR; tokens use actual or estimated accounting and are a soft
guard. Account concurrency is repository-scoped; connecting several repositories
with the same Google account does not create a global lock or shared quota service.
Provider or verification failure is partial/failed, never a clean review. Review
coverage does not establish full product CI, installed runtime or browser acceptance.

The [mature review study](../../docs/plans/pr-review-quality.md) and
[English summary preview](../../docs/examples/pr-review-summary.md) explain the
adopted design. The original [OAuth study](../../docs/plans/pr-review-workflow.md)
is historical context; the standalone engine docs describe current operations.
