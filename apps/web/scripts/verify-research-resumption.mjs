// Real-backend browser acceptance for the Research catalog and dossier (R1c).
//
// One temporary, provider-less instance of the actual product: the real
// `cortexd` ControlAPI over its own `control.db`, the real query-only
// `ResearchCatalog` over a SYNTHETIC legacy Research database, the real
// explicit `ResearchDocumentAdopter`, the built Web payload behind the loopback
// Node adapter, and a real browser driving it. The harness patterns (reserved
// ports, capability-marked root, bounded output collectors, loopback-only
// routing, quiesced response scanning, SIGTERM/SIGKILL cleanup with a port and
// PID check) are `verify-control-workflow.mjs`'s; what is proven here is
// different.
//
// Nothing here reaches a provider, the network, a deployment host or Telegram, and no
// real research record or document is read.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(webRoot, "../..");
const fixtureScript = path.join(webRoot, "scripts/research-resumption-fixture.py");
const python = process.env.CORTEX_TEST_PYTHON ?? path.join(repositoryRoot, ".venv/bin/python");
const outputLimit = 8 * 1024;
const completeOutputLimit = 2_000_000;
const readinessTimeout = 20_000;

// What the operator reads, spelled here rather than imported from the shell:
// an acceptance that learned its expectations from the code under test would
// agree with that code however it changed.
const text = {
  blocked: "This item cannot be continued yet.",
  details: "Details",
  documentRedacted: "Some private details are omitted. What you see here is what gets copied.",
  documentUnavailable: "That document could not be read. It may not have been adopted yet.",
  dossierUnreadable: "That research item could not be read.",
  empty: "Nothing of this kind is here.",
  explorations: "Explorations",
  global: "Global",
  ideas: "Ideas",
  kinds: "Research kinds",
  list: "Research items",
  loading: "Reading your research…",
  noDocuments: "No document is registered for this item yet.",
  open: "Open research conversation",
  projects: "Projects",
  research: "Research",
};

// The same synthetic bytes the fixture writes. Duplicated deliberately, as the
// workflow acceptance duplicates its artifact bodies: the verifier asserts what
// the operator reads, so it must not learn the answer from the process it is
// driving.
const PRIVATE_LINE = "Working copy: /Users/synthetic-operator/agent-research/bounded-memory.md";
const IDEA_DOCUMENT = `# Bounded memory dossier

The controller keeps a bounded state, so the retained window is $w_t = \\sum_{i=1}^{k} m_i$.

${PRIVATE_LINE}

- Round 3 stopped on thin evidence.
- The successor has to measure drift before it scales the window.
`;
const PROJECT_DOCUMENT = `# Bounded memory project dossier

The graduated project carries the same measurement plan, with $\\alpha = 0.5$ as
the retention floor.

1. Freeze the encoder and train the projector alone.
2. Compare recurrent memory against a frozen cache.
`;
// What the API's line redaction leaves of the idea's dossier: the one private
// line is replaced whole, and every other line survives byte for byte.
const IDEA_PROJECTION = IDEA_DOCUMENT.replace(PRIVATE_LINE, "[redacted]");

const IDEA_TITLE = "synthetic-bounded-memory-idea";
const EXPLORATION_TITLE = "synthetic-live-exploration";
const PROJECT_TITLE = "synthetic-bounded-memory-project";
// Adoption titles a document by its file, not by the item that registered it.
const IDEA_DOCUMENT_LABEL = `${IDEA_TITLE} · version 1`;
const PROJECT_DOCUMENT_LABEL = "dossier · version 1";
// A well-formed catalog identity that names no record: what a link to research
// this Cortex does not hold looks like.
const ABSENT_ITEM_ID = `ri_${"0".repeat(32)}`;

const execution = { pids: [], ports: [], root: null, secrets: [] };

function outputCollector(description) {
  return { description, full: "", overflow: false, tail: "" };
}

function boundedAppend(target, chunk) {
  const chunkText = String(chunk);
  target.tail = `${target.tail}${chunkText}`.slice(-outputLimit);
  if (target.overflow) return;
  const remaining = completeOutputLimit - target.full.length;
  target.full += chunkText.slice(0, Math.max(0, remaining));
  target.overflow = chunkText.length > remaining;
}

function assertCompleteOutput(output, description) {
  assert.equal(output.overflow, false, `${description} exceeded the complete output scan limit`);
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
      CORTEX_RESEARCH_FIXTURE_CAPABILITY: capability,
      CORTEX_RESEARCH_FIXTURE_ROOT: temporaryRoot,
      PYTHONDONTWRITEBYTECODE: "1",
      // This checkout, and only this one: the fixture must exercise the code
      // under test rather than whatever else is installed on the machine.
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
  assert.ok(!serialized.includes(temporaryRoot), `${command} summary carried the temporary root`);
  assert.ok(!serialized.includes(PRIVATE_LINE), `${command} summary carried the private line`);
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
  throw new Error(`${description} readiness exceeded ${readinessTimeout}ms: ${sanitize(output.tail, secrets, temporaryRoot)}`);
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

async function startWeb(temporaryRoot, daemonPort, webPort, token, secrets, pids) {
  const payload = path.join(temporaryRoot, "payload");
  await cp(path.join(webRoot, "dist"), payload, { recursive: true });
  await mkdir(path.join(payload, "server"), { recursive: true });
  for (const relative of ["server/access-identity-bound.mjs", "server/node-adapter.mjs"]) {
    await cp(path.join(webRoot, relative), path.join(payload, relative));
  }
  const bootstrap = randomBytes(32).toString("base64url");
  assert.equal(bootstrap.length, 43);
  secrets.push(bootstrap);
  const output = outputCollector("web");
  const child = spawn(process.execPath, [path.join(payload, "server/node-adapter.mjs")], {
    cwd: payload,
    env: {
      HOME: path.join(temporaryRoot, "home"),
      PATH: process.env.PATH,
      CORTEX_ACCESS_BOOTSTRAP_TOKEN: bootstrap,
      CORTEX_CONTROL_API_URL: `http://127.0.0.1:${daemonPort}`,
      CORTEX_CONTROL_TOKEN: token,
      CORTEX_LOCAL_ACCESS_ENABLED: "1",
      CORTEX_WEB_BUILD_ID: "research-resumption",
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

function collectResponses(page, records) {
  page.on("response", async (response) => {
    const record = {
      body: "",
      complete: false,
      method: response.request().method(),
      sequence: records.length + 1,
      url: response.url(),
    };
    records.push(record);
    try {
      const body = await response.body();
      assert.ok(body.length <= completeOutputLimit, `response body exceeded scan limit: ${record.url}`);
      record.body = body.toString("utf8");
    } catch (error) {
      record.error = error instanceof Error ? error.message : "response body could not be read";
    }
    record.complete = true;
  });
}

async function waitForValue(read, expected, timeout = 15_000) {
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

// The shell polls Control while the page is open, so a body whose read is still
// in flight when the context goes away could never be scanned. Wait for the page
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

// One desktop context, confined to the Web origin: the catalog and the dossier
// are two columns at `lg`, which is where an operator reads them.
async function openContext(browser, origin, records, blockedRequests) {
  const context = await browser.newContext({
    serviceWorkers: "block",
    viewport: { width: 1280, height: 900 },
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
  const page = await context.newPage();
  collectResponses(page, records);
  return { context, page };
}

function researchSection(page) {
  return page.getByRole("region", { name: text.research, exact: true });
}

function catalogRows(page) {
  return page.locator(`nav[aria-label="${text.list}"] button`);
}

function openButton(page) {
  return researchSection(page).getByRole("button", { name: text.open, exact: true });
}

function documentSection(page) {
  return researchSection(page).locator('section[aria-label="Documents"]');
}

function threadFromUrl(page) {
  return new URL(page.url()).searchParams.get("thread");
}

async function selectKind(page, name) {
  const kinds = page.locator(`nav[aria-label="${text.kinds}"] button`);
  await kinds.filter({ hasText: name }).first().click();
  await waitForValue(
    async () => (await page.locator(`nav[aria-label="${text.kinds}"] button[aria-current="true"]`).textContent())?.trim(),
    name,
  );
}

async function catalogTitles(page) {
  return await catalogRows(page).evaluateAll((rows) => rows.map((row) => row.querySelector("span")?.textContent ?? ""));
}

// The list is read from the daemon, so what is on screen the instant a kind is
// picked may still be the previous answer. Wait for the page to settle, then
// assert -- a list that never becomes the expected one fails here with the one
// it did show.
async function expectCatalog(page, expected, description) {
  await waitForValue(async () => await researchSection(page).getByText(text.loading).count(), 0);
  await waitForValue(async () => JSON.stringify(await catalogTitles(page)), JSON.stringify(expected));
  assert.deepEqual(await catalogTitles(page), expected, description);
}

async function openResearchItem(page, origin, itemId, title) {
  await page.goto(`${origin}/?view=research&item=${itemId}`, { waitUntil: "domcontentloaded" });
  await researchSection(page).getByRole("heading", { level: 3, name: title }).waitFor();
}

// The dossier's own header: its kind, its status, its rounds, why it stopped
// and -- when it cannot be continued -- the backend's reason. Scoped to the
// header so the status filter's own option list cannot answer for it.
async function dossierHeaderText(page, title) {
  const header = researchSection(page).locator("header")
    .filter({ has: page.getByRole("heading", { level: 3, name: title }) });
  await header.waitFor();
  return (await header.textContent()) ?? "";
}

// Preview renders the document's Markdown with KaTeX; Source shows the exact
// bytes the API delivered. Both are the shared reader's own controls.
async function assertDocumentModes(page, expected) {
  const region = documentSection(page);
  await region.getByRole("button", { name: "Preview", exact: true }).click();
  await region.locator(".aui-md h1").first().waitFor();
  const math = region.locator(".katex");
  await math.first().waitFor();
  const rendered = await math.count();
  assert.ok(rendered >= 1, "the dossier preview rendered no KaTeX math");
  await region.getByRole("button", { name: "Source", exact: true }).click();
  await waitForValue(
    async () => await region.getByRole("region", { name: "Markdown source" }).textContent(),
    expected,
  );
  await region.getByRole("button", { name: "Preview", exact: true }).click();
  await region.getByRole("region", { name: "Rendered Markdown" }).waitFor();
  return rendered;
}

// The item's own disclosure, which is where the linked conversation is named.
async function itemDetails(page) {
  const group = researchSection(page).getByRole("group", { name: text.details });
  if (await group.count() === 0) {
    await researchSection(page).locator("button").filter({ hasText: text.details }).first().click();
  }
  await group.waitFor();
  return (await group.textContent()) ?? "";
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
  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "cortex-research-resumption-"));
  execution.root = temporaryRoot;
  const capability = randomBytes(32).toString("base64url");
  execution.secrets.push(capability);
  await writeFile(path.join(temporaryRoot, ".cortex-research-fixture-v1"), capability, { encoding: "ascii", flag: "wx", mode: 0o600 });
  const ports = execution.ports;
  while (ports.length < 2) {
    const port = await reservePort();
    if (!ports.includes(port)) ports.push(port);
  }
  const [daemonPort, webPort] = ports;
  const pids = execution.pids;
  const secrets = execution.secrets;
  const summaries = [];
  const blockedRequests = [];
  const records = [];
  const browserStates = [];
  const processOutputs = [];
  let browser;
  let daemon;
  let web;
  let context;
  let page;
  let failure;
  let networkGuardProbes = 0;
  let renderedMath = 0;
  try {
    await mkdir(path.join(temporaryRoot, "home"), { mode: 0o700 });
    const guard = await runFixture("assert-network-guard", temporaryRoot, capability, secrets, processOutputs);
    networkGuardProbes = guard.probes;
    assert.equal(networkGuardProbes, 12);
    summaries.push(guard);
    const seeded = await runFixture("seed", temporaryRoot, capability, secrets, processOutputs);
    summaries.push(seeded);
    const items = seeded.items;
    assert.deepEqual(Object.keys(items).sort(), ["exploration", "idea", "project"]);
    summaries.push(await runFixture("assert-catalog", temporaryRoot, capability, secrets, processOutputs));
    summaries.push(await runFixture("assert-unadopted", temporaryRoot, capability, secrets, processOutputs));

    browser = await chromium.launch(process.env.CAPTURE_BROWSER_EXECUTABLE
      ? { executablePath: process.env.CAPTURE_BROWSER_EXECUTABLE, headless: true }
      : { channel: process.env.CAPTURE_BROWSER_CHANNEL ?? "msedge", headless: true });

    daemon = await startDaemon(temporaryRoot, daemonPort, secrets, pids);
    web = await startWeb(temporaryRoot, daemonPort, webPort, daemon.token, secrets, pids);
    processOutputs.push(daemon.output, web.output);
    ({ context, page } = await openContext(browser, web.origin, records, blockedRequests));

    // 1. The catalog, reached the way an operator reaches it: three kinds, each
    //    holding exactly its own legacy record, read through the real daemon.
    await page.goto(`${web.origin}/`, { waitUntil: "domcontentloaded" });
    await page.locator(`nav[aria-label="${text.global}"]`).getByRole("button", { name: text.research }).click();
    await researchSection(page).waitFor();
    await expectCatalog(page, [IDEA_TITLE], "Ideas must list the idea alone");
    await selectKind(page, text.explorations);
    await expectCatalog(page, [EXPLORATION_TITLE], "Explorations must list the exploration alone");
    await selectKind(page, text.projects);
    await expectCatalog(page, [PROJECT_TITLE], "Projects must list the project alone");

    // 2. Before adoption the dossier says the backend's own reason, offers no
    //    document and no continuation. `assert-unadopted` above proves that is
    //    the real Control state and not a UI guess.
    await openResearchItem(page, web.origin, items.idea, IDEA_TITLE);
    const dormant = await dossierHeaderText(page, IDEA_TITLE);
    for (const expected of ["Idea", "Dormant", "Rounds 3", "Stopped because the operator stopped every running exploration"]) {
      assert.ok(dormant.includes(expected), `the dossier header does not say ${JSON.stringify(expected)}`);
    }
    await researchSection(page).getByText(text.noDocuments).waitFor();
    await researchSection(page).getByText("documents_not_adopted").waitFor();
    await researchSection(page).getByText(text.blocked).waitFor();
    assert.equal(await openButton(page).isDisabled(), true, "an unadopted item must not offer a continuation");
    browserStates.push(await browserState(page));

    // 3. Explicit adoption, then the same dossier reads the real documents.
    summaries.push(await runFixture("adopt", temporaryRoot, capability, secrets, processOutputs));
    const adoptionBoundary = records.length;
    await openResearchItem(page, web.origin, items.idea, IDEA_TITLE);
    await documentSection(page).getByText(IDEA_DOCUMENT_LABEL).first().waitFor();
    renderedMath += await assertDocumentModes(page, IDEA_PROJECTION);
    // The line the API withheld is named on screen, and the withheld bytes are
    // nowhere in the page.
    await researchSection(page).getByText(text.documentRedacted).waitFor();
    assert.ok(!(await page.content()).includes(PRIVATE_LINE), "the withheld line reached the page");
    assert.equal(await openButton(page).isDisabled(), false, "an adopted item must offer its continuation");
    const contentRecord = records.find((record) => record.sequence > adoptionBoundary
      && /\/api\/cortex\/research-documents\/rdv_[a-f0-9]{32}\/content$/.test(new URL(record.url).pathname));
    assert.ok(contentRecord, "the dossier did not read a document through the proxy");
    const delivered = JSON.parse(contentRecord.body);
    assert.equal(delivered.content, IDEA_PROJECTION);
    assert.equal(delivered.redacted, true);
    assert.equal(delivered.byte_length, Buffer.byteLength(IDEA_PROJECTION, "utf8"));
    assert.equal(delivered.retained_byte_length, Buffer.byteLength(IDEA_DOCUMENT, "utf8"));

    // 4. The project reads the same way -- through an operator-supplied document
    //    map, because a project registers no path -- and carries no redaction
    //    notice, because nothing of it was withheld.
    await openResearchItem(page, web.origin, items.project, PROJECT_TITLE);
    await documentSection(page).getByText(PROJECT_DOCUMENT_LABEL).first().waitFor();
    renderedMath += await assertDocumentModes(page, PROJECT_DOCUMENT);
    assert.equal(await researchSection(page).getByText(text.documentRedacted).count(), 0);

    // 5. The project's conversation: created here, and nothing runs in it.
    await openButton(page).click();
    await waitForValue(async () => threadFromUrl(page) !== null, true);
    const projectThread = threadFromUrl(page);
    assert.ok(projectThread);
    summaries.push(await runFixture("assert-threads", temporaryRoot, capability, secrets, processOutputs, ["--expected", "1"]));

    // 6. A URL that names the item opens the same dossier again, and opening the
    //    conversation a second time reuses the one that already exists.
    await openResearchItem(page, web.origin, items.project, PROJECT_TITLE);
    await documentSection(page).getByRole("region", { name: "Rendered Markdown" }).waitFor();
    assert.equal(await openButton(page).isDisabled(), false);
    await openButton(page).click();
    await waitForValue(async () => threadFromUrl(page), projectThread);
    summaries.push(await runFixture("assert-threads", temporaryRoot, capability, secrets, processOutputs, ["--expected", "1"]));

    // 7. The idea's own conversation, so that an archived one can be shown below
    //    without taking the project's away.
    await openResearchItem(page, web.origin, items.idea, IDEA_TITLE);
    await openButton(page).click();
    await waitForValue(async () => {
      const thread = threadFromUrl(page);
      return thread !== null && thread !== projectThread;
    }, true);
    const ideaThread = threadFromUrl(page);
    summaries.push(await runFixture("assert-threads", temporaryRoot, capability, secrets, processOutputs, ["--expected", "2"]));
    await openResearchItem(page, web.origin, items.idea, IDEA_TITLE);
    assert.ok((await itemDetails(page)).includes(ideaThread), "the dossier does not name the conversation it opened");

    // 8. Archived: the conversation the item had is no longer offered for reuse,
    //    and the store refuses a selection on it.
    summaries.push(await runFixture("archive-thread", temporaryRoot, capability, secrets, processOutputs));
    await openResearchItem(page, web.origin, items.idea, IDEA_TITLE);
    const archivedDetails = await itemDetails(page);
    assert.ok(!archivedDetails.includes(ideaThread), "an archived conversation is still offered for reuse");
    assert.ok(archivedDetails.includes("—"), "the dossier does not say the item has no conversation");

    // 9. Revoked: the adopted document root is disabled, so the dossier says
    //    what the backend says, the document is unreadable and no continuation
    //    is offered.
    summaries.push(await runFixture("revoke-root", temporaryRoot, capability, secrets, processOutputs));
    await openResearchItem(page, web.origin, items.idea, IDEA_TITLE);
    await researchSection(page).getByText("document_root_unavailable").waitFor();
    await researchSection(page).getByText(text.documentUnavailable).waitFor();
    assert.equal(await openButton(page).isDisabled(), true, "a revoked document root must block the continuation");
    summaries.push(await runFixture("restore-root", temporaryRoot, capability, secrets, processOutputs));

    // 10. A link to research this Cortex does not hold says so, and shows no
    //     dossier at all -- while the catalog beside it still lists what it has.
    await page.goto(`${web.origin}/?view=research&item=${ABSENT_ITEM_ID}`, { waitUntil: "domcontentloaded" });
    await researchSection(page).getByText(text.dossierUnreadable).waitFor();
    assert.equal(await researchSection(page).getByRole("heading", { level: 3 }).count(), 0);
    await expectCatalog(page, [IDEA_TITLE], "the catalog must still list what it holds");

    browserStates.push(await browserState(page));
    await closeAndDrainResponses(context, records);
    context = page = null;
    await browser.close();
    browser = null;
    await stopChild(web, webPort);
    web = null;
    await stopChild(daemon, daemonPort);
    daemon = null;

    // Opening a conversation is not starting one: no run was ever requested,
    // and the fixture's own assertions say the store holds no run or message on
    // either linked thread.
    assert.deepEqual(
      records.filter((record) => record.method === "POST" && /\/runs$/.test(new URL(record.url).pathname)).map((record) => record.url),
      [],
      "a model run was started",
    );

    const scans = [];
    for (const snapshot of browserStates) {
      scans.push(snapshot.html, JSON.stringify(snapshot.local), JSON.stringify(snapshot.session), JSON.stringify(snapshot.caches));
    }
    for (const record of records) scans.push(record.url, record.body);
    for (const summary of summaries) scans.push(JSON.stringify(summary));
    for (const output of processOutputs) {
      assertCompleteOutput(output, "captured process");
      scans.push(output.full);
    }
    const combined = scans.join("\n");
    assert.deepEqual(blockedRequests, [], "browser attempted a request outside its loopback origin");
    assert.equal(new Set(secrets).size, secrets.length, "every credential must be distinct");
    assert.ok(!combined.includes(temporaryRoot), "the temporary root reached a scanned channel");
    assert.ok(!combined.includes(PRIVATE_LINE), "the withheld document line reached a scanned channel");
    for (const secret of secrets) assert.ok(!combined.includes(secret), "a credential reached a scanned channel");
    assert.doesNotMatch(combined, /(?:engine_ref|provider_payload|<thinking>|hidden reasoning|internal reasoning)/i);
  } catch (error) {
    failure = error;
    if (page) {
      try {
        failure = new Error(`${failure.message}\n--- page ---\n${(await page.content()).slice(0, 4_000)}`);
      } catch {
        // The page is already gone; the original failure is what matters.
      }
    }
  } finally {
    const cleanupErrors = [];
    const cleanup = async (operation) => {
      try {
        await operation();
      } catch (error) {
        cleanupErrors.push(error);
      }
    };
    if (context) await cleanup(() => context.close());
    if (browser) await cleanup(() => browser.close());
    if (web) await cleanup(() => stopChild(web, webPort));
    if (daemon) await cleanup(() => stopChild(daemon, daemonPort));
    for (const port of ports) await cleanup(() => assertPortReusable(port));
    for (const pid of pids) {
      await cleanup(async () => assert.equal(processExists(pid), false, `PID ${pid} survived cleanup`));
    }
    await cleanup(() => rm(temporaryRoot, { recursive: true }));
    if (cleanupErrors.length) {
      if (failure) cleanupErrors.unshift(failure);
      throw new AggregateError(cleanupErrors, "research resumption acceptance cleanup failed");
    }
  }
  if (failure) throw failure;
  process.stdout.write(`${JSON.stringify({
    fixture_commands: summaries.length,
    network_guard_probes: networkGuardProbes,
    pids,
    ports,
    rendered_math: renderedMath,
    responses: records.length,
    status: "ok",
  })}\n`);
}

await main().catch((error) => {
  const message = error instanceof Error ? (error.stack ?? error.message) : "research resumption acceptance failed";
  process.stderr.write(`${sanitize(message, execution.secrets, execution.root)}\n`);
  process.stdout.write(`${JSON.stringify({ pids: execution.pids, ports: execution.ports, status: "error" })}\n`);
  process.exitCode = 1;
});
