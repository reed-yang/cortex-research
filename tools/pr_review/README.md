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
- Gemini: native Gemini HTTP adapter implemented; requires a gateway key, HTTPS
  API base including `/v1beta`, and an exact Gemini model ID. The optional agy
  subscription backend remains disabled and is not a fallback.
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

Create a GitHub environment named `pr-review`, select **Selected branches and
tags**, and allow only the `main` **branch**. Save both keys as environment
secrets; do not create repository-wide copies. The review job references this
environment. This blocks a workflow running on another branch from obtaining
the keys, including a modified workflow proposed in a PR.

The protected job checks out the trusted workflow SHA, fetches PR content only
as data, and passes model credentials only to the inference step. It runs no PR
scripts, package installation, CLI agents or tools. HTTP redirects are refused;
provider errors are reduced to status codes. The optional publisher has no model
keys. Dry runs receive neither key. Keep `main` and its workflow changes trusted:
environment protection is not a defense against a maintainer changing trusted
code on that branch. See GitHub's
[environment protection contract](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

| Repository setting | Purpose |
| --- | --- |
| Environment secret `GROK_API_KEY` | Key authorized for the Grok review gateway |
| Variable `GROK_BASE_URL` | Exact HTTPS compatible API base, including `/v1` |
| Variable `GROK_MODEL` | Model ID qualified through that key |
| Environment secret `GEMINI_API_KEY` | Key authorized for the Gemini review gateway |
| Variable `GEMINI_BASE_URL` | Exact HTTPS Gemini API base, including `/v1beta` |
| Variable `GEMINI_MODEL` | Exact Gemini model ID qualified through that key |
| Variable `AUTO_REVIEW_ENABLED` | Set `true` to permit eligible PR-event triggers |
| Variable `AUTO_REVIEW_PUBLISH` | Set `true` separately to post/update the report comment |

Without activation, use Actions → **Independent PR review** → **Run workflow**,
select the default branch, a PR number and `dry-run`. This validates collection
and artifact delivery without secrets. Its readiness output will report the Grok
keys missing because dry-run deliberately does not receive model credentials.

After configuration, dispatch `review` with publication disabled. A deliberate
manual review on `main` can run while automatic PR triggers remain disabled.
Check actual
model identity, usage, finding quality and failure status before enabling PR
comments. Publishing is a separate job with GitHub write permission and no model
credentials; it rechecks both base and head and refuses stale results or dry runs.
Only JSON/Markdown results are uploaded, with three-day artifact retention.
Gateway access uses the explicitly selected key and gateway billing route;
failures never fall back to subscription login or a different provider.

To provision or rotate a key, run `gh secret set GROK_API_KEY --env pr-review`
or `gh secret set GEMINI_API_KEY --env pr-review` from this repository and use
the hidden prompt. For automation, pass the value to `gh secret set` through
stdin. Never put the value in a command argument, tracked `.env`, workflow YAML,
issue, comment, or Actions artifact. Existing values cannot be read back from
GitHub; verify secret names and update timestamps with
`gh secret list --env pr-review`.

To pause, set `AUTO_REVIEW_ENABLED=false`; cancel already-running jobs separately.
Set `AUTO_REVIEW_PUBLISH=false` to keep reports in Actions only. Revert this
integration to remove the tool; no product version or schema rollback is involved.

## Optional Gemini subscription gate

Google's [migration announcement](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)
states consumer AI Pro/Ultra/free Gemini CLI service ended on June 18, 2026.
Current [agy authentication docs](https://antigravity.google/docs/cli/install/)
describe interactive sign-in backed by the OS keyring. Its
[headless contract](https://antigravity.google/docs/cli/headless/) requires cached
credentials and `status == SUCCESS`; exit zero alone is insufficient. These
facts do not qualify a portable subscription session on a fresh hosted runner.

Before selecting the disabled subscription backend, establish supported sign-in and refresh on
the chosen host, verify actual AI Pro entitlement, pin an available Gemini model,
and enforce [tool restrictions](https://antigravity.google/docs/cli/permissions/)
in an isolated workspace. Default headless mode can still read/write workspace
files. Do not simply rename the old CLI or upload its OAuth file. Keep paid API
mode and automatic [credit overages](https://antigravity.google/docs/plans)
out of this subscription lane. A separate persistent reviewer host is a possible
follow-up if hosted login cannot be qualified, not part of this setup.

## Authentication options verified on September 14, 2026

Choose the execution host together with the authentication route:

| Route | Session ownership | Integration work still required |
| --- | --- | --- |
| Grok Build + agy subscription login | Persistent operator host | Two CLI adapters and qualification under the actual unattended user |
| Grok gateway + agy subscription login | Gateway key plus persistent operator host | agy adapter; hosted Actions alone cannot use the host's keyring |
| Grok + Gemini through an authorized gateway | Gateway manages upstream accounts; Actions receives API keys | Adapters and protected workflow inputs implemented; qualify the deployed upstream accounts |

Persistent login means credentials can be reused across processes, not that
consent, entitlement or a refresh token can never expire or be revoked. A fresh
GitHub-hosted runner has neither the workstation's cached login nor its keyring.
Start subscription qualification locally before designing runner infrastructure.

### Grok Build subscription login

Official [Grok Build authentication](https://docs.x.ai/build/enterprise)
supports refreshable browser OIDC and device-code sessions. The
[CLI reference](https://docs.x.ai/build/cli/reference) documents these commands:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
grok login --device-auth
grok models
```

Complete the browser consent from the URL/code printed by the CLI. Browser login
is also available through `grok login`. Confirm account entitlement and choose an
actual Grok model from the returned list. Authentication success alone does not
prove that the chosen model is callable. Per-model API-key configuration can take
precedence over a session, so verify the effective route when testing subscription
access. See the [official installation overview](https://docs.x.ai/build/overview).

The [headless interface](https://docs.x.ai/build/cli/headless-scripting) supports
`grok --no-auto-update -p "Reply with OK only. Do not use tools." --output-format json`.
Use only a harmless smoke prompt in an empty workspace initially. The prompt is
not a tool restriction: a review adapter still needs enforceable isolation,
explicit model selection, output validation and failure handling. The existing
HTTP adapter cannot consume a Grok Build session by setting `GROK_API_KEY`.

### agy subscription login

Install using Google's [documented installer](https://antigravity.google/docs/cli/install/).
These flags preserve shell aliases and profiles:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --skip-aliases --skip-path
~/.local/bin/agy
~/.local/bin/agy models
```

Sign in interactively once using the intended Google account. Local login uses
the OS keyring; SSH login prints a URL and accepts the authorization code returned
by the browser. Keep the default account provider for subscription access.

Then start a new process on the same host and user:

```bash
~/.local/bin/agy -p "Reply with OK only. Do not use tools." --output-format json
```

Require both exit zero and `status == SUCCESS`, as described in the
[headless documentation](https://antigravity.google/docs/cli/headless/). Check
again from the intended unattended session after login/token renewal; the docs
do not promise an indefinitely valid session. A login in the desktop session
does not prove that an SSH process or service can unlock the same keyring.

Before reviewing PR data, qualify an isolated adapter with explicit model
selection and [deny rules](https://antigravity.google/docs/cli/permissions/) for
file, command, URL and MCP operations. Also exclude inherited hooks and plugins.
Headless defaults allow workspace file access, and a prompt saying "do not use
tools" is insufficient. Keep subscription credit overages disabled.

### Gateway API keys

For this packet reviewer, direct HTTP is the shortest gateway integration; a
CLI installation or workstation OAuth session is unnecessary. Provision a key
authorized for review and select each exact model from that key's model catalog.
Do not infer access from another application's working OpenAI model.

Upstream Sub2API [group routing](https://github.com/Wei-Shaw/sub2api/blob/main/docs/COMPOSITE_GROUPS.md)
can bind a key to a provider group or route models through a composite group.
The deployed gateway must actually support and enable those routes. Separate
Grok and Gemini keys may be needed; one working Codex key does not establish
access to either family.

The [upstream gateway routes](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/server/routes/gateway.go)
include `/v1/chat/completions` and Gemini-native `/v1beta/models/...` endpoints.
First qualify the deployed protocol with an authorized key and a small prompt.
The selected Gemini backend uses native `generateContent`, with
`opinion_family: gemini`, `auth_mode: gateway_api_key`, and distinct
`GEMINI_BASE_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL` inputs. It accepts only complete
text output, excludes thought parts and rejects tool calls. Never relabel gateway
access as a verified personal Google AI Pro session.

For users who specifically want agy through a Gemini-compatible gateway, its
[API mode](https://antigravity.google/docs/cli/install/) requires
`"modelProvider": "gemini"` in `~/.gemini/antigravity-cli/settings.json`, plus
`GEMINI_API_KEY` and `GOOGLE_GEMINI_BASE_URL` in the child process environment.
Use the Gemini API root, not the OpenAI `/v1` base. Confirm any gateway-specific
prefix. This selects API-key access instead of account login and follows gateway
billing. Removing `modelProvider` restores account login. This mode is an
explicit alternative, never an automatic fallback after subscription failure.

After both lanes pass locally, supply their qualified variables/secrets to the
workflow and validate an artifact-only run. OAuth adapters remain unimplemented;
the gateway adapters do not depend on a workstation or any subscription CLI.

## Limits

The 180,000-character packet budget is a heuristic, not a token count. Collection
includes changed patches and eligible full head files, not arbitrary callers,
repository search or executed tests. Missing context is recorded. Evidence
validation confirms a quote/location, not that the bug is real. No incremental
cache, semantic deduplication, agentic tools or automatic fixes are implemented.
The summary comment is not a GitHub inline review. A small check-to-comment race
remains; every report carries its reviewed SHA. New adapters must preserve opinion
family and authentication route; quota/auth failures do not trigger silent fallback.
