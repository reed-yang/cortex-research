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
- [ ] Validate tests, workflow syntax and a bounded GitHub packet without model calls.
- [ ] Commit the integration and record exact remaining activation requirements.

## Decisions

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
- GitHub currently has no open PR for a real collection exercise. Push this
  implementation as a reviewable change, then validate its immutable packet.
- No model credential has been read or transferred. No provider call or PR
  report publication has occurred. Gemini remains explicitly unavailable.
