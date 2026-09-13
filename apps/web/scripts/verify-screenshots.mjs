import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyScreenshotArtifacts } from "./screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const screenshotRoot = path.join(webRoot, "artifacts/screenshots");
const spec = JSON.parse(await readFile(path.join(screenshotRoot, "capture-spec.json"), "utf8"));
const imageBuffers = new Map(
  await Promise.all(
    Object.keys(spec.screenshots).map(async (filename) => [
      filename,
      await readFile(path.join(screenshotRoot, filename)),
    ]),
  ),
);

verifyScreenshotArtifacts(spec, imageBuffers);
console.log("Scenario URLs, PNG fingerprints, capture geometry, and visible columns verified.");
