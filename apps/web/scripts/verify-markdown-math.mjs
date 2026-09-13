// Browser acceptance for the shared renderer and output reading modes. All
// content is a synthetic local fixture; the live product is never contacted.
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
    assert.deepEqual(errors, []);
    results.push({ name, ...geometry, mathCount: 3, sourceExact: true, copyExact: true, errors });
    await context.close();
  }
  await writeFile(new URL("results.json", output), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
} finally {
  await browser.close();
  await harness.close();
}
