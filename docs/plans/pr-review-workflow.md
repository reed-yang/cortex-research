# Independent PR review integration

Date: 2026-09-14. Base: public `cortex-research` main at `d1f5a4f`.

## Objective and boundaries

Integrate the existing bounded packet reviewer as repository tooling. Use hosted
Ubuntu Actions and the existing Grok gateway contract. Reserve an independent
Gemini opinion for a qualified Antigravity subscription adapter. Do not introduce
review dependencies into the product, change a deployed installation, export
personal credentials, or substitute separately billed Google APIs.

## Implementation plan

- [x] Restore the named review discussion and check its corrected prototype.
- [x] Verify current target architecture, Actions settings and official agy docs.
- [x] Port the coordinator and focused tests; remove retired Gemini CLI OAuth code.
- [x] Update the policy for the extracted research-only product.
- [x] Add a packet-only Actions dry run and configuration diagnostics; default
      model execution and comment publication to off.
- [x] Add review-tool tests to the existing provider-free fast-checks workflow.
- [x] Validate tests, workflow syntax and a bounded GitHub packet without model calls.
- [x] Commit the integration and record exact remaining activation requirements.

## Original integration decisions

- PR head is API data only; checkout and execute trusted default-branch tooling.
- Only same-repository, non-draft PRs against the default branch are eligible.
- The two reviewer slots remain independent; unavailable Gemini yields a partial
  report, never a second Grok opinion or an apparently clean dual review.
- No credentials are currently configured in this repository or the review
  environment. A key from a different local application is not an implied review key.
- Current agy docs support cached keyring sign-in and headless output. They do
  not establish portable personal subscription login on a fresh hosted runner.
  No agy executable was found at the usual local or mini installation locations.
  Keep this adapter unavailable until authentication and tool isolation pass.
- A manual dry run collects real immutable PR input and reports readiness, without
  a provider call or a PR comment. It is useful before secrets are provisioned.
- Automatic review and publishing have separate activation switches. Neither is
  enabled by this implementation. Preserve the existing fast-checks behavior.

## Acceptance and rollback

Run dependency-free unittest and actionlint. Exercise missing configuration,
partial results, evidence anchors, stale snapshots and publisher target checks.
Use an actual same-repository PR packet when one is available. Provider success,
Google entitlement/refresh, Actions execution and comment delivery are separate
gates; document unperformed gates explicitly. Disable automatic review to stop
new inference; disable publishing independently. Revert the integration commit
to remove repository tooling; no product release or database rollback is needed.

## Validation record

- 25 dependency-free unit tests passed. Coverage includes missing configuration,
  independent/failing slots, dry-run with no provider or publisher calls, manual
  base-branch/fork refusal, evidence anchors and stale result rejection.
- actionlint 1.7.12 passed both workflow files (external shellcheck/pyflakes
  integrations disabled). No Python/Web product implementation changed.
- Real PR #1 at implementation head `5846df4` and base `d1f5a4f`: collected
  all 10 changed files, omitted none, and saved `dry_run` with both slots
  `not_run`. Collection used the real GitHub API; no provider request was made.
- Hosted Fast checks run `34814969491` passed Web, Control and review-tools.
  This proves the provider-free CI integration, not a successful live reviewer.
- The implementation was fast-forwarded to the default branch with both
  activation variables unset. Hosted Independent PR review run `34815165489`
  then succeeded on PR #1 in dry-run mode: report artifacts downloaded and
  inspected; both opinions were `not_run`; the publish job was skipped.
- Real provider calls, subscription authentication and review-comment delivery
  remain unperformed. No model activation or publication variable was enabled.
- No model credential has been read or transferred. No provider call or PR
  report publication has occurred. Gemini remains explicitly unavailable.

## Original subscription activation requirements

1. Select the Grok review gateway, callable model and existing authorized key.
   Provision only the review-specific repository variables/secret named in the
   tool README; no unrelated application key is used implicitly.
2. Qualify a supported agy personal-subscription login/refresh path and a tool-free
   adapter. Do not substitute paid Gemini API mode or count another Grok call as
   the Gemini opinion.
3. The workflow is installed and its hosted dry run passed. After provider
   qualification, enable artifact-only model runs. Confirm model identity and
   finding quality before separately authorizing comment publication.

Product 0.1.20, Control schema 19 and the qualified Hermes gen9 runtime do not
change as a result of this repository-tooling integration.

## Authentication research follow-up: September 14, 2026

The operator requested persistent OAuth where practical and proposed a gateway
API key as an alternative. This expands the options for a deliberately selected
Gemini gateway lane; it does not enable automatic billing-route fallback.

- Official Grok Build docs establish browser and device-code login with renewal.
  The current HTTP-only Grok adapter does not implement that CLI route.
- agy docs establish cached keyring and SSH authorization, plus an explicit
  Gemini-compatible API-key mode. Unattended subscription renewal and tool
  isolation on the selected host remain qualification gates.
- Upstream Sub2API supports provider/composite routing and multiple protocols.
  Deployment support, review-key scope and actual callable models must be tested
  separately. A working OpenAI key is not Grok/Gemini access evidence.
- The tool README now contains concrete login and gateway setup steps. No CLI
  was installed, provider inference attempted, Actions credential provisioned, or
  workflow activation changed by this research follow-up. An explicitly
  authorized gateway catalog check did not list Grok or Gemini models.

## Hosted gateway configuration: September 14, 2026

The operator selected GitHub-hosted execution and supplied separate gateway keys.
The current follow-up selects Grok chat completions and Gemini native
generateContent; the disabled subscription adapter remains an optional future
route, not an automatic fallback.

- Provisioned `GROK_API_KEY` and `GEMINI_API_KEY` as encrypted secrets in the
  `pr-review` environment. Its selected-branch policy permits only `main`.
  There are no repository-level copies of these secrets.
- Added the protected environment to the review job and supplied credentials
  only to its inference step. Dry runs and the publisher receive no model keys.
- Manual review on the default branch is independent of automatic PR triggers.
  Forks, drafts and non-default targets remain refused; PR head content remains
  API data and never executable code.
- Gateway catalogs accepted both keys, but smoke inference returned HTTP 503.
  Gemini's native endpoint reported no available upstream Gemini accounts.
  Catalog entries are not evidence of working inference. Automatic invocation
  and publication remain explicitly false until upstream qualification passes.
- After the operator repaired gateway groups, Grok completed an actual packet
  review as `grok-4.6`. Gemini's updated catalog listed only 2.0/2.5 models and
  rejected the previously listed 3.1 model. Testing `gemini-2.5-pro` returned
  HTTP 502, followed by HTTP 503 with no available Gemini accounts. The Gemini
  upstream and hosted dual-review acceptance remain incomplete.
- 31 focused tests and actionlint 1.7.12 passed. Tests cover independent keys and
  protocols, native Gemini output, incomplete/blocked/tool output, invalid model
  paths, redacted failures and the existing snapshot/publication gates.
- Workflow changes are prepared for review; hosted dual-model inference has not
  passed. Key storage is complete independently of provider availability.

## Existing PR compatibility

The first live PR retained its original base object after the default branch
advanced. Both workflow jobs therefore checkout `github.sha`, the immutable
trusted workflow revision, instead of the PR's older base object (which may
predate the tool). Manual dispatch is restricted to the default branch;
`pull_request_target` runs in default-branch context. Packet base/head identity
and stale-result checks remain separate. See the official GitHub
[trigger reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target).
