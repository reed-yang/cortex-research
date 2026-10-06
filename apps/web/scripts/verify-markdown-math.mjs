// Browser acceptance for the shared renderer and its reading modes, in Outputs
// and in the Library's Preview. All content is a synthetic local fixture; the
// live product is never contacted.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright-core";
import { launchOptions, loopbackPorts, startLoopbackWeb } from "./loopback-web-harness.mjs";

const output = new URL("../../../logs/web-markdown-math/browser/", import.meta.url);
await mkdir(output, { recursive: true });
const formula = String.raw`$c_\text{act}\in\mathbb{R}^{T\times12}$`;
const body = `# Action conditioning\n\nThe action sequence is ${formula}.\n\n## Model inputs\n\n| Input | Space |\n| --- | --- |\n| Actions | ${formula} |\n\n$$\n\\mathcal{L} = \\sum_{t=1}^T \\lVert \\hat{a}_t-a_t \\rVert_2^2 + a_1 + a_2 + a_3 + a_4 + a_5 + a_6 + a_7 + a_8 + a_9 + a_{10}\n$$\n\n\`\`\`latex\n${formula}\n\`\`\`\n\n` + "Readable retained research context. ".repeat(160);
const appendix = `\n\n## Retained source provenance\n\n\`\`\`json\n${JSON.stringify({ context_sha256: "a".repeat(64), run_id: "run_mobile", attempt_id: "attempt_mobile", runtime_release_id: "fixture", runtime_worker_protocol: "1", retrieval_mode: "adopted", citation_status: "labels_valid_claims_unverified", sources: [{ source_id: "src_echo", excerpt: "x".repeat(12000) }] }, null, 2)}\n\`\`\`\n`;
const text = body + appendix;
const sha = createHash("sha256").update(text).digest("hex");
const at = "2026-07-23T12:00:00Z";
const generator = { name: "research-response", version: "1" };
const tool = { name: "application-research", version: "1" };
const version = {
  id: "version_math", artifact_id: "artifact_math", logical_version: 1,
  resource_uri: "cortex://artifacts/artifact_math/version_math", sha256: sha, byte_length: Buffer.byteLength(text),
  media_type: "text/markdown", run_id: "run_mobile", attempt_id: "attempt_mobile",
  parents: [], source_ids: ["src_echo"], generator, tool, state: "committed", created_at: at, committed_at: at,
  provenance: { schema_version: 1, run_id: "run_mobile", attempt_id: "attempt_mobile", source_ids: ["src_echo"], generator, tool, parents: [], media_type: "text/markdown", byte_length: Buffer.byteLength(text), sha256: sha, committed_at: at },
};
const harness = await startLoopbackWeb(loopbackPorts("verify-markdown-math"));
const browser = await chromium.launch(launchOptions());
const results = [];
try {
  for (const [name, width, height, colorScheme] of [["desktop-light", 1440, 1000, "light"], ["mobile-dark", 390, 844, "dark"]]) {
    const context = await browser.newContext({ viewport: { width, height }, colorScheme, permissions: ["clipboard-read", "clipboard-write"] });
    const errors = [];
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route("**/api/cortex/runs/run_mobile/research-workflow", async (route) => {
      const response = await route.fetch({ headers: { ...route.request().headers(), "sec-fetch-site": "same-origin" } });
      const data = await response.json();
      data.sources = [{ id: "binding_math", disposition: "reused", created_at: at, source: { id: "src_echo", authority: "arxiv", authority_id: "2606.04527", canonical_id: "arxiv:2606.04527", source_kind: "paper", official_title: "Echo-Infinity", import_state: "imported", revision: 1, aliases: [], created_at: at, updated_at: at } }];
      data.artifacts = [{ id: "artifact_math", workspace_id: "ws_mobile", thread_id: "thread_mobile", kind: "research-memo", title: "Action conditioning", head_artifact_version_id: version.id, head_revision: 1, created_at: at, updated_at: at, versions: [version] }];
      await route.fulfill({ response, json: data });
    });
    await context.route("**/api/cortex/artifact-versions/version_math/content", (route) => route.fulfill({ json: { artifact_version_id: version.id, media_type: "text/markdown", byte_length: Buffer.byteLength(text), sha256: sha, content: text } }));
    await context.route("**/api/cortex/threads/thread_mobile/messages", async (route) => {
      const response = await route.fetch({ headers: { ...route.request().headers(), "sec-fetch-site": "same-origin" } });
      const data = await response.json();
      data.items[0].content = `The action sequence is ${formula}.`;
      await route.fulfill({ response, json: data });
    });
    await page.goto(`${harness.origin}/?project=ws_mobile&thread=thread_mobile`, { waitUntil: "networkidle" });
    const trigger = page.getByRole("button", { name: /^Outputs/ });
    await trigger.waitFor({ timeout: 10000 }).catch(async (error) => { console.log((await page.locator("body").innerText()).slice(0,3000)); console.log("errors", errors); throw error; });
    await page.locator(".aui-md .katex").first().waitFor();
    assert.equal(await trigger.getAttribute("aria-expanded"), "false");
    await trigger.click();
    const preview = page.getByRole("region", { name: "Rendered Markdown" });
    await preview.locator(".katex").first().waitFor();
    assert.equal(await preview.locator(".katex").count(), 3);
    assert.equal(await trigger.getAttribute("aria-expanded"), "true");
    assert.equal(await page.locator(".artifact-provenance").getAttribute("open"), null);
    await page.evaluate(() => document.fonts.ready);
    const geometry = await page.evaluate(() => {
      const preview = document.querySelector('[aria-label="Rendered Markdown"]');
      const panel = preview.closest('[data-slot="collapsible-content"]');
      const composer = document.querySelector("textarea");
      return { documentWidth: document.documentElement.scrollWidth, viewport: innerWidth, panelHeight: panel.getBoundingClientRect().height, panelScroll: panel.scrollHeight, composerTop: composer.getBoundingClientRect().top, katexFont: document.fonts.check('16px KaTeX_Main'), formulaWidth: preview.querySelector(".katex-display").clientWidth, formulaScrollWidth: preview.querySelector(".katex-display").scrollWidth };
    });
    assert.ok(geometry.documentWidth <= width + 1, JSON.stringify(geometry));
    assert.ok(geometry.panelHeight < height * 0.6, JSON.stringify(geometry));
    assert.ok(geometry.composerTop < height, JSON.stringify(geometry));
    assert.ok(geometry.katexFont);
    if (width < 500) assert.ok(geometry.formulaScrollWidth > geometry.formulaWidth, "wide display math should scroll inside the reader");
    await page.screenshot({ path: new URL(`${name}-preview.png`, output).pathname });
    await page.getByRole("button", { name: "Source", exact: true }).click();
    assert.equal(await page.getByRole("region", { name: "Markdown source" }).textContent(), body);
    assert.equal(await page.getByRole("button", { name: "Source", exact: true }).getAttribute("aria-pressed"), "true");
    await page.screenshot({ path: new URL(`${name}-source.png`, output).pathname });
    await page.getByRole("button", { name: "Copy source" }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), text);
    await page.locator(".artifact-provenance > summary").click();
    assert.equal(await page.locator(".artifact-provenance pre").textContent(), appendix);
    assert.ok(await page.getByRole("button", { name: "Preview", exact: true }).isVisible());
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await trigger.click();
    assert.equal(await trigger.getAttribute("aria-expanded"), "false");

    // Library Preview: the same renderer over a stored source copy, whose own
    // figure loads through the source-bound asset route and whose missing
    // figure is named rather than hidden.
    const assetResponses = [];
    page.on("response", (response) => { if (/\/api\/cortex\/sources\/[^/]+\/asset\?/.test(response.url())) assetResponses.push(response); });
    // Opened through the shell's own navigation: a direct load of
    // `?view=library` is server-rendered as the thread and fails hydration,
    // which is a separate defect from what this acceptance checks.
    if (width < 500) await page.getByRole("button", { name: "Open navigation" }).click();
    await page.getByRole("button", { name: "Library", exact: true }).click();
    await page.getByRole("navigation", { name: "Adopted sources" }).getByRole("button").first().click();
    const reader = page.locator(".source-content-reader");
    const libraryPreview = reader.getByRole("tabpanel");
    await libraryPreview.locator(".katex").first().waitFor();
    assert.equal(await reader.getByRole("button", { name: "Preview", exact: true }).getAttribute("aria-pressed"), "true");
    // Figures load lazily, so the end of the reader is brought into view first.
    await reader.getByRole("button", { name: "Reopen document" }).scrollIntoViewIfNeeded();
    await libraryPreview.getByText("Figure not in the stored copy").waitFor();
    await libraryPreview.getByText("assets/ablation.png").waitFor();
    await page.waitForFunction(() => {
      const image = document.querySelector('.source-content-reader img[alt="Memory drift by layer"]');
      return Boolean(image?.complete && image.naturalWidth > 0);
    });
    const library = await page.evaluate(() => {
      const panel = document.querySelector('.source-content-reader [role="tabpanel"]');
      const image = panel.querySelector('img[alt="Memory drift by layer"]');
      return { documentWidth: document.documentElement.scrollWidth, imageWidth: image.getBoundingClientRect().width, panelWidth: panel.getBoundingClientRect().width, naturalWidth: image.naturalWidth, src: image.getAttribute("src"), srcset: image.getAttribute("srcset"), loading: image.getAttribute("loading"), images: panel.querySelectorAll("img").length, mathCount: panel.querySelectorAll(".katex").length };
    });
    assert.ok(library.documentWidth <= width + 1, JSON.stringify(library));
    assert.ok(library.imageWidth <= library.panelWidth + 1, JSON.stringify(library));
    assert.equal(library.naturalWidth, 480);
    assert.equal(library.src, "/api/cortex/sources/src_echo/asset?path=assets%2Fdrift.png");
    assert.equal(library.srcset, null);
    assert.equal(library.loading, "lazy");
    assert.equal(library.images, 1);
    assert.equal(library.mathCount, 1);
    const served = assetResponses.find((response) => response.url().endsWith("path=assets%2Fdrift.png"));
    assert.equal(served?.status(), 200);
    assert.deepEqual(
      ["content-type", "cache-control", "x-content-type-options", "cross-origin-resource-policy"].map((header) => served.headers()[header]),
      ["image/png", "no-store", "nosniff", "same-origin"],
    );
    assert.equal(assetResponses.find((response) => response.url().endsWith("path=assets%2Fablation.png"))?.status(), 404);
    await page.screenshot({ path: new URL(`${name}-library-preview.png`, output).pathname });
    await reader.getByRole("button", { name: "Source", exact: true }).click();
    await reader.getByLabel("Line 13").waitFor();
    assert.equal(await page.evaluate(() => localStorage.getItem("cortex.library.reader-mode.v1")), "source");
    await reader.getByRole("button", { name: "Copy source" }).click();
    const stored = await page.evaluate(async () => (await (await fetch("/api/cortex/sources/src_echo/document?kind=notes")).json()).text);
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), stored);
    await page.screenshot({ path: new URL(`${name}-library-source.png`, output).pathname });

    assert.deepEqual(errors, []);
    results.push({ name, ...geometry, mathCount: 3, sourceExact: true, copyExact: true, library, errors });
    await context.close();
  }
  await writeFile(new URL("results.json", output), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
} finally {
  await browser.close();
  await harness.close();
}
