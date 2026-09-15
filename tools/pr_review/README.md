# Independent PR review

Repository tooling for independent Grok and Gemini opinions. It fetches immutable
PR base/head content through GitHub API, reviews patches and selected complete
changed files, validates quoted evidence, and emits JSON and Markdown reports.
It never checks out, imports or executes a PR head. This is outside the Cortex
product wheel, Hermes backend and installed runtime.

## Execution and authentication

- Grok uses the explicitly configured compatible HTTP gateway and review key.
- Gemini uses Google's official **agy 1.2.2** CLI with **personal consumer OAuth**.
  The binary and SHA-512 digest are pinned in `agy-release.json`. It runs directly
  on a fresh GitHub-hosted Ubuntu VM; no mini service or self-hosted runner is used.
- The Gemini native HTTP adapter remains available for an explicitly chosen
  alternative configuration. It is not a fallback for OAuth, quota or CLI errors.
- Automatic inference and comment publication are separate switches. The workflow
  is advisory and should not be required for merging while a lane is unavailable.
- One completed opinion and one failure is **partial**, exits nonzero, and saves
  the available report. Findings remain independent; they are not majority-voted.

Local qualification completed on September 14, 2026: native sign-in, fresh-process
reuse, forced native renewal from a clean HOME, unchanged refresh token, and a
single JSON response and an actual PR review passed. Hosted qualification is the next deployment gate;
installation or mocked tests alone are not acceptance evidence.

## Public repository boundary

Both workflows run only in trusted default-branch context and use the `pr-review`
GitHub environment, configured with **Selected branches and tags → main branch
only**. Store review credentials as environment secrets, without repository-level
copies. Keep main and workflow changes trusted: this protection cannot defend
against a maintainer changing trusted code on main.

`auto-review.yml` refuses forks, drafts and PRs targeting other branches. It
checks out the trusted workflow SHA with persisted GitHub credentials disabled,
collects PR content as API data, and injects provider credentials only into the
inference step. No dependencies or scripts from the PR head execute. HTTP gateway
redirects are refused. Provider errors and native diagnostics are redacted.

Each agy invocation creates a private disposable HOME and workspace, copies only
native OAuth and a trusted custom agent, disables inherited customizations and
default agent components, and denies all file, command, URL and MCP operations.
The child environment excludes GitHub/gateway credentials and alternate auth/API
route overrides. Subscription credit overages remain off.

agy 1.2.2's `init.tools` reports the global registry even for a restricted custom
agent; it is not treated as an effective permissions report. The adapter verifies
the selected agent/model and rejects tool/action events. It uses regular JSON
output validated by this repository: native `--json-schema` requires a `finish`
tool and can otherwise add unwanted turns. A successful result must be complete,
single-turn and free of timeout/truncation signals. The adapter bounds output and
wall time, kills its process group and disposes the native profile, including logs
and conversation databases.

Only normalized result JSON/Markdown are uploaded, retained for three days. The
optional publisher is a separate job with GitHub write permission and no model
credentials; it checks the current base/head again and refuses stale or dry-run
results. See GitHub's [environment protection contract](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

## Configuration

| Repository setting | Purpose |
| --- | --- |
| Environment secret `GROK_API_KEY` | Key authorized for the Grok gateway |
| Variable `GROK_BASE_URL` | Exact HTTPS compatible API base, including `/v1` |
| Variable `GROK_MODEL` | Qualified Grok model ID |
| Environment secret `AGY_OAUTH_JSON` | Native agy consumer OAuth document, including refresh token |
| Variable `AGY_MODEL` | Qualified subscription model, currently `gemini-3.1-pro-high` |
| Variable `AUTO_REVIEW_ENABLED` | `true` permits eligible PR-event inference |
| Variable `AUTO_REVIEW_PUBLISH` | `true` separately permits report comments |

The workflow maps `AGY_MODEL` to the adapter's `GEMINI_MODEL` environment variable
and supplies private temporary `AGY_BIN` / `AGY_WORK_ROOT` paths. The old Gemini
and Antigravity gateway secrets are not injected into the selected OAuth lane.

For first-time login, install the pinned binary and launch it interactively:

```bash
python3 tools/pr_review/install_agy.py --out "$HOME/.local/share/cortex-pr-review/bin/agy/1.2.2/agy"
AGY_CLI_DISABLE_AUTO_UPDATE=true "$HOME/.local/share/cortex-pr-review/bin/agy/1.2.2/agy"
```

Choose the intended personal Google account and finish browser consent in your
own terminal. Exit the CLI, then provision the native file directly into the
protected encrypted secret:

```bash
python3 tools/pr_review/agy_auth.py sync --repo OWNER/cortex-research
```

The helper checks private source permissions, consumer auth and a main-only
environment, and pipes the document to `gh secret set` through stdin. It never
prints the value or puts it in command arguments. Its default source is
`~/.gemini/antigravity-cli/antigravity-oauth-token`; `--source` accepts another
native file. Authenticate GitHub CLI first. If no native file exists, complete
and inspect the CLI's supported login/storage setup rather than substituting an
API key or another application's OAuth token.

To rotate the Grok key, use `gh secret set GROK_API_KEY --env pr-review` with the
hidden prompt. Verify names and timestamps using `gh secret list --env pr-review`.
Never place credentials in a tracked file, shell history, issue, comment, cache
or artifact. GitHub does not let callers read back secret values.

## OAuth lifecycle and health check

Every invocation restores the original encrypted document to a mode-0600 file
inside a mode-0700 HOME, expires its copied access token, and lets **native agy**
perform renewal. The operator's login file and GitHub secret are not modified.
The adapter verifies a new valid access token and an unchanged refresh token,
registers sensitive fields for Actions masking, and rejects credentials in the
returned review. No separate OAuth client or token broker is introduced.

Run Actions → **agy OAuth check** → **Run workflow** on main after provisioning
or changing the pinned release. Two independent Ubuntu jobs each restore the
same original secret, force renewal and execute a harmless single-turn prompt.
They upload no profile or raw CLI output. This checks cross-job reuse rather than
only reuse of an access token on one machine.

OAuth health checks and reviews share an account concurrency group with
cancellation disabled at the job level. Native local invocations also acquire a
file lock. GitHub concurrency allows a limited pending queue, not a durable FIFO;
under a burst of PRs, superseded pending reviews can require a manual dispatch.
Per-PR workflow cancellation still coalesces superseded heads.

A refresh token can be revoked or expire. `agy_authentication_required` means
reauthorize locally and rerun `agy_auth.py sync`. If Google rotates the refresh
token, the adapter fails with `agy_refresh_token_rotated_reprovision_required`;
GitHub Secrets do not automatically receive file writes. Reprovision and rerun
the health check. Frequent rotation requires a durable credential service or
persistent isolated worker before continuing this deployment. No permanent-login
guarantee is implied. See [Google OAuth lifecycle](https://developers.google.com/identity/protocols/oauth2#expiration).

## Running reviews

Run tests and check configuration without calling a provider:

```bash
python3 -m unittest discover -s tools/pr_review -v
python3 tools/pr_review/review.py check --config tools/pr_review/backends.json
```

The check prints variable names and availability, never values. Python 3.11+ and
the standard library suffice. For local collection, supply a GitHub token only
to `prepare` and keep its output in ignored logs:

```bash
GH_TOKEN="$(gh auth token)" python3 tools/pr_review/review.py prepare \
  --repo OWNER/cortex-research --pr 123 --base-branch main \
  --rules tools/pr_review/cortex-rules.md --out logs/pr-review/packet.json
python3 tools/pr_review/review.py run --dry-run \
  --packet logs/pr-review/packet.json --config tools/pr_review/backends.json \
  --out logs/pr-review/result.json
```

In Actions → **Independent PR review** → **Run workflow**, choose main, an open
non-draft same-repository PR number, and `dry-run` or `review`. Dry run collects
input and uploads a readiness report without installing agy or receiving model
credentials. Manual review works while automatic inference is disabled. Check
both actual lane statuses and normalized findings before enabling automatic PR
reviews. Comment publication requires its separate switch.

To pause, set `AUTO_REVIEW_ENABLED=false` and cancel any active jobs separately.
Keep `AUTO_REVIEW_PUBLISH=false` for reports in Actions only. Removing this tooling
needs no product version or schema rollback. For the earlier deployment research
and alternatives, see the [OAuth study](../../docs/plans/pr-review-workflow.md#agy-cli-and-oauth-actions-deployment-study).

## Limits

The 180,000-character packet budget is a heuristic, not a token count. Collection
includes patches and eligible changed files, not arbitrary callers, repository
search or executed tests. Missing context is recorded. Evidence validation checks
a quote/location, not whether a bug is real. No incremental cache, semantic
deduplication, repository exploration or automatic fixes are implemented. The
summary comment is not an inline review; a small check-to-comment race remains.
Reports carry immutable reviewed SHAs. Auth/quota failures never silently change
provider, model family or billing route.
