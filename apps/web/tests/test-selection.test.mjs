// A test that is written but never selected is worse than no test: it reads as
// covered while nothing runs it. The suites here are named path by path in
// `package.json`, so this checks the selection itself -- every test file is run
// by the full `npm test` chain, every named path still exists, no runner script
// is stranded outside a chain, and the fast tier stays a subset of the release
// chain rather than a second, quietly diverging suite.
//
// The same question applies to the browser acceptances in `scripts/`, which no
// test-file rule can see: `scripts/browser-verifiers.mjs` says which chain runs
// each one, and the last tests here check that roster against the directory and
// against the scripts.

import assert from "node:assert/strict";
import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { BROWSER_VERIFIERS, VERIFIER_TIERS } from "../scripts/browser-verifiers.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const scripts = JSON.parse(await readFile(path.join(webRoot, "package.json"), "utf8")).scripts;
const TEST_FILE = /\.test\.(?:mjs|ts|tsx)$/;
const FULL_CHAIN = "test";
const FAST_CHAIN = "test:fast";

const testFiles = (await readdir(path.join(webRoot, "tests"), { recursive: true }))
  .map((entry) => `tests/${entry.split(path.sep).join("/")}`)
  .filter((entry) => TEST_FILE.test(entry))
  .sort();

// Only `node --test` and `vitest run` select files; a `node scripts/...` step is
// a verifier, not a suite, and is checked by its own script.
function selectedPaths(script) {
  const selections = [];
  for (const command of script.split("&&")) {
    const tokens = command.trim().split(/\s+/).filter(Boolean);
    const isNodeTest = tokens[0] === "node" && tokens.includes("--test");
    const isVitest = tokens[0] === "vitest" && tokens[1] === "run";
    if (!isNodeTest && !isVitest) continue;
    const paths = tokens.slice(1).filter((token) => !token.startsWith("-") && token !== "run");
    assert.ok(paths.length > 0, `${command.trim()} must name the files it runs`);
    selections.push(...paths);
  }
  return selections;
}

function reachableScripts(entry) {
  const reached = new Set();
  const walk = (name) => {
    if (reached.has(name)) return;
    const script = scripts[name];
    assert.equal(typeof script, "string", `${name} script is required`);
    reached.add(name);
    for (const [, target] of script.matchAll(/npm run ([\w:.-]+)/g)) walk(target);
  };
  walk(entry);
  return reached;
}

function coveredFiles(scriptNames) {
  const covered = new Map();
  for (const name of scriptNames) {
    for (const selection of selectedPaths(scripts[name])) {
      const normalized = selection.replace(/\/$/, "");
      for (const file of testFiles) {
        if (file === normalized || file.startsWith(`${normalized}/`)) {
          covered.set(file, [...(covered.get(file) ?? []), name]);
        }
      }
    }
  }
  return covered;
}

const fullChain = reachableScripts(FULL_CHAIN);
const fastChain = reachableScripts(FAST_CHAIN);

test("every test file is selected by the full npm test chain", () => {
  assert.ok(testFiles.length > 0, "no test files were discovered");
  const covered = coveredFiles(fullChain);
  assert.deepEqual(
    testFiles.filter((file) => !covered.has(file)),
    [],
    "these test files exist but nothing in `npm test` runs them",
  );
});

test("every selected path exists and holds tests", async () => {
  for (const [name, script] of Object.entries(scripts)) {
    for (const selection of selectedPaths(script)) {
      const normalized = selection.replace(/\/$/, "");
      const target = await stat(path.join(webRoot, normalized)).catch(() => null);
      assert.ok(target, `${name} selects ${selection}, which does not exist`);
      if (TEST_FILE.test(normalized)) {
        assert.ok(target.isFile(), `${name} selects ${selection}, which is not a file`);
        continue;
      }
      assert.ok(
        testFiles.some((file) => file.startsWith(`${normalized}/`)),
        `${name} selects ${selection}, which holds no test file`,
      );
    }
  }
});

test("no suite script is stranded outside a chain", () => {
  for (const [name, script] of Object.entries(scripts)) {
    if (selectedPaths(script).length === 0) continue;
    assert.ok(
      fullChain.has(name) || fastChain.has(name),
      `${name} runs tests but neither \`npm test\` nor \`npm run ${FAST_CHAIN}\` reaches it`,
    );
  }
});

test("the fast tier is a subset of the release chain", () => {
  for (const name of fastChain) {
    if (name === FAST_CHAIN) continue;
    assert.ok(fullChain.has(name), `${FAST_CHAIN} runs ${name}, which \`npm test\` does not`);
  }
  const fast = coveredFiles(fastChain);
  const full = coveredFiles(fullChain);
  assert.deepEqual(
    [...fast.keys()].filter((file) => !full.has(file)),
    [],
    `${FAST_CHAIN} selects test files the release chain does not`,
  );
  assert.ok(fast.size > 0, `${FAST_CHAIN} must run some tests`);
  assert.ok(fast.size < full.size, `${FAST_CHAIN} must stay a subset, not a second full suite`);
});

test("this guard is itself selected by both chains", () => {
  const self = `tests/${path.basename(fileURLToPath(import.meta.url))}`;
  assert.ok(coveredFiles(fullChain).has(self), `${self} must run in \`npm test\``);
  assert.ok(coveredFiles(fastChain).has(self), `${self} must run in \`npm run ${FAST_CHAIN}\``);
});

// Only a browser acceptance needs a roster entry, and importing the driver is
// what makes a script one.
const scriptSources = new Map(
  await Promise.all(
    (await readdir(path.join(webRoot, "scripts")))
      .filter((entry) => entry.endsWith(".mjs"))
      .map(async (entry) => [entry, await readFile(path.join(webRoot, "scripts", entry), "utf8")]),
  ),
);
const browserScripts = [...scriptSources]
  .filter(([, source]) => /from "playwright-core"/.test(source))
  .map(([entry]) => entry)
  .sort();

const testSources = new Map(
  await Promise.all(testFiles.map(async (file) => [file, await readFile(path.join(webRoot, file), "utf8")])),
);

// A comment naming a verifier does not run it: `tests/research-outputs.test.tsx`
// cites the selectors `verify-control-workflow.mjs` drives without spawning it.
function runsScript(source, filename) {
  return source
    .split("\n")
    .filter((line) => !/^\s*(?:\/\/|\*)/.test(line))
    .some((line) => line.includes(`scripts/${filename}`));
}

// The script names and selected test files that run the verifier: a script
// command that names it, or a test file that spawns it.
function runnersOf(filename) {
  const runners = Object.entries(scripts)
    .filter(([, script]) => script.includes(`scripts/${filename}`))
    .map(([name]) => name);
  for (const file of coveredFiles(fullChain).keys()) {
    if (runsScript(testSources.get(file) ?? "", filename)) runners.push(file);
  }
  return runners.sort();
}

// Of those, the ones the release chain actually executes. A verifier may also
// have an on-demand write mode -- `capture:mobile` -- which the chain must not
// run, because it rewrites the evidence the same command is checking.
function releaseRunnersOf(filename) {
  return runnersOf(filename).filter((runner) => (TEST_FILE.test(runner) ? coveredFiles(fullChain).has(runner) : fullChain.has(runner)));
}

test("every browser acceptance is classified", () => {
  assert.deepEqual(
    Object.keys(BROWSER_VERIFIERS).sort(),
    browserScripts,
    "scripts/browser-verifiers.mjs and the scripts that launch a browser disagree",
  );
  for (const [filename, entry] of Object.entries(BROWSER_VERIFIERS)) {
    assert.ok(VERIFIER_TIERS.includes(entry.tier), `${filename} has an unknown tier ${entry.tier}`);
    assert.ok(entry.reason?.length > 40, `${filename} must say why it is in that tier`);
  }
});

test("every full-release verifier is actually reached by npm test", () => {
  for (const [filename, entry] of Object.entries(BROWSER_VERIFIERS)) {
    if (entry.tier !== "full-release") continue;
    assert.ok(runnersOf(filename).includes(entry.via), `${filename} claims ${entry.via} runs it`);
    assert.deepEqual(
      releaseRunnersOf(filename),
      [entry.via],
      `\`npm test\` must reach ${filename} through ${entry.via} and nothing else`,
    );
  }
});

test("a verifier the release chain skips is named as skipped, not merely absent", () => {
  for (const [filename, entry] of Object.entries(BROWSER_VERIFIERS)) {
    if (entry.tier === "full-release") continue;
    if (entry.tier === "baseline-capture") {
      assert.ok(runnersOf(filename).includes(entry.via), `${filename} claims ${entry.via} writes its baselines`);
      assert.deepEqual(releaseRunnersOf(filename), [], `${entry.via} rewrites committed evidence and must stay out of \`npm test\``);
      assert.ok(fastChain.has(entry.verifiedBy), `${entry.verifiedBy} must check those baselines in ${FAST_CHAIN}`);
      continue;
    }
    assert.deepEqual(runnersOf(filename), [], `${filename} is classified ${entry.tier} but something runs it`);
    if (entry.tier !== "superseded") continue;
    assert.ok(entry.supersededBy?.length > 0, `${filename} must name what replaced it`);
    for (const replacement of entry.supersededBy) {
      const target = replacement.replace(/\/$/, "");
      assert.ok(
        testFiles.some((file) => file === target || file.startsWith(`${target}/`)) || scriptSources.has(path.basename(target)),
        `${filename} names ${replacement}, which does not exist`,
      );
    }
  }
});
