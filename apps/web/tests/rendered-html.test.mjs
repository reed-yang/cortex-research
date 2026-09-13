import assert from "node:assert/strict";
import test from "node:test";

async function render(url = "http://127.0.0.1/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request(url, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders G0 as durable control state with zero research changes", async () => {
  const response = await render("http://127.0.0.1/?mode=demo");
  assert.equal(response.status, 200);
  const html = await response.text();

  assert.match(html, /<title>Cortex — Research Workspace<\/title>/i);
  assert.match(html, /Decision required/);
  assert.match(html, /Durable control ledger/);
  assert.match(html, /Research side-effect ledger/);
  assert.match(html, /0 changes/);
  assert.match(html, /Decision Inbox/);
  assert.match(html, /6 durable events(?:<!-- -->)? · collapsed by default/);
  assert.match(html, /Artifacts are not created yet/);
  assert.doesNotMatch(html, /<h2>Living Brief<\/h2>/);
  assert.doesNotMatch(html, /<h2>Evidence<\/h2>/);
  assert.doesNotMatch(html, /<h2>Training Plan<\/h2>/);
  assert.doesNotMatch(html, /<h2>Snapshot<\/h2>/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/);
});

test("server-renders canonical source identity and a collapsed raw log", async () => {
  const html = await (await render("http://127.0.0.1/?mode=demo")).text();
  assert.match(html, /Canonical ID/);
  assert.match(html, /arxiv:2607\.07675/);
  assert.match(html, /Model alias/);
  assert.match(html, /LingBot-Video/);
  assert.match(html, /<details class="raw-log">/);
  assert.doesNotMatch(html, /<details class="raw-log" open/);
});

test("server-renders the deterministic full-width capture mode", async () => {
  const html = await (await render("http://127.0.0.1/?mode=demo&capture=full&scenario=g0")).text();
  assert.match(html, /prototype-shell capture-full-page/);
  assert.match(html, /data-capture-scenario="g0"/);
  assert.match(html, /transform:scale\(0\.5\);transform-origin:top left/);
});

test("cold-starts the G1 capture URL without a scenario click", async () => {
  const html = await (
    await render("http://127.0.0.1/?mode=demo&capture=full&scenario=g1")
  ).text();
  assert.match(html, /data-capture-scenario="g1"/);
  assert.match(html, /Successor memory research workspace/);
  assert.match(html, /One successor, two independent reuse links/);
  assert.match(html, /Canonical fixture resolution/);
  assert.doesNotMatch(html, /Artifacts are not created yet/);
});

test("rejects an unknown capture scenario", async () => {
  const response = await render("http://127.0.0.1/?mode=demo&capture=full&scenario=unknown");
  assert.equal(response.status, 404);
});

test("server-renders installable standalone and iPhone metadata", async () => {
  const html = await (await render()).text();
  assert.match(html, /<link rel="manifest" href="\/manifest\.webmanifest"\/>/);
  assert.match(html, /<link rel="apple-touch-icon" href="\/icons\/cortex-180\.png" sizes="180x180" type="image\/png"\/>/);
  assert.match(html, /<meta name="mobile-web-app-capable" content="yes"\/>/);
  assert.match(html, /<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"\/>/);
  assert.match(html, /<meta name="theme-color" content="#ffffff" media="\(prefers-color-scheme: light\)"\/>/);
  assert.match(html, /<meta name="theme-color" content="#0a0a0a" media="\(prefers-color-scheme: dark\)"\/>/);
  assert.match(html, /content="width=device-width, initial-scale=1, viewport-fit=cover" name="viewport"/);
});

// The default route is the shell, and this asserts its SHAPE, never its words:
// the shell's copy is owned elsewhere and revised independently, so a sentence
// change must not turn into a red render test. Landmarks, the assistant-ui
// registry's own data slots and ARIA roles are the stable contract.
test("server-renders the shell's own layout by default without fixture state", async () => {
  const html = await (await render()).text();
  assert.match(html, /<aside [^>]*aria-label="[^"]+"/);
  assert.match(html, /<nav [^>]*aria-label="[^"]+"/);
  // `main` is a landmark and deliberately not a live region: the status strip
  // and the notice bar are the two lines that change, and announcing the whole
  // view for either of them would read all of it out again (`app/shell/shell.tsx`).
  assert.match(html, /<main [^>]*>/);
  assert.doesNotMatch(html, /<main [^>]*aria-live=/);
  assert.match(html, /<section [^>]*aria-label="[^"]+"/);
  assert.match(html, /data-slot="aui_thread-list-root"/);
  assert.match(html, /data-slot="aui_thread-list-items"/);
  assert.match(html, /data-slot="sheet-trigger"/);
  assert.match(html, /role="status"/);
  // Nothing of the demo fixture reaches the default route.
  assert.doesNotMatch(html, /prototype-shell/);
  assert.doesNotMatch(html, /data-capture-scenario/);
  assert.doesNotMatch(html, /Resolve source identity before research/);
  assert.doesNotMatch(html, /Successor memory research workspace/);
});

test("requires demo mode for fixture-only query parameters", async () => {
  assert.equal((await render("http://127.0.0.1/?scenario=g0")).status, 404);
  assert.equal((await render("http://127.0.0.1/?capture=full")).status, 404);
});
