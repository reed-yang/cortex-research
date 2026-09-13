import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { decodeRgbPng } from "../scripts/screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => readFile(path.join(webRoot, relative), "utf8");

test("manifest is scoped, standalone, local, and icon-complete", async () => {
  const manifest = JSON.parse(await read("public/manifest.webmanifest"));
  assert.equal(manifest.id, "/");
  assert.equal(manifest.start_url, "/");
  assert.equal(manifest.scope, "/");
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.theme_color, "#ffffff");
  assert.equal(manifest.background_color, "#ffffff");
  assert.deepEqual(
    manifest.icons.map(({ src, sizes, type, purpose }) => ({ src, sizes, type, purpose })),
    [
      { src: "/icons/cortex-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/cortex-512.png", sizes: "512x512", type: "image/png", purpose: "any maskable" },
    ],
  );
  assert.ok(!JSON.stringify(manifest).includes("http"));
});

test("committed icons are valid RGB PNGs at their declared sizes", async () => {
  for (const size of [180, 192, 512]) {
    const image = decodeRgbPng(await readFile(path.join(webRoot, `public/icons/cortex-${size}.png`)));
    assert.equal(image.width, size);
    assert.equal(image.height, size);
    assert.ok(image.pixels.some((value) => value !== image.pixels[0]));
  }
});

test("service worker caches only install assets and uses an honest navigation fallback", async () => {
  const worker = await read("public/sw.js");
  const offline = await read("public/offline.html");
  assert.match(worker, /event\.request\.mode !== "navigate"/);
  assert.match(worker, /fetch\(event\.request\)\.catch\(\(\) => caches\.match\(OFFLINE_URL\)\)/);
  assert.match(worker, /key\.startsWith\(CACHE_PREFIX\) && key !== CACHE_NAME/);
  assert.doesNotMatch(worker, /PushManager|Notification|addEventListener\("push"|api\//i);
  assert.match(offline, /No research state is stored in this offline page/);
  assert.match(offline, /no action will be queued/);
  assert.doesNotMatch(offline, /onclick=|<script/i);
});

test("mobile CSS includes dynamic viewport, safe areas, navigation, and touch targets", async () => {
  const css = await read("app/globals.css");
  assert.match(css, /min-height:\s*100vh;\s*min-height:\s*100dvh;/);
  assert.match(css, /@media \(max-width: 560px\)/);
  assert.match(css, /env\(safe-area-inset-bottom\)/);
  assert.match(css, /\.mobile-nav\s*\{[^}]*position:\s*fixed;/s);
  assert.match(css, /\.mobile-nav a,\s*\.mobile-nav button\s*\{[^}]*min-height:\s*44px;/s);
  assert.match(css, /max-height:\s*min\(34dvh, 280px\)/);
});
