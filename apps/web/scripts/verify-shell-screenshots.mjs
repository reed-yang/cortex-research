// The committed shell evidence, checked without a browser: every declared shot
// exists at its declared digest and geometry, the folder holds nothing else,
// and the ledger covers exactly the scenes the contract names.

import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { decodeRgbPng, sha256 } from "./screenshot-contract.mjs";
import { expectedShots, VIEWPORTS } from "./shell-screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const shellRoot = path.join(webRoot, "artifacts/shell");
const spec = JSON.parse(await readFile(path.join(shellRoot, "capture-spec.json"), "utf8"));
const shots = expectedShots();

assert.deepEqual(
  Object.keys(spec.screenshots).sort(),
  shots.map((shot) => shot.filename).sort(),
  "the ledger and the scene contract name different shots",
);
assert.deepEqual(
  (await readdir(shellRoot)).filter((name) => name.endsWith(".png")).sort(),
  shots.map((shot) => shot.filename).sort(),
  "artifacts/shell holds an undeclared or missing PNG",
);

for (const { filename, scene, scheme, viewport } of shots) {
  const entry = spec.screenshots[filename];
  assert.equal(entry.scene, scene.name, `${filename} names another scene`);
  assert.equal(entry.scheme, scheme);
  assert.equal(entry.viewport, viewport);
  const url = new URL(entry.url);
  assert.equal(url.origin, spec.base_origin);
  assert.equal(url.pathname, "/");
  assert.equal(url.search, `?${scene.query}`, `${filename} scene URL drifted`);
  assert.match(entry.sha256, /^[0-9a-f]{64}$/);
  const buffer = await readFile(path.join(shellRoot, filename));
  assert.equal(sha256(buffer), entry.sha256, `${filename} content fingerprint drifted`);
  const image = decodeRgbPng(buffer);
  const geometry = VIEWPORTS[viewport];
  assert.equal(image.width, geometry.viewport.width * geometry.deviceScaleFactor, `${filename} width drifted`);
  assert.equal(image.height, geometry.viewport.height * geometry.deviceScaleFactor, `${filename} height drifted`);
}

console.log(`${shots.length} shell screenshots verified against artifacts/shell/capture-spec.json.`);
