import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { copy, runStateLabel } from "../app/shell/copy.ts";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(webRoot, "../..");
const fixtureScript = path.join(webRoot, "scripts/workflow-control-fixture.py");
const python = process.env.CORTEX_TEST_PYTHON ?? path.join(repositoryRoot, ".venv/bin/python");
const outputLimit = 8 * 1024;
const completeOutputLimit = 2_000_000;
const readinessTimeout = 15_000;

const livingV1 = `# Living Brief v1

Echo-Infinity supplies the evolving-memory baseline for the Helios-14B study [Echo-Infinity, 2026].

- Preserve causal temporal updates.
- Measure memory drift before scaling context.
`;
const livingV2 = `# Living Brief v2

The successor combines online TTT updates with a bounded Helios-14B memory controller while retaining the v1 baseline [Echo-Infinity, 2026].

- Gate writes by novelty and reconstruction error.
- Compare recurrent memory against a frozen Wan2.2 cache.

wrap_probe_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789
`;
const evidence = `# Evidence Matrix

| Claim | Evidence | Risk |
| --- | --- | --- |
| Evolving memory supports long video | Echo-Infinity [2026] | Distribution shift |
| TTT can adapt state online | TTT literature [2025] | Update instability |

The matrix separates observed evidence from the proposed Helios mechanism.
`;
const training = `# Training Plan

1. Freeze Wan2.2 and train only the memory projector.
2. Add truncated online TTT updates with a replay guard.
3. Unfreeze the final temporal blocks after stability ablations.

Primary metrics: temporal consistency, memory retrieval precision, and update latency [Helios protocol, 2026].
`;
const artifactBodies = [livingV1, livingV2, evidence, training];
const execution = { pids: [], ports: [], root: null, secrets: [] };

function outputCollector(description) {
  return { description, full: "", overflow: false, tail: "" };
}

function boundedAppend(target, chunk) {
  const text = String(chunk);
  target.tail = `${target.tail}${text}`.slice(-outputLimit);
  if (target.overflow) return;
  const remaining = completeOutputLimit - target.full.length;
  target.full += text.slice(0, Math.max(0, remaining));
  target.overflow = text.length > remaining;
}

function assertCompleteOutput(output, description) {
  assert.equal(output.overflow, false, `${description} exceeded the complete output scan limit`);
}

function artifactCanaries(body) {
  const raw = new Set([body]);
  for (const line of body.split(/\r?\n/).map((value) => value.trim())) {
    if (line.length < 16) continue;
    raw.add(line);
    if (line.length > 48) {
      raw.add(line.slice(0, 32));
      const words = line.split(/\s+/);
      if (words.length >= 6) {
        const width = Math.min(6, words.length);
        const midpoint = Math.floor((words.length - width) / 2);
        raw.add(words.slice(0, width).join(" "));
        raw.add(words.slice(midpoint, midpoint + width).join(" "));
        raw.add(words.slice(-width).join(" "));
      }
    }
  }
  const canaries = new Set();
  for (const value of raw) {
    canaries.add(value);
    canaries.add(JSON.stringify(value).slice(1, -1));
  }
  return [...canaries];
}

function assertArtifactBodyIsolation(bodies, channels) {
  for (const [bodyIndex, body] of bodies.entries()) {
    for (const [canaryIndex, canary] of artifactCanaries(body).entries()) {
      for (const [name, values] of Object.entries(channels)) {
        assert.ok(
          values.every((value) => !value.includes(canary)),
          `artifact body leaked through ${name} (body ${bodyIndex}, canary ${canaryIndex})`,
        );
      }
    }
  }
}

function runArtifactLeakProbes() {
  const rawCanary = "CORTEX_ARTIFACT_RAW_CANARY_6cbfa7e9";
  const escapedCanary = 'CORTEX_ARTIFACT_ESCAPED_CANARY_"quoted\\path"';
  const body = `# Synthetic artifact leak probe\n\n${rawCanary}\n${escapedCanary}\n`;
  const names = [
    "sse",
    "fixture_stdout_stderr",
    "daemon_stdout_stderr_shutdown",
    "web_stdout_stderr_shutdown",
    "fixture_summaries",
    "non_content_response",
    "service_worker_source",
    "cache_storage",
  ];
  for (const name of names) {
    assert.throws(
      () => assertArtifactBodyIsolation([body], { [name]: [`prefix:${rawCanary}:suffix`] }),
      new RegExp(`artifact body leaked through ${name}`),
    );
    assert.throws(
      () => assertArtifactBodyIsolation([body], { [name]: [JSON.stringify(escapedCanary)] }),
      new RegExp(`artifact body leaked through ${name}`),
    );
  }
  assertArtifactBodyIsolation([body], { safe: ["unrelated output"] });
  return names.length * 2;
}

function hasCompleteSseEvent(body, eventName) {
  const normalized = body.replaceAll("\r\n", "\n");
  const frames = normalized.split("\n\n");
  if (!normalized.endsWith("\n\n")) frames.pop();
  return frames.some((frame) => frame.split("\n").includes(`event: ${eventName}`));
}

function runSseFrameProbes() {
  assert.equal(hasCompleteSseEvent("event: event.redacted\n", "event.redacted"), false);
  assert.equal(hasCompleteSseEvent("event: event.redacted\ndata: {\"partial\":true}", "event.redacted"), false);
  assert.equal(hasCompleteSseEvent("event: event.redacted\ndata: {\"complete\":true}\n\n", "event.redacted"), true);
  return 3;
}

function responseBodyForScan(body, url) {
  assert.ok(body.length <= completeOutputLimit, `response body exceeded scan limit: ${url}`);
  return body.toString("utf8");
}

function isArtifactContentPathname(pathname) {
  return /^\/api\/cortex\/artifact-versions\/[A-Za-z0-9_.:-]+\/content$/.test(pathname);
}

function runResponseBoundaryProbes() {
  const canary = "CORTEX_BINARY_RESPONSE_CANARY_7a5e4d2c";
  const binary = Buffer.concat([Buffer.from([0, 255, 1]), Buffer.from(canary), Buffer.from([254, 2])]);
  assert.ok(responseBodyForScan(binary, "probe://binary").includes(canary));
  assert.equal(isArtifactContentPathname("/api/cortex/artifact-versions/v1/content"), true);
  assert.equal(isArtifactContentPathname("/unexpected/api/cortex/artifact-versions/v1/content"), false);
  return 3;
}

async function waitForChildClose(child) {
  if (child.exitCode !== null && child.stdout.readableEnded && child.stderr.readableEnded) return child.exitCode;
  return await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
}

function sanitize(value, secrets, temporaryRoot) {
  let result = String(value);
  for (const secret of secrets) {
    if (secret) result = result.replaceAll(secret, "[credential]");
  }
  if (temporaryRoot) result = result.replaceAll(temporaryRoot, "[temporary-root]");
  return result;
}

async function reservePort() {
  return await new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      assert.ok(address && typeof address !== "string");
      const port = address.port;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function assertPortReusable(port) {
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => server.close(resolve));
  });
}

async function startWebSocketProbe(port) {
  const probe = { connections: 0, server: null };
  probe.server = createServer((socket) => {
    probe.connections += 1;
    socket.destroy();
  });
  await new Promise((resolve, reject) => {
    probe.server.once("error", reject);
    probe.server.listen(port, "127.0.0.1", resolve);
  });
  return probe;
}

async function stopWebSocketProbe(probe) {
  if (!probe?.server?.listening) return;
  await new Promise((resolve, reject) => {
    probe.server.close((error) => error ? reject(error) : resolve());
  });
}

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

async function runFixture(command, temporaryRoot, capability, secrets, processOutputs, extra = []) {
  const child = spawn(python, [fixtureScript, command, "--root", temporaryRoot, ...extra], {
    cwd: repositoryRoot,
    env: {
      HOME: path.join(temporaryRoot, "home"),
      PATH: process.env.PATH,
      CORTEX_WORKFLOW_FIXTURE_CAPABILITY: capability,
      CORTEX_WORKFLOW_FIXTURE_ROOT: temporaryRoot,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONPATH: repositoryRoot,
      TMPDIR: os.tmpdir(),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const output = outputCollector(`fixture:${command}`);
  processOutputs.push(output);
  child.stdout.on("data", (chunk) => boundedAppend(output, chunk));
  child.stderr.on("data", (chunk) => boundedAppend(output, chunk));
  const code = await waitForChildClose(child);
  assertCompleteOutput(output, command);
  if (code !== 0) throw new Error(`${command} failed: ${sanitize(output.tail, secrets, temporaryRoot)}`);
  const line = output.full.trim().split("\n").at(-1);
  const summary = JSON.parse(line ?? "null");
  const serialized = JSON.stringify(summary);
  assert.doesNotMatch(serialized, /(?:engine_ref|provider_payload|<thinking>|hidden reasoning)/i);
  assert.ok(!serialized.includes(temporaryRoot));
  assert.ok(secrets.every((secret) => !serialized.includes(secret)));
  return summary;
}

async function waitUntilReady(child, description, probe, output, secrets, temporaryRoot) {
  const deadline = Date.now() + readinessTimeout;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`${description} exited early: ${sanitize(output.tail, secrets, temporaryRoot)}`);
    }
    try {
      const value = await probe();
      if (value) return value;
    } catch {
      // The loopback process is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${description} readiness exceeded 15 seconds: ${sanitize(output.tail, secrets, temporaryRoot)}`);
}

async function startDaemon(temporaryRoot, port, secrets, pids) {
  const directories = ["config", "data", "state", "cache", "logs", "home"];
  await Promise.all(directories.map((name) => mkdir(path.join(temporaryRoot, name), { mode: 0o700, recursive: true })));
  const output = outputCollector("daemon");
  const child = spawn(python, [
    "-m", "cortex_platform.product.daemon",
    "--config-dir", path.join(temporaryRoot, "config"),
    "--data-dir", path.join(temporaryRoot, "data"),
    "--state-dir", path.join(temporaryRoot, "state"),
    "--cache-dir", path.join(temporaryRoot, "cache"),
    "--log-dir", path.join(temporaryRoot, "logs"),
    "--host", "127.0.0.1",
    "--port", String(port),
  ], {
    cwd: repositoryRoot,
    env: { HOME: path.join(temporaryRoot, "home"), PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: "1", PYTHONPATH: repositoryRoot },
    stdio: ["ignore", "pipe", "pipe"],
  });
  assert.ok(child.pid);
  pids.push(child.pid);
  child.stdout.on("data", (chunk) => boundedAppend(output, chunk));
  child.stderr.on("data", (chunk) => boundedAppend(output, chunk));
  const metadataPath = path.join(temporaryRoot, "state/cortexd.json");
  const metadata = await waitUntilReady(child, "cortexd", async () => {
    const raw = JSON.parse(await readFile(metadataPath, "utf8"));
    return raw.host === "127.0.0.1" && raw.port === port && typeof raw.control_token === "string" ? raw : null;
  }, output, secrets, temporaryRoot);
  assert.equal(metadata.pid, child.pid);
  secrets.push(metadata.control_token);
  const response = await fetch(`http://127.0.0.1:${port}/api/v1/workspaces`, {
    headers: { "X-Cortex-Control-Token": metadata.control_token },
  });
  assert.equal(response.status, 200);
  return { child, output, token: metadata.control_token };
}

async function startWeb(temporaryRoot, daemonPort, webPort, token, generation, secrets, pids) {
  const payload = path.join(temporaryRoot, `payload-${generation}`);
  await cp(path.join(webRoot, "dist"), payload, { recursive: true });
  await mkdir(path.join(payload, "server"), { recursive: true });
  for (const relative of ["server/access-identity-bound.mjs", "server/node-adapter.mjs"]) {
    await cp(path.join(webRoot, relative), path.join(payload, relative));
  }
  const bootstrap = randomBytes(32).toString("base64url");
  assert.equal(bootstrap.length, 43);
  secrets.push(bootstrap);
  const output = outputCollector(`web:${generation}`);
  const child = spawn(process.execPath, [path.join(payload, "server/node-adapter.mjs")], {
    cwd: payload,
    env: {
      HOME: path.join(temporaryRoot, "home"),
      PATH: process.env.PATH,
      CORTEX_ACCESS_BOOTSTRAP_TOKEN: bootstrap,
      CORTEX_CONTROL_API_URL: `http://127.0.0.1:${daemonPort}`,
      CORTEX_CONTROL_TOKEN: token,
      CORTEX_LOCAL_ACCESS_ENABLED: "1",
      CORTEX_WEB_BUILD_ID: `workflow-${String(generation).padStart(2, "0")}`,
      CORTEX_WEB_LISTEN_HOST: "127.0.0.1",
      CORTEX_WEB_LISTEN_PORT: String(webPort),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  assert.ok(child.pid);
  pids.push(child.pid);
  child.stdout.on("data", (chunk) => boundedAppend(output, chunk));
  child.stderr.on("data", (chunk) => boundedAppend(output, chunk));
  const origin = `http://127.0.0.1:${webPort}`;
  await waitUntilReady(child, "Cortex Web", async () => {
    const response = await fetch(`${origin}/_cortex/health`);
    return response.ok;
  }, output, secrets, temporaryRoot);
  return { bootstrap, child, origin, output };
}

async function stopChild(service, port) {
  const closePromise = waitForChildClose(service.child);
  if (service.child.exitCode === null) service.child.kill("SIGTERM");
  let timeout;
  const closed = await Promise.race([
    closePromise.then(() => true),
    new Promise((resolve) => { timeout = setTimeout(() => resolve(false), 5_000); }),
  ]).finally(() => clearTimeout(timeout));
  if (!closed) {
    service.child.kill("SIGKILL");
    await closePromise;
  }
  assertCompleteOutput(service.output, `process ${service.child.pid}`);
  await assertPortReusable(port);
  assert.equal(processExists(service.child.pid), false);
}

async function assertNoOverflow(page, selector = "html") {
  const dimensions = await page.locator(selector).evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  assert.ok(dimensions.scrollWidth <= dimensions.clientWidth, `${selector} overflowed: ${dimensions.scrollWidth} > ${dimensions.clientWidth}`);
}

// Below `lg` the shell's rail is a drawer, and this trigger is the only way
// into it: the projects, the thread list and the Library/Inbox/Status entries
// are all unreachable on a phone without it.
async function assertMobileNavigation(page) {
  const trigger = page.getByRole("button", { name: copy.sidebar.openNavigation });
  await trigger.waitFor();
  assert.equal(await page.locator(`aside[aria-label="${copy.sidebar.navigation}"]`).isVisible(), false, "the fixed rail must stay hidden at the mobile viewport");
  await trigger.click();
  const drawer = page.getByRole("dialog");
  await drawer.waitFor();
  await drawer.getByRole("navigation", { name: copy.sidebar.global }).waitFor();
  await page.keyboard.press("Escape");
  await drawer.waitFor({ state: "hidden" });
}

// `Runs` and `Outputs` are disclosures that open closed, and Radix unmounts a
// closed panel, so an acceptance that reads either must open it first -- which
// is also what makes the counts below mean anything. They are closed again
// afterwards because at a phone viewport an open panel takes the room the
// composer and its decision cards need.
async function setDisclosure(page, name, open) {
  const trigger = page.getByRole("button", { name, exact: true });
  await trigger.waitFor();
  const wanted = open ? "true" : "false";
  if ((await trigger.getAttribute("aria-expanded")) !== wanted) await trigger.click();
  await waitForValue(async () => await trigger.getAttribute("aria-expanded"), wanted);
}

async function openDisclosure(page, name) {
  await setDisclosure(page, name, true);
}

async function closeDisclosure(page, name) {
  await setDisclosure(page, name, false);
}

// The run rows carry the selection as `aria-pressed`, so this locator excludes
// the disclosure's own trigger without naming a class.
function runRows(page) {
  return page.locator(`[aria-label="${copy.thread.runHistory}"] button[aria-pressed]`);
}

function decisionCards(page) {
  return page.locator(`article[aria-label="${copy.decision.title}"]`);
}

// A decision the operator answered is gone from the composer, and it is the
// disappearance -- not a sentence about it -- that says the answer was recorded.
async function assertDecisionsResolved(page) {
  await waitForValue(async () => await decisionCards(page).count(), 0);
}

function collectResponses(page, records) {
  page.on("response", async (response) => {
    const record = {
      body: "",
      complete: false,
      sequence: records.length + 1,
      url: response.url(),
    };
    records.push(record);
    try {
      const body = await response.body();
      record.body = responseBodyForScan(body, record.url);
      record.complete = true;
    } catch (error) {
      record.error = error instanceof Error ? error.message : "response body could not be read";
      record.complete = true;
    }
  });
}

async function waitForResponse(records, afterSequence, predicate, timeout = 12_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const record = records.find((item) => item.sequence > afterSequence && item.complete && predicate(item));
    if (record) return record;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail("expected browser response was not observed");
}

async function captureSse(daemonPort, token, afterCursor) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12_000);
  const query = afterCursor ? `?after_cursor=${encodeURIComponent(afterCursor)}` : "";
  let body = "";
  try {
    const response = await fetch(`http://127.0.0.1:${daemonPort}/api/v1/events/stream${query}`, {
      headers: { "X-Cortex-Control-Token": token },
      signal: controller.signal,
    });
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type") ?? "", /^text\/event-stream/);
    assert.ok(response.body);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (!hasCompleteSseEvent(body, "event.redacted")) {
      const chunk = await reader.read();
      if (chunk.done) break;
      body += decoder.decode(chunk.value, { stream: true });
      assert.ok(body.length <= completeOutputLimit, "SSE body exceeded scan limit");
    }
    assert.equal(hasCompleteSseEvent(body, "event.redacted"), true);
    await reader.cancel();
    return body;
  } finally {
    clearTimeout(timeout);
    controller.abort();
  }
}

async function waitForValue(read, expected, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await read();
    if (value === expected) return value;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const value = await read();
  assert.equal(value, expected);
  return value;
}

// A body is read back out of the browser, so a response whose read is still in
// flight when the context goes away can never be scanned -- and the shell polls
// Control every 2.5s right up to the last moment, which is what made this
// acceptance fail intermittently on an unread poll response. Wait for the page
// to go quiet and for every started read to land, and only then close.
async function quiesceResponses(records, quiet = 3, timeout = 15_000) {
  const deadline = Date.now() + timeout;
  let observed = -1;
  let settled = 0;
  while (Date.now() < deadline) {
    const idle = records.length === observed && records.every((record) => record.complete);
    settled = idle ? settled + 1 : 0;
    observed = records.length;
    if (settled >= quiet) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.fail("browser responses never went quiet before the context was closed");
}

async function closeAndDrainResponses(context, records) {
  await quiesceResponses(records);
  await context.close();
  await new Promise((resolve) => setImmediate(resolve));
  await waitForValue(async () => records.every((record) => record.complete), true);
  assert.deepEqual(
    records.filter((record) => record.error).map((record) => `${record.url}: ${record.error}`),
    [],
    "every browser response body must be available for scanning",
  );
}

async function freshPage(browser, origin, records, blockedRequests, blockedWebSockets) {
  const context = await browser.newContext({
    hasTouch: true,
    isMobile: true,
    serviceWorkers: "block",
    viewport: { width: 390, height: 844 },
  });
  await context.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.origin === origin && requestUrl.protocol === "http:" && requestUrl.hostname === "127.0.0.1") {
      await route.continue();
      return;
    }
    blockedRequests.push(requestUrl.href);
    await route.abort("blockedbyclient");
  });
  const allowedWebSocketOrigin = new URL(origin);
  allowedWebSocketOrigin.protocol = "ws:";
  await context.routeWebSocket("**/*", (route) => {
    const requestUrl = new URL(route.url());
    if (requestUrl.origin === allowedWebSocketOrigin.origin && requestUrl.hostname === "127.0.0.1") {
      route.connectToServer();
      return;
    }
    blockedWebSockets.push(requestUrl.href);
    route.close();
  });
  const page = await context.newPage();
  collectResponses(page, records);
  await page.goto(origin, { waitUntil: "domcontentloaded" });
  // The shell's thread header carries the open thread's title as its `h1`.
  await page.getByRole("heading", { name: "Helios-14B memory plan" }).waitFor();
  return { context, page };
}

async function selectVersion(page, title, label) {
  const selector = page.getByRole("combobox", { name: `${title} version` });
  const value = await selector.locator("option").evaluateAll((options, wanted) => {
    const option = options.find((item) => item.textContent?.trim() === wanted);
    return option?.getAttribute("value") ?? null;
  }, label);
  assert.ok(value, `${title} ${label} is missing`);
  await selector.selectOption(value);
}

async function assertDocument(page, title, expected) {
  const tab = page.getByRole("tab", { name: title });
  if (await tab.getAttribute("aria-selected") !== "true") await tab.click();
  const document = page.getByRole("region", { name: `${title} document` });
  await document.getByRole("button", { name: "Source", exact: true }).click();
  await waitForValue(async () => await document.getByRole("region", { name: "Markdown source" }).textContent(), expected);
  await assertNoOverflow(page, `[aria-label="${title} document"]`);
  await document.getByRole("button", { name: "Preview", exact: true }).click();
  await document.locator(".aui-md h1").waitFor();
}

async function assertArtifactMetadata(page, expectedParents) {
  const details = page.locator(".artifact-reader details[data-artifact-metadata]");
  await details.locator("summary").click();
  await details.getByText("Version ID", { exact: true }).waitFor();
  const values = await page.locator(".artifact-metadata div").evaluateAll((rows) => Object.fromEntries(rows.map((row) => [
    row.querySelector("dt")?.textContent ?? "",
    row.querySelector("dd")?.textContent ?? "",
  ])));
  assert.equal(Object.keys(values).length, 6);
  assert.equal(values.Generator, "workflow-research 1");
  assert.equal(values.Tool, "workflow-writer 1");
  assert.equal(values["Paper sources"].split(", ").filter(Boolean).length, 2);
  if (expectedParents === "root") assert.equal(values.Parents, "Root version");
  else assert.notEqual(values.Parents, "Root version");
  await details.locator("summary").click();
}

async function browserState(page) {
  return await page.evaluate(async () => {
    const local = Object.entries(localStorage);
    const session = Object.entries(sessionStorage);
    const cachesState = [];
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      for (const request of await cache.keys()) {
        const response = await cache.match(request);
        cachesState.push({ body: await response?.text(), name, url: request.url });
      }
    }
    return { caches: cachesState, html: document.documentElement.outerHTML, local, session };
  });
}

async function main() {
  const artifactLeakProbes = runArtifactLeakProbes();
  const responseBoundaryProbes = runResponseBoundaryProbes();
  const sseFrameProbes = runSseFrameProbes();
  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "cortex-control-workflow-"));
  execution.root = temporaryRoot;
  const capability = randomBytes(32).toString("base64url");
  execution.secrets.push(capability);
  await writeFile(path.join(temporaryRoot, ".cortex-workflow-fixture-v1"), capability, { encoding: "ascii", flag: "wx", mode: 0o600 });
  const ports = execution.ports;
  while (ports.length < 3) {
    const port = await reservePort();
    if (!ports.includes(port)) ports.push(port);
  }
  const [daemonPort, webPort] = ports;
  const pids = execution.pids;
  const secrets = execution.secrets;
  const summaries = [];
  const blockedRequests = [];
  const blockedWebSockets = [];
  const responseRecords = [];
  const sseBodies = [];
  const browserStates = [];
  const processOutputs = [];
  let browser;
  let daemon;
  let web;
  let activePage;
  let activeContext;
  let websocketProbe;
  let failure;
  let networkGuardProbes = 0;
  try {
    await mkdir(path.join(temporaryRoot, "home"), { mode: 0o700 });
    const networkGuardSummary = await runFixture("assert-network-guard", temporaryRoot, capability, secrets, processOutputs);
    networkGuardProbes = networkGuardSummary.probes;
    assert.equal(networkGuardProbes, 12);
    summaries.push(networkGuardSummary);
    summaries.push(await runFixture("seed-g0", temporaryRoot, capability, secrets, processOutputs));
    summaries.push(await runFixture("assert-g0", temporaryRoot, capability, secrets, processOutputs));

    const launchOptions = process.env.CAPTURE_BROWSER_EXECUTABLE
      ? { executablePath: process.env.CAPTURE_BROWSER_EXECUTABLE, headless: true }
      : { channel: process.env.CAPTURE_BROWSER_CHANNEL ?? "msedge", headless: true };
    browser = await chromium.launch(launchOptions);

    daemon = await startDaemon(temporaryRoot, daemonPort, secrets, pids);
    web = await startWeb(temporaryRoot, daemonPort, webPort, daemon.token, 1, secrets, pids);
    processOutputs.push(daemon.output, web.output);
    ({ context: activeContext, page: activePage } = await freshPage(browser, web.origin, responseRecords, blockedRequests, blockedWebSockets));
    const websocketProbeUrl = `ws://127.0.0.1:${ports[2]}/probe`;
    websocketProbe = await startWebSocketProbe(ports[2]);
    const websocketOutcome = await activePage.evaluate((url) => new Promise((resolve) => {
      const socket = new WebSocket(url);
      const timeout = setTimeout(() => resolve("timeout"), 2_000);
      socket.addEventListener("open", () => {
        clearTimeout(timeout);
        socket.close();
        resolve("opened");
      }, { once: true });
      socket.addEventListener("close", () => {
        clearTimeout(timeout);
        resolve("closed");
      }, { once: true });
      socket.addEventListener("error", () => {
        clearTimeout(timeout);
        resolve("blocked");
      }, { once: true });
    }), websocketProbeUrl);
    assert.notEqual(websocketOutcome, "opened");
    await waitForValue(async () => blockedWebSockets.length, 1);
    assert.equal(websocketProbe.connections, 0, "blocked WebSocket reached its loopback listener");
    await stopWebSocketProbe(websocketProbe);
    websocketProbe = null;
    await openDisclosure(activePage, copy.thread.outputs);
    await activePage.locator(".research-workflow-details > summary").click();
    await activePage.getByText("arxiv:2606.04527", { exact: true }).first().waitFor();
    await activePage.getByText("arxiv:2607.07675", { exact: true }).first().waitFor();
    assert.equal(await activePage.locator(".source-gate").count(), 1);
    assert.equal(await activePage.locator(".bound-source").count(), 0, "G0 must have no selected Source");
    assert.equal(await activePage.locator(".lineage-node").count(), 0, "G0 must have no lineage node");
    assert.equal(await activePage.locator(".output-tabs").count(), 0, "G0 must have no artifact tab");
    assert.equal(await activePage.locator(".artifact-snapshot").count(), 0, "G0 must have no snapshot");
    assert.equal(await decisionCards(activePage).count(), 1, "G0 must raise exactly one decision");
    await assertNoOverflow(activePage);
    await assertMobileNavigation(activePage);
    await closeDisclosure(activePage, copy.thread.outputs);
    await decisionCards(activePage).getByRole("button", { name: "Keep both sources" }).click();
    await assertDecisionsResolved(activePage);
    browserStates.push(await browserState(activePage));
    await closeAndDrainResponses(activeContext, responseRecords);
    activeContext = activePage = null;
    await stopChild(web, webPort);
    await stopChild(daemon, daemonPort);
    web = daemon = null;

    daemon = await startDaemon(temporaryRoot, daemonPort, secrets, pids);
    web = await startWeb(temporaryRoot, daemonPort, webPort, daemon.token, 2, secrets, pids);
    processOutputs.push(daemon.output, web.output);
    ({ context: activeContext, page: activePage } = await freshPage(browser, web.origin, responseRecords, blockedRequests, blockedWebSockets));
    await openDisclosure(activePage, copy.thread.outputs);
    await activePage.locator(".research-workflow-details > summary").click();
    await activePage.locator(".source-gate-state").filter({ hasText: /^resolved$/ }).waitFor();
    assert.equal(await decisionCards(activePage).count(), 0, "an answered decision must not come back after a restart");
    assert.equal(await activePage.locator(".bound-source").count(), 1, "resolved Source choice must retain the existing binding before import");
    summaries.push(await runFixture("advance-source", temporaryRoot, capability, secrets, processOutputs));
    await activePage.reload({ waitUntil: "domcontentloaded" });
    const successor = decisionCards(activePage).getByRole("button", { name: "Create successor" });
    await successor.waitFor();
    await openDisclosure(activePage, copy.thread.outputs);
    await activePage.locator(".research-workflow-details > summary").click();
    assert.equal(await activePage.locator(".bound-source").count(), 2);
    await activePage.getByText("arxiv:2606.04527", { exact: true }).first().waitFor();
    await activePage.getByText("arxiv:2607.07675", { exact: true }).first().waitFor();
    await closeDisclosure(activePage, copy.thread.outputs);
    await successor.click();
    await assertDecisionsResolved(activePage);
    browserStates.push(await browserState(activePage));
    await closeAndDrainResponses(activeContext, responseRecords);
    activeContext = activePage = null;
    await stopChild(web, webPort);
    await stopChild(daemon, daemonPort);
    web = daemon = null;

    daemon = await startDaemon(temporaryRoot, daemonPort, secrets, pids);
    web = await startWeb(temporaryRoot, daemonPort, webPort, daemon.token, 3, secrets, pids);
    processOutputs.push(daemon.output, web.output);
    ({ context: activeContext, page: activePage } = await freshPage(browser, web.origin, responseRecords, blockedRequests, blockedWebSockets));
    await assertDecisionsResolved(activePage);
    summaries.push(await runFixture("advance-lineage", temporaryRoot, capability, secrets, processOutputs));
    const g1ReloadBoundary = responseRecords.length;
    await activePage.reload({ waitUntil: "domcontentloaded" });
    await openDisclosure(activePage, copy.thread.outputs);
    await activePage.locator(".research-workflow-details > summary").click();
    await activePage.getByRole("heading", { name: "Research artifacts" }).waitFor();
    // The run's terminal state is read where the operator reads it: the newest
    // row of the run list, selected, in the words the shell speaks.
    await openDisclosure(activePage, copy.thread.runs);
    const newestRun = runRows(activePage).first();
    await waitForValue(async () => (await newestRun.textContent())?.startsWith(`${runStateLabel("completed")} · `) ?? false, true);
    assert.equal(await newestRun.getAttribute("aria-pressed"), "true");
    await activePage.getByText("Dormant baseline").first().waitFor();
    await activePage.getByText("Graduated baseline").first().waitFor();
    await activePage.getByText("Helios-14B TTT successor").first().waitFor();
    assert.equal(await activePage.locator(".lineage-node").count(), 3);
    assert.equal(await activePage.locator(".lineage-link-list li").count(), 2);
    assert.equal(await activePage.locator(".research-stage").count(), 11);
    assert.equal(await activePage.locator(".research-stage.completed").count(), 11);
    assert.equal(await activePage.locator(".output-tabs [role=tab]").count(), 3);
    assert.equal(await activePage.locator(".artifact-snapshot").count(), 1);
    assert.equal(await activePage.locator(".artifact-snapshot li").count(), 3);
    await activePage.getByText("Helios immutable snapshot").waitFor();
    await assertDocument(activePage, "Living Brief", livingV2);
    await assertArtifactMetadata(activePage, "parent");
    await selectVersion(activePage, "Living Brief", "v1");
    await assertDocument(activePage, "Living Brief", livingV1);
    await assertArtifactMetadata(activePage, "root");
    await assertNoOverflow(activePage, '[aria-label="Living Brief document"]');
    await selectVersion(activePage, "Living Brief", "v2");
    await assertDocument(activePage, "Living Brief", livingV2);
    await assertArtifactMetadata(activePage, "parent");
    await assertDocument(activePage, "Evidence Matrix", evidence);
    await assertArtifactMetadata(activePage, "root");
    await assertDocument(activePage, "Training Plan", training);
    await assertArtifactMetadata(activePage, "root");
    await assertNoOverflow(activePage);
    await assertMobileNavigation(activePage);
    const g1Summary = await runFixture("assert-g1", temporaryRoot, capability, secrets, processOutputs);
    summaries.push(g1Summary);

    const beforeReplay = await activePage.locator(".bound-source, .lineage-node, .lineage-link-list li, .artifact-reader-toolbar, .artifact-snapshot").count();
    const cursorRecord = await waitForResponse(
      responseRecords,
      g1ReloadBoundary,
      (record) => new URL(record.url).pathname === "/api/cortex/events",
    );
    const cursor = JSON.parse(cursorRecord.body).next_cursor;
    assert.equal(typeof cursor, "string");
    const ssePromise = captureSse(daemonPort, daemon.token, cursor);
    summaries.push(await runFixture("emit-redacted", temporaryRoot, capability, secrets, processOutputs));
    sseBodies.push(await ssePromise);
    const redactedRecord = await waitForResponse(
      responseRecords,
      cursorRecord.sequence,
      (record) => new URL(record.url).pathname === "/api/cortex/events" && record.body.includes('"event.redacted"'),
    );
    await waitForResponse(
      responseRecords,
      redactedRecord.sequence,
      (record) => /\/api\/cortex\/runs\/[A-Za-z0-9_.:-]+\/research-workflow$/.test(new URL(record.url).pathname),
    );
    // The two waits above are the arrival proof: the browser read the redacted
    // event and re-read the run's research projection because of it. What the
    // shell owes the operator is the opposite of the deleted event log -- the
    // redacted envelope and its payload are Control's own vocabulary, and none
    // of it may reach the screen (spec section 7).
    const renderedAfterRedaction = await activePage.content();
    for (const wireToken of ["event.redacted", "workflow.fixture.invalidated", "acceptance_replay_probe"]) {
      assert.ok(!renderedAfterRedaction.includes(wireToken), `the shell rendered the wire token ${wireToken}`);
    }

    const replaySummary = await runFixture("replay-completed", temporaryRoot, capability, secrets, processOutputs, ["--count", "2"]);
    summaries.push(replaySummary);
    assert.equal(await activePage.locator(".bound-source, .lineage-node, .lineage-link-list li, .artifact-reader-toolbar, .artifact-snapshot").count(), beforeReplay);
    for (const key of ["bindings", "effects", "artifacts", "artifact_versions", "lineage_nodes", "lineage_links", "snapshots", "snapshot_members"]) {
      assert.equal(replaySummary.manifest[key], g1Summary.manifest[key], `${key} changed during completed replay`);
    }
    assert.equal(replaySummary.manifest.events, g1Summary.manifest.events + 1);

    const state = await browserState(activePage);
    browserStates.push(state);
    const serviceWorker = await activePage.evaluate(async () => await (await fetch("/sw.js")).text());
    await closeAndDrainResponses(activeContext, responseRecords);
    activeContext = activePage = null;
    await browser.close();
    browser = null;
    await stopChild(web, webPort);
    web = null;
    await stopChild(daemon, daemonPort);
    daemon = null;

    const scans = [serviceWorker, ...sseBodies];
    for (const browserSnapshot of browserStates) {
      scans.push(browserSnapshot.html, JSON.stringify(browserSnapshot.local), JSON.stringify(browserSnapshot.session), JSON.stringify(browserSnapshot.caches));
    }
    for (const record of responseRecords) scans.push(record.url, record.body);
    for (const summary of summaries) scans.push(JSON.stringify(summary));
    for (const output of processOutputs) {
      assertCompleteOutput(output, "captured process");
      scans.push(output.full);
    }
    const contentResponses = responseRecords.filter((record) => isArtifactContentPathname(new URL(record.url).pathname));
    const artifactLeakChannels = {
      sse: sseBodies,
      fixture_stdout_stderr: processOutputs.filter((output) => output.description.startsWith("fixture:")).map((output) => output.full),
      daemon_stdout_stderr_shutdown: processOutputs.filter((output) => output.description === "daemon").map((output) => output.full),
      web_stdout_stderr_shutdown: processOutputs.filter((output) => output.description.startsWith("web:")).map((output) => output.full),
      fixture_summaries: summaries.map((summary) => JSON.stringify(summary)),
      service_worker_source: [serviceWorker],
      cache_storage: browserStates.flatMap((snapshot) => snapshot.caches.map((entry) => entry.body ?? "")),
    };
    const combined = scans.join("\n");
    assert.deepEqual(blockedRequests, [], "browser attempted a request outside its loopback origin");
    assert.equal(new Set(secrets).size, secrets.length, "every restart must rotate its credentials");
    assert.ok(!combined.includes(temporaryRoot));
    for (const secret of secrets) assert.ok(!combined.includes(secret));
    assert.doesNotMatch(combined, /(?:engine_ref|provider_payload|<thinking>|hidden reasoning|internal reasoning)/i);
    const responseContents = contentResponses.map((record) => JSON.parse(record.body).content);
    for (const body of artifactBodies) {
      assert.ok(responseContents.includes(body), "exact artifact content response was not observed");
    }
    assertArtifactBodyIsolation(artifactBodies, artifactLeakChannels);
    for (const record of responseRecords.filter((item) => !contentResponses.includes(item))) {
      assertArtifactBodyIsolation(artifactBodies, {
        [`non_content_response:${new URL(record.url).pathname}`]: [record.body],
      });
    }
    assert.ok(state.caches.every((entry) => !entry.url.includes("/api/")));
    assert.deepEqual(blockedWebSockets, [`ws://127.0.0.1:${ports[2]}/probe`]);
  } catch (error) {
    failure = error;
  } finally {
    const cleanupErrors = [];
    const cleanup = async (operation) => {
      try {
        await operation();
      } catch (error) {
        cleanupErrors.push(error);
      }
    };
    if (activeContext) await cleanup(() => activeContext.close());
    if (browser) await cleanup(() => browser.close());
    if (websocketProbe) await cleanup(() => stopWebSocketProbe(websocketProbe));
    if (web) await cleanup(() => stopChild(web, webPort));
    if (daemon) await cleanup(() => stopChild(daemon, daemonPort));
    for (const port of ports) await cleanup(() => assertPortReusable(port));
    for (const pid of pids) {
      await cleanup(async () => assert.equal(processExists(pid), false, `PID ${pid} survived cleanup`));
    }
    await cleanup(() => rm(temporaryRoot, { recursive: true }));
    if (cleanupErrors.length) {
      if (failure) cleanupErrors.unshift(failure);
      throw new AggregateError(cleanupErrors, "workflow acceptance cleanup failed");
    }
  }
  if (failure) throw failure;
  process.stdout.write(`${JSON.stringify({ artifact_leak_probes: artifactLeakProbes, browser_websocket_guard_probes: blockedWebSockets.length, network_guard_probes: networkGuardProbes, pids, ports, response_boundary_probes: responseBoundaryProbes, sse_frame_probes: sseFrameProbes, status: "ok" })}\n`);
}

await main().catch((error) => {
  const message = error instanceof Error ? error.message : "workflow acceptance failed";
  process.stderr.write(`${sanitize(message, execution.secrets, execution.root)}\n`);
  process.stdout.write(`${JSON.stringify({ pids: execution.pids, ports: execution.ports, status: "error" })}\n`);
  process.exitCode = 1;
});
