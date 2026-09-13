// The reviewed, version-pinned identity of every KaTeX font the release payload
// may carry, plus the upstream licence that must travel with them.
//
// The payload inspector must be able to decide a build with no `node_modules`
// and no network at all, so the digests live in `scripts/katex-fonts.json` --
// source, reviewed once per upgrade -- and never in the installed package. This
// module is the only reader of that file and the only writer of it:
// `node scripts/katex-font-manifest.mjs --write` re-derives it from the pinned
// `katex` in this checkout, and `tests/release-build.test.mjs` fails whenever
// the file and the installed package disagree, so an upgrade cannot land with
// stale digests.

import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MANIFEST_PATH = path.join(webRoot, "scripts/katex-fonts.json");
const FONT_NAME = /^KaTeX_[A-Za-z0-9]+-[A-Za-z]+\.(?:ttf|woff|woff2)$/;
const DIGEST = /^[0-9a-f]{64}$/;

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

// Derived from the installed package, so an upgrade is a regeneration and a
// review of one file rather than an edit spread over the inspector.
export async function derivePinnedKatexFonts(katexRoot = path.join(webRoot, "node_modules/katex")) {
  const manifest = JSON.parse(await readFile(path.join(katexRoot, "package.json"), "utf8"));
  const licence = await readFile(path.join(katexRoot, "LICENSE"));
  const fontRoot = path.join(katexRoot, "dist/fonts");
  const fonts = {};
  for (const entry of (await readdir(fontRoot)).sort()) {
    if (!FONT_NAME.test(entry)) throw new Error(`katex ships an unexpected font file: ${entry}`);
    fonts[entry] = sha256(await readFile(path.join(fontRoot, entry)));
  }
  return {
    package: manifest.name,
    version: manifest.version,
    license: manifest.license,
    license_file: "licenses/MIT-KaTeX.txt",
    license_sha256: sha256(licence),
    upstream: "https://github.com/KaTeX/KaTeX",
    regenerate: "node scripts/katex-font-manifest.mjs --write",
    fonts,
  };
}

export function assertKatexFontManifest(manifest) {
  if (manifest?.package !== "katex" || typeof manifest.version !== "string") {
    throw new Error("KaTeX font manifest does not identify a pinned katex version");
  }
  if (manifest.license !== "MIT" || !DIGEST.test(manifest.license_sha256 ?? "")) {
    throw new Error("KaTeX font manifest does not pin the MIT licence bytes");
  }
  const fonts = Object.entries(manifest.fonts ?? {});
  if (fonts.length === 0) throw new Error("KaTeX font manifest pins no font");
  for (const [name, digest] of fonts) {
    if (!FONT_NAME.test(name)) throw new Error(`KaTeX font manifest names an unusable font: ${name}`);
    if (!DIGEST.test(digest)) throw new Error(`KaTeX font manifest has no digest for ${name}`);
  }
  if (new Set(Object.values(manifest.fonts)).size !== fonts.length) {
    throw new Error("KaTeX font manifest pins one digest twice");
  }
  return manifest;
}

export async function readKatexFontManifest(filename = MANIFEST_PATH) {
  return assertKatexFontManifest(JSON.parse(await readFile(filename, "utf8")));
}

export function katexManifestPath() {
  return MANIFEST_PATH;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const derived = assertKatexFontManifest(await derivePinnedKatexFonts());
  if (process.argv.includes("--write")) {
    await writeFile(MANIFEST_PATH, `${JSON.stringify(derived, null, 2)}\n`);
    console.log(`Pinned ${Object.keys(derived.fonts).length} ${derived.package}@${derived.version} fonts.`);
  } else {
    const current = await readKatexFontManifest();
    const same = JSON.stringify(current) === JSON.stringify(derived);
    if (!same) throw new Error("scripts/katex-fonts.json does not match the installed katex; rerun with --write");
    console.log(`${Object.keys(derived.fonts).length} ${derived.package}@${derived.version} font digests verified.`);
  }
}
