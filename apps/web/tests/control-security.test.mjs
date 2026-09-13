import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function textTree(root) {
  const values = [];
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) values.push(await textTree(target));
    else values.push(await readFile(target, "utf8").catch(() => ""));
  }
  return values.join("\n");
}

test("client output contains no daemon-token boundary", async () => {
  const clientOutput = await textTree(path.join(webRoot, "dist/client"));
  assert.doesNotMatch(
    clientOutput,
    /CORTEX_CONTROL_TOKEN|X-Cortex-Control-Token|CORTEX_ACCESS_BOOTSTRAP_TOKEN|X-Cortex-Access-Bootstrap|test-only-control-token/,
  );
});

test("browser source stores only the opaque event cursor", async () => {
  const controlSource = await textTree(path.join(webRoot, "app/control"));
  assert.doesNotMatch(controlSource, /CORTEX_CONTROL_TOKEN|X-Cortex-Control-Token/);
  const storageCalls = [...controlSource.matchAll(/(?:localStorage|\.storage\?\.)[^\n]*/g)].map((match) => match[0]);
  assert.ok(storageCalls.length > 0);
  assert.ok(storageCalls.every((line) => !/token|message|decision|artifact/i.test(line)));
  assert.match(controlSource, /cortex\.control\.event-cursor\.v1/);
});

test("research workflow client source keeps credentials and private paths server-side", async () => {
  const workflowSource = await Promise.all([
    "app/control/research-contracts.ts",
    "app/control/research-workflow.tsx",
    "app/control/client.ts",
  ].map((relative) => readFile(path.join(webRoot, relative), "utf8")));
  const combined = workflowSource.join("\n");
  assert.doesNotMatch(combined, /CORTEX_CONTROL_TOKEN|X-Cortex-Control-Token|CORTEX_ACCESS_BOOTSTRAP_TOKEN/);
  assert.doesNotMatch(combined, /(?:localStorage|sessionStorage).*?(?:artifact|decision|message|token)/i);
  assert.doesNotMatch(workflowSource[1], /resource_uri/);
});

test("service worker excludes private control responses", async () => {
  const worker = await readFile(path.join(webRoot, "public/sw.js"), "utf8");
  assert.match(worker, /event\.request\.mode !== "navigate"/);
  assert.doesNotMatch(worker, /api\/|decisions|artifacts|events/i);
});

test("access boundary executes before daemon-token configuration", async () => {
  const gateway = await readFile(
    path.join(webRoot, "app/api/cortex/[...path]/route.ts"),
    "utf8",
  );
  const boundaryCall = gateway.indexOf(
    "const accessBoundary = verifyAccessBoundary(request, method)",
  );
  const upstreamCall = gateway.indexOf(
    "const configuration = upstreamConfiguration()",
  );
  assert.ok(boundaryCall >= 0);
  assert.ok(upstreamCall > boundaryCall);
  assert.doesNotMatch(gateway, /browserOrigin|requestUrl\.protocol/);
});

test("server boundary source has no credential-bearing diagnostics", async () => {
  const serverBoundary = await textTree(path.join(webRoot, "app/api/cortex"));
  assert.doesNotMatch(
    serverBoundary,
    /console\.(?:debug|error|info|log|warn)\s*\(/,
  );
  assert.doesNotMatch(serverBoundary, /DEBUG_HEADERS|JSON\.stringify\(request\.headers/);
});
