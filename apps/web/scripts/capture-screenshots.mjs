import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { sha256, validateCaptureSpec } from "./screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const screenshotRoot = path.join(webRoot, "artifacts/screenshots");
const specPath = path.join(screenshotRoot, "capture-spec.json");
const spec = JSON.parse(await readFile(specPath, "utf8"));
const entries = validateCaptureSpec(spec);

async function assertPortAvailable() {
  await new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", (error) => reject(new Error(`TCP 3000 must be free: ${error.message}`)));
    probe.listen(3000, "127.0.0.1", () => probe.close(resolve));
  });
}

async function waitForServer(child, output) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Loopback dev server exited early:\n${output.value}`);
    }
    try {
      const response = await fetch(spec.base_origin);
      if (response.ok) return;
    } catch {
      // The loopback server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Loopback dev server did not become ready:\n${output.value}`);
}

async function stopServer(child) {
  if (child.exitCode !== null) return;
  process.kill(-child.pid, "SIGTERM");
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, 5_000)),
  ]);
  if (child.exitCode === null) process.kill(-child.pid, "SIGKILL");
}

async function assertScenarioDom(page, expected) {
  assert.equal(page.url(), expected.url);
  for (const contract of expected.expected_dom) {
    const locator = page.locator(contract.selector);
    await locator.first().waitFor({ state: "visible" });
    assert.equal(await locator.count(), contract.count, `${expected.scenario}: ${contract.selector}`);
    const texts = await locator.allTextContents();
    if (contract.exact_text) {
      assert.ok(
        texts.every((text) => text.trim() === contract.exact_text),
        `${expected.scenario}: ${contract.selector} exact text drifted`,
      );
    } else {
      assert.ok(
        texts.every((text) => text.includes(contract.text)),
        `${expected.scenario}: ${contract.selector} text drifted`,
      );
    }
  }
  for (const selector of expected.forbidden_dom) {
    assert.equal(await page.locator(selector).count(), 0, `${expected.scenario}: ${selector} must be absent`);
  }
}

await assertPortAvailable();
const output = { value: "" };
const server = spawn(
  process.execPath,
  [path.join(webRoot, "node_modules/next/dist/bin/next"), "dev", "-H", "127.0.0.1", "-p", "3000"],
  {
    cwd: webRoot,
    detached: true,
    env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  },
);
for (const stream of [server.stdout, server.stderr]) {
  stream.on("data", (chunk) => {
    output.value = `${output.value}${chunk}`.slice(-8_000);
  });
}

let browser;
try {
  await waitForServer(server, output);
  const launchOptions = process.env.CAPTURE_BROWSER_EXECUTABLE
    ? { executablePath: process.env.CAPTURE_BROWSER_EXECUTABLE, headless: true }
    : { channel: process.env.CAPTURE_BROWSER_CHANNEL ?? "msedge", headless: true };
  browser = await chromium.launch(launchOptions);
  for (const [filename, expected] of entries) {
    const context = await browser.newContext({
      viewport: spec.css_viewport,
      deviceScaleFactor: spec.device_scale_factor,
    });
    const page = await context.newPage();
    try {
      await page.goto(expected.url, { waitUntil: "networkidle" });
      await page.evaluate(() => document.fonts.ready);
      await assertScenarioDom(page, expected);
      const root = page.locator(`[data-capture-scenario="${expected.scenario}"]`);
      const bounds = await root.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return { width: rect.width, height: rect.height };
      });
      assert.equal(bounds.width, expected.clip_css.width);
      assert.ok(Math.ceil(bounds.height) >= expected.clip_css.height);
      const buffer = await page.screenshot({
        type: "png",
        path: path.join(screenshotRoot, filename),
        clip: expected.clip_css,
        animations: "disabled",
      });
      expected.sha256 = sha256(buffer);
      console.log(`${expected.scenario}: DOM asserted in a fresh context; captured ${filename}`);
    } finally {
      await page.close();
      await context.close();
    }
  }
  await writeFile(specPath, `${JSON.stringify(spec, null, 2)}\n`);
} finally {
  if (browser) await browser.close();
  await stopServer(server);
}

console.log("Cold-context scenario screenshots captured; loopback server stopped.");
