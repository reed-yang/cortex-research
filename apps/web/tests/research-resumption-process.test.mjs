// The Research resumption acceptance, run the way the workflow acceptance is
// run: its own guards checked statically, its fixture's refusals checked for
// real, and then the whole browser acceptance in a bounded child whose process
// group is always cleaned up.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(webRoot, "../..");
const fixtureScript = path.join(webRoot, "scripts/research-resumption-fixture.py");
const python = process.env.CORTEX_TEST_PYTHON ?? path.join(repositoryRoot, ".venv/bin/python");
const outputLimit = 8 * 1024;

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

function processGroupExists(pid) {
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

async function cleanupProcessGroup(pid) {
  if (!processGroupExists(pid)) return;
  try {
    process.kill(-pid, "SIGTERM");
  } catch (error) {
    if (error?.code === "ESRCH") return;
    throw error;
  }
  const deadline = Date.now() + 2_000;
  while (processGroupExists(pid) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  if (processGroupExists(pid)) process.kill(-pid, "SIGKILL");
  const killDeadline = Date.now() + 2_000;
  while (processGroupExists(pid) && Date.now() < killDeadline) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.equal(processGroupExists(pid), false, `process group ${pid} survived cleanup`);
}

async function assertPortReusable(port) {
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => server.close(resolve));
  });
}

async function runFixture(command, { root, capability, boundRoot = root }) {
  const child = spawn(python, [fixtureScript, command, "--root", root], {
    cwd: repositoryRoot,
    env: {
      HOME: os.tmpdir(),
      PATH: process.env.PATH,
      CORTEX_RESEARCH_FIXTURE_CAPABILITY: capability,
      CORTEX_RESEARCH_FIXTURE_ROOT: boundRoot,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONPATH: repositoryRoot,
      TMPDIR: os.tmpdir(),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const append = (chunk) => { output = `${output}${chunk}`.slice(-outputLimit); };
  child.stdout.on("data", append);
  child.stderr.on("data", append);
  const code = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
  return { code, output };
}

async function temporaryRoot() {
  const root = await mkdtemp(path.join(os.tmpdir(), "cortex-research-resumption-"));
  const capability = randomBytes(32).toString("base64url");
  await writeFile(path.join(root, ".cortex-research-fixture-v1"), capability, {
    encoding: "ascii", flag: "wx", mode: 0o600,
  });
  return { capability, root };
}

test("the research resumption harness keeps its loopback, capability and cleanup guards", async () => {
  const [verifier, fixture, wrapper] = await Promise.all([
    readFile(path.join(webRoot, "scripts/verify-research-resumption.mjs"), "utf8"),
    readFile(fixtureScript, "utf8"),
    readFile(fileURLToPath(import.meta.url), "utf8"),
  ]);
  assert.ok(/context\.route\(/.test(verifier), "browser requests must be loopback-routed");
  assert.ok(/blockedRequests/.test(verifier), "a request outside the loopback origin must fail the acceptance");
  assert.ok(/await response\.body\(\)/.test(verifier), "every browser response body must be scanned as bytes");
  assert.ok(/closeAndDrainResponses/.test(verifier), "response scans must drain after context close");
  assert.ok(/quiesceResponses/.test(verifier), "responses must go quiet before the context is closed");
  assert.ok(/output\.full/.test(verifier), "complete process output must be scanned");
  assert.ok(/assertPortReusable/.test(verifier), "every reserved port must be proven free again");
  assert.ok(/PRIVATE_LINE/.test(verifier), "the withheld document line must be scanned for");
  assert.ok(/CORTEX_RESEARCH_FIXTURE_CAPABILITY/.test(verifier), "the fixture root must be capability-bound");
  // The guard has to be installed before the product is imported, or an import
  // could open a socket before the fixture ever denies one.
  assert.ok(
    fixture.indexOf("_install_network_guard()\n") < fixture.indexOf("from cortex_platform"),
    "the fixture must deny networking before it imports the product",
  );
  assert.ok(/CORTEX_RESEARCH_FIXTURE_CAPABILITY/.test(fixture), "fixture roots must use a capability");
  assert.ok(/cleanupProcessGroup/.test(wrapper), "detached process groups must always be cleaned");
});

test("the fixture denies networking and refuses a root it was not given", { timeout: 60_000 }, async () => {
  const { capability, root } = await temporaryRoot();
  const stranger = await mkdtemp(path.join(os.tmpdir(), "cortex-research-resumption-"));
  try {
    const guarded = await runFixture("assert-network-guard", { capability, root });
    assert.equal(guarded.code, 0, guarded.output);
    assert.deepEqual(JSON.parse(guarded.output.trim().split("\n").at(-1)), {
      probes: 12, state: "network_guarded",
    });
    // A root with no capability marker, and a root the verifier did not bind:
    // both are refused before anything is written.
    const unmarked = await runFixture("seed", { capability, root: stranger });
    assert.notEqual(unmarked.code, 0, "the fixture accepted an unmarked root");
    const unbound = await runFixture("seed", { capability, root, boundRoot: stranger });
    assert.notEqual(unbound.code, 0, "the fixture accepted a root it was not bound to");
  } finally {
    await rm(root, { force: true, recursive: true });
    await rm(stranger, { force: true, recursive: true });
  }
});

test("the real research resumption acceptance passes and cleans up", { timeout: 300_000 }, async () => {
  const child = spawn(process.execPath, ["scripts/verify-research-resumption.mjs"], {
    cwd: webRoot,
    detached: true,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const append = (chunk) => { output = `${output}${chunk}`.slice(-outputLimit); };
  child.stdout.on("data", append);
  child.stderr.on("data", append);

  let timeout;
  let exit;
  const close = new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => resolve({ code, signal }));
  });
  try {
    exit = await Promise.race([
      close,
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("research resumption acceptance exceeded 290 seconds")), 290_000);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
    await cleanupProcessGroup(child.pid);
  }

  const summary = output.trim().split("\n").reverse().map((line) => {
    try {
      return JSON.parse(line);
    } catch {
      return null;
    }
  }).find((value) => value && Array.isArray(value.ports) && Array.isArray(value.pids));
  assert.ok(summary, `acceptance did not report cleanup identities:\n${output}`);
  assert.equal(exit.code, 0, `acceptance failed (${exit.signal ?? "no signal"}):\n${output}`);
  assert.equal(summary.status, "ok");
  assert.equal(summary.ports.length, 2);
  assert.equal(summary.pids.length, 2);
  for (const port of summary.ports) await assertPortReusable(port);
  for (const pid of summary.pids) assert.equal(processExists(pid), false, `PID ${pid} survived acceptance`);
  assert.equal(summary.network_guard_probes, 12);
  assert.equal(summary.fixture_commands, 11);
  assert.ok(summary.rendered_math >= 2, "both dossiers must render KaTeX math");
  assert.ok(summary.responses > 0);
});
