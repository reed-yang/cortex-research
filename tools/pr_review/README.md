# Independent PR review

Repository tooling for bounded, independent Grok and Gemini opinions. This tool
is outside the Cortex product wheel, Hermes backend and installed mini runtime.
It fetches a fixed PR base/head packet through GitHub API, reviews patches plus
selected complete changed files, validates quoted evidence, and emits JSON and
Markdown. It never checks out, imports or executes a PR head.

## Current readiness

- Grok: compatible HTTP adapter implemented; requires an explicitly selected
  gateway key, HTTPS base URL including `/v1`, and an exact callable model ID.
  Environment presence is configuration evidence, not a successful model call.
- Gemini: unavailable pending an Antigravity (`agy`) subscription adapter and
  hosted-runner authentication qualification. No legacy Gemini CLI OAuth code,
  credential export or paid-API fallback is included.
- Both automatic inference and comment publication default to off. The workflow
  is advisory; do not require its check for merging while a lane is unavailable.
- A completed Grok opinion plus unavailable Gemini is **partial**, exits nonzero,
  and still saves its report. Findings are kept independently, not majority-voted.

## Local commands

Run from this checkout; Python 3.11+ and the standard library suffice:

```bash
python3 -m unittest discover -s tools/pr_review -v
python3 tools/pr_review/review.py check --config tools/pr_review/backends.json
```

The check prints variable **names** and availability, never credential values.
For a real open, non-draft, same-repository PR targeting `main`, authenticate
GitHub CLI and collect input without calling a model:

```bash
export GH_TOKEN="$(gh auth token)"
python3 tools/pr_review/review.py prepare \
  --repo OWNER/cortex-research --pr 123 --base-branch main \
  --rules tools/pr_review/cortex-rules.md --out logs/pr-review/packet.json
unset GH_TOKEN
python3 tools/pr_review/review.py run --dry-run \
  --packet logs/pr-review/packet.json --config tools/pr_review/backends.json \
  --out logs/pr-review/result.json
```

A dry run reports `dry_run` / `not_run`; it does not simulate successful model
opinions. Do not commit packets, responses, logs or credentials. Remove
`--dry-run` only with deliberately configured review credentials.

## GitHub Actions setup

`.github/workflows/auto-review.yml` executes trusted default-branch tooling.
Fork PRs, drafts and other base branches are refused. It coalesces runs per PR.
The workflow must first exist on the default branch for manual dispatch.

| Repository setting | Purpose |
| --- | --- |
| Secret `GROK_API_KEY` | Key authorized for this review gateway |
| Variable `GROK_BASE_URL` | Exact HTTPS compatible API base, including `/v1` |
| Variable `GROK_MODEL` | Model ID qualified through that key |
| Variable `AUTO_REVIEW_ENABLED` | Set `true` to permit model runs and eligible PR-event triggers |
| Variable `AUTO_REVIEW_PUBLISH` | Set `true` separately to post/update the report comment |

Without activation, use Actions → **Independent PR review** → **Run workflow**,
select the default branch, a PR number and `dry-run`. This validates collection
and artifact delivery without secrets. Its readiness output will report the Grok
key missing because dry-run deliberately does not receive model credentials.

After configuration, dispatch `review` with publication disabled. Check actual
model identity, usage, finding quality and failure status before enabling PR
comments. Publishing is a separate job with GitHub write permission and no model
credentials; it rechecks both base and head and refuses stale results or dry runs.
Only JSON/Markdown results are uploaded, with three-day artifact retention.
Neither configuration nor this integration authorizes a billing-route change.

To pause, set `AUTO_REVIEW_ENABLED=false`; cancel already-running jobs separately.
Set `AUTO_REVIEW_PUBLISH=false` to keep reports in Actions only. Revert this
integration to remove the tool; no product version or schema rollback is involved.

## Gemini subscription gate

Google's [migration announcement](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)
states consumer AI Pro/Ultra/free Gemini CLI service ended on June 18, 2026.
Current [agy authentication docs](https://antigravity.google/docs/cli/install/)
describe interactive sign-in backed by the OS keyring. Its
[headless contract](https://antigravity.google/docs/cli/headless/) requires cached
credentials and `status == SUCCESS`; exit zero alone is insufficient. These
facts do not qualify a portable subscription session on a fresh hosted runner.

Before replacing the disabled slot, establish supported sign-in and refresh on
the chosen host, verify actual AI Pro entitlement, pin an available Gemini model,
and enforce [tool restrictions](https://antigravity.google/docs/cli/permissions/)
in an isolated workspace. Default headless mode can still read/write workspace
files. Do not simply rename the old CLI or upload its OAuth file. Keep paid API
mode and automatic [credit overages](https://antigravity.google/docs/plans)
out of this subscription lane. A separate persistent reviewer host is a possible
follow-up if hosted login cannot be qualified, not part of this setup.

## Limits

The 180,000-character packet budget is a heuristic, not a token count. Collection
includes changed patches and eligible full head files, not arbitrary callers,
repository search or executed tests. Missing context is recorded. Evidence
validation confirms a quote/location, not that the bug is real. No incremental
cache, semantic deduplication, agentic tools or automatic fixes are implemented.
The summary comment is not a GitHub inline review. A small check-to-comment race
remains; every report carries its reviewed SHA. New adapters must preserve opinion
family and authentication route; quota/auth failures do not trigger silent fallback.
