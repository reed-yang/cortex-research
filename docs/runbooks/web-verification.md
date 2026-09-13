# Web verification tiers

Which command covers what, what each one needs, and which gates no command
covers. Timings are wall clock on a warm 15-core macOS laptop with dependencies
installed; treat them as orders of magnitude, not budgets.

## Fast tier — `npm run test:fast`

Browser-free and the only tier hosted CI runs. Roughly 8-10 s warm.

`verify:safety` (loopback bindings and the port registry), `typecheck`,
`verify:fixtures` and `test:fixtures` (canonical UI snapshot), `verify:screenshots`
and `verify:shell-screenshots` (committed PNG digests and geometry, decoded
without a browser), `test:screenshots`, `verify:pwa` (icons and manifest
contract), `test:selection` (suite selection and the browser-verifier roster),
`test:access-identity`, `test:ui` (25 vitest/jsdom files, 412 tests).

Needs: Node 22.13+ (26.0.0 here) and `npm ci` in `apps/web`. Nothing else.

`typecheck` compiles two programs. `tsconfig.json` covers the app with the DOM
lib; `worker/tsconfig.json` covers `worker/` with `@cloudflare/workers-types`
and the workerd lib, because the worker entry runs on workerd and its `Fetcher`
and `D1Database` bindings do not exist in a browser. 2.2 s cold, 1.0 s warm.

## Full release chain — `npm test`

The fast tier, then `npm run build`, then the suites that need a build, a
browser or Python. Add roughly ten minutes over the fast tier.

Needs, beyond the fast tier:

- A Chromium-family browser. The harness launches the `msedge` channel by
  default; override with `CAPTURE_BROWSER_CHANNEL` or point
  `CAPTURE_BROWSER_EXECUTABLE` at an executable. Nothing downloads a browser.
- `CORTEX_TEST_PYTHON` pointing at this checkout's own interpreter, for the two
  process gates that start a Python Control fixture. Without it they fall back
  to `<repo>/.venv/bin/python`, so create that with `uv sync --frozen --python
  3.12` in the checkout and confirm `cortex_platform` and `cortex_research`
  import from the same checkout before trusting a result.
- `git`, for the release-supply fixture repository.

A browser acceptance is expensive, so `scripts/browser-verifiers.mjs` states,
for each script that launches one, whether the release chain runs it and how.
`tests/test-selection.test.mjs` checks that statement, so the roster is the
place to look and the place to change. Current entries:

| Script | Tier | Run by |
|---|---|---|
| `verify-control-workflow.mjs` | full-release | `tests/control-workflow-process.test.mjs` |
| `verify-research-resumption.mjs` | full-release | `tests/research-resumption-process.test.mjs` |
| `verify-mobile-pwa.mjs` | full-release | `test:mobile` |
| `verify-markdown-math.mjs` | full-release | `test:markdown-math` |
| `capture-screenshots.mjs` | baseline-capture | `capture:screenshots`, checked by `verify:screenshots` |
| `capture-shell-screenshots.mjs` | baseline-capture | `capture:shell`, checked by `verify:shell-screenshots` |
| `verify-research-composer.mjs` | superseded | nothing |

A capture script rewrites committed evidence, so it stays out of the release
chain by design and the roster guard enforces that. Never run one to make a
failing screenshot check pass: investigate the drift first.

## Hosted CI — `.github/workflows/fast-checks.yml`

Two ubuntu jobs: the Control job runs `uv sync --frozen`, asserts the checkout
owns the imported `cortex_platform` and that the installed version matches
`distribution/release.toml`, checks `uv.lock` is unchanged, and runs the
provider-free Control and artifact contracts; the Web job runs `npm ci` and
`npm run test:fast`.

CI cannot run the browser acceptances, the build, macOS-only work, the
installed bundle or anything needing a provider. Those are named gates below,
not silent gaps. Whether a workflow file exists says nothing about whether a
run passed or whether any branch requires it; check the Actions run and the
branch settings.

## Gates no local command covers

- **Release bundle**: offline install, private-access boundary, start/stop and
  rollback of a built bundle. Those run through `cortex-dist` against an
  installed generation, not from this checkout.
- **macOS host**: launch job lifecycle and Keychain access on the deployment
  host.
- **Provider**: real model turns, Telegram receive and delivery, and acceptance
  against a served address. No test here uses a real credential, and none
  should.

A missing prerequisite is a gate. Report it as unavailable, never as a pass.
