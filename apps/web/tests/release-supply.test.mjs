import assert from "node:assert/strict";
import { execFile as execFileCallback } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod,
  cp,
  link,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

import * as releaseSupply from "../scripts/release-supply.mjs";
import {
  acquireReleaseSupply,
  buildReleaseOffline,
  collectNodeRuntimeClosure,
  parseMachOLoadCommands,
  verifyReleaseSupply,
} from "../scripts/release-supply.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const executeFile = promisify(execFileCallback);
const BUILD_ID = "cortex-r0-supply-test";
const SOURCE_EXCLUSIONS = new Set([
  ".next",
  ".vinext",
  ".wrangler",
  "dist",
  "node_modules",
  "outputs",
  "tsconfig.tsbuildinfo",
  "work",
]);

async function npmCliFixture(root, version = "11.0.0-fixture", options = {}) {
  const packageRoot = path.join(root, "npm-package");
  const filename = path.join(packageRoot, "bin", "npm-cli-fixture.mjs");
  await mkdir(path.join(packageRoot, "bin"), { recursive: true });
  await mkdir(path.join(packageRoot, "lib"), { recursive: true });
  await mkdir(path.join(packageRoot, "node_modules", "@npmcli", "fixture"), { recursive: true });
  await writeFile(path.join(packageRoot, "package.json"), `${JSON.stringify({
    bin: { npm: options.bin ?? "bin/npm-cli-fixture.mjs" },
    name: options.name ?? "npm",
    version,
  }, null, 2)}\n`);
  await writeFile(
    path.join(packageRoot, "lib", "implementation.mjs"),
    `export const version = ${JSON.stringify(version)};\n`,
  );
  await writeFile(
    path.join(packageRoot, "node_modules", "@npmcli", "fixture", "index.mjs"),
    "export const fixture = true;\n",
  );
  await writeFile(
    filename,
    options.cliSource ??
      `import { version } from "../lib/implementation.mjs";\n` +
      `if (process.argv[2] !== "--version") process.exit(19);\n` +
      `process.stdout.write(\`${"${version}"}\\n\`);\n`,
    { mode: 0o700 },
  );
  return filename;
}

async function removeFixtureRoot(root) {
  async function makeWritable(directory) {
    let details;
    try {
      details = await lstat(directory);
    } catch (error) {
      if (error?.code === "ENOENT") return;
      throw error;
    }
    if (details.isSymbolicLink() || !details.isDirectory()) return;
    await chmod(directory, 0o700);
    for (const child of await readdir(directory, { withFileTypes: true })) {
      if (child.isDirectory()) await makeWritable(path.join(directory, child.name));
    }
  }

  await makeWritable(root);
  await rm(root, { recursive: true, force: true });
}

const sharedToolRoot = await mkdtemp(path.join(os.tmpdir(), "cortex-web-shared-tools-"));
const NPM_CLI_FIXTURE = await npmCliFixture(sharedToolRoot);
after(() => removeFixtureRoot(sharedToolRoot));

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function repinSupplyMetadata(root) {
  const metadataPath = path.join(root, "metadata.json");
  const ledgerPath = path.join(root, "checksums.jsonl");
  const completionPath = path.join(root, "complete.json");
  const metadataBytes = await readFile(metadataPath);
  const entries = (await readFile(ledgerPath, "utf8"))
    .trimEnd()
    .split("\n")
    .map((line) => JSON.parse(line));
  const metadataEntry = entries.find((entry) => entry.path === "metadata.json");
  assert.ok(metadataEntry);
  metadataEntry.sha256 = sha256(metadataBytes);
  metadataEntry.size = metadataBytes.byteLength;
  const ledgerBytes = Buffer.from(`${entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`, "utf8");
  await writeFile(ledgerPath, ledgerBytes);
  const supplyDigest = sha256(ledgerBytes);
  const completion = JSON.parse(await readFile(completionPath, "utf8"));
  completion.supply_sha256 = supplyDigest;
  await writeFile(completionPath, `${JSON.stringify(completion, null, 2)}\n`);
  return supplyDigest;
}

function machoPathCommand(command, value) {
  const encoded = Buffer.from(`${value}\0`, "utf8");
  const commandSize = Math.ceil((12 + encoded.byteLength) / 8) * 8;
  const result = Buffer.alloc(commandSize);
  result.writeUInt32LE(command, 0);
  result.writeUInt32LE(commandSize, 4);
  result.writeUInt32LE(12, 8);
  encoded.copy(result, 12);
  return result;
}

function thinMachOFixture(fileType, commands) {
  const commandBytes = Buffer.concat(commands);
  const result = Buffer.alloc(32 + commandBytes.byteLength);
  result.writeUInt32LE(0xfeedfacf, 0);
  result.writeInt32LE(process.arch === "arm64" ? 0x0100000c : 0x01000007, 4);
  result.writeUInt32LE(fileType, 12);
  result.writeUInt32LE(commands.length, 16);
  result.writeUInt32LE(commandBytes.byteLength, 20);
  commandBytes.copy(result, 32);
  return result;
}

async function sourceFixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "cortex-web-supply-source-"));
  t.after(() => removeFixtureRoot(root));
  await cp(webRoot, root, {
    recursive: true,
    filter(source) {
      const relative = path.relative(webRoot, source);
      return !SOURCE_EXCLUSIONS.has(relative.split(path.sep)[0]);
    },
  });
  await mkdir(path.join(root, "node_modules"));
  await writeFile(path.join(root, "node_modules", "borrowed-checkout-sentinel"), "must-not-be-read\n");
  return root;
}

async function writeDependencyFixture(root, marker = "sealed dependency\n") {
  await mkdir(path.join(root, "node_modules", ".bin"), { recursive: true });
  await mkdir(path.join(root, "node_modules", "vinext", "dist"), { recursive: true });
  await mkdir(path.join(root, "node_modules", "vite", "bin"), { recursive: true });
  await writeFile(
    path.join(root, "node_modules", "vinext", "package.json"),
    `${JSON.stringify({ bin: { vinext: "dist/cli.js" }, name: "vinext", version: "0.0.50" })}\n`,
  );
  await writeFile(
    path.join(root, "node_modules", "vite", "package.json"),
    `${JSON.stringify({ bin: { vite: "bin/vite.js" }, name: "vite", version: "8.1.5" })}\n`,
  );
  await writeFile(path.join(root, "node_modules", "vinext", "bin.mjs"), marker, { mode: 0o755 });
  await writeFile(
    path.join(root, "node_modules", "vinext", "dist", "cli.js"),
    "if (process.argv[2] !== 'build') process.exit(23);\n",
    { mode: 0o755 },
  );
  await writeFile(
    path.join(root, "node_modules", "vite", "bin", "vite.js"),
    "if (process.argv[2] !== 'build') process.exit(23);\n",
    { mode: 0o755 },
  );
  await symlink("../vinext/bin.mjs", path.join(root, "node_modules", ".bin", "vinext"));
}

async function acquireFixture(t, sourceRoot, label = "supply") {
  const parent = await mkdtemp(path.join(os.tmpdir(), `cortex-web-${label}-`));
  t.after(() => removeFixtureRoot(parent));
  const destination = path.join(parent, "sealed");
  let acquisitionRoot;
  const result = await acquireReleaseSupply({
    destination,
    installRunner: async (command, args, options) => {
      acquisitionRoot = options.cwd;
      assert.match(command, /sealed-runtime\/bin\/node$/);
      assert.match(args[0], /sealed-runtime\/npm\/bin\/npm-cli-fixture\.mjs$/);
      assert.deepEqual(args.slice(1, 3), ["ci", "--cache"]);
      assert.notEqual(options.cwd, sourceRoot);
      await assert.rejects(
        readFile(path.join(options.cwd, "node_modules", "borrowed-checkout-sentinel")),
        /ENOENT/,
      );
      assert.deepEqual(await readdir(options.env.npm_config_cache), []);
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath: NPM_CLI_FIXTURE,
    sourceRoot,
  });
  assert.notEqual(acquisitionRoot, sourceRoot);
  assert.equal((await lstat(destination)).isDirectory(), true);
  return { destination, result };
}

async function writeFakeDist(root, buildId) {
  const dist = path.join(root, "dist");
  for (const directory of [
    "client/assets",
    "server/ssr",
  ]) {
    await mkdir(path.join(dist, directory), { recursive: true });
  }
  await writeFile(path.join(dist, "client", "assets", "fonts.css"), await readFile(path.join(root, "app", "fonts.css")));
  await writeFile(path.join(dist, "client", "manifest.webmanifest"), "{}\n");
  await writeFile(path.join(dist, "client", "offline.html"), "<!doctype html><title>offline</title>\n");
  await writeFile(path.join(dist, "client", "sw.js"), "self.addEventListener('fetch', () => {});\n");
  await writeFile(path.join(dist, "server", "__vite_rsc_assets_manifest.js"), "export default {};\n");
  await writeFile(
    path.join(dist, "server", "index.js"),
    `const BUILD_ID = ${JSON.stringify(buildId)};\n` +
      "const DRAFT_SECRET = process.env.CORTEX_WEB_DRAFT_SECRET;\n" +
      "export default { fetch: async () => new Response(BUILD_ID + DRAFT_SECRET) };\n",
  );
  await writeFile(path.join(dist, "server", "ssr", "__vite_rsc_assets_manifest.js"), "export default {};\n");
  await writeFile(path.join(dist, "server", "ssr", "index.js"), "export default {};\n");
  await writeFile(path.join(dist, "server", "vinext-externals.json"), "[]\n");
}

test("acquisition seals a deterministic external dependency closure", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const first = await acquireFixture(t, sourceRoot, "supply-first");
  const second = await acquireFixture(t, sourceRoot, "supply-second");

  assert.match(first.result.supply_sha256, /^[0-9a-f]{64}$/);
  assert.equal(first.result.supply_sha256, second.result.supply_sha256);
  assert.equal(first.result.lock_sha256, sha256(await readFile(path.join(sourceRoot, "package-lock.json"))));

  const verified = await verifyReleaseSupply({
    expectedLockDigest: first.result.lock_sha256,
    expectedSupplyDigest: first.result.supply_sha256,
    supplyRoot: first.destination,
  });
  assert.equal(verified.supply_sha256, first.result.supply_sha256);
  assert.equal(verified.metadata.closure, "complete");
  assert.equal((await lstat(path.join(first.destination, "node_modules"))).isSymbolicLink(), false);
});

test("acquisition environment supports npm lifecycle shell execution", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-lifecycle-shell-"));
  t.after(() => removeFixtureRoot(parent));

  await acquireReleaseSupply({
    destination: path.join(parent, "sealed"),
    installRunner: async (_command, _args, options) => {
      const result = await executeFile("sh", ["-c", "printf lifecycle-shell"], {
        cwd: options.cwd,
        env: options.env,
      });
      assert.equal(result.stdout, "lifecycle-shell");
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath: NPM_CLI_FIXTURE,
    sourceRoot,
  });
});

test("supply verification rejects changed bytes, closure, lock, identity, and embedded tooling", async (t) => {
  const sourceRoot = await sourceFixture(t);

  for (const attack of ["bytes", "extra", "lock", "identity", "tooling", "node-tooling"]) {
    const fixture = await acquireFixture(t, sourceRoot, `supply-${attack}`);
    const options = {
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: fixture.result.supply_sha256,
      supplyRoot: fixture.destination,
    };
    if (attack === "bytes") {
      await writeFile(path.join(fixture.destination, "node_modules", "vinext", "bin.mjs"), "changed\n");
    } else if (attack === "extra") {
      await writeFile(path.join(fixture.destination, "node_modules", "extra"), "unexpected\n");
    } else if (attack === "lock") {
      options.expectedLockDigest = "0".repeat(64);
    } else if (attack === "identity") {
      options.expectedSupplyDigest = "1".repeat(64);
    } else if (attack === "tooling") {
      await chmod(path.join(fixture.destination, "tooling", "npm"), 0o700);
      await chmod(path.join(fixture.destination, "tooling", "npm", "lib"), 0o700);
      await chmod(
        path.join(fixture.destination, "tooling", "npm", "lib", "implementation.mjs"),
        0o600,
      );
      await writeFile(
        path.join(fixture.destination, "tooling", "npm", "lib", "implementation.mjs"),
        "export const version = '11.0.0-fixture';\n// changed tooling\n",
      );
    } else {
      const libraryRoot = path.join(fixture.destination, "tooling", "node", "lib");
      await chmod(path.join(fixture.destination, "tooling", "node"), 0o700);
      await mkdir(libraryRoot, { recursive: true, mode: 0o700 });
      await chmod(libraryRoot, 0o700);
      const library = (await readdir(libraryRoot)).sort()[0];
      if (library) await chmod(path.join(libraryRoot, library), 0o600);
      await writeFile(path.join(libraryRoot, library ?? "unexpected.dylib"), "changed private runtime library\n");
    }
    await assert.rejects(
      verifyReleaseSupply(options),
      /checksum ledger|lock digest|supply identity|npm package closure/,
    );
  }
});

test("acquisition rejects dependency links that escape the sealed root", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-unsafe-supply-"));
  t.after(() => removeFixtureRoot(parent));

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "sealed"),
      installRunner: async (_command, _args, options) => {
        await mkdir(path.join(options.cwd, "node_modules"));
        await symlink("../../outside", path.join(options.cwd, "node_modules", "escape"));
      },
      npmCliPath: NPM_CLI_FIXTURE,
      sourceRoot,
    }),
    /escapes the dependency root/,
  );
  await assert.rejects(lstat(path.join(parent, "sealed")), /ENOENT/);
});

test("offline build copies verified supply and records every exact build identity", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "offline-build-supply");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-offline-output-"));
  t.after(() => removeFixtureRoot(parent));
  const outputRoot = path.join(parent, "result");
  let isolatedRoot;

  const result = await buildReleaseOffline({
    buildId: BUILD_ID,
    buildRunner: async (command, args, options) => {
      isolatedRoot = options.cwd;
      assert.equal(command, "/usr/bin/sandbox-exec");
      assert.equal(args[2], "/usr/bin/env");
      assert.match(args[3], /^DYLD_LIBRARY_PATH=.*\/sealed-runtime\/lib$/);
      assert.match(args[4], /sealed-runtime\/bin\/node$/);
      assert.equal(
        args[5],
        await realpath(path.join(options.cwd, "node_modules", "vinext", "dist", "cli.js")),
      );
      assert.deepEqual(args.slice(6), ["build"]);
      assert.equal(options.env.DYLD_LIBRARY_PATH, args[3].slice("DYLD_LIBRARY_PATH=".length));
      assert.ok(!args.join("\0").includes("build-only-control-secret"));
      assert.notEqual(options.cwd, sourceRoot);
      assert.equal((await lstat(path.join(options.cwd, "node_modules"))).isSymbolicLink(), false);
      assert.equal(
        await readFile(path.join(options.cwd, "node_modules", "vinext", "bin.mjs"), "utf8"),
        "sealed dependency\n",
      );
      await assert.rejects(
        readFile(path.join(options.cwd, "node_modules", "borrowed-checkout-sentinel")),
        /ENOENT/,
      );
      assert.deepEqual(await readdir(options.env.npm_config_cache), []);
      assert.equal(options.env.HOME, options.env.CORTEX_EMPTY_HOME);
      assert.equal(options.env.NODE_PATH, "");
      assert.equal(options.env.npm_config_offline, "true");
      assert.match(options.env.NODE_OPTIONS, /cortex-network-guard/);
      assert.equal(options.env.PATH.split(path.delimiter)[0], path.join(options.cwd, "node_modules", ".bin"));
      assert.ok(!options.env.PATH.includes(path.join(sourceRoot, "node_modules")));
      assert.equal(options.env.CORTEX_WEB_RELEASE_BUILD, "1");
      assert.equal(options.env.WRANGLER_LOG_PATH, ".wrangler/wrangler.log");
      await writeFakeDist(options.cwd, BUILD_ID);
    },
    expectedLockDigest: fixture.result.lock_sha256,
    expectedSupplyDigest: fixture.result.supply_sha256,
    outputRoot,
    supplyRoot: fixture.destination,
  });

  assert.notEqual(isolatedRoot, sourceRoot);
  const provenance = JSON.parse(await readFile(path.join(outputRoot, "provenance.json"), "utf8"));
  const payloadLedger = await readFile(path.join(outputRoot, "release-payload.sha256"));
  const lockDigest = sha256(await readFile(path.join(sourceRoot, "package-lock.json")));
  assert.deepEqual(provenance, result.provenance);
  assert.equal(provenance.schema, 1);
  assert.equal(provenance.build_id, BUILD_ID);
  assert.deepEqual(
    provenance.build_command,
    ["sealed-node", "sealed-vinext", "build"],
  );
  assert.equal(provenance.lock_sha256, lockDigest);
  assert.equal(provenance.supply_sha256, fixture.result.supply_sha256);
  assert.equal(provenance.build_sha256, sha256(payloadLedger));
  assert.match(provenance.source_sha256, /^[0-9a-f]{64}$/);
  assert.match(provenance.adapter.sha256, /^[0-9a-f]{64}$/);
  assert.equal(provenance.adapter.version, 1);
  assert.equal(provenance.runtime.node.version, process.version);
  assert.equal(provenance.runtime.npm_cli.version, "11.0.0-fixture");
  assert.equal(provenance.vinext.path, "vinext/dist/cli.js");
  assert.equal(provenance.vinext.version, "0.0.50");
  assert.equal(provenance.vinext.vite, "8.1.5");
  assert.match(provenance.vinext.sha256, /^[0-9a-f]{64}$/);
  assert.equal((await lstat(path.join(outputRoot, "payload", "server", "node-adapter.mjs"))).isFile(), true);
  await assert.rejects(lstat(path.join(outputRoot, "node_modules")), /ENOENT/);
});

test("offline build verifies the caller-pinned supply before running", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "unpinned-supply");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-unpinned-output-"));
  t.after(() => removeFixtureRoot(parent));
  let invoked = false;

  await assert.rejects(
    buildReleaseOffline({
      buildId: BUILD_ID,
      buildRunner: async () => {
        invoked = true;
      },
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: "f".repeat(64),
      outputRoot: path.join(parent, "result"),
      supplyRoot: fixture.destination,
    }),
    /supply identity/,
  );
  assert.equal(invoked, false);
  await assert.rejects(lstat(path.join(parent, "result")), /ENOENT/);
});

test("offline build requires the exact sealed Vinext runtime before running", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-missing-vite-"));
  t.after(() => removeFixtureRoot(parent));
  const supplyRoot = path.join(parent, "sealed");
  const supply = await acquireReleaseSupply({
    destination: supplyRoot,
    installRunner: async (_command, _args, options) => {
      await writeDependencyFixture(options.cwd);
      await removeFixtureRoot(path.join(options.cwd, "node_modules", "vite"));
    },
    npmCliPath: NPM_CLI_FIXTURE,
    sourceRoot,
  });
  let invoked = false;

  await assert.rejects(
    buildReleaseOffline({
      buildId: BUILD_ID,
      buildRunner: async () => {
        invoked = true;
      },
      expectedLockDigest: supply.lock_sha256,
      expectedSupplyDigest: supply.supply_sha256,
      outputRoot: path.join(parent, "result"),
      supplyRoot,
    }),
    /sealed Vite package manifest is missing/,
  );
  assert.equal(invoked, false);
  await assert.rejects(lstat(path.join(parent, "result")), /ENOENT/);
});

test("supply verification rejects an unbound checksum ledger", async (t) => {
  const sourceRoot = await sourceFixture(t);

  for (const attack of ["symlink", "hardlink"]) {
    const fixture = await acquireFixture(t, sourceRoot, `ledger-${attack}`);
    const ledgerPath = path.join(fixture.destination, "checksums.jsonl");
    const externalLedger = path.join(path.dirname(fixture.destination), `${attack}-ledger.jsonl`);
    await writeFile(externalLedger, await readFile(ledgerPath));
    await rm(ledgerPath);
    if (attack === "symlink") {
      await symlink(externalLedger, ledgerPath);
    } else {
      await link(externalLedger, ledgerPath);
    }

    await assert.rejects(
      verifyReleaseSupply({
        expectedLockDigest: fixture.result.lock_sha256,
        expectedSupplyDigest: fixture.result.supply_sha256,
        supplyRoot: fixture.destination,
      }),
      /checksum ledger must be one regular, unlinked file/,
    );
  }
});

test("acquisition rejects control characters in dependency paths", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-control-path-"));
  t.after(() => removeFixtureRoot(parent));

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "sealed"),
      installRunner: async (_command, _args, options) => {
        await mkdir(path.join(options.cwd, "node_modules"));
        await writeFile(path.join(options.cwd, "node_modules", "line\nbreak"), "unsafe\n");
      },
      npmCliPath: NPM_CLI_FIXTURE,
      sourceRoot,
    }),
    /unsafe path/,
  );
});

test("acquisition and build do not inherit ambient secrets or tool paths", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const ambientTools = await mkdtemp(path.join(os.tmpdir(), "cortex-web-ambient-tools-"));
  t.after(() => removeFixtureRoot(ambientTools));
  const previousSecret = process.env.CORTEX_TEST_AMBIENT_SECRET;
  const previousPath = process.env.PATH;
  process.env.CORTEX_TEST_AMBIENT_SECRET = "must-not-cross-release-boundary";
  process.env.PATH = [ambientTools, previousPath ?? ""].filter(Boolean).join(path.delimiter);
  t.after(() => {
    if (previousSecret === undefined) delete process.env.CORTEX_TEST_AMBIENT_SECRET;
    else process.env.CORTEX_TEST_AMBIENT_SECRET = previousSecret;
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
  });

  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-clean-env-"));
  t.after(() => removeFixtureRoot(parent));
  const destination = path.join(parent, "sealed");
  const supply = await acquireReleaseSupply({
    destination,
    installRunner: async (_command, _args, options) => {
      assert.equal(options.env.CORTEX_TEST_AMBIENT_SECRET, undefined);
      assert.ok(!options.env.PATH.includes(ambientTools));
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath: NPM_CLI_FIXTURE,
    sourceRoot,
  });

  await buildReleaseOffline({
    buildId: BUILD_ID,
    buildRunner: async (_command, _args, options) => {
      assert.equal(options.env.CORTEX_TEST_AMBIENT_SECRET, undefined);
      assert.ok(!options.env.PATH.includes(ambientTools));
      await writeFakeDist(options.cwd, BUILD_ID);
    },
    expectedLockDigest: supply.lock_sha256,
    expectedSupplyDigest: supply.supply_sha256,
    outputRoot: path.join(parent, "result"),
    supplyRoot: destination,
  });
});

test("offline build rejects mutation of its verified dependency copy", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "mutated-build-dependency");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-mutated-build-"));
  t.after(() => removeFixtureRoot(parent));

  await assert.rejects(
    buildReleaseOffline({
      buildId: BUILD_ID,
      buildRunner: async (_command, _args, options) => {
        await writeFile(path.join(options.cwd, "node_modules", "vinext", "bin.mjs"), "mutated\n");
        await writeFakeDist(options.cwd, BUILD_ID);
      },
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: fixture.result.supply_sha256,
      outputRoot: path.join(parent, "result"),
      supplyRoot: fixture.destination,
    }),
    /EACCES|dependency tree changed during the release build/,
  );
  await assert.rejects(lstat(path.join(parent, "result")), /ENOENT/);
});

test("offline build rejects mutation of its private staged Node", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "mutated-build-node");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-mutated-node-"));
  t.after(() => removeFixtureRoot(parent));

  await assert.rejects(
    buildReleaseOffline({
      buildId: BUILD_ID,
      buildRunner: async (_command, args, options) => {
        await chmod(args[4], 0o700);
        await writeFile(args[4], "changed staged node\n");
        await writeFakeDist(options.cwd, BUILD_ID);
      },
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: fixture.result.supply_sha256,
      outputRoot: path.join(parent, "result"),
      supplyRoot: fixture.destination,
    }),
    /private build Node identity changed/,
  );
  await assert.rejects(lstat(path.join(parent, "result")), /ENOENT/);
});

test("acquisition binds the unchanged package and lock inputs", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-mutated-acquisition-"));
  t.after(() => removeFixtureRoot(parent));

  for (const input of ["package.json", "package-lock.json"]) {
    const destination = path.join(parent, input.replace(".json", ""));
    await assert.rejects(
      acquireReleaseSupply({
        destination,
        installRunner: async (_command, _args, options) => {
          await writeFile(path.join(options.cwd, input), "{}\n");
          await writeDependencyFixture(options.cwd);
        },
        npmCliPath: NPM_CLI_FIXTURE,
        sourceRoot,
      }),
      /changed during dependency acquisition/,
    );
    await assert.rejects(lstat(destination), /ENOENT/);
  }
});

test("acquisition materializes hardlinks and rejects special files in the dependency closure", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-unsafe-kind-"));
  t.after(() => removeFixtureRoot(parent));

  const hardlinkDestination = path.join(parent, "hardlink");
  await acquireReleaseSupply({
    destination: hardlinkDestination,
    installRunner: async (_command, _args, options) => {
      const packageRoot = path.join(options.cwd, "node_modules", "package");
      await mkdir(packageRoot, { recursive: true });
      const original = path.join(packageRoot, "original.js");
      await writeFile(original, "export default true;\n");
      await link(original, path.join(packageRoot, "alias.js"));
    },
    npmCliPath: NPM_CLI_FIXTURE,
    sourceRoot,
  });
  const sealedPackage = path.join(hardlinkDestination, "node_modules", "package");
  assert.equal((await lstat(path.join(sealedPackage, "original.js"))).nlink, 1);
  assert.equal((await lstat(path.join(sealedPackage, "alias.js"))).nlink, 1);

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "special"),
      installRunner: async (_command, _args, options) => {
        const packageRoot = path.join(options.cwd, "node_modules", "package");
        await mkdir(packageRoot, { recursive: true });
        await executeFile("mkfifo", [path.join(packageRoot, "named-pipe")]);
      },
      npmCliPath: NPM_CLI_FIXTURE,
      sourceRoot,
    }),
    /special file/,
  );
});

test("acquisition rejects case-folded and Unicode-normalized collisions when representable", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-collision-"));
  t.after(() => removeFixtureRoot(parent));
  let exercised = 0;

  for (const [label, names] of [
    ["case", ["Module.js", "module.js"]],
    ["unicode", ["caf\u00e9.js", "cafe\u0301.js"]],
  ]) {
    let distinct = false;
    const destination = path.join(parent, label);
    try {
      await acquireReleaseSupply({
        destination,
        installRunner: async (_command, _args, options) => {
          const packageRoot = path.join(options.cwd, "node_modules", "package");
          await mkdir(packageRoot, { recursive: true });
          for (const name of names) await writeFile(path.join(packageRoot, name), `${name}\n`);
          const observed = await readdir(packageRoot);
          distinct = names.every((name) => observed.includes(name));
        },
        npmCliPath: NPM_CLI_FIXTURE,
        sourceRoot,
      });
      if (distinct) assert.fail(`${label} collision was accepted`);
    } catch (error) {
      if (!distinct) throw error;
      if (distinct) assert.match(error.message, /case-folded or Unicode path collision/);
      exercised += 1;
    }
  }

  if (exercised === 0) {
    t.diagnostic("filesystem canonicalization prevented materializing distinct colliding names");
  }
});

test("supply ledger rejects duplicate closure entries even with a caller pin", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "duplicate-ledger");
  const ledgerPath = path.join(fixture.destination, "checksums.jsonl");
  const ledger = await readFile(ledgerPath, "utf8");
  const duplicateLedger = `${ledger}${ledger.split("\n")[0]}\n`;
  await writeFile(ledgerPath, duplicateLedger);

  await assert.rejects(
    verifyReleaseSupply({
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: sha256(Buffer.from(duplicateLedger, "utf8")),
      supplyRoot: fixture.destination,
    }),
    /checksum ledger does not match its closed contents/,
  );
});

test("offline build uses its sealed source after the live checkout drifts", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "lock-drift");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-lock-drift-output-"));
  t.after(() => removeFixtureRoot(parent));
  await writeFile(path.join(sourceRoot, "package-lock.json"), "{}\n");
  await buildReleaseOffline({
    buildId: BUILD_ID,
    buildRunner: async (_command, _args, options) => {
      assert.notEqual(await readFile(path.join(options.cwd, "package-lock.json"), "utf8"), "{}\n");
      await writeFakeDist(options.cwd, BUILD_ID);
    },
    expectedLockDigest: fixture.result.lock_sha256,
    expectedSupplyDigest: fixture.result.supply_sha256,
    outputRoot: path.join(parent, "result"),
    supplyRoot: fixture.destination,
  });
});

test("offline build installs an active Node network guard", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "network-guard");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-network-guard-output-"));
  t.after(() => removeFixtureRoot(parent));

  await buildReleaseOffline({
    buildId: BUILD_ID,
    buildRunner: async (_command, _args, options) => {
      const probe = `
        import dns from "node:dns";
        import http from "node:http";
        import net from "node:net";
        const probes = [
          () => fetch("http://127.0.0.1:9/"),
          () => dns.lookup("localhost", () => {}),
          () => dns.promises.lookup("localhost"),
          () => http.get("http://127.0.0.1:9/"),
          () => net.connect(9, "127.0.0.1"),
        ];
        let denied = 0;
        for (const run of probes) {
          try {
            await run();
          } catch (error) {
            if (error.message !== "offline release build attempted network access") throw error;
            denied += 1;
          }
        }
        if (denied !== probes.length) process.exit(17);
        process.stdout.write(String(denied));
      `;
      const result = await executeFile(process.execPath, ["--input-type=module", "--eval", probe], {
        cwd: options.cwd,
        env: options.env,
      });
      assert.equal(result.stdout, "5");
      await writeFakeDist(options.cwd, BUILD_ID);
    },
    expectedLockDigest: fixture.result.lock_sha256,
    expectedSupplyDigest: fixture.result.supply_sha256,
    outputRoot: path.join(parent, "result"),
    supplyRoot: fixture.destination,
  });
});

test("runtime identity probing uses a closed environment", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-runtime-probe-"));
  t.after(() => removeFixtureRoot(parent));
  const ambientTools = path.join(parent, "ambient-tools");
  const trustedTools = path.join(parent, "trusted-tools");
  await mkdir(ambientTools);
  await mkdir(trustedTools);
  const npmCliPath = await npmCliFixture(trustedTools, "11.0.0-fixture", {
    cliSource:
      "if (process.env.CORTEX_TEST_RUNTIME_SECRET) process.exit(41);\n" +
      `if ((process.env.PATH ?? \"\").includes(${JSON.stringify(ambientTools)})) process.exit(42);\n` +
      "if (process.argv[2] !== '--version') process.exit(19);\n" +
      "process.stdout.write('11.0.0-fixture\\n');\n",
  });
  const previousSecret = process.env.CORTEX_TEST_RUNTIME_SECRET;
  const previousPath = process.env.PATH;
  process.env.CORTEX_TEST_RUNTIME_SECRET = "must-not-cross-runtime-probe";
  process.env.PATH = [ambientTools, previousPath ?? ""].filter(Boolean).join(path.delimiter);
  t.after(() => {
    if (previousSecret === undefined) delete process.env.CORTEX_TEST_RUNTIME_SECRET;
    else process.env.CORTEX_TEST_RUNTIME_SECRET = previousSecret;
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
  });

  const result = await acquireReleaseSupply({
    destination: path.join(parent, "sealed"),
    installRunner: async (_command, _args, options) => {
      assert.equal(options.env.CORTEX_TEST_RUNTIME_SECRET, undefined);
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath,
    sourceRoot,
  });
  assert.match(result.supply_sha256, /^[0-9a-f]{64}$/);
});

test("canonical root checks reject aliased acquisition and build destinations", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-root-alias-"));
  t.after(() => removeFixtureRoot(parent));
  await symlink(sourceRoot, path.join(parent, "source-alias"));
  let acquisitionInvoked = false;

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "source-alias", "sealed"),
      installRunner: async (_command, _args, options) => {
        acquisitionInvoked = true;
        await writeDependencyFixture(options.cwd);
      },
      npmCliPath: NPM_CLI_FIXTURE,
      sourceRoot,
    }),
    /canonical roots must be disjoint/,
  );
  assert.equal(acquisitionInvoked, false);

  const fixture = await acquireFixture(t, sourceRoot, "aliased-build-output");
  await symlink(fixture.destination, path.join(parent, "supply-alias"));
  let buildInvoked = false;
  await assert.rejects(
    buildReleaseOffline({
      buildId: BUILD_ID,
      buildRunner: async () => {
        buildInvoked = true;
      },
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: fixture.result.supply_sha256,
      outputRoot: path.join(parent, "supply-alias", "result"),
      supplyRoot: fixture.destination,
    }),
    /canonical roots must be disjoint/,
  );
  assert.equal(buildInvoked, false);
});

test("package scripts expose only the sealed release entry and run its suite", async () => {
  const manifest = JSON.parse(await readFile(path.join(webRoot, "package.json"), "utf8"));
  assert.equal(manifest.scripts["release:supply:acquire"], "node scripts/release-supply-cli.mjs acquire");
  assert.equal(manifest.scripts["release:supply:verify"], "node scripts/release-supply-cli.mjs verify");
  assert.equal(manifest.scripts["build:release"], "node scripts/release-supply-cli.mjs build");
  assert.equal(manifest.scripts["build:release:sealed-internal"], undefined);
  assert.equal(manifest.scripts["test:release-supply"], "node --test tests/release-supply.test.mjs");
  assert.match(manifest.scripts.test, /npm run test:release-supply/);
  assert.doesNotMatch(manifest.scripts["build:release"], /vinext|node_modules/);
  const cli = await lstat(path.join(webRoot, "scripts", "release-supply-cli.mjs"));
  assert.equal(cli.isFile(), true);
});

test("runtime identity is measured from exact Node and npm CLI files", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-toolchain-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const npmCliCanonical = await realpath(npmCliPath);
  const destination = path.join(parent, "sealed");

  const result = await acquireReleaseSupply({
    destination,
    installRunner: async (command, args, options) => {
      assert.match(command, /sealed-runtime\/bin\/node$/);
      assert.notEqual(args[0], npmCliCanonical);
      assert.match(args[0], /sealed-runtime\/npm\/bin\/npm-cli-fixture\.mjs$/);
      assert.equal(args[1], "ci");
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath,
    sourceRoot,
  });
  const metadata = JSON.parse(await readFile(path.join(destination, "metadata.json"), "utf8"));
  assert.equal(metadata.runtime.node.sha256, sha256(await readFile(process.execPath)));
  assert.equal(metadata.runtime.npm_cli.sha256, sha256(await readFile(npmCliCanonical)));
  assert.equal(metadata.runtime.node.version, process.version);
  assert.equal(metadata.runtime.npm_cli.version, "11.0.0-fixture");
  assert.equal(result.evidence, "test-fixture");

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "forged"),
      installRunner: async () => {},
      npmCliPath,
      runtimeIdentity: { forged: true },
      sourceRoot,
    }),
    /runtimeIdentity is not an accepted production input/,
  );
});

test("runtime provenance rejects fully repinned identity tampering", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "runtime-provenance-tamper");
  const metadataPath = path.join(fixture.destination, "metadata.json");
  const baseline = JSON.parse(await readFile(metadataPath, "utf8"));
  const attacks = [
    ["node", "closure_count"],
    ["node", "closure_sha256"],
    ["node", "mode"],
    ["node", "path"],
    ["node", "sha256"],
    ["node", "size"],
    ["node", "version"],
    ["npm_cli", "closure_count"],
    ["npm_cli", "closure_sha256"],
    ["npm_cli", "mode"],
    ["npm_cli", "package_json_sha256"],
    ["npm_cli", "package_version"],
    ["npm_cli", "path"],
    ["npm_cli", "sha256"],
    ["npm_cli", "size"],
    ["npm_cli", "version"],
  ];

  for (const [component, field] of attacks) {
    const metadata = structuredClone(baseline);
    const current = metadata.original_runtime[component][field];
    if (field.endsWith("sha256")) {
      metadata.original_runtime[component][field] = current === "0".repeat(64)
        ? "1".repeat(64)
        : "0".repeat(64);
    } else if (["closure_count", "size"].includes(field)) {
      metadata.original_runtime[component][field] = current + 1;
    } else if (field === "mode") {
      metadata.original_runtime[component][field] = current ^ 0o100;
    } else if (field === "path") {
      metadata.original_runtime[component][field] = `${current}.tampered`;
    } else {
      metadata.original_runtime[component][field] = `${current}.tampered`;
    }
    await writeFile(metadataPath, `${JSON.stringify(metadata, null, 2)}\n`);
    const repinnedSupplyDigest = await repinSupplyMetadata(fixture.destination);
    await assert.rejects(
      verifyReleaseSupply({
        expectedLockDigest: fixture.result.lock_sha256,
        expectedSupplyDigest: repinnedSupplyDigest,
        supplyRoot: fixture.destination,
      }),
      /runtime|origin|metadata|identity/,
      `${component}.${field}`,
    );
  }
});

test("injected build runner is sandbox-shaped and emits non-release provenance", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-sandbox-shape-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const fixture = await acquireReleaseSupply({
    destination: path.join(parent, "sealed"),
    installRunner: async (_command, _args, options) => writeDependencyFixture(options.cwd),
    npmCliPath,
    sourceRoot,
  });

  const result = await buildReleaseOffline({
    buildId: BUILD_ID,
    buildRunner: async (command, args, options) => {
      assert.equal(command, "/usr/bin/sandbox-exec");
      assert.equal(args[0], "-f");
      const profile = await readFile(args[1], "utf8");
      assert.match(profile, /\(deny network\*\)/);
      assert.match(profile, /cooperative release lock/);
      assert.doesNotMatch(profile, /\/opt\/homebrew\/opt/);
      assert.doesNotMatch(profile, /\(subpath "\/usr"\)/);
      assert.doesNotMatch(profile, /\(subpath "\/Library"\)/);
      assert.match(profile, /\(literal "\/"\)/);
      assert.doesNotMatch(profile, /\(subpath "\/"\)/);
      assert.equal(args[2], "/usr/bin/env");
      assert.match(args[3], /^DYLD_LIBRARY_PATH=.*\/sealed-runtime\/lib$/);
      assert.match(args[4], /sealed-runtime\/bin\/node$/);
      assert.equal(
        args[5],
        await realpath(path.join(options.cwd, "node_modules", "vinext", "dist", "cli.js")),
      );
      assert.deepEqual(args.slice(6), ["build"]);
      assert.doesNotMatch(profile, new RegExp(parent.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      const writeRule = profile.split("\n").find((line) => line.startsWith("(allow file-write*"));
      assert.match(writeRule, /node_modules\/\.vite-temp/);
      assert.equal([...writeRule.matchAll(/node_modules/g)].length, 1);

      const nodeProbe = await executeFile(command, [...args.slice(0, 5), "--version"], {
        cwd: options.cwd,
        env: options.env,
      }).then((probe) => probe, (error) => error);
      if (nodeProbe instanceof Error &&
          /sandbox_apply: Operation not permitted/.test(nodeProbe.stderr ?? "")) {
        t.skip("nested sandbox-exec is unavailable; fixture evidence remains unverified");
        await writeFakeDist(options.cwd, BUILD_ID);
        return;
      }
      assert.equal(nodeProbe instanceof Error, false);
      assert.equal(nodeProbe.stdout.trim(), process.version);

      const allowedRead = await executeFile(
        "/usr/bin/sandbox-exec",
        ["-f", args[1], "/bin/cat", path.join(options.cwd, "package.json")],
      ).then((result) => result, (error) => error);
      assert.equal(allowedRead instanceof Error, false);

      const checkoutRead = await executeFile(
        "/usr/bin/sandbox-exec",
        ["-f", args[1], "/bin/cat", path.join(webRoot, "package.json")],
      ).then(() => null, (error) => error);
      assert.ok(checkoutRead);
      const supplyRead = await executeFile(
        "/usr/bin/sandbox-exec",
        ["-f", args[1], "/bin/cat", path.join(parent, "sealed", "metadata.json")],
      ).then(() => null, (error) => error);
      assert.ok(supplyRead);
      const dependencyWrite = await executeFile(
        "/usr/bin/sandbox-exec",
        ["-f", args[1], "/usr/bin/touch", path.join(options.cwd, "node_modules", "vite", "changed")],
      ).then(() => null, (error) => error);
      assert.ok(dependencyWrite);
      await writeFakeDist(options.cwd, BUILD_ID);
    },
    expectedLockDigest: fixture.lock_sha256,
    expectedSupplyDigest: fixture.supply_sha256,
    outputRoot: path.join(parent, "result"),
    supplyRoot: path.join(parent, "sealed"),
  });

  assert.equal(result.provenance.evidence, "test-fixture");
  assert.equal(result.provenance.release_eligible, false);
  assert.equal(result.provenance.sandbox.application, "fixture-unverified");
});

test("sandbox evidence binds the exact policy, guard, argv, and closed environment", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "sandbox-evidence");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-sandbox-evidence-"));
  t.after(() => removeFixtureRoot(parent));
  const outputRoot = path.join(parent, "result");
  let executedArguments;

  const result = await buildReleaseOffline({
    buildId: BUILD_ID,
    buildRunner: async (command, args, options) => {
      executedArguments = [command, ...args];
      assert.equal(options.env.OPENSSL_CONF, "/dev/null");
      await writeFakeDist(options.cwd, BUILD_ID);
    },
    expectedLockDigest: fixture.result.lock_sha256,
    expectedSupplyDigest: fixture.result.supply_sha256,
    outputRoot,
    supplyRoot: fixture.destination,
  });

  const sandbox = result.provenance.sandbox;
  assert.equal(sandbox.policy.schema, 1);
  assert.match(sandbox.policy.descriptor_sha256, /^[0-9a-f]{64}$/);
  assert.match(sandbox.profile_sha256, /^[0-9a-f]{64}$/);
  assert.match(sandbox.network_guard_sha256, /^[0-9a-f]{64}$/);
  assert.match(sandbox.closed_environment_sha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(
    sandbox.closed_environment.find(([name]) => name === "OPENSSL_CONF"),
    ["OPENSSL_CONF", "/dev/null"],
  );
  assert.deepEqual(sandbox.argv, executedArguments);
  assert.deepEqual(
    [...sandbox.closed_environment].sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0),
    sandbox.closed_environment,
  );
  for (const secret of [
    "build-only-control-secret",
    "build-only-bootstrap-secret",
    "build-only-draft-secret",
  ]) {
    assert.doesNotMatch(JSON.stringify(sandbox), new RegExp(secret));
  }
  const verified = await releaseSupply.verifyReleaseBuildOutput({ outputRoot });
  assert.deepEqual(verified.provenance, result.provenance);

  const provenancePath = path.join(outputRoot, "provenance.json");
  const completionPath = path.join(outputRoot, "complete.json");
  const attacks = [
    ["profile", (provenance) => {
      provenance.sandbox.profile_sha256 = "0".repeat(64);
    }],
    ["CLI", (provenance) => {
      provenance.sandbox.argv[6] = "/tmp/unsealed-build.mjs";
    }],
    ["argument", (provenance) => {
      provenance.sandbox.argv.splice(7, 0, "--unsealed-option");
    }],
    ["OpenSSL config", (provenance) => {
      const entry = provenance.sandbox.closed_environment.find(([name]) => name === "OPENSSL_CONF");
      assert.ok(entry);
      entry[1] = "/tmp/host-openssl.cnf";
      provenance.sandbox.closed_environment_sha256 = sha256(
        Buffer.from(`${JSON.stringify(provenance.sandbox.closed_environment, null, 2)}\n`, "utf8"),
      );
    }],
  ];
  for (const [label, attack] of attacks) {
    const tampered = structuredClone(result.provenance);
    attack(tampered);
    const tamperedBytes = Buffer.from(`${JSON.stringify(tampered, null, 2)}\n`, "utf8");
    await writeFile(provenancePath, tamperedBytes);
    const completion = JSON.parse(await readFile(completionPath, "utf8"));
    completion.provenance_sha256 = sha256(tamperedBytes);
    await writeFile(completionPath, `${JSON.stringify(completion, null, 2)}\n`);
    await assert.rejects(
      releaseSupply.verifyReleaseBuildOutput({ outputRoot }),
      /sandbox|profile|policy|argv/,
      label,
    );
  }
});

test("sandbox profile mutation during the runner fails closed", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "sandbox-profile-mutation");
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-sandbox-profile-mutation-"));
  t.after(() => removeFixtureRoot(parent));

  await assert.rejects(
    buildReleaseOffline({
      buildId: BUILD_ID,
      buildRunner: async (_command, args, options) => {
        await writeFile(args[1], "(version 1)\n(allow default)\n");
        await writeFakeDist(options.cwd, BUILD_ID);
      },
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: fixture.result.supply_sha256,
      outputRoot: path.join(parent, "result"),
      supplyRoot: fixture.destination,
    }),
    /sandbox profile identity changed/,
  );
});

test("publication requires a stable private parent and leaves completion last", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-publication-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const unsafeParent = path.join(parent, "unsafe-parent");
  await mkdir(unsafeParent, { mode: 0o755 });
  await chmod(unsafeParent, 0o755);

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(unsafeParent, "sealed"),
      installRunner: async (_command, _args, options) => writeDependencyFixture(options.cwd),
      npmCliPath,
      sourceRoot,
    }),
    /publication parent must have mode 0700/,
  );

  const occupied = path.join(parent, "occupied");
  await mkdir(occupied, { mode: 0o700 });
  await assert.rejects(
    acquireReleaseSupply({
      destination: occupied,
      installRunner: async () => assert.fail("occupied destination reached its runner"),
      npmCliPath,
      sourceRoot,
    }),
    /already exists/,
  );
  await assert.rejects(lstat(path.join(parent, ".occupied.lock")), /ENOENT/);

  const destination = path.join(parent, "sealed");
  const result = await acquireReleaseSupply({
    destination,
    installRunner: async (_command, _args, options) => writeDependencyFixture(options.cwd),
    npmCliPath,
    sourceRoot,
  });
  const completion = JSON.parse(await readFile(path.join(destination, "complete.json"), "utf8"));
  assert.equal(completion.kind, "release-supply");
  assert.equal(completion.supply_sha256, result.supply_sha256);
  assert.equal(completion.release_eligible, false);
  const lockName = `.${path.basename(destination)}.lock`;
  await assert.rejects(lstat(path.join(parent, lockName)), /ENOENT/);
});

test("completion marker is synced strictly after the closed artifact tree", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-publication-sync-order-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const events = [];

  await acquireReleaseSupply({
    destination: path.join(parent, "sealed"),
    installRunner: async (_command, _args, options) => writeDependencyFixture(options.cwd),
    npmCliPath,
    sourceRoot,
    syncRecorder: async (event) => events.push(event),
  });

  const markerIndex = events.indexOf("marker-file:complete.json");
  const contentEvents = events.filter((event) => event.startsWith("content-file:"));
  const directoryEvents = events.filter((event) => event.startsWith("content-directory:"));
  assert.ok(contentEvents.length > 0);
  assert.ok(directoryEvents.length > 0);
  assert.ok(contentEvents.every((event) => events.indexOf(event) < markerIndex));
  assert.ok(directoryEvents.every((event) => events.indexOf(event) < markerIndex));
  assert.deepEqual(events.slice(markerIndex), [
    "marker-file:complete.json",
    "destination-directory:.",
    "publication-parent:..",
  ]);
  const directoryDepths = directoryEvents.map((event) => event.split("/").length);
  assert.deepEqual(directoryDepths, [...directoryDepths].sort((left, right) => right - left));
});

test("parent replacement fails closed without cleaning an unrelated path", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parentRoot = await mkdtemp(path.join(os.tmpdir(), "cortex-web-parent-replace-"));
  t.after(() => removeFixtureRoot(parentRoot));
  const npmCliPath = await npmCliFixture(parentRoot);
  const ownedParent = path.join(parentRoot, "owned");
  const displacedParent = path.join(parentRoot, "displaced");
  const attackerParent = path.join(parentRoot, "attacker");
  await mkdir(ownedParent, { mode: 0o700 });
  await mkdir(attackerParent, { mode: 0o700 });
  await writeFile(path.join(attackerParent, "sentinel"), "preserve\n");

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(ownedParent, "sealed"),
      installRunner: async (_command, _args, options) => {
        await writeDependencyFixture(options.cwd);
        await rename(ownedParent, displacedParent);
        await symlink(attackerParent, ownedParent);
      },
      npmCliPath,
      sourceRoot,
    }),
    /publication parent identity changed/,
  );
  assert.equal(await readFile(path.join(attackerParent, "sentinel"), "utf8"), "preserve\n");
  await assert.rejects(lstat(path.join(attackerParent, "sealed")), /ENOENT/);
});

test("cooperative publication lock rejects a concurrent destination operation", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-concurrent-publish-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const destination = path.join(parent, "sealed");
  let enterFirst;
  const firstEntered = new Promise((resolve) => {
    enterFirst = resolve;
  });
  let releaseFirst;
  const firstMayFinish = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const first = acquireReleaseSupply({
    destination,
    installRunner: async (_command, _args, options) => {
      enterFirst();
      await firstMayFinish;
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath,
    sourceRoot,
  });
  await firstEntered;
  let secondInvoked = false;
  await assert.rejects(
    acquireReleaseSupply({
      destination,
      installRunner: async () => {
        secondInvoked = true;
      },
      npmCliPath,
      sourceRoot,
    }),
    /publication lock is already held/,
  );
  assert.equal(secondInvoked, false);
  releaseFirst();
  await first;
});

test("destination race after tool inspection does not leave a stale lock", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-destination-race-"));
  t.after(() => removeFixtureRoot(parent));
  const destination = path.join(parent, "sealed");
  const npmCliPath = await npmCliFixture(parent, "11.0.0-fixture", {
    cliSource:
      `import { mkdirSync } from "node:fs";\n` +
      `mkdirSync(${JSON.stringify(destination)}, { recursive: true });\n` +
      `if (process.argv[2] !== "--version") process.exit(19);\n` +
      `process.stdout.write("11.0.0-fixture\\n");\n`,
  });

  await assert.rejects(
    acquireReleaseSupply({
      destination,
      installRunner: async () => assert.fail("destination race reached its runner"),
      npmCliPath,
      sourceRoot,
    }),
    /already exists/,
  );
  await assert.rejects(lstat(path.join(parent, ".sealed.lock")), /ENOENT/);
});

test("formal CLI verifies fixture evidence and refuses to release-build from it", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-release-cli-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const supplyRoot = path.join(parent, "sealed");
  const fixture = await acquireReleaseSupply({
    destination: supplyRoot,
    installRunner: async (_command, _args, options) => writeDependencyFixture(options.cwd),
    npmCliPath,
    sourceRoot,
  });
  const cli = path.join(webRoot, "scripts", "release-supply-cli.mjs");
  const verified = await executeFile(process.execPath, [
    cli,
    "verify",
    "--lock-digest", fixture.lock_sha256,
    "--supply", supplyRoot,
    "--supply-digest", fixture.supply_sha256,
  ]);
  assert.equal(JSON.parse(verified.stdout).release_eligible, false);

  const outputRoot = path.join(parent, "result");
  await assert.rejects(
    executeFile(process.execPath, [
      cli,
      "build",
      "--lock-digest", fixture.lock_sha256,
      "--supply", supplyRoot,
      "--supply-digest", fixture.supply_sha256,
      "--output", outputRoot,
      "--build-id", BUILD_ID,
    ]),
    (error) => {
      assert.match(error.stderr, /production offline build refuses a test-fixture supply/);
      return true;
    },
  );
  await assert.rejects(lstat(outputRoot), /ENOENT/);
});

test("sealed npm identity covers the package closure without later host npm access", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-npm-closure-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const destination = path.join(parent, "sealed");
  const result = await acquireReleaseSupply({
    destination,
    installRunner: async (_command, _args, options) => writeDependencyFixture(options.cwd),
    npmCliPath,
    sourceRoot,
  });

  await writeFile(
    path.join(parent, "npm-package", "lib", "implementation.mjs"),
    "export const version = '11.0.0-fixture';\n// changed implementation\n",
  );
  const verified = await verifyReleaseSupply({
    expectedLockDigest: result.lock_sha256,
    expectedSupplyDigest: result.supply_sha256,
    supplyRoot: destination,
  });
  assert.match(verified.metadata.runtime.npm_cli.closure_sha256, /^[0-9a-f]{64}$/);
  assert.ok(verified.metadata.runtime.npm_cli.closure_count > 3);
});

test("acquisition executes a private staged Node and npm package", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-staged-runtime-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const npmCliCanonical = await realpath(npmCliPath);

  await acquireReleaseSupply({
    destination: path.join(parent, "sealed"),
    installRunner: async (command, args, options) => {
      assert.notEqual(command, process.execPath);
      assert.notEqual(args[0], npmCliCanonical);
      assert.match(command, /sealed-runtime\/bin\/node$/);
      assert.match(args[0], /sealed-runtime\/npm\/bin\/npm-cli-fixture\.mjs$/);
      assert.equal(
        await readFile(path.join(path.dirname(args[0]), "..", "lib", "implementation.mjs"), "utf8"),
        "export const version = \"11.0.0-fixture\";\n",
      );
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath,
    sourceRoot,
  });
});

test("private Node runtime closure executes without host libraries", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-node-closure-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const destination = path.join(parent, "sealed");

  const result = await acquireReleaseSupply({
    destination,
    installRunner: async (command, args, options) => {
      assert.match(command, /sealed-runtime\/bin\/node$/);
      const runtimeRoot = path.dirname(path.dirname(command));
      assert.equal(options.env.DYLD_LIBRARY_PATH, path.join(runtimeRoot, "lib"));
      assert.match(args[0], /sealed-runtime\/npm\/bin\/npm-cli-fixture\.mjs$/);
      const probe = await executeFile(command, ["--version"], {
        cwd: options.cwd,
        env: {
          CI: "1",
          DYLD_LIBRARY_PATH: options.env.DYLD_LIBRARY_PATH,
          DYLD_PRINT_LIBRARIES: "1",
          HOME: options.env.HOME,
          PATH: path.dirname(command),
        },
        timeout: 10_000,
      });
      assert.equal(probe.stdout.trim(), process.version);
      const libraries = await readdir(path.join(runtimeRoot, "lib")).catch((error) => {
        if (error.code === "ENOENT") return [];
        throw error;
      });
      if (libraries.length) {
        assert.match(probe.stderr, new RegExp(`${runtimeRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\/lib\/`));
      } else {
        assert.match(probe.stderr, /\/(?:usr\/lib|System\/Library)\//);
      }
      assert.doesNotMatch(probe.stderr, /\/opt\/homebrew\/(?:Cellar|opt)\//);
      assert.doesNotMatch(probe.stderr, /\/usr\/local\/(?:Cellar|opt)\//);
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath,
    sourceRoot,
  });

  assert.ok(result.release_eligible === false);
  const metadata = JSON.parse(await readFile(path.join(destination, "metadata.json"), "utf8"));
  assert.ok(metadata.runtime.node.closure_count > 1);
  assert.match(metadata.runtime.node.closure_sha256, /^[0-9a-f]{64}$/);
  assert.equal(
    (await lstat(path.join(destination, "tooling", "node", "bin", "node"))).isFile(),
    true,
  );
});

test("Mach-O validator binds the one trusted dynamic loader to the executable", () => {
  const trustedLoader = machoPathCommand(0x0e, "/usr/lib/dyld");
  assert.deepEqual(
    parseMachOLoadCommands(thinMachOFixture(0x02, [trustedLoader]), "fixture executable", "executable")
      .dylinkers,
    ["/usr/lib/dyld"],
  );
  assert.deepEqual(
    parseMachOLoadCommands(thinMachOFixture(0x06, []), "fixture dylib", "dylib").dylinkers,
    [],
  );

  for (const [label, fileType, commands] of [
    ["missing", 0x02, []],
    ["untrusted", 0x02, [machoPathCommand(0x0e, "/tmp/dyld")]],
    ["duplicate", 0x02, [trustedLoader, trustedLoader]],
    ["dylib", 0x06, [trustedLoader]],
  ]) {
    assert.throws(
      () => parseMachOLoadCommands(
        thinMachOFixture(fileType, commands),
        `${label} fixture`,
        fileType === 0x02 ? "executable" : "dylib",
      ),
      /dynamic loader/,
    );
  }
});

test("Node runtime closure revisits one image under every inherited rpath context", async () => {
  const load = (value) => machoPathCommand(0x0c, value);
  const rpath = (value) => machoPathCommand(0x8000001c, value);
  const images = new Map([
    ["/virtual/node", thinMachOFixture(0x02, [
      machoPathCommand(0x0e, "/usr/lib/dyld"),
      load("@rpath/A.dylib"),
      load("@rpath/B.dylib"),
    ])],
    ["/virtual/A.dylib", thinMachOFixture(0x06, [
      rpath("/a"),
      load("@rpath/D.dylib"),
    ])],
    ["/virtual/B.dylib", thinMachOFixture(0x06, [
      rpath("/b"),
      load("@rpath/C.dylib"),
    ])],
    ["/virtual/D.dylib", thinMachOFixture(0x06, [load("@rpath/C.dylib")])],
    ["/virtual/C.dylib", thinMachOFixture(0x06, [load("@rpath/E.dylib")])],
    ["/virtual/E.dylib", thinMachOFixture(0x06, [])],
  ]);
  const observedEContexts = [];

  await collectNodeRuntimeClosure("/virtual/node", {
    inspectFile: async (filename) => {
      const contents = images.get(filename);
      assert.ok(contents, `unexpected image ${filename}`);
      return {
        canonical: filename,
        identity: { mode: 0o555, sha256: sha256(contents), size: contents.byteLength },
      };
    },
    readImage: async (filename) => images.get(filename),
    resolveDependency: async (value, _image, _executable, searchRpaths) => {
      const leaf = path.posix.basename(value);
      if (leaf === "E.dylib") {
        observedEContexts.push(searchRpaths.map((entry) => entry.original).join(":"));
      }
      return { private: `lib/${leaf}`, source: `/virtual/${leaf}` };
    },
  });

  assert.deepEqual(observedEContexts.sort(), ["/a", "/b"]);
});

test("npm package identity rejects the wrong package name or bin target", async (t) => {
  const sourceRoot = await sourceFixture(t);
  for (const [label, options] of [
    ["name", { name: "not-npm" }],
    ["bin", { bin: "bin/not-the-selected-cli.mjs" }],
  ]) {
    const parent = await mkdtemp(path.join(os.tmpdir(), `cortex-web-npm-${label}-`));
    t.after(() => removeFixtureRoot(parent));
    const npmCliPath = await npmCliFixture(parent, "11.0.0-fixture", options);
    let invoked = false;
    await assert.rejects(
      acquireReleaseSupply({
        destination: path.join(parent, "sealed"),
        installRunner: async (_command, _args, runnerOptions) => {
          invoked = true;
          await writeDependencyFixture(runnerOptions.cwd);
        },
        npmCliPath,
        sourceRoot,
      }),
      /npm package name|npm package bin/,
    );
    assert.equal(invoked, false);
  }
});

test("runtime mutation during acquisition fails closed", async (t) => {
  const sourceRoot = await sourceFixture(t);

  for (const target of ["original", "staged"]) {
    const parent = await mkdtemp(path.join(os.tmpdir(), `cortex-web-runtime-${target}-`));
    t.after(() => removeFixtureRoot(parent));
    const npmCliPath = await npmCliFixture(parent);
    await assert.rejects(
      acquireReleaseSupply({
        destination: path.join(parent, "sealed"),
        installRunner: async (_command, args, options) => {
          const cli = target === "original" ? npmCliPath : args[0];
          const implementation = path.join(path.dirname(cli), "..", "lib", "implementation.mjs");
          if (target === "staged") await chmod(implementation, 0o600);
          await writeFile(
            implementation,
            "export const version = '11.0.0-fixture';\n// self-modified\n",
          );
          await writeDependencyFixture(options.cwd);
        },
        npmCliPath,
        sourceRoot,
      }),
      /runtime identity changed|npm package closure/,
    );
    await assert.rejects(lstat(path.join(parent, "sealed")), /ENOENT/);
  }
});

test("offline source closure rejects dotenv, key, and untracked secret inputs", async (t) => {
  for (const [relative, contents] of [
    [".env.production", "NEXT_PUBLIC_LEAK=must-not-load\n"],
    ["app/local.pem", "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"],
    ["app/untracked-secret.ts", "const secret = '-----BEGIN PRIVATE KEY-----';\n"],
  ]) {
    const sourceRoot = await sourceFixture(t);
    const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-source-secret-supply-"));
    t.after(() => removeFixtureRoot(parent));
    const filename = path.join(sourceRoot, ...relative.split("/"));
    await mkdir(path.dirname(filename), { recursive: true });
    await writeFile(filename, contents);
    let invoked = false;

    await assert.rejects(
      acquireReleaseSupply({
        destination: path.join(parent, "sealed"),
        installRunner: async (_command, _args, options) => {
          invoked = true;
          await writeDependencyFixture(options.cwd);
        },
        npmCliPath: NPM_CLI_FIXTURE,
        sourceRoot,
      }),
      /dotenv|private key|untracked|source closure/,
    );
    assert.equal(invoked, false);
    await assert.rejects(lstat(path.join(parent, "sealed")), /ENOENT/);
  }
});

test("offline source closure rejects a large private key across scan chunks", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-large-source-secret-"));
  t.after(() => removeFixtureRoot(parent));
  const prefix = "x".repeat((64 * 1024) - 10);
  const contents = `${prefix}-----BEGIN PRIVATE KEY-----${"y".repeat(1024 * 1024)}`;
  await writeFile(path.join(sourceRoot, "app", "large-secret.ts"), contents);
  let invoked = false;

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "sealed"),
      installRunner: async (_command, _args, options) => {
        invoked = true;
        await writeDependencyFixture(options.cwd);
      },
      npmCliPath: NPM_CLI_FIXTURE,
      sourceRoot,
    }),
    /private key material/,
  );
  assert.equal(invoked, false);
  await assert.rejects(lstat(path.join(parent, "sealed")), /ENOENT/);
});

test("tracked source contract accepts framework build paths and excludes repository-only paths", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-source-contract-"));
  t.after(() => removeFixtureRoot(parent));
  await writeFile(path.join(sourceRoot, "next-env.d.ts"), "/// generated by Next.js\n");
  for (const [relative, contents] of [
    ["pages/legacy.tsx", "export default function Legacy() { return null; }\n"],
    ["src/feature.ts", "export const feature = true;\n"],
    ["middleware.ts", "export function middleware() {}\n"],
    ["instrumentation.ts", "export function register() {}\n"],
    ["components/ui/button.tsx", "export function Button() { return null; }\n"],
    ["hooks/use-copy-to-clipboard.ts", "export const useCopyToClipboard = () => {};\n"],
    ["lib/utils.ts", "export const cn = (value) => value;\n"],
  ]) {
    const filename = path.join(sourceRoot, ...relative.split("/"));
    await mkdir(path.dirname(filename), { recursive: true });
    await writeFile(filename, contents);
  }

  await acquireReleaseSupply({
    destination: path.join(parent, "sealed"),
    installRunner: async (_command, _args, options) => {
      for (const relative of [
        "pages/legacy.tsx",
        "src/feature.ts",
        "middleware.ts",
        "instrumentation.ts",
        "components/ui/button.tsx",
        "hooks/use-copy-to-clipboard.ts",
        "lib/utils.ts",
      ]) {
        assert.ok((await readFile(path.join(options.cwd, "source-export", relative), "utf8")).length > 0);
      }
      await assert.rejects(readFile(path.join(options.cwd, "source-export", "README.md")), /ENOENT/);
      await assert.rejects(readFile(path.join(options.cwd, "source-export", "next-env.d.ts")), /ENOENT/);
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath: NPM_CLI_FIXTURE,
    sourceRoot,
  });
});

test("tracked source contract rejects an unclassified potential build input", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-unknown-source-contract-"));
  t.after(() => removeFixtureRoot(parent));
  await mkdir(path.join(sourceRoot, "features"));
  await writeFile(path.join(sourceRoot, "features", "runtime.ts"), "export const runtime = true;\n");
  let invoked = false;

  await assert.rejects(
    acquireReleaseSupply({
      destination: path.join(parent, "sealed"),
      installRunner: async (_command, _args, options) => {
        invoked = true;
        await writeDependencyFixture(options.cwd);
      },
      npmCliPath: NPM_CLI_FIXTURE,
      sourceRoot,
    }),
    /unclassified tracked source path/,
  );
  assert.equal(invoked, false);
  await assert.rejects(lstat(path.join(parent, "sealed")), /ENOENT/);
});

test("tracked source snapshot pins every tree read to the resolved commit", async () => {
  const implementation = await readFile(path.join(webRoot, "scripts", "release-supply.mjs"), "utf8");
  const start = implementation.indexOf("async function exportTrackedSource");
  const end = implementation.indexOf("async function prepareSourceExport", start);
  assert.ok(start >= 0 && end > start);
  const body = implementation.slice(start, end);
  const commitDeclaration = body.indexOf("const commit");
  const afterCommit = body.slice(body.indexOf("\n", commitDeclaration) + 1);

  assert.doesNotMatch(afterCommit, /["'`]HEAD(?::|["'`])/);
  assert.match(afterCommit, /`\$\{commit\}:\$\{sourceRelative\}`/);
  assert.match(afterCommit, /"ls-tree"[\s\S]*commit[\s\S]*sourceRelative/);
});

test("publication reserves one owned destination before invoking the runner", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-owned-destination-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const destination = path.join(parent, "sealed");
  let ownedIdentity;

  await acquireReleaseSupply({
    destination,
    installRunner: async (_command, _args, options) => {
      const details = await lstat(destination, { bigint: true });
      assert.equal(details.isDirectory(), true);
      assert.equal(Number(details.mode & 0o777n), 0o700);
      ownedIdentity = { dev: details.dev, ino: details.ino };
      await assert.rejects(mkdir(destination), /EEXIST/);
      await writeDependencyFixture(options.cwd);
    },
    npmCliPath,
    sourceRoot,
  });
  const published = await lstat(destination, { bigint: true });
  assert.deepEqual({ dev: published.dev, ino: published.ino }, ownedIdentity);
});

test("failed publication removes its owned read-only partial destination", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-partial-cleanup-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const destination = path.join(parent, "sealed");

  await assert.rejects(
    acquireReleaseSupply({
      destination,
      installRunner: async () => {
        const partial = path.join(destination, "tooling", "npm");
        await mkdir(partial, { recursive: true });
        await writeFile(path.join(partial, "partial"), "incomplete\n", { mode: 0o400 });
        await chmod(partial, 0o500);
        await chmod(path.dirname(partial), 0o500);
        throw new Error("fixture publication failure");
      },
      npmCliPath,
      sourceRoot,
    }),
    /fixture publication failure/,
  );
  await assert.rejects(lstat(destination), /ENOENT/);
  await assert.rejects(lstat(path.join(parent, ".sealed.lock")), /ENOENT/);
});

test("invalid completion marker does not preserve an owned partial publication", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-web-invalid-marker-cleanup-"));
  t.after(() => removeFixtureRoot(parent));
  const npmCliPath = await npmCliFixture(parent);
  const destination = path.join(parent, "sealed");

  await assert.rejects(
    acquireReleaseSupply({
      destination,
      installRunner: async () => {
        await writeFile(path.join(destination, "complete.json"), "{}\n", { mode: 0o600 });
        throw new Error("fixture failed after invalid marker creation");
      },
      npmCliPath,
      sourceRoot,
    }),
    /invalid marker creation/,
  );
  await assert.rejects(lstat(destination), /ENOENT/);
  await assert.rejects(lstat(path.join(parent, ".sealed.lock")), /ENOENT/);
});

test("verification rejects a partially populated destination without its final marker", async (t) => {
  const sourceRoot = await sourceFixture(t);
  const fixture = await acquireFixture(t, sourceRoot, "partial-marker");
  await rm(path.join(fixture.destination, "complete.json"));
  await assert.rejects(
    verifyReleaseSupply({
      expectedLockDigest: fixture.result.lock_sha256,
      expectedSupplyDigest: fixture.result.supply_sha256,
      supplyRoot: fixture.destination,
    }),
    /completion marker/,
  );
});
