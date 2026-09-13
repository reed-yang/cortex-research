import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  decodeRgbPng,
  encodeRgbPng,
  verifyScreenshotArtifacts,
} from "../scripts/screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const screenshotRoot = path.join(webRoot, "artifacts/screenshots");
const spec = JSON.parse(await readFile(path.join(screenshotRoot, "capture-spec.json"), "utf8"));
const images = new Map(
  await Promise.all(
    Object.keys(spec.screenshots).map(async (filename) => [
      filename,
      await readFile(path.join(screenshotRoot, filename)),
    ]),
  ),
);

test("accepts the independently captured scenario artifacts", () => {
  assert.doesNotThrow(() => verifyScreenshotArtifacts(spec, images));
});

test("rejects G0 copied and cropped to the G1 geometry", () => {
  const g0 = decodeRgbPng(images.get("g0-source-conflict.png"));
  const target = spec.screenshots["g1-successor-lineage.png"];
  const cropped = encodeRgbPng({
    width: target.width,
    height: target.height,
    stride: g0.stride,
    pixels: g0.pixels.subarray(0, target.height * g0.stride),
  });
  const tampered = new Map(images);
  tampered.set("g1-successor-lineage.png", cropped);
  assert.throws(
    () => verifyScreenshotArtifacts(spec, tampered),
    /g1-successor-lineage\.png content fingerprint drifted/,
  );
});

test("rejects the two scenario images when they are swapped", () => {
  const tampered = new Map(images);
  tampered.set("g0-source-conflict.png", images.get("g1-successor-lineage.png"));
  tampered.set("g1-successor-lineage.png", images.get("g0-source-conflict.png"));
  assert.throws(() => verifyScreenshotArtifacts(spec, tampered), /content fingerprint drifted/);
});

test("rejects a tampered artifact hash", () => {
  const tampered = structuredClone(spec);
  tampered.screenshots["g0-source-conflict.png"].sha256 = "0".repeat(64);
  assert.throws(() => verifyScreenshotArtifacts(tampered, images), /content fingerprint drifted/);
});

test("rejects duplicate scenario URLs", () => {
  const tampered = structuredClone(spec);
  tampered.screenshots["g1-successor-lineage.png"].url =
    tampered.screenshots["g0-source-conflict.png"].url;
  assert.throws(() => verifyScreenshotArtifacts(tampered, images));
});
