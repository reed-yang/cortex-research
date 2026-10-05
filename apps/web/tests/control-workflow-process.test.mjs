import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
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

test("temporary workflow harness fail-closes network, logs, and process cleanup", async () => {
  const [verifier, fixture, wrapper] = await Promise.all([
    readFile(path.join(webRoot, "scripts/verify-control-workflow.mjs"), "utf8"),
    readFile(path.join(webRoot, "scripts/workflow-control-fixture.py"), "utf8"),
    readFile(fileURLToPath(import.meta.url), "utf8"),
  ]);
  assert.ok(/context\.route\(/.test(verifier), "browser requests must be loopback-routed");
  assert.ok(/context\.routeWebSocket\(/.test(verifier), "browser WebSockets must be loopback-routed");
  assert.ok(/captureSse/.test(verifier), "SSE payloads must be captured");
  assert.ok(/artifactCanaries/.test(verifier), "partial artifact canaries must be scanned");
  assert.ok(/closeAndDrainResponses/.test(verifier), "response scans must drain after context close");
  assert.ok(/await response\.body\(\)/.test(verifier), "every browser response body must be scanned as bytes");
  assert.ok(/isArtifactContentPathname/.test(verifier), "artifact content responses must use one exact path predicate");
  assert.ok(/event\.redacted/.test(verifier), "redacted replay events must be observed");
  assert.ok(/output\.full/.test(verifier), "complete process output must be scanned");
  assert.ok(/CORTEX_WORKFLOW_FIXTURE_CAPABILITY/.test(fixture), "fixture roots must use a capability");
  assert.ok(/cleanupProcessGroup/.test(wrapper), "detached process groups must always be cleaned");
});

test("a wait started before its trigger is awaited together with it", async () => {
  const verifier = await readFile(path.join(webRoot, "scripts/verify-control-workflow.mjs"), "utf8");
  // A wait parked in a variable has no rejection handler while the steps before
  // its `await` run. If one of them throws, closing the browser or stopping the
  // daemon in `finally` rejects the parked wait, Node exits on the unhandled
  // rejection, and both the cleanup and the original error are lost.
  const parked = [...verifier.matchAll(
    /\b(?:const|let)\s+(\w+)\s*=\s*(?:[\w.]+\.)?(?:waitFor(?:Event|Request|Response|URL)|captureSse)\(/g,
  )].map((match) => match[1]);
  assert.deepEqual(parked, []);
});

// The source of every `Promise.all`/`race`/`any` call, its parentheses balanced.
function combinatorCalls(source) {
  const calls = [];
  for (const match of source.matchAll(/\bPromise\.(?:all|race|any)\(/g)) {
    let depth = 0;
    let end = match.index + match[0].length - 1;
    for (; end < source.length; end += 1) {
      if (source[end] === "(") depth += 1;
      if (source[end] === ")" && --depth === 0) break;
    }
    calls.push(source.slice(match.index, end + 1));
  }
  return calls;
}

test("a fixture run is never abandoned by a combinator that rejects early", async () => {
  const verifier = await readFile(path.join(webRoot, "scripts/verify-control-workflow.mjs"), "utf8");
  // `Promise.all` rejects on the first failure while its other operations still
  // run. A fixture is a child process working under the temporary root, and the
  // verifier's `finally` removes that root, so a fixture run must be settled by
  // `awaitAllSettled`, which waits for every operation before it reports.
  const calls = combinatorCalls(verifier);
  assert.ok(calls.length >= 2, "the combinator scan must see the verifier's calls");
  assert.deepEqual(calls.filter((call) => /\brunFixture\(/.test(call)), []);
  assert.match(verifier, /awaitAllSettled\(\[\s*captureSse\([^)]*\),\s*runFixture\("emit-redacted"/);
});

test("a stream that fails first leaves the fixture running until it exits", async () => {
  const { awaitAllSettled } = await import("../scripts/await-all-settled.mjs");
  const root = await mkdtemp(path.join(os.tmpdir(), "cortex-settle-"));
  const marker = path.join(root, "fixture-finished");
  // A child that writes under the temporary root after the stream has failed,
  // as the `emit-redacted` fixture does while the SSE read gives up.
  const fixture = spawn(process.execPath, [
    "-e",
    `setTimeout(() => require("node:fs").writeFileSync(${JSON.stringify(marker)}, "written"), 300)`,
  ], { stdio: "ignore" });
  const fixtureRun = new Promise((resolve, reject) => {
    fixture.once("error", reject);
    fixture.once("close", (code) => (code === 0 ? resolve("fixture") : reject(new Error(`fixture exited ${code}`))));
  });
  const streamFailure = new Error("stream failed first");
  const stream = new Promise((_, reject) => setTimeout(() => reject(streamFailure), 10));
  try {
    await assert.rejects(awaitAllSettled([stream, fixtureRun]), (error) => error === streamFailure);
    assert.equal(fixture.exitCode, 0, "the fixture must have exited before the failure is reported");
    assert.equal(processExists(fixture.pid), false);
    assert.equal(await readFile(marker, "utf8"), "written");
  } finally {
    if (fixture.exitCode === null) fixture.kill("SIGKILL");
    await rm(root, { force: true, recursive: true });
  }
});

test("awaitAllSettled reports the first failure in time and returns values in order", async () => {
  const { awaitAllSettled } = await import("../scripts/await-all-settled.mjs");
  const delayed = (milliseconds, value, failure) =>
    new Promise((resolve, reject) => setTimeout(() => (failure ? reject(failure) : resolve(value)), milliseconds));
  assert.deepEqual(await awaitAllSettled([delayed(20, "stream"), delayed(5, "fixture"), "plain"]), ["stream", "fixture", "plain"]);

  const fixtureFailure = new Error("fixture failed first");
  const streamTimeout = new Error("stream timed out later");
  let streamSettled = false;
  const stream = delayed(40, undefined, streamTimeout).finally(() => {
    streamSettled = true;
  });
  await assert.rejects(awaitAllSettled([stream, delayed(5, undefined, fixtureFailure)]), (error) => {
    assert.equal(streamSettled, true, "a later failure must still be awaited");
    return error === fixtureFailure;
  });
});

test("real temporary research workflow crosses the Web boundary and cleans up", { timeout: 120_000 }, async () => {
  const child = spawn(process.execPath, ["scripts/verify-control-workflow.mjs"], {
    cwd: webRoot,
    detached: true,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const append = (chunk) => {
    output = `${output}${chunk}`.slice(-outputLimit);
  };
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
        timeout = setTimeout(() => reject(new Error("workflow acceptance exceeded 115 seconds")), 115_000);
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
  assert.equal(summary.ports.length, 3);
  assert.ok(summary.pids.length >= 2);
  for (const port of summary.ports) await assertPortReusable(port);
  for (const pid of summary.pids) assert.equal(processExists(pid), false, `PID ${pid} survived acceptance`);
  assert.equal(summary.artifact_leak_probes, 16);
  assert.equal(summary.browser_websocket_guard_probes, 1);
  assert.equal(summary.capture_source_opened, true);
  assert.equal(summary.idea_saved, true);
  assert.equal(summary.network_guard_probes, 12);
  assert.equal(summary.response_boundary_probes, 3);
  assert.equal(summary.sse_frame_probes, 3);
  assert.equal(summary.status, "ok");
});
