import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const verifier = path.join(webRoot, "scripts/verify-canonical-fixtures.mjs");
const baseline = JSON.parse(
  await readFile(path.join(webRoot, "app/mock/fixtures/productization-v0.1.snapshot.json"), "utf8"),
);

const mutations = [
  ["G0 persisted control", (value) => value.g0.persisted_control.event_ids.pop()],
  ["G1 artifact version", (value) => { value.g1.artifacts[0].versions[1].sha256 = "tampered"; }],
  ["G1 immutable snapshot", (value) => { value.g1.immutable_snapshot.versions[0].sha256 = "tampered"; }],
  ["G1 event", (value) => { value.g1.events[24].type = "run.tampered"; }],
  ["G1 lineage", (value) => { value.g1.lineage.links[0].to_node_id = "tampered"; }],
  ["G1 thread", (value) => { value.g1.threads[0].title = "tampered"; }],
];

for (const [label, mutate] of mutations) {
  test(`rejects drift in ${label}`, async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "cortex-ui-fixture-"));
    try {
      const candidate = structuredClone(baseline);
      mutate(candidate);
      const candidatePath = path.join(directory, "snapshot.json");
      await writeFile(candidatePath, `${JSON.stringify(candidate, null, 2)}\n`);
      const result = spawnSync(process.execPath, [verifier, "--snapshot", candidatePath], {
        cwd: webRoot,
        encoding: "utf8",
      });
      assert.notEqual(result.status, 0, `${label} mutation unexpectedly passed`);
      assert.match(`${result.stdout}${result.stderr}`, /complete canonical UI projection/i);
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
}
