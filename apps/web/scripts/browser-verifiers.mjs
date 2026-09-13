// Every script in this directory that launches a browser, and what runs it.
//
// A browser acceptance is expensive, so the ones that matter are named in a
// chain and the ones that are not are named here as not running. Without this
// list the difference is invisible: `verify-markdown-math.mjs` and
// `verify-research-composer.mjs` both sat in `scripts/` reachable from no
// command at all, and reading the directory could not tell you that.
//
// `tests/test-selection.test.mjs` checks this roster against the directory and
// against `package.json`, so a new browser acceptance has to be classified and
// a classification cannot quietly stop being true.
//
// Tiers:
// - `full-release`: run by the full `npm test` chain, through `via`.
// - `baseline-capture`: writes committed evidence on demand; a browser-free
//   verifier named in `verifiedBy` checks that evidence in the fast tier.
// - `superseded`: kept for its history, run by nothing, with the checks that
//   replaced it named in `supersededBy`.
// - `manual-diagnostic`: run by hand for a stated reason, run by nothing.
//
// `via` is either a `package.json` script name or a test file that spawns the
// verifier as a child process.

export const BROWSER_VERIFIERS = Object.freeze({
  "capture-screenshots.mjs": Object.freeze({
    tier: "baseline-capture",
    via: "capture:screenshots",
    verifiedBy: "verify:screenshots",
    reason:
      "Writes the desktop G0/G1 baselines. `verify:screenshots` checks the committed PNGs, geometry and scenario URLs without a browser, so the fast tier stays browser-free.",
  }),
  "capture-shell-screenshots.mjs": Object.freeze({
    tier: "baseline-capture",
    via: "capture:shell",
    verifiedBy: "verify:shell-screenshots",
    reason:
      "Writes the shell scene baselines. `verify:shell-screenshots` checks digests, geometry and the scene ledger without a browser.",
  }),
  "verify-control-workflow.mjs": Object.freeze({
    tier: "full-release",
    via: "tests/control-workflow-process.test.mjs",
    reason:
      "The temporary-workflow acceptance: loopback routing, SSE and redaction boundaries, artifact leak canaries and process-group cleanup. Nothing else drives a real run across the Web boundary.",
  }),
  "verify-research-resumption.mjs": Object.freeze({
    tier: "full-release",
    via: "tests/research-resumption-process.test.mjs",
    reason:
      "The real Control catalog, document adoption and dossier flow against the checkout-local Python fixture. Requires `CORTEX_TEST_PYTHON`.",
  }),
  "verify-mobile-pwa.mjs": Object.freeze({
    tier: "full-release",
    via: "test:mobile",
    reason:
      "The only real-browser phone gate: installability, service-worker offline behavior, horizontal overflow, the 44 px touch-target floor across the shell, and the mobile screenshot fingerprints.",
  }),
  "verify-markdown-math.mjs": Object.freeze({
    tier: "full-release",
    via: "test:markdown-math",
    reason:
      "The only check that renders KaTeX in a browser: math count and font loading in chat and the Outputs reader, exact Markdown source and clipboard bytes, folded provenance, and display math scrolling inside its container on a phone. jsdom suites cannot measure any of it.",
  }),
  "verify-research-composer.mjs": Object.freeze({
    tier: "superseded",
    supersededBy: Object.freeze([
      "tests/research-composer.test.tsx",
      "tests/shell/",
      "scripts/verify-mobile-pwa.mjs",
    ]),
    reason:
      "Written for the July composer markup. The shell replaced it: the mode control is now a `role=radio` button rather than a fieldset input, and the textbox it fills (`Send a durable message`) no longer exists, so a 2026-09-09 run failed waiting for that label. Composer mode behavior is covered by `tests/research-composer.test.tsx` and `tests/shell/`; real-browser layout, overflow and touch targets by `verify-mobile-pwa.mjs`. It also starts its own dev server on ports outside the loopback registry.",
  }),
});

export const VERIFIER_TIERS = Object.freeze([
  "full-release",
  "baseline-capture",
  "superseded",
  "manual-diagnostic",
]);
