# Cortex independent PR review

The reusable engine is hosted in
[reed-yang/independent-pr-review](https://github.com/reed-yang/independent-pr-review).
Cortex keeps its project rules, configuration and protected credentials; the engine,
its tests and orchestration live in that repository.

## Consumer files

- `.github/workflows/auto-review.yml`: immutable reusable workflow pin, automatic
  PR events (including `labeled`), maintainer commands and manual
  dry-run/review/full modes.
- `.github/review.json`: brief limits, per-PR run/token limits, rules paths and the
  GPT generation policy.
- `tools/pr_review/cortex-rules.md`: Cortex invariants; not generic product policy.
- `.github/workflows/agy-oauth-check.yml`: manual check of the native agy OAuth
  session, kept for re-enabling the retired Gemini lane; still pinned to v0.3.0.
- `.github/workflows/fast-checks.yml`: validates this consumer's configuration;
  provider-free engine tests run in the standalone repository's CI.

These files are development tooling, not product runtime, distribution payloads,
production state, or a new background service on the mini.

## Deployed behavior

Engine v0.4.0 runs two independent reviewers. Each reads the repository itself
from an immutable snapshot of the PR head and merge base; the engine sends only
policy, PR metadata, the description (treated as author claims) and bounded patches.

| Lane | Model and effort | Repository access |
| --- | --- | --- |
| Grok | `grok-4.7`, xhigh, 500k window | Engine read-only tools over git objects; no execution |
| GPT | `gpt-6.1-sol`, ultra (verification xhigh) | Official Codex CLI in its read-only, no-network sandbox |

Both go through the configured sub2api gateway. Grok reviews every eligible run.
GPT generates opinions only when a PR changes at least 300 lines or 8 files, is
marked ready for review, carries the `deep-review` label, or receives `/review full`
or `/review verify`; otherwise it is shown as skipped but still verifies Grok's
candidates. Each family verifies the other's candidates before anything is
published. GPT ultra on a 200 to 2,000-line PR took 3 to 9 minutes and 0.7M to
3.2M tokens (mostly cached input) in local replays; Grok took 10 to 12 minutes.

One English summary shows the reviewed SHA, each reviewer's model, effort, scope
and repository reads, verified findings, dismissed candidates, reviewer
observations and budget, followed by a machine-readable result block. Verified
P1/P2 findings with exact diff anchors can receive inline comments. Uncertain or
ambiguous evidence stays in the summary. Only owned threads with demonstrated fixes
are resolved. Reviews never approve, request changes, merge or write product code.

An identical successful snapshot reuses the result. Incremental work uses separate
per-lane baselines and falls back to full review on changed base/config or
non-ancestor history. `/review`, `/review full`, `/review pause`, `/review resume`
and `/review verify <finding-id>` require current repository write access. Commands
cannot bypass fork/draft/default-branch restrictions or configured budgets.

The per-PR accounting ceiling is 60M tokens: one two-lane run reserves 17M (Grok
4M + 2M, GPT 8M + 3M) and reconciles to actual usage. The hard run cap is eight.

## Credentials and activation

The `pr-review` Environment allows only `main`. It stores `GROK_API_KEY`,
`GPT_API_KEY` (a dedicated gateway key) and the unique state-signing
`REVIEW_STATE_KEY`; `AGY_OAUTH_JSON` remains only for the manual agy check. No
credential is stored in the public engine repository. Preserve the caller's
explicit secret name mappings: Environment binding alone did not expose Secrets in
hosted acceptance. Repository Variables select models and routes (`GROK_*`,
`GPT_BASE_URL`, `GPT_MODEL`, `GPT_EFFORT`); `AUTO_REVIEW_ENABLED=true` and
`AUTO_REVIEW_PUBLISH=true` activate automatic review and persistent state/comments.
A manual `dry-run` performs no provider calls or PR writes.

For credential handling and recovery, use the standalone
[credentials guide](https://github.com/reed-yang/independent-pr-review/blob/main/docs/authentication.md)
and [operations guide](https://github.com/reed-yang/independent-pr-review/blob/main/docs/operations.md).

## Boundaries and evidence

Preparation, each reviewer lane and publication run in separate jobs. A reviewer
job receives only its own provider key, no GitHub token and no state-signing key.
The GPT key stays in a loopback proxy in the engine process; Codex holds only a
per-run proxy token that sandboxed commands cannot see, and the action qualifies
the sandbox (no writes, no network, no visible secrets) before any model call.
PR content may execute only inside that sandbox. The HMAC state is authenticated,
not encrypted and not an administrator-proof audit log.

Run caps are hard per PR; tokens use actual or estimated accounting and are a soft
guard. Provider or verification failure is partial/failed, never a clean review.
Review coverage does not establish full product CI, installed runtime or browser
acceptance.

Before release, the v0.4 engine replayed five of this repository's PRs locally with
the real models. It found and cross-verified the PR #22 reread race (lost by the
v0.3 verifier) and the PR #30 nested-destination citation bypass (missed by v0.3),
and reported no finding on PR #31, where v0.3 had produced a false positive. The
[mature review study](../../docs/plans/pr-review-quality.md) and the original
[OAuth study](../../docs/plans/pr-review-workflow.md) are historical context for the
v0.2/v0.3 design.
