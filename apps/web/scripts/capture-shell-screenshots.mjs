// Rendered evidence for the shell: every view, in both colour schemes, at a
// desktop and a phone viewport, against the same read-only mock Control world
// every other loopback acceptance uses.
//
// This records evidence; it is not a pixel baseline, and it deliberately has no
// compare mode. The shell is a live view of a daemon it polls, and repeated
// captures of the same scene were measured to differ in a handful of files per
// run even with the update poll frozen and the dev overlay suppressed -- so a
// re-render comparison would report noise as regression. What IS gated is the
// committed bytes: `verify:shell-screenshots` re-derives every digest and the
// geometry from the files themselves, with no browser involved.

import assert from "node:assert/strict";
import { readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { copy } from "../app/shell/copy.ts";
import { assertPortAvailable, launchOptions, loopbackPorts, startLoopbackWeb } from "./loopback-web-harness.mjs";
import { sha256 } from "./screenshot-contract.mjs";
import { expectedShots, SCENES, SCHEMES, VIEWPORTS } from "./shell-screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const shellRoot = path.join(webRoot, "artifacts/shell");
const specPath = path.join(shellRoot, "capture-spec.json");
// Its own three ports, so this may be run beside the mobile acceptance without
// either of them waiting for the other's listener to be released.
const { controlPort, proxyPort, upstreamPort } = loopbackPorts("capture-shell-screenshots");

// A shot is taken only once the page has stopped changing: Radix reveals a
// panel over a frame or two, and the sidebar's thread list shows a loading
// placeholder until Control answers -- which is why waiting on the main view
// alone once caught a project's empty rail mid-skeleton.
async function settle(page) {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction(() => new Promise((resolve) => {
    let previous = "";
    let stable = 0;
    const check = () => {
      const current = `${scrollX}:${scrollY}:${document.documentElement.outerHTML}`;
      stable = current === previous ? stable + 1 : 0;
      previous = current;
      if (stable >= 10) resolve(true);
      else requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  }), null, { timeout: 20_000 });
}

async function openExtra(page, scene) {
  if (scene.open === "archived") {
    // The group's trigger carries its own count, so the name is a prefix.
    await page.getByRole("button", { name: new RegExp(`^${copy.sidebar.archived}`) }).click();
    await page.getByRole("button", { name: "Retired context sweep", exact: true }).waitFor();
  }
  if (scene.open === "source") {
    await page.getByRole("navigation", { name: copy.library.sources }).getByRole("button").first().click();
    await page.getByText("Evolving memory keeps a long video coherent", { exact: false }).waitFor();
  }
  if (scene.open === "drawer") {
    await page.getByRole("button", { name: copy.sidebar.openNavigation }).click();
    await page.getByRole("dialog").waitFor();
  }
}

// Every scene waits on the data its view is there to show, not merely on the
// view's own heading: the first request to a route compiles it, so a shot taken
// on the heading alone can hold a list that is still being read.
async function landed(page, scene) {
  if (scene.name === "empty-project") {
    await page.getByText(copy.thread.noThread).waitFor();
  } else if (scene.name === "library") {
    await page.getByRole("navigation", { name: copy.library.sources }).getByRole("button").nth(2).waitFor();
  } else if (scene.name === "inbox") {
    await page.getByText("https://arxiv.org/abs/2607.07675", { exact: true }).waitFor();
    await page.getByText("Memory drift should be measured before the context is scaled.").waitFor();
  } else if (scene.name === "status") {
    await page.getByText(copy.status.capabilities.control_store).waitFor();
    await page.getByText(copy.status.replay.polling).waitFor();
  } else if (scene.name === "thread-failed") {
    await page.getByRole("heading", { name: "Wan2.2 cache ablation" }).waitFor();
    await page.getByText("Compare the recurrent memory controller", { exact: false }).waitFor();
  } else {
    await page.getByRole("heading", { name: "Helios-14B memory plan" }).waitFor();
    await page.getByRole("article", { name: copy.decision.title }).waitFor();
  }
}

// The shell follows Control's updates on a timer, and it reports "checked on a
// timer" only for the few milliseconds a read is actually in flight. Answering
// the first read and never answering the next one leaves the reported state
// still on that sentence instead of racing back to "nothing is being followed"
// between polls -- which is both what the status shot shows and what the status
// scene's landing wait can wait for.
async function freezeEventPoll(context) {
  let eventReads = 0;
  await context.route("**/api/cortex/events*", async (route) => {
    eventReads += 1;
    if (eventReads === 1) await route.continue();
  });
}

// One pass over every scene before the first shot, so no capture pays for a
// route the dev server has not compiled yet.
async function warmRoutes(browser, origin) {
  const context = await browser.newContext(VIEWPORTS.desktop);
  await freezeEventPoll(context);
  const page = await context.newPage();
  try {
    for (const scene of SCENES) {
      await page.goto(`${origin}/?${scene.query}`, { waitUntil: "domcontentloaded" });
      await landed(page, scene);
    }
  } finally {
    await page.close();
    await context.close();
  }
}

async function main() {
  for (const port of [proxyPort, upstreamPort, controlPort]) {
    await assertPortAvailable("before shell capture", port);
  }
  const spec = { base_origin: `http://127.0.0.1:${proxyPort}`, schemes: [...SCHEMES], viewports: VIEWPORTS, screenshots: {} };
  const harness = await startLoopbackWeb({ controlPort, proxyPort, upstreamPort });
  let browser;
  try {
    browser = await chromium.launch(launchOptions());
    await warmRoutes(browser, harness.origin);
    for (const { scene, scheme, viewport, filename } of expectedShots()) {
      const context = await browser.newContext({ ...VIEWPORTS[viewport], colorScheme: scheme });
      // The dev server draws its own tools overlay over the bottom-left corner
      // whenever it has something to report, which is both not the shell and
      // not the same from run to run. `devIndicators: false` does not remove
      // the element, so the page is told not to paint it.
      await freezeEventPoll(context);
      await context.addInitScript(() => {
        const style = document.createElement("style");
        style.textContent = "nextjs-portal{display:none !important}";
        const install = () => document.head?.append(style);
        if (document.head) install();
        else document.addEventListener("DOMContentLoaded", install, { once: true });
      });
      const page = await context.newPage();
      try {
        const url = `${harness.origin}/?${scene.query}`;
        await page.goto(url, { waitUntil: "domcontentloaded" });
        await landed(page, scene);
        await openExtra(page, scene);
        await settle(page);
        const buffer = await page.screenshot({ animations: "disabled", type: "png" });
        const fingerprint = sha256(buffer);
        const geometry = VIEWPORTS[viewport];
        assert.equal(buffer.readUInt32BE(16), geometry.viewport.width * geometry.deviceScaleFactor);
        assert.equal(buffer.readUInt32BE(20), geometry.viewport.height * geometry.deviceScaleFactor);
        await writeFile(path.join(shellRoot, filename), buffer);
        spec.screenshots[filename] = { scene: scene.name, scheme, viewport, url, sha256: fingerprint };
        console.log(`${scene.name} ${scheme} ${viewport}: ${filename} ${fingerprint}`);
      } finally {
        await page.close();
        await context.close();
      }
    }
    await writeFile(specPath, `${JSON.stringify(spec, null, 2)}\n`);
    const present = (await readdir(shellRoot)).filter((name) => name.endsWith(".png")).sort();
    assert.deepEqual(present, Object.keys(spec.screenshots).sort(), "artifacts/shell holds an undeclared PNG");
  } finally {
    if (browser) await browser.close();
    await harness.close();
  }
  console.log(`${SCENES.length} shell scenes captured at ${Object.keys(VIEWPORTS).length} viewports in ${SCHEMES.length} colour schemes.`);
}

await main();
