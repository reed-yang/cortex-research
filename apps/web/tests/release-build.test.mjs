import assert from "node:assert/strict";
import { execFile as execFileCallback } from "node:child_process";
import { createHash } from "node:crypto";
import { cp, lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  assertKatexFontManifest,
  derivePinnedKatexFonts,
  katexManifestPath,
  readKatexFontManifest,
} from "../scripts/katex-font-manifest.mjs";
import {
  inspectReleasePayload,
  releasePayloadLedger,
  writeReleasePayload,
} from "../scripts/release-payload.mjs";

const executeFile = promisify(execFileCallback);
const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BUILD_ID = "cortex-r0-payload-fixture";

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function payloadFixture(t, label) {
  const root = await mkdtemp(path.join(os.tmpdir(), `cortex-web-payload-fixture-${label}-`));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const relative of [
    "THIRD_PARTY_NOTICES.md",
    "app/fonts.css",
    "licenses/MIT-KaTeX.txt",
    "licenses/OFL-1.1.txt",
    "server/access-identity-bound.mjs",
    "server/node-adapter.mjs",
  ]) {
    const destination = path.join(root, ...relative.split("/"));
    await mkdir(path.dirname(destination), { recursive: true });
    await cp(path.join(webRoot, ...relative.split("/")), destination);
  }
  for (const directory of ["dist/client/assets", "dist/server/ssr"]) {
    await mkdir(path.join(root, directory), { recursive: true });
  }
  await cp(path.join(root, "app/fonts.css"), path.join(root, "dist/client/assets/fonts.css"));
  await writeFile(path.join(root, "dist/client/manifest.webmanifest"), "{}\n");
  await writeFile(path.join(root, "dist/client/offline.html"), "<!doctype html><title>offline</title>\n");
  await writeFile(path.join(root, "dist/client/sw.js"), "self.addEventListener('fetch', () => {});\n");
  await writeFile(path.join(root, "dist/server/__vite_rsc_assets_manifest.js"), "export default {};\n");
  await writeFile(
    path.join(root, "dist/server/index.js"),
    `const BUILD_ID = ${JSON.stringify(BUILD_ID)};\n` +
      "export default { fetch: async () => new Response(BUILD_ID + process.env.CORTEX_WEB_DRAFT_SECRET) };\n",
  );
  await writeFile(path.join(root, "dist/server/ssr/__vite_rsc_assets_manifest.js"), "export default {};\n");
  await writeFile(path.join(root, "dist/server/ssr/index.js"), "export default {};\n");
  await writeFile(path.join(root, "dist/server/vinext-externals.json"), "[]\n");
  return root;
}

// A payload shaped like the maths-enabled build: every pinned KaTeX face emitted
// as a content-hashed client asset, and one stylesheet that reaches them the way
// the bundler's stylesheet does. Built from the fonts installed in this checkout,
// so a fixture can never claim a digest the pinned manifest does not know.
async function mathFixture(t, label) {
  const root = await payloadFixture(t, label);
  const manifest = await readKatexFontManifest();
  const fontRoot = path.join(webRoot, "node_modules/katex/dist/fonts");
  const emitted = new Map();
  const rules = [];
  for (const [name, digest] of Object.entries(manifest.fonts)) {
    const extension = name.slice(name.lastIndexOf(".") + 1);
    const face = name.slice(0, name.lastIndexOf("."));
    const asset = `${face}-${digest.slice(0, 8)}.${extension}`;
    await cp(path.join(fontRoot, name), path.join(root, "dist/client/assets", asset));
    emitted.set(name, asset);
    rules.push(
      `@font-face{font-family:"${face}";src:url(/assets/${asset}) format("${extension}");}`,
    );
  }
  const stylesheet = path.join(root, "dist/client/assets/katex.css");
  await writeFile(stylesheet, `${rules.join("\n")}\n`);
  return { emitted, manifest, root, stylesheet };
}

function safety(root) {
  return {
    forbiddenPrefixes: [webRoot, root, path.dirname(root), process.env.HOME ?? ""],
    secretValues: ["payload-fixture-secret-must-not-ship"],
    sourceRoot: root,
  };
}

test("synthetic payload fixture cannot be mistaken for offline release evidence", async (t) => {
  const root = await payloadFixture(t, "non-release");
  const entries = await inspectReleasePayload(path.join(root, "dist"), safety(root));
  assert.ok(entries.length > 0);
  await assert.rejects(lstat(path.join(root, "complete.json")), /ENOENT/);
  await assert.rejects(lstat(path.join(root, "provenance.json")), /ENOENT/);
  await assert.rejects(lstat(path.join(root, "node_modules")), /ENOENT/);
});

test("selected synthetic payload fixture is deterministic, closed, and standalone", async (t) => {
  const firstRoot = await payloadFixture(t, "first");
  const secondRoot = await payloadFixture(t, "second");
  const first = await inspectReleasePayload(path.join(firstRoot, "dist"), safety(firstRoot));
  const second = await inspectReleasePayload(path.join(secondRoot, "dist"), safety(secondRoot));
  assert.equal(releasePayloadLedger(first), releasePayloadLedger(second));

  const server = first.find((entry) => entry.path === "server/index.js").contents.toString("utf8");
  assert.match(server, /CORTEX_WEB_DRAFT_SECRET/);
  assert.match(server, new RegExp(BUILD_ID));
  assert.ok(first.some((entry) => entry.path === "server/node-adapter.mjs"));
  assert.ok(first.some((entry) => entry.path === "THIRD_PARTY_NOTICES.md"));
  assert.ok(first.some((entry) => entry.path === "licenses/OFL-1.1.txt"));

  const cssEntry = first.find(
    (entry) => entry.path.startsWith("client/assets/") && entry.path.endsWith(".css"),
  );
  const cssPath = path.join(firstRoot, "dist", ...cssEntry.path.split("/"));
  const tamperedCss = cssEntry.contents.toString("utf8").replace(
    /(data:font\/woff2;base64,)([A-Za-z0-9+/])/,
    (_, prefix, firstCharacter) => `${prefix}${firstCharacter === "A" ? "B" : "A"}`,
  );
  await writeFile(cssPath, tamperedCss);
  await assert.rejects(
    inspectReleasePayload(path.join(firstRoot, "dist"), safety(firstRoot)),
    /unapproved embedded font component/,
  );
  await writeFile(cssPath, cssEntry.contents);

  const unexpected = path.join(firstRoot, "dist/client/unexpected.txt");
  await writeFile(unexpected, "unexpected");
  await assert.rejects(
    inspectReleasePayload(path.join(firstRoot, "dist"), safety(firstRoot)),
    /unexpected file/,
  );
  await rm(unexpected);

  const malicious = path.join(firstRoot, "dist/client/malicious-link");
  await symlink(path.join(firstRoot, "app/fonts.css"), malicious);
  await assert.rejects(
    inspectReleasePayload(path.join(firstRoot, "dist"), safety(firstRoot)),
    /symlink/,
  );
  await rm(malicious);

  const standaloneParent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-payload-standalone-"));
  t.after(() => rm(standaloneParent, { recursive: true, force: true }));
  const standalone = path.join(standaloneParent, "payload");
  await writeReleasePayload(first, standalone);
  const runtimeSecret = "runtime-only-payload-fixture-secret";
  const program = [
    `const mod = await import(${JSON.stringify(pathToFileURL(path.join(standalone, "server/index.js")).href)});`,
    "const response = await mod.default.fetch(new Request('http://127.0.0.1/'));",
    "process.stdout.write(await response.text());",
  ].join("\n");
  const result = await executeFile(process.execPath, ["--input-type=module", "--eval", program], {
    cwd: standalone,
    env: {
      CORTEX_WEB_DRAFT_SECRET: runtimeSecret,
      HOME: path.join(standaloneParent, "empty-home"),
      NODE_PATH: "",
      PATH: path.dirname(process.execPath),
    },
    timeout: 10_000,
  });
  assert.equal(result.stdout, `${BUILD_ID}${runtimeSecret}`);
});

// The module-closure guard is decided by the repository's pinned ECMAScript
// parser, so it must read the parse tree and not the text: import-like prose is
// inert, and a real reference counts however it is written.
test("release module closure reads the parse tree and not the text", async (t) => {
  const root = await payloadFixture(t, "module-closure");
  const ssr = path.join(root, "dist/server/ssr/index.js");

  await writeFile(
    ssr,
    "/**\n * @typedef {import('hast').Nodes} Nodes\n * @import {Element} from 'hast'\n */\n" +
      "const documentation = \"import hast from 'hast'\";\n" +
      "export default { documentation };\n",
  );
  const entries = await inspectReleasePayload(path.join(root, "dist"), safety(root));
  assert.ok(entries.some((entry) => entry.path === "server/ssr/index.js"));

  await writeFile(
    ssr,
    "const ready = true; export { parse } from \"hast\";\nexport default { ready };\n",
  );
  await assert.rejects(
    inspectReleasePayload(path.join(root, "dist"), safety(root)),
    /release server has a bare external import: hast/,
  );

  await writeFile(ssr, "export default await import(String.fromCharCode(104));\n");
  await assert.rejects(
    inspectReleasePayload(path.join(root, "dist"), safety(root)),
    /computed module specifier in server\/ssr\/index\.js/,
  );

  // The analyser reports no `require()` fact, so a CommonJS escape hatch is
  // decided by a second read of the same pinned parser's tree.
  for (const escape of [
    "import { createRequire } from \"node:module\";\nexport default createRequire(import.meta.url)(\"hast\");\n",
    "export default globalThis.require(\"hast\");\n",
    "const node = await import(\"node:module\");\nexport default node[\"createRequire\"](import.meta.url)(\"hast\");\n",
    "const load = globalThis[\"require\"];\nexport default load(\"hast\");\n",
  ]) {
    await writeFile(ssr, escape);
    await assert.rejects(
      inspectReleasePayload(path.join(root, "dist"), safety(root)),
      /release payload contains a CommonJS require in server\/ssr\/index\.js/,
      escape,
    );
  }

  // Prose, data and pattern are not calls. The maths renderer ships exactly the
  // first of these (parse5, through hast-util-from-html) and a text scan refused
  // it; the last two are why this reads a tree and not a token stream, since a
  // slash is a division or a regular expression only once something has parsed
  // the code around it.
  for (const inert of [
    "/**\n * Usage:\n *   const parse5 = require('parse5');\n */\n" +
      "const usage = \"const parse5 = require('parse5')\";\nexport default { usage };\n",
    "const pattern = /require\\(['\"]hast['\"]\\)/;\nconst quoted = `const x = require(\"hast\")`;\n" +
      "export default { pattern: pattern.source, quoted };\n",
    "const half = 8 / 2 / 2;\nconst matched = \"require(x)\".replace(/require\\(/g, \"\");\n" +
      "export default { half, matched };\n",
  ]) {
    await writeFile(ssr, inert);
    const entries = await inspectReleasePayload(path.join(root, "dist"), safety(root));
    assert.ok(entries.some((entry) => entry.path === "server/ssr/index.js"), inert);
  }

  await writeFile(ssr, "export default { broken: (\n");
  await assert.rejects(
    inspectReleasePayload(path.join(root, "dist"), safety(root)),
    /release server module could not be parsed: server\/ssr\/index\.js/,
  );
});

test("pinned KaTeX font manifest is the installed package, reviewed in source", async () => {
  const pinned = await readKatexFontManifest();
  const derived = await derivePinnedKatexFonts();
  assert.deepEqual(pinned, derived, `${katexManifestPath()} is stale; rerun ${pinned.regenerate}`);
  assert.equal(Object.keys(pinned.fonts).length, 60);
  assert.equal(pinned.license, "MIT");
  assert.equal(
    sha256(await readFile(path.join(webRoot, pinned.license_file))),
    pinned.license_sha256,
    "the shipped KaTeX license bytes must be the ones the manifest pins",
  );

  const corrupt = (mutate) => {
    const copy = JSON.parse(JSON.stringify(pinned));
    mutate(copy);
    return copy;
  };
  assert.throws(
    () => assertKatexFontManifest(corrupt((m) => { m.package = "not-katex"; })),
    /does not identify a pinned katex version/,
  );
  assert.throws(
    () => assertKatexFontManifest(corrupt((m) => { m.license_sha256 = "not-a-digest"; })),
    /does not pin the MIT licence bytes/,
  );
  assert.throws(
    () => assertKatexFontManifest(corrupt((m) => { m.fonts["evil.woff2"] = m.license_sha256; })),
    /names an unusable font/,
  );
  assert.throws(
    () => assertKatexFontManifest(corrupt((m) => {
      m.fonts["KaTeX_AMS-Regular.woff2"] = m.fonts["KaTeX_AMS-Regular.woff"];
    })),
    /pins one digest twice/,
  );
});

test("maths payload admits exactly the pinned KaTeX fonts and refuses anything else", async (t) => {
  const { emitted, manifest, root, stylesheet } = await mathFixture(t, "katex");
  const dist = path.join(root, "dist");
  const asset = (name) => path.join(dist, "client/assets", emitted.get(name));
  const css = await readFile(stylesheet);

  const entries = await inspectReleasePayload(dist, safety(root));
  const carried = new Set(entries.map((entry) => entry.path));
  for (const name of emitted.values()) {
    assert.ok(carried.has(`client/assets/${name}`), `${name} must be admitted`);
  }
  assert.ok(carried.has("licenses/MIT-KaTeX.txt"));
  assert.match(releasePayloadLedger(entries), /  licenses\/MIT-KaTeX\.txt\n/);
  // Still exactly the two Geist subsets, decided on the embedded bytes.
  assert.ok(carried.has("client/assets/fonts.css"));

  const unpinned = path.join(dist, "client/assets/KaTeX_Fake-Regular-0123abcd.woff2");
  await cp(asset("KaTeX_AMS-Regular.woff2"), unpinned);
  await assert.rejects(inspectReleasePayload(dist, safety(root)), /unpinned KaTeX font: KaTeX_Fake-Regular\.woff2/);
  await rm(unpinned);

  const duplicate = path.join(dist, "client/assets/KaTeX_AMS-Regular-99999999.woff2");
  await cp(asset("KaTeX_AMS-Regular.woff2"), duplicate);
  await assert.rejects(
    inspectReleasePayload(dist, safety(root)),
    /contains KaTeX_AMS-Regular\.woff2 twice/,
  );
  await rm(duplicate);

  const original = await readFile(asset("KaTeX_Main-Regular.woff2"));
  const tampered = Buffer.from(original);
  tampered[tampered.length - 1] ^= 0xff;
  await writeFile(asset("KaTeX_Main-Regular.woff2"), tampered);
  await assert.rejects(inspectReleasePayload(dist, safety(root)), /unapproved font component/);
  await writeFile(asset("KaTeX_Main-Regular.woff2"), original);

  await rm(asset("KaTeX_Size4-Regular.ttf"));
  await assert.rejects(
    inspectReleasePayload(dist, safety(root)),
    /missing pinned KaTeX fonts: KaTeX_Size4-Regular\.ttf/,
  );
  await cp(
    path.join(webRoot, "node_modules/katex/dist/fonts/KaTeX_Size4-Regular.ttf"),
    asset("KaTeX_Size4-Regular.ttf"),
  );

  await writeFile(stylesheet, `${css}@font-face{src:url(/assets/KaTeX_Gone-Regular-deadbeef.woff2);}\n`);
  await assert.rejects(
    inspectReleasePayload(dist, safety(root)),
    /references a missing asset: KaTeX_Gone-Regular-deadbeef\.woff2/,
  );

  await writeFile(stylesheet, `${css}@font-face{src:url(https://cdn.jsdelivr.net/npm/katex/dist/fonts/KaTeX_Main-Regular.woff2);}\n`);
  await assert.rejects(inspectReleasePayload(dist, safety(root)), /fetches a remote resource/);
  await writeFile(stylesheet, css);

  // The SSR graph's copy of a KaTeX face is build residue and is dropped; a font
  // of any other origin in the same directory is still an unexpected file.
  await mkdir(path.join(dist, "server/ssr/assets"), { recursive: true });
  const residue = path.join(dist, "server/ssr/assets/KaTeX_Main-Regular-0123abcd.woff2");
  await cp(asset("KaTeX_Main-Regular.woff2"), residue);
  assert.ok((await inspectReleasePayload(dist, safety(root))).length > 0);
  const foreign = path.join(dist, "server/ssr/assets/Vendor-Regular-0123abcd.woff2");
  await cp(asset("KaTeX_Main-Regular.woff2"), foreign);
  await assert.rejects(inspectReleasePayload(dist, safety(root)), /unexpected file/);
  await rm(foreign);
  await rm(residue);

  const notices = await readFile(path.join(root, "THIRD_PARTY_NOTICES.md"), "utf8");
  await writeFile(
    path.join(root, "THIRD_PARTY_NOTICES.md"),
    notices.replace(manifest.fonts["KaTeX_Main-Regular.woff2"], "0".repeat(64)),
  );
  await assert.rejects(
    inspectReleasePayload(dist, safety(root)),
    /notices do not identify every pinned KaTeX font component/,
  );
  await writeFile(
    path.join(root, "THIRD_PARTY_NOTICES.md"),
    notices.replaceAll(`katex@${manifest.version}`, "katex@0.0.0"),
  );
  await assert.rejects(
    inspectReleasePayload(dist, safety(root)),
    /notices do not identify the pinned KaTeX release/,
  );
  await writeFile(path.join(root, "THIRD_PARTY_NOTICES.md"), notices);

  const license = await readFile(path.join(root, "licenses/MIT-KaTeX.txt"));
  await writeFile(path.join(root, "licenses/MIT-KaTeX.txt"), `${license.toString("utf8")}\n`);
  await assert.rejects(
    inspectReleasePayload(dist, safety(root)),
    /does not carry the pinned KaTeX MIT license bytes/,
  );
  await writeFile(path.join(root, "licenses/MIT-KaTeX.txt"), license);

  assert.ok((await inspectReleasePayload(dist, safety(root))).length > 0);
});
