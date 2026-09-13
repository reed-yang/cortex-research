import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { MOCK_CONTROL_TOKEN, startMockControlServer } from "./mock-control-server.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const port = 3007;
const mockPort = 8798;
const origin = `http://127.0.0.1:${port}`;
const screenshotRoot = path.join(webRoot, "artifacts/research-composer");

async function assertPortFree(value) {
  const probe = createServer();
  await new Promise((resolve, reject) => {
    probe.once("error", reject);
    probe.listen(value, "127.0.0.1", () => probe.close(resolve));
  });
}

await assertPortFree(port);
await assertPortFree(mockPort);
await mkdir(screenshotRoot, { recursive: true });
const mock = await startMockControlServer(mockPort);
const server = spawn(process.execPath, [path.join(webRoot, "node_modules/next/dist/bin/next"), "dev", "-H", "127.0.0.1", "-p", String(port)], {
  cwd: webRoot, detached: true, env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" }, stdio: ["ignore", "pipe", "pipe"],
});
let output = "";
for (const stream of [server.stdout, server.stderr]) stream.on("data", (chunk) => { output = `${output}${chunk}`.slice(-8000); });
let browser;
try {
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (server.exitCode !== null) throw new Error(output);
    try { if ((await fetch(origin)).ok) { ready = true; break; } } catch { /* Wait for Next startup. */ }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  assert.ok(ready, output);
  browser = await chromium.launch(process.env.CAPTURE_BROWSER_EXECUTABLE
    ? { executablePath: process.env.CAPTURE_BROWSER_EXECUTABLE, headless: true }
    : { channel: process.env.CAPTURE_BROWSER_CHANNEL ?? "msedge", headless: true });
  for (const [name, viewport] of [["desktop", { width: 1440, height: 1000 }], ["mobile", { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport, isMobile: name === "mobile", hasTouch: name === "mobile" });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    // This is a visual composer check. Existing gateway suites own access proof.
    await page.route("**/api/cortex/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const response = await fetch(`http://127.0.0.1:${mockPort}${url.pathname.replace("/api/cortex/", "/api/v1/")}${url.search}`, { headers: { "x-cortex-control-token": MOCK_CONTROL_TOKEN } });
      await route.fulfill({ status: response.status, body: await response.text(), contentType: "application/json" });
    });
    await page.goto(origin);
    await page.getByRole("radio", { name: "Research", exact: true }).check();
    await page.getByRole("textbox", { name: "Send a durable message" }).fill("Compare memory mechanisms and identify a falsifiable next experiment.");
    await page.locator(".composer").scrollIntoViewIfNeeded();
    await page.waitForTimeout(200);
    assert.equal(await page.getByRole("radio", { name: "Research", exact: true }).isChecked(), true);
    const geometry = await page.evaluate(() => {
      const composer = document.querySelector(".composer").getBoundingClientRect();
      return { viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth, composer: { left: composer.left, right: composer.right }, targets: [...document.querySelectorAll(".composer-mode span")].map((element) => { const rect = element.getBoundingClientRect(); return { width: rect.width, height: rect.height }; }) };
    });
    assert.ok(geometry.content <= geometry.viewport, `${name}: horizontal overflow`);
    assert.ok(geometry.composer.left >= 0 && geometry.composer.right <= viewport.width, `${name}: composer clipping`);
    for (const target of geometry.targets) assert.ok(target.width >= 44 && target.height >= (name === "mobile" ? 44 : 32));
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(screenshotRoot, `${name}.png`) });
    console.log(JSON.stringify({ viewport: name, ...geometry, screenshot: `${name}.png`, pageErrors: errors.length }));
    await context.close();
  }
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    process.kill(-server.pid, "SIGTERM");
    await Promise.race([new Promise((resolve) => server.once("exit", resolve)), new Promise((resolve) => setTimeout(resolve, 5000))]);
    if (server.exitCode === null) process.kill(-server.pid, "SIGKILL");
  }
  await mock.close();
  await assertPortFree(port);
  await assertPortFree(mockPort);
}
