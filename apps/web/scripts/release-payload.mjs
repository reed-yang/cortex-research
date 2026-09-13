import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { lstat, mkdir, mkdtemp, open, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { readKatexFontManifest } from "./katex-font-manifest.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// The payload's module graph is decided by the repository's one digest-pinned
// ECMAScript parser -- the same analyser and the same vendored acorn the
// distribution closure gate runs (`distribution/bundle.py`). A second walker
// here would be a second answer to the same question, and a text scan is not an
// answer at all: the regex this replaced read a JSDoc `@typedef {import('hast')}`
// inside a bundled markdown dependency as a real external import, while missing
// `export ... from "bare"` and any import that did not begin its line.
const analyserRoot = path.resolve(webRoot, "../../distribution");
const ACORN_RELATIVE = "vendor/acorn-8.16.0.mjs";
const ANALYSER_RELATIVE = "web_closure.mjs";
// Pinned in source, exactly as `distribution/bundle.py` pins it, so a parser
// found beside the analyser can never be substituted for the reviewed one.
const ACORN_SHA256 = "efb0124a960b34d53f9928c4926bfcfd300bb6a3d7ab64ee949b3a8bed1c7e5f";
const ANALYSER_TIMEOUT = 180_000;
const ANALYSER_OUTPUT_LIMIT = 64 * 1024 * 1024;
const ANALYSER_SOURCE_LIMIT = 8 * 1024 * 1024;
// Identical bytes through an identical parser give an identical answer, so an
// inspection that runs twice in one process pays for the parse once. Keyed on
// the request bytes, never on a path, and held in memory only.
const analysisCache = new Map();

const THIRD_PARTY_FILES = new Map([
  ["THIRD_PARTY_NOTICES.md", path.join(webRoot, "THIRD_PARTY_NOTICES.md")],
  ["licenses/MIT-KaTeX.txt", path.join(webRoot, "licenses/MIT-KaTeX.txt")],
  ["licenses/katex-fonts.json", path.join(webRoot, "scripts/katex-fonts.json")],
  ["licenses/OFL-1.1.txt", path.join(webRoot, "licenses/OFL-1.1.txt")],
]);

const FIRST_PARTY_FILES = new Map([
  ["server/access-identity-bound.mjs", path.join(webRoot, "server/access-identity-bound.mjs")],
  ["server/node-adapter.mjs", path.join(webRoot, "server/node-adapter.mjs")],
]);

const EXPECTED_FONT_COMPONENTS = new Map([
  [
    "9b6f5ff45b278c744b5f379a2c4ecbaf858a842b8eaf82ac8d21b699ca16c608",
    "https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwcGFWNOITd.woff2",
  ],
  [
    "5f3d6ad60f29d6cb708414ec6887163d63bf197377ef5417d2483ff31ace6c3b",
    "https://fonts.gstatic.com/s/geistmono/v6/or3nQ6H-1_WfwkMZI_qYFrcdmhHkjko.woff2",
  ],
]);

const REQUIRED_FILES = new Set([
  "client/manifest.webmanifest",
  "client/offline.html",
  "client/sw.js",
  "server/__vite_rsc_assets_manifest.js",
  "server/index.js",
  "server/ssr/__vite_rsc_assets_manifest.js",
  "server/ssr/index.js",
]);

const EXCLUDED_FILES = new Set([
  ".openai/hosting.json",
  "client/.assetsignore",
  "client/.vite/manifest.json",
  "client/_headers",
  "server/.vite/manifest.json",
  "server/image-config.json",
  "server/ssr/vinext-server.json",
  "server/vinext-externals.json",
  "server/vinext-server.json",
  "server/wrangler.json",
]);

const GENERATED_PACKAGE = Buffer.from(
  `${JSON.stringify({ private: true, type: "module" })}\n`,
  "utf8",
);

// A KaTeX face as the bundler emits it: the logical font name, the bundler's
// content hash, and the format. The digest behind the name is what decides the
// file (`assertFontComponents`); the hash in the filename is only a cache key.
const KATEX_ASSET = /^client\/assets\/(KaTeX_[A-Za-z0-9]+-[A-Za-z]+)-[A-Za-z0-9_-]{8,12}\.(ttf|woff|woff2)$/;
const CSS_ASSET_REFERENCE = /url\(\s*\/assets\/([A-Za-z0-9._-]+)\s*\)/g;
const EMBEDDED_FONT = /data:font\/(ttf|woff|woff2);base64,([A-Za-z0-9+/]+={0,2})/g;

function isSelected(relative) {
  if (REQUIRED_FILES.has(relative)) return true;
  if (KATEX_ASSET.test(relative)) return true;
  if (/^client\/assets\/[A-Za-z0-9._/-]+\.(?:css|js)$/.test(relative)) {
    return !relative.split("/").includes("..");
  }
  if (/^client\/icons\/cortex-(?:180|192|512)\.png$/.test(relative)) return true;
  if (/^server\/ssr\/assets\/[A-Za-z0-9._-]+\.js$/.test(relative)) return true;
  return false;
}

// Build residue, not payload: the SSR graph emits a copy of every stylesheet a
// client reference imports, and the browser is served the `client/assets/` copy
// through the manifest. The server copy is never read at runtime.
function isExcluded(relative) {
  return EXCLUDED_FILES.has(relative) ||
    /^server\/assets\/[A-Za-z0-9._-]+\.css$/.test(relative) ||
    /^server\/ssr\/assets\/[A-Za-z0-9._-]+\.css$/.test(relative) ||
    // The same residue for the font files that stylesheet references: the SSR
    // graph copies them, the browser is served the `client/assets/` copy, and a
    // server-side copy is never read at runtime. Only a KaTeX-shaped name is
    // dropped this way, so a stray font of any other origin is still refused.
    /^server\/(?:ssr\/)?assets\/KaTeX_[A-Za-z0-9]+-[A-Za-z]+-[A-Za-z0-9_-]{8,12}\.(?:ttf|woff|woff2)$/.test(relative);
}

async function walk(root) {
  const files = [];
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const target = path.join(directory, entry.name);
      const details = await lstat(target);
      if (details.isSymbolicLink()) {
        throw new Error(`release build output contains a symlink: ${target}`);
      }
      if (details.isDirectory()) await visit(target);
      else if (details.isFile()) files.push(target);
      else throw new Error(`release build output contains a special file: ${target}`);
    }
  }
  await visit(root);
  return files;
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function assertTextSafety(relative, contents, options) {
  if (!/\.(?:css|html|js|json|mjs|webmanifest)$/.test(relative)) return;
  const text = contents.toString("utf8");
  for (const prefix of options.forbiddenPrefixes ?? []) {
    if (prefix && text.includes(prefix)) {
      throw new Error(`release payload contains an absolute build path in ${relative}`);
    }
  }
  for (const secret of options.secretValues ?? []) {
    if (secret && text.includes(secret)) {
      throw new Error(`release payload contains a build-time secret in ${relative}`);
    }
  }
  if (text.includes("CORTEX_DEV_ORIGIN")) {
    throw new Error(`release payload contains the retired development-origin boundary in ${relative}`);
  }
  if (/sourceMappingURL=|\bfile:\/\//.test(text)) {
    throw new Error(`release payload contains source-map or file-URL metadata in ${relative}`);
  }
  if (/(?:^|["'(=])(?:\/Users\/|\/home\/|\/private\/tmp\/|\/tmp\/|[A-Za-z]:\\Users\\)/.test(text)) {
    throw new Error(`release payload contains a local filesystem path in ${relative}`);
  }
  if (relative.startsWith("client/") &&
      /CORTEX_(?:ACCESS_BOOTSTRAP_TOKEN|CONTROL_TOKEN|WEB_DRAFT_SECRET)|X-Cortex-(?:Access-Bootstrap|Control-Token|Local-Proof)/i.test(text)) {
    throw new Error(`client payload contains a server-only boundary in ${relative}`);
  }
  // `require`/`createRequire` are refused too, but on the token stream rather
  // than on this text: see `assertNoCommonJsRequire`.
}

function isServerModule(relative) {
  return relative.startsWith("server/") && /\.m?js$/.test(relative);
}

// The one reviewed parser, loaded from its digest-pinned bytes -- the same file
// the analyser subprocess is handed, never a package resolved out of some
// `node_modules`.
async function pinnedParser() {
  const acorn = path.join(analyserRoot, ...ACORN_RELATIVE.split("/"));
  const details = await lstat(acorn);
  if (details.isSymbolicLink() || !details.isFile()) {
    throw new Error("release payload module analyser is missing");
  }
  if (sha256(await readFile(acorn)) !== ACORN_SHA256) {
    throw new Error("release payload module parser digest mismatch");
  }
  return { path: acorn, module: await import(pathToFileURL(acorn).href) };
}

// Every node of a parsed module, iteratively: a bundled server chunk nests far
// enough that a recursive walk is a stack overflow waiting for the next build.
function* moduleNodes(root) {
  const stack = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (Array.isArray(node)) {
      for (const item of node) if (item && typeof item === "object") stack.push(item);
      continue;
    }
    if (typeof node?.type !== "string") continue;
    yield node;
    for (const key of Object.keys(node)) {
      const value = node[key];
      if (value && typeof value === "object") stack.push(value);
    }
  }
}

// `require` and `createRequire` however they are written as code: a plain
// reference, a property (`module.createRequire`), or a computed property whose
// key is a literal string. A name that only ever appears inside a string, a
// template or a comment is not a node of this shape at all.
const COMMONJS_NAMES = new Set(["require", "createRequire"]);

function namesCommonJsRequire(node) {
  if (node.type === "Identifier") return COMMONJS_NAMES.has(node.name);
  if (node.type === "MemberExpression" && node.computed) {
    return node.property?.type === "Literal" && COMMONJS_NAMES.has(node.property.value);
  }
  return false;
}

// The pinned analyser reports import and export declarations and `import()`; it
// has no `require()` fact to report, so `createRequire(import.meta.url)` would
// carry an external reference straight past the closure decision below. The
// server payload is ESM and makes no such call, so any of these is refused --
// but on the parse tree, like every other closure decision here, and through the
// same pinned parser.
//
// It is the tree and not the token stream, because a token stream cannot be read
// without a parser's context: `a = b / require(/x/)` and a regular expression
// that merely contains `require(` are the same tokens until something decides
// whether a slash opened a literal or divided. It is not the text either -- a
// bundled dependency's JSDoc example (`const parse5 = require('parse5')`, which
// the maths pipeline ships through parse5) is inert prose, and a string that
// quotes the call is data.
async function assertNoCommonJsRequire(sources) {
  const { module: acorn } = await pinnedParser();
  for (const { path: relative, source } of sources) {
    let tree;
    try {
      tree = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
    } catch {
      throw new Error(`release server module could not be parsed: ${relative}`);
    }
    for (const node of moduleNodes(tree)) {
      if (namesCommonJsRequire(node)) {
        throw new Error(`release payload contains a CommonJS require in ${relative}`);
      }
    }
  }
}

// Run the pinned analyser exactly as the distribution gate runs it: a closed
// environment, a fixed argv, and both streams through temporary files so a
// large report cannot deadlock against the request or be read into memory
// before its size has been checked.
async function analyseModules(sources) {
  const { path: acorn } = await pinnedParser();
  const analyser = path.join(analyserRoot, ANALYSER_RELATIVE);
  const analyserDetails = await lstat(analyser);
  if (analyserDetails.isSymbolicLink() || !analyserDetails.isFile()) {
    throw new Error("release payload module analyser is missing");
  }
  const request = JSON.stringify({ files: sources });
  const cacheKey = sha256(request);
  const cached = analysisCache.get(cacheKey);
  if (cached) return cached;
  const scratch = await mkdtemp(path.join(os.tmpdir(), "cortex-release-payload-analysis-"));
  let stdout;
  try {
    const requestPath = path.join(scratch, "request.json");
    const reportPath = path.join(scratch, "report.json");
    await writeFile(requestPath, request, { flag: "wx", mode: 0o600 });
    await writeFile(reportPath, "", { flag: "wx", mode: 0o600 });
    const requestHandle = await open(requestPath, "r");
    const reportHandle = await open(reportPath, "r+");
    try {
      await new Promise((resolve, reject) => {
        const child = spawn(process.execPath, ["--no-warnings", analyser, acorn], {
          env: { HOME: "", LANG: "C", LC_ALL: "C", NODE_OPTIONS: "", NODE_PATH: "", PATH: "" },
          stdio: [requestHandle.fd, reportHandle.fd, "ignore"],
          timeout: ANALYSER_TIMEOUT,
        });
        child.once("error", reject);
        child.once("close", (code) => code === 0
          ? resolve()
          : reject(new Error("release payload module analyser failed")));
      });
    } finally {
      await requestHandle.close();
      await reportHandle.close();
    }
    const details = await lstat(reportPath);
    if (details.size > ANALYSER_OUTPUT_LIMIT) {
      throw new Error("release payload module analyser output is oversized");
    }
    stdout = await readFile(reportPath, "utf8");
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
  let report;
  try {
    report = JSON.parse(stdout);
  } catch {
    throw new Error("release payload module analyser output is malformed");
  }
  if (!Array.isArray(report?.files) || report.files.length !== sources.length) {
    throw new Error("release payload module analyser output is malformed");
  }
  const facts = new Map();
  for (const [index, file] of report.files.entries()) {
    if (file?.path !== sources[index].path) {
      throw new Error("release payload module analyser output is malformed");
    }
    facts.set(file.path, file);
  }
  analysisCache.set(cacheKey, facts);
  return facts;
}

// Every module reference the payload's server graph actually makes -- static
// import, dynamic `import()`, and `export ... from` alike -- must resolve inside
// the payload or to a Node built-in. A specifier the parser cannot read as a
// literal is refused rather than guessed at.
export async function assertServerModuleClosure(entries) {
  const sources = [];
  for (const entry of entries) {
    if (!isServerModule(entry.path)) continue;
    if (entry.contents.byteLength > ANALYSER_SOURCE_LIMIT) {
      throw new Error(`release server module is oversized: ${entry.path}`);
    }
    sources.push({ path: entry.path, source: entry.contents.toString("utf8") });
  }
  if (sources.length === 0) return;
  await assertNoCommonJsRequire(sources);
  const facts = await analyseModules(sources);
  for (const { path: relative } of sources) {
    const file = facts.get(relative);
    if (file.error) {
      throw new Error(`release server module could not be parsed: ${relative}`);
    }
    if (!Array.isArray(file.references) || !Array.isArray(file.nonLiteralReferences)) {
      throw new Error("release payload module analyser output is malformed");
    }
    if (file.nonLiteralReferences.length > 0) {
      throw new Error(`release server has a computed module specifier in ${relative}`);
    }
    for (const reference of file.references) {
      const specifier = reference?.specifier;
      if (typeof specifier !== "string") {
        throw new Error("release payload module analyser output is malformed");
      }
      if (!specifier.startsWith("./") &&
          !specifier.startsWith("../") &&
          !specifier.startsWith("node:")) {
        throw new Error(`release server has a bare external import: ${specifier}`);
      }
    }
  }
}

async function firstPartyEntries(options) {
  const entries = [];
  for (const [relative, defaultFilename] of FIRST_PARTY_FILES) {
    const filename = options.sourceRoot
      ? path.join(options.sourceRoot, ...relative.split("/"))
      : defaultFilename;
    const details = await lstat(filename);
    if (details.isSymbolicLink() || !details.isFile()) {
      throw new Error(`release first-party input is unsafe: ${relative}`);
    }
    const contents = await readFile(filename);
    assertTextSafety(relative, contents, options);
    entries.push({
      contents,
      path: relative,
      sha256: sha256(contents),
      size: contents.byteLength,
    });
  }
  return entries;
}

// Every font byte the browser can reach, whether it arrives as a file or inside
// a stylesheet, is identified against a reviewed digest: the two Geist subsets
// this payload has always embedded, and the pinned KaTeX faces the maths
// renderer needs. Unknown or altered bytes are refused, a face may arrive only
// once, and a payload that carries any KaTeX face must carry the whole pinned
// set -- a half-shipped family renders maths in a substituted font.
function assertFontComponents(selected, katex) {
  const katexByDigest = new Map(Object.entries(katex.fonts).map(([name, digest]) => [digest, name]));
  const observedKatex = new Map();
  const claim = (name, source) => {
    if (observedKatex.has(name)) {
      throw new Error(`release payload contains ${name} twice: ${observedKatex.get(name)} and ${source}`);
    }
    observedKatex.set(name, source);
  };

  for (const entry of selected) {
    const match = KATEX_ASSET.exec(entry.path);
    if (!match) continue;
    const name = `${match[1]}.${match[2]}`;
    const expected = katex.fonts[name];
    if (!expected) throw new Error(`release payload contains an unpinned KaTeX font: ${name}`);
    if (entry.sha256 !== expected) {
      throw new Error(`release payload contains an unapproved font component: ${entry.path}`);
    }
    claim(name, entry.path);
  }

  const stylesheets = selected
    .filter((entry) => entry.path.startsWith("client/assets/") && entry.path.endsWith(".css"));
  const css = stylesheets.map((entry) => entry.contents.toString("utf8")).join("\n");
  const observedGeist = [];
  for (const [, , encoded] of css.matchAll(EMBEDDED_FONT)) {
    const decoded = Buffer.from(encoded, "base64");
    if (decoded.toString("base64") !== encoded) {
      throw new Error("release payload contains a non-canonical embedded font");
    }
    const digest = sha256(decoded);
    if (EXPECTED_FONT_COMPONENTS.has(digest)) observedGeist.push(digest);
    else if (katexByDigest.has(digest)) claim(katexByDigest.get(digest), "an embedded stylesheet font");
    else throw new Error("release payload contains an unapproved embedded font component");
  }
  assertExactSet(observedGeist, EXPECTED_FONT_COMPONENTS.keys(), "embedded font component");

  if (observedKatex.size > 0) {
    const missing = Object.keys(katex.fonts).filter((name) => !observedKatex.has(name));
    if (missing.length > 0) {
      throw new Error(`release payload is missing pinned KaTeX fonts: ${missing.join(", ")}`);
    }
  }

  // A stylesheet that points at an asset the payload does not carry is a font
  // the browser would fetch from nowhere -- or, worse, a build that dropped the
  // file while keeping the reference.
  const carried = new Set(selected.map((entry) => entry.path));
  for (const entry of stylesheets) {
    const text = entry.contents.toString("utf8");
    for (const [, asset] of text.matchAll(CSS_ASSET_REFERENCE)) {
      if (!carried.has(`client/assets/${asset}`)) {
        throw new Error(`release payload stylesheet ${entry.path} references a missing asset: ${asset}`);
      }
    }
    if (/url\(\s*["']?(?:https?:)?\/\//.test(text)) {
      throw new Error(`release payload stylesheet ${entry.path} fetches a remote resource`);
    }
  }
  return observedKatex;
}

async function thirdPartyEntries(selected, sourceRoot, katex) {
  assertFontComponents(selected, katex);

  const entries = [];
  for (const [relative, defaultFilename] of THIRD_PARTY_FILES) {
    const filename = sourceRoot
      ? path.join(sourceRoot, ...relative.split("/"))
      : defaultFilename;
    const contents = relative === "licenses/katex-fonts.json"
      ? Buffer.from(`${JSON.stringify(katex, null, 2)}\n`, "utf8")
      : await readFile(filename);
    entries.push({
      contents,
      path: relative,
      sha256: sha256(contents),
      size: contents.byteLength,
    });
  }
  const notices = entries
    .find((entry) => entry.path === "THIRD_PARTY_NOTICES.md")
    .contents.toString("utf8");
  const license = entries
    .find((entry) => entry.path === "licenses/OFL-1.1.txt")
    .contents.toString("utf8");
  if (!license.includes("Copyright 2024 The Geist Project Authors") ||
      !license.includes("SIL OPEN FONT LICENSE Version 1.1")) {
    throw new Error("release payload is missing the OFL-1.1 license text");
  }
  for (const [digest, source] of EXPECTED_FONT_COMPONENTS) {
    if (!notices.includes(digest) || !notices.includes(source)) {
      throw new Error("third-party notices do not identify every embedded font component");
    }
  }

  // The KaTeX licence travels with the fonts, byte for byte as upstream ships
  // it, and the notices carry the digest of every face the pinned manifest
  // admits -- so the manifest, the shipped licence and the published notices
  // cannot drift apart across an upgrade.
  const katexLicense = entries.find((entry) => entry.path === katex.license_file);
  if (!katexLicense || katexLicense.sha256 !== katex.license_sha256) {
    throw new Error("release payload does not carry the pinned KaTeX MIT license bytes");
  }
  const katexLicenseText = katexLicense.contents.toString("utf8");
  if (!katexLicenseText.includes("The MIT License (MIT)") ||
      !katexLicenseText.includes("Khan Academy")) {
    throw new Error("release payload is missing the KaTeX MIT license text");
  }
  if (!notices.includes(`${katex.package}@${katex.version}`) || !notices.includes(katex.upstream)) {
    throw new Error("third-party notices do not identify the pinned KaTeX release");
  }
  for (const [name, digest] of Object.entries(katex.fonts)) {
    if (!notices.includes(name) || !notices.includes(digest)) {
      throw new Error("third-party notices do not identify every pinned KaTeX font component");
    }
  }
  return entries;
}

function assertExactSet(observed, expectedIterable, label) {
  const expected = new Set(expectedIterable);
  if (observed.length !== expected.size || new Set(observed).size !== expected.size) {
    throw new Error(`release payload must contain exactly ${expected.size} ${label}s`);
  }
  for (const value of observed) {
    if (!expected.has(value)) throw new Error(`release payload contains an unapproved ${label}`);
  }
}

export async function inspectReleasePayload(distRoot, options = {}) {
  const root = path.resolve(distRoot);
  const rootDetails = await lstat(root);
  if (rootDetails.isSymbolicLink() || !rootDetails.isDirectory()) {
    throw new Error("release build output root must be a real directory");
  }
  const selected = [];
  const observedRequired = new Set();
  for (const filename of await walk(root)) {
    const relative = path.relative(root, filename).split(path.sep).join("/");
    if (relative.startsWith("../") || path.isAbsolute(relative)) {
      throw new Error("release build output escaped its root");
    }
    if (isSelected(relative)) {
      const contents = await readFile(filename);
      assertTextSafety(relative, contents, options);
      selected.push({
        contents,
        path: relative,
        sha256: sha256(contents),
        size: contents.byteLength,
      });
      if (REQUIRED_FILES.has(relative)) observedRequired.add(relative);
    } else if (!isExcluded(relative)) {
      throw new Error(`release build output contains an unexpected file: ${relative}`);
    }
  }
  for (const required of REQUIRED_FILES) {
    if (!observedRequired.has(required)) {
      throw new Error(`release build output is missing ${required}`);
    }
  }
  const externals = JSON.parse(
    await readFile(path.join(root, "server/vinext-externals.json"), "utf8"),
  );
  if (!Array.isArray(externals) || externals.length !== 0) {
    throw new Error("release server has runtime npm externals");
  }
  selected.push(...await firstPartyEntries(options));
  // Decided over the whole server graph at once, after every module the payload
  // will actually ship is in hand: one parse of one complete set, rather than a
  // per-file verdict that could not see an `export ... from` in a sibling.
  await assertServerModuleClosure(selected);
  selected.push(...await thirdPartyEntries(selected, options.sourceRoot, await readKatexFontManifest()));
  selected.push({
    contents: GENERATED_PACKAGE,
    path: "package.json",
    sha256: sha256(GENERATED_PACKAGE),
    size: GENERATED_PACKAGE.byteLength,
  });
  selected.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return selected;
}

export function releasePayloadLedger(entries) {
  return `${entries.map((entry) => `${entry.sha256}  ${entry.size}  ${entry.path}`).join("\n")}\n`;
}

export async function writeReleasePayload(entries, destination) {
  const root = path.resolve(destination);
  await mkdir(root, { recursive: false, mode: 0o700 });
  for (const entry of entries) {
    const components = entry.path.split("/");
    if (path.isAbsolute(entry.path) || components.some((part) => !part || part === "." || part === "..")) {
      throw new Error(`release payload entry has an unsafe path: ${entry.path}`);
    }
    const target = path.join(root, ...components);
    await mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
    await writeFile(target, entry.contents, { flag: "wx", mode: 0o600 });
  }
}
