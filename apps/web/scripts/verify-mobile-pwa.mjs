import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { copy } from "../app/shell/copy.ts";
import {
  assertPortAvailable,
  launchOptions,
  loopbackPorts,
  MOCK_CONTROL_TOKEN,
  startLoopbackWeb,
} from "./loopback-web-harness.mjs";
import { sha256 } from "./screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const screenshotRoot = path.join(webRoot, "artifacts/screenshots");
const specPath = path.join(screenshotRoot, "mobile-capture-spec.json");
const spec = JSON.parse(await readFile(specPath, "utf8"));
const writeMode = process.argv.includes("--write");
const { controlPort, proxyPort, upstreamPort } = loopbackPorts("verify-mobile-pwa");
const baseURL = new URL(spec.base_origin);
baseURL.port = String(proxyPort);
const origin = baseURL.origin;

async function assertNoHorizontalOverflow(page) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  assert.ok(
    dimensions.content <= dimensions.viewport,
    `mobile content overflowed horizontally: ${dimensions.content} > ${dimensions.viewport}`,
  );
}

// The phone is a first-class surface (spec section 4), so one floor covers both
// the retired prototype's own navigation bar and the shell: 44 CSS pixels, the
// iOS Human Interface Guidelines target. The shell's controls carry it below
// `lg` through `min-h-11`/`min-w-11` and reset to the compact shadcn sizes at
// `lg`, so this is the size a thumb actually gets.
const TOUCH_TARGET = 44;

async function assertTouchTargets(page, selector, minimum = TOUCH_TARGET) {
  const targets = await page.locator(selector).evaluateAll((elements) =>
    elements
      // `checkVisibility` is what excludes the fixed rail the shell hides below
      // `lg`: a `display: none` ANCESTOR leaves the button's own computed
      // display untouched, so reading the element's style alone measured those
      // buttons at 0x0. `opacityProperty` is asked for as well, because a
      // control the thumb cannot see is not a target however large its box is:
      // measuring a transparent one certifies a hit area nobody can aim at.
      .filter((element) => element.checkVisibility({ opacityProperty: true, visibilityProperty: true }))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return { label: element.textContent?.trim(), width: rect.width, height: rect.height };
      }),
  );
  assert.ok(targets.length > 0, `${selector} must expose visible touch targets`);
  for (const target of targets) {
    assert.ok(target.width >= minimum, `${target.label} is only ${target.width}px wide`);
    assert.ok(target.height >= minimum, `${target.label} is only ${target.height}px tall`);
  }
}

async function settleScroll(page) {
  await page.evaluate(() => new Promise((resolve) => {
    let previous = `${scrollX}:${scrollY}`;
    let stableFrames = 0;
    const check = () => {
      const current = `${scrollX}:${scrollY}`;
      stableFrames = current === previous ? stableFrames + 1 : 0;
      previous = current;
      if (stableFrames >= 3) resolve();
      else requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  }));
}

async function assertInstallability(page) {
  const manifestHref = await page.locator('link[rel="manifest"]').getAttribute("href");
  assert.equal(manifestHref, "/manifest.webmanifest");
  const manifest = await page.evaluate(async () => (await fetch("/manifest.webmanifest")).json());
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.start_url, "/");
  assert.deepEqual(manifest.icons.map((icon) => icon.sizes), ["192x192", "512x512"]);
  assert.equal(await page.locator('meta[name="apple-mobile-web-app-status-bar-style"]').getAttribute("content"), "black-translucent");
  assert.match(await page.locator('meta[name="viewport"]').last().getAttribute("content"), /viewport-fit=cover/);
}

async function ensureServiceWorkerControl(page) {
  await page.waitForFunction(() => "serviceWorker" in navigator);
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  if (!(await page.evaluate(() => Boolean(navigator.serviceWorker.controller)))) {
    await page.reload({ waitUntil: "domcontentloaded" });
  }
  await page.waitForFunction(() => Boolean(navigator.serviceWorker.controller));
}

async function captureOrVerify(page, filename) {
  await page.evaluate(() => document.fonts.ready);
  const buffer = await page.screenshot({ animations: "disabled", type: "png" });
  const fingerprint = sha256(buffer);
  const entry = spec.screenshots[filename];
  assert.ok(entry, `${filename} must be declared in mobile-capture-spec.json`);
  if (writeMode) {
    await writeFile(path.join(screenshotRoot, filename), buffer);
    entry.sha256 = fingerprint;
  } else {
    assert.equal(fingerprint, entry.sha256, `${filename} content fingerprint drifted`);
  }
  assert.equal(buffer.readUInt32BE(16), spec.css_viewport.width * spec.device_scale_factor);
  assert.equal(buffer.readUInt32BE(20), spec.css_viewport.height * spec.device_scale_factor);
  console.log(`${entry.scenario}: ${filename} ${fingerprint}`);
}

await assertPortAvailable("before mobile acceptance", proxyPort);
await assertPortAvailable("before Web upstream acceptance", upstreamPort);
await assertPortAvailable("before mock Control acceptance", controlPort);
const harness = await startLoopbackWeb({ controlPort, proxyPort, upstreamPort });
let browser;
try {
  assert.equal(harness.origin, origin);
  browser = await chromium.launch(launchOptions());

  const controlContext = await browser.newContext({
    viewport: spec.css_viewport,
    deviceScaleFactor: spec.device_scale_factor,
    hasTouch: true,
    isMobile: true,
  });
  const controlPage = await controlContext.newPage();
  try {
    await controlPage.goto(origin, { waitUntil: "domcontentloaded" });
    // The shell's thread header carries the open thread's title as its `h1`.
    await controlPage.getByRole("heading", { name: "Helios-14B memory plan" }).waitFor();
    await controlPage.getByText("Echo-Infinity and Helios evidence is durable.", { exact: false }).waitFor();
    await assertInstallability(controlPage);
    await assertNoHorizontalOverflow(controlPage);
    await controlPage.getByRole("article", { name: copy.decision.title }).waitFor();
    await assertTouchTargets(controlPage, `article[aria-label="${copy.decision.title}"] button`);
    // Captured before anything is clicked: the drawer below hands focus back to
    // its trigger when it closes, and a page holding a focus ring is a page
    // whose bytes depend on the frame the screenshot landed on.
    await settleScroll(controlPage);
    await captureOrVerify(controlPage, "mobile-control-decision.png");
    // The rest of what the thread view puts under a thumb: the composer's send
    // control, and the run strip's own action (this run is waiting for the
    // decision above, so the strip offers Cancel).
    await assertTouchTargets(controlPage, ".aui-composer-send");
    await assertTouchTargets(controlPage, '[role="status"] button');
    // Below `lg` the rail is a drawer, so the projects, the thread list and the
    // Library/Inbox/Status entries are reachable on a phone only through it.
    await assertTouchTargets(controlPage, `button[aria-label="${copy.sidebar.openNavigation}"]`);
    await controlPage.getByRole("button", { name: copy.sidebar.openNavigation }).click();
    const drawer = controlPage.getByRole("dialog");
    await drawer.waitFor();
    // Radix animates the drawer in, and Playwright calls an element visible
    // while it is still transparent and still being transformed. The sweeps
    // below read both opacity and geometry, so they wait for the panel to
    // finish arriving: a box measured under the last frame of a zoom lands a
    // few ten-thousandths short of the 44 it was given. A spinner inside the
    // panel repeats forever and is not something to wait for.
    await controlPage.waitForFunction(() => {
      const panel = document.querySelector('[role="dialog"]');
      if (!panel || !panel.checkVisibility({ opacityProperty: true })) return false;
      return panel.getAnimations({ subtree: true }).every((animation) =>
        animation.playState !== "running" ||
        animation.effect?.getComputedTiming().iterations === Infinity);
    });
    await assertTouchTargets(drawer, `[aria-label="${copy.sidebar.global}"] button`);
    // Not only the destinations: the project switcher, the thread list, each
    // row's menu and the drawer's own close control are all reached by thumb.
    await assertTouchTargets(drawer, "button");
    // Named on its own because the sweep above skips a transparent control
    // rather than failing on it: below `lg` the row menu is always visible, and
    // that is exactly what makes its 44-pixel box a target and not a trap.
    await assertTouchTargets(drawer, '[data-slot="aui_thread-list-item-more"]');
    await controlPage.keyboard.press("Escape");
    await drawer.waitFor({ state: "hidden" });
    const browserState = await controlPage.evaluate(async () => {
      const localValues = Object.keys(localStorage).map((key) => localStorage.getItem(key));
      const cachedRequests = [];
      for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        cachedRequests.push(...(await cache.keys()).map((request) => request.url));
      }
      return { cachedRequests, localValues, url: location.href };
    });
    assert.ok(browserState.localValues.every((value) => !value?.includes(MOCK_CONTROL_TOKEN)));
    assert.ok(browserState.cachedRequests.every((url) => !url.includes("/api/")));
    assert.ok(!browserState.url.includes(MOCK_CONTROL_TOKEN));
    // The notice bar is the last control carrying the phone floor. It needs no
    // command to raise -- the mock daemon is read-only -- because a link naming
    // a thread the project does not hold opens nothing and says so.
    await controlPage.goto(`${origin}/?project=ws_mobile&thread=thread_absent`, { waitUntil: "domcontentloaded" });
    await controlPage.getByRole("status", { name: copy.notice.region }).waitFor();
    await assertTouchTargets(controlPage, `button[aria-label="${copy.notice.dismiss}"]`);
  } finally {
    await controlPage.close();
    await controlContext.close();
  }

  const g0Context = await browser.newContext({
    viewport: spec.css_viewport,
    deviceScaleFactor: spec.device_scale_factor,
    hasTouch: true,
    isMobile: true,
  });
  const g0Page = await g0Context.newPage();
  try {
    await g0Page.goto(`${origin}/?mode=demo`, { waitUntil: "domcontentloaded" });
    await g0Page.getByRole("heading", { name: "Resolve source identity before research" }).waitFor();
    await assertInstallability(g0Page);
    await assertNoHorizontalOverflow(g0Page);
    await assertTouchTargets(g0Page, ".mobile-nav a");
    await assertTouchTargets(g0Page, ".scenario-button");
    await ensureServiceWorkerControl(g0Page);
    await g0Page.getByRole("link", { name: /Decisions/ }).click();
    await g0Page.locator("#mobile-decisions").scrollIntoViewIfNeeded();
    await settleScroll(g0Page);
    await assertTouchTargets(g0Page, ".decision-button");
    await g0Context.setOffline(true);
    await g0Page.getByText("Offline · read-only fixture").waitFor();
    assert.equal(await g0Page.locator(".decision-button:disabled").count(), 3);
    assert.equal(await g0Page.getByRole("textbox", { name: "Steer this run" }).isDisabled(), true);
    await captureOrVerify(g0Page, "mobile-g0-offline.png");
    await g0Page.goto(`${origin}/offline-check`, { waitUntil: "domcontentloaded" });
    await g0Page.getByRole("heading", { name: "Cortex is offline" }).waitFor();
    assert.match(await g0Page.textContent("body"), /No research state is stored in this offline page/);
  } finally {
    await g0Context.setOffline(false);
    await g0Page.close();
    await g0Context.close();
  }

  const g1Context = await browser.newContext({
    viewport: spec.css_viewport,
    deviceScaleFactor: spec.device_scale_factor,
    hasTouch: true,
    isMobile: true,
  });
  const g1Page = await g1Context.newPage();
  try {
    await g1Page.goto(`${origin}/?mode=demo&scenario=g1`, { waitUntil: "domcontentloaded" });
    await g1Page.getByRole("heading", { name: "Successor memory research workspace" }).waitFor();
    await g1Page.getByRole("button", { name: "Helios memory architecture" }).click();
    await g1Page.getByRole("link", { name: "Artifacts" }).click();
    await settleScroll(g1Page);
    await g1Page.getByRole("heading", { name: "Living Brief" }).waitFor();
    await g1Page.getByRole("heading", { name: "Snapshot" }).waitFor();
    await assertNoHorizontalOverflow(g1Page);
    await assertTouchTargets(g1Page, ".mobile-nav a");
    await assertTouchTargets(g1Page, ".thread-link:not(:disabled)");
    await g1Page.getByRole("link", { name: "Overview" }).click();
    await settleScroll(g1Page);
    await g1Page.getByRole("button", { name: "Training and ablation plan" }).click();
    await g1Page.getByRole("heading", { name: "Training Plan" }).waitFor();
    await g1Page.getByRole("button", { name: "Helios memory architecture" }).click();
    await g1Page.locator("#mobile-artifacts").scrollIntoViewIfNeeded();
    await settleScroll(g1Page);
    await captureOrVerify(g1Page, "mobile-g1-artifact.png");
  } finally {
    await g1Page.close();
    await g1Context.close();
  }

  if (writeMode) await writeFile(specPath, `${JSON.stringify(spec, null, 2)}\n`);
} finally {
  if (browser) await browser.close();
  await harness.close();
}

console.log(`iPhone-size PWA acceptance passed; loopback server stopped and TCP ${proxyPort} is free.`);
