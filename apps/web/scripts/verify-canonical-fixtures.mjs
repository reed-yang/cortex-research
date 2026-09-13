import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { access, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { projectCanonicalSnapshot } from "./project-canonical-snapshot.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(webRoot, "../..");
const canonical = {
  commit: "8a9c32c",
  integrationRef: "codex/productization-integration",
  adrPath: "docs/adr/0005-golden-mutation-scope-and-causality.md",
  adrDecisionIds: ["PROD-AUDIT-002", "PROD-EVENT-CAUSALITY-001"],
  g0Path: "cortex_platform/tests/fixtures/productization/g0-source-conflict.json",
  g1Path: "cortex_platform/tests/fixtures/productization/g1-keep-both-successor.json",
};

const snapshotFlag = process.argv.indexOf("--snapshot");
const snapshotPath =
  snapshotFlag >= 0
    ? path.resolve(process.argv[snapshotFlag + 1])
    : path.join(webRoot, "app/mock/fixtures/productization-v0.1.snapshot.json");

// The canonical inputs are tracked files in this checkout. There used to be a
// `git show <integrationRef>:<path>` fallback here for the case where they were
// absent; that ref is an old-repository branch name, so in any other clone the
// fallback failed with a git error that named neither the missing file nor what
// to do about it. Absence is now its own message: these three paths have to be
// part of the checkout for this check to mean anything.
async function canonicalText(relativePath) {
  const localPath = path.join(repoRoot, relativePath);
  try {
    await access(localPath);
  } catch {
    throw new Error(
      `canonical input is missing from this checkout: ${relativePath}. ` +
        "It is a tracked file; restore it rather than regenerating the snapshot.",
    );
  }
  return readFile(localPath, "utf8");
}

const digest = (text) => createHash("sha256").update(text).digest("hex");
const g0Text = await canonicalText(canonical.g0Path);
const g1Text = await canonicalText(canonical.g1Path);
const expected = {
  origin: {
    canonical_commit: canonical.commit,
    integration_ref: canonical.integrationRef,
    adr_path: canonical.adrPath,
    adr_decision_ids: canonical.adrDecisionIds,
    files: {
      g0: { path: canonical.g0Path, sha256: digest(g0Text) },
      g1: { path: canonical.g1Path, sha256: digest(g1Text) },
    },
  },
  ...projectCanonicalSnapshot(JSON.parse(g0Text), JSON.parse(g1Text)),
};

if (process.argv.includes("--write")) {
  await writeFile(snapshotPath, `${JSON.stringify(expected, null, 2)}\n`);
  console.log(`Canonical UI snapshot written to ${snapshotPath}.`);
  process.exit(0);
}

const snapshot = JSON.parse(await readFile(snapshotPath, "utf8"));

assert.deepEqual(
  snapshot,
  expected,
  "UI-local snapshot must equal the complete canonical UI projection",
);

const adr = await canonicalText(canonical.adrPath);
for (const decisionId of canonical.adrDecisionIds) {
  assert.match(adr, new RegExp(`\\b${decisionId}\\b`));
}

console.log("Complete canonical UI snapshot projection and ADR0005 scopes verified.");
