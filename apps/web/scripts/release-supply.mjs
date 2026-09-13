import { execFile as execFileCallback } from "node:child_process";
import { createHash } from "node:crypto";
import { constants, createReadStream } from "node:fs";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  readlink,
  realpath,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { pathToFileURL } from "node:url";

import {
  inspectReleasePayload,
  releasePayloadLedger,
  writeReleasePayload,
} from "./release-payload.mjs";

const executeFile = promisify(execFileCallback);
const SUPPLY_SCHEMA = 2;
const METADATA_FILE = "metadata.json";
const LEDGER_FILE = "checksums.jsonl";
const COMPLETION_FILE = "complete.json";
const SANDBOX_EXECUTABLE = "/usr/bin/sandbox-exec";
const SANDBOX_POLICY_SCHEMA = 1;
const ENV_EXECUTABLE = "/usr/bin/env";
const GIT_EXECUTABLE = "/usr/bin/git";
const MACHO_MAGIC_64 = 0xfeedfacf;
const MACHO_CPU_TYPES = new Map([
  ["arm64", 0x0100000c],
  ["x64", 0x01000007],
]);
const MACHO_DYLIB_COMMANDS = new Set([
  0x0c,
  0x20,
  0x80000018,
  0x8000001f,
  0x80000023,
]);
const MACHO_LOAD_DYLINKER_COMMAND = 0x0e;
const MACHO_RPATH_COMMAND = 0x8000001c;
const MACHO_REJECTED_COMMANDS = new Set([0x06, 0x0f, 0x10, 0x27]);
const NODE_RUNTIME_MAX_FILES = 256;
const NODE_RUNTIME_MAX_BYTES = 512 * 1024 * 1024;
const NODE_RUNTIME_MAX_DEPTH = 64;
const NODE_RUNTIME_MAX_CONTEXTS = 1024;
const SOURCE_FILES = new Set([
  ".openai/hosting.json",
  "THIRD_PARTY_NOTICES.md",
  "licenses/MIT-KaTeX.txt",
  "licenses/OFL-1.1.txt",
  "next.config.ts",
  "package-lock.json",
  "package.json",
  "postcss.config.mjs",
  "server/access-identity-bound.mjs",
  "server/node-adapter.mjs",
  "tsconfig.json",
  "vite.config.ts",
]);
const SOURCE_DIRECTORIES = new Set(["app", "build", "components", "hooks", "lib", "pages", "public", "src", "worker"]);
const SOURCE_EXCLUDED_FILES = new Set([
  ".gitignore",
  "README.md",
  "UX-NOTES.md",
  "components.json",
  "eslint.config.mjs",
  "next-env.d.ts",
  "vitest.config.ts",
]);
const SOURCE_EXCLUDED_DIRECTORIES = new Set(["artifacts", "scripts", "tests"]);
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
const BUILD_SECRETS = Object.freeze([
  "build-only-control-secret-000000000000000000000",
  "build-only-bootstrap-secret-0000000000000000000",
  "build-only-draft-secret-0000000000000000000000",
]);
const NODE_RUNTIME_IDENTITY_FIELDS = Object.freeze([
  "closure_count",
  "closure_sha256",
  "mode",
  "path",
  "sha256",
  "size",
  "version",
]);
const NPM_RUNTIME_IDENTITY_FIELDS = Object.freeze([
  "closure_count",
  "closure_sha256",
  "mode",
  "package_json_sha256",
  "package_version",
  "path",
  "sha256",
  "size",
  "version",
]);
const NETWORK_GUARD = `
import dns from "node:dns";
import dgram from "node:dgram";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import tls from "node:tls";

const deny = () => {
  throw new Error("offline release build attempted network access");
};

globalThis.fetch = deny;
dns.lookup = deny;
dns.promises.lookup = deny;
dgram.createSocket = deny;
http.get = deny;
http.request = deny;
https.get = deny;
https.request = deny;
net.connect = deny;
net.createConnection = deny;
net.Socket.prototype.connect = deny;
tls.connect = deny;
`;

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function canonicalDigest(value) {
  return sha256(Buffer.from(canonicalJson(value), "utf8"));
}

function closedEnvironmentDescriptor(environment) {
  return Object.entries(environment)
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
    .map(([key, value]) => [key, BUILD_SECRETS.includes(value) ? "<secret>" : value]);
}

function assertDigest(value, label) {
  if (!/^[0-9a-f]{64}$/.test(value ?? "")) {
    throw new Error(`${label} must be an exact lowercase SHA-256 digest`);
  }
}

function assertGitObjectId(value, label) {
  if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value ?? "")) {
    throw new Error(`${label} must be an exact lowercase Git object identity`);
  }
}

function assertRuntimeIdentity(runtime) {
  const expected = ["arch", "node", "npm_cli", "platform"];
  if (!runtime || typeof runtime !== "object" || Array.isArray(runtime) ||
      Object.keys(runtime).sort().join("\0") !== expected.join("\0")) {
    throw new Error("release supply runtime identity is incomplete");
  }
  if (!/^[A-Za-z0-9._+-]{1,80}$/.test(runtime.arch ?? "") ||
      !/^[A-Za-z0-9._+-]{1,80}$/.test(runtime.platform ?? "")) {
    throw new Error("release supply runtime platform identity is unsafe");
  }
  assertExactObjectKeys(
    runtime.node,
    NODE_RUNTIME_IDENTITY_FIELDS,
    "release node identity",
  );
  assertDigest(runtime.node.sha256, "release node digest");
  assertDigest(runtime.node.closure_sha256, "release node closure digest");
  if (!Number.isSafeInteger(runtime.node.closure_count) || runtime.node.closure_count <= 0 ||
      runtime.node.path !== "bin/node" ||
      !Number.isSafeInteger(runtime.node.mode) || runtime.node.mode < 0 || runtime.node.mode > 0o777 ||
      !Number.isSafeInteger(runtime.node.size) || runtime.node.size <= 0 ||
      typeof runtime.node.version !== "string" || !/^[A-Za-z0-9._+-]{1,80}$/.test(runtime.node.version)) {
    throw new Error("release node identity is unsafe");
  }
  assertExactObjectKeys(
    runtime.npm_cli,
    NPM_RUNTIME_IDENTITY_FIELDS,
    "release npm CLI identity",
  );
  assertDigest(runtime.npm_cli.closure_sha256, "release npm package closure digest");
  assertDigest(runtime.npm_cli.package_json_sha256, "release npm package manifest digest");
  assertDigest(runtime.npm_cli.sha256, "release npm CLI digest");
  assertSafeRelative(runtime.npm_cli.path);
  if (!Number.isSafeInteger(runtime.npm_cli.closure_count) || runtime.npm_cli.closure_count <= 0 ||
      !Number.isSafeInteger(runtime.npm_cli.mode) || runtime.npm_cli.mode < 0 || runtime.npm_cli.mode > 0o777 ||
      !Number.isSafeInteger(runtime.npm_cli.size) || runtime.npm_cli.size <= 0 ||
      typeof runtime.npm_cli.package_version !== "string" ||
      !/^[A-Za-z0-9._+-]{1,80}$/.test(runtime.npm_cli.package_version) ||
      typeof runtime.npm_cli.version !== "string" ||
      !/^[A-Za-z0-9._+-]{1,80}$/.test(runtime.npm_cli.version)) {
    throw new Error("release npm CLI identity is unsafe");
  }
}

function assertRuntimeComponentEqual(actual, expected, fields, label, readonlyMode = false) {
  for (const field of fields) {
    const expectedValue = readonlyMode && field === "mode" ? expected[field] & ~0o222 : expected[field];
    if (actual[field] !== expectedValue) {
      throw new Error(`${label} changed at ${field}`);
    }
  }
}

function assertRuntimeEqual(actual, expected, label) {
  assertRuntimeIdentity(actual);
  assertRuntimeIdentity(expected);
  if (actual.arch !== expected.arch || actual.platform !== expected.platform) {
    throw new Error(`${label} changed at platform identity`);
  }
  try {
    assertRuntimeComponentEqual(actual.node, expected.node, NODE_RUNTIME_IDENTITY_FIELDS, `${label} Node`);
    assertRuntimeComponentEqual(actual.npm_cli, expected.npm_cli, NPM_RUNTIME_IDENTITY_FIELDS, `${label} npm`);
  } catch (error) {
    throw new Error(`${label} changed`, { cause: error });
  }
}

function assertStagedRuntimeMatchesOrigin(staged, original, label) {
  assertRuntimeIdentity(staged);
  assertRuntimeIdentity(original);
  if (staged.arch !== original.arch || staged.platform !== original.platform) {
    throw new Error(`${label} platform identity differs`);
  }
  assertRuntimeComponentEqual(
    staged.node,
    original.node,
    NODE_RUNTIME_IDENTITY_FIELDS,
    `${label} Node`,
    true,
  );
  assertRuntimeComponentEqual(
    staged.npm_cli,
    original.npm_cli,
    NPM_RUNTIME_IDENTITY_FIELDS,
    `${label} npm`,
    true,
  );
}

function sameFileIdentity(left, right) {
  return left.dev === right.dev && left.ino === right.ino && left.mode === right.mode &&
    left.nlink === right.nlink && left.size === right.size && left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs;
}

async function inspectToolFile(filename, label) {
  if (!path.isAbsolute(filename ?? "")) {
    throw new Error(`${label} path must be absolute`);
  }
  const canonical = await realpath(filename);
  const handle = await open(
    canonical,
    constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
  );
  try {
    const before = await handle.stat({ bigint: true });
    if (!before.isFile() || before.nlink !== 1n) {
      throw new Error(`${label} must be one regular, unlinked file`);
    }
    const contents = await handle.readFile();
    const after = await handle.stat({ bigint: true });
    if (!sameFileIdentity(before, after) || BigInt(contents.byteLength) !== after.size) {
      throw new Error(`${label} changed during no-follow inspection`);
    }
    return {
      canonical,
      identity: {
        mode: Number(after.mode & 0o777n),
        sha256: sha256(contents),
        size: Number(after.size),
      },
      stable_identity: stableDirectoryIdentity(after),
    };
  } finally {
    await handle.close();
  }
}

function readMachOCommandString(contents, commandOffset, commandSize, valueOffset, label) {
  if (valueOffset < 12 || valueOffset >= commandSize) {
    throw new Error(`${label} contains an invalid Mach-O string offset`);
  }
  const start = commandOffset + valueOffset;
  const commandEnd = commandOffset + commandSize;
  const end = contents.indexOf(0, start);
  if (end < start || end >= commandEnd) {
    throw new Error(`${label} contains an unterminated Mach-O string`);
  }
  const bytes = contents.subarray(start, end);
  const value = bytes.toString("utf8");
  if (!value || !Buffer.from(value, "utf8").equals(bytes) || /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new Error(`${label} contains an unsafe Mach-O path`);
  }
  return value;
}

export function parseMachOLoadCommands(contents, label, imageKind) {
  if (contents.byteLength < 32 || contents.readUInt32LE(0) !== MACHO_MAGIC_64) {
    throw new Error(`${label} must be one thin little-endian 64-bit Mach-O image`);
  }
  const expectedCpu = MACHO_CPU_TYPES.get(process.arch);
  if (!expectedCpu || contents.readInt32LE(4) !== expectedCpu) {
    throw new Error(`${label} Mach-O architecture does not match the release runtime`);
  }
  const expectedFileType = imageKind === "executable" ? 0x02 : imageKind === "dylib" ? 0x06 : null;
  if (!expectedFileType || contents.readUInt32LE(12) !== expectedFileType) {
    throw new Error(`${label} Mach-O file type does not match its runtime role`);
  }
  const commandCount = contents.readUInt32LE(16);
  const commandBytes = contents.readUInt32LE(20);
  const commandEnd = 32 + commandBytes;
  if (commandCount > 16_384 || commandEnd > contents.byteLength) {
    throw new Error(`${label} has an invalid Mach-O load-command table`);
  }
  const dependencies = [];
  const dylinkers = [];
  const rpaths = [];
  let offset = 32;
  for (let index = 0; index < commandCount; index += 1) {
    if (offset + 8 > commandEnd) {
      throw new Error(`${label} has a truncated Mach-O load command`);
    }
    const command = contents.readUInt32LE(offset);
    const commandSize = contents.readUInt32LE(offset + 4);
    if (commandSize < 8 || commandSize % 8 !== 0 || offset + commandSize > commandEnd) {
      throw new Error(`${label} has an invalid Mach-O load command`);
    }
    if (MACHO_DYLIB_COMMANDS.has(command) || command === MACHO_RPATH_COMMAND ||
        command === MACHO_LOAD_DYLINKER_COMMAND) {
      if (commandSize < 12) throw new Error(`${label} has a truncated Mach-O path command`);
      const value = readMachOCommandString(
        contents,
        offset,
        commandSize,
        contents.readUInt32LE(offset + 8),
        label,
      );
      if (command === MACHO_RPATH_COMMAND) rpaths.push(value);
      else if (command === MACHO_LOAD_DYLINKER_COMMAND) dylinkers.push(value);
      else dependencies.push(value);
    } else if (MACHO_REJECTED_COMMANDS.has(command)) {
      throw new Error(`${label} contains an unsupported legacy or environment load command`);
    }
    offset += commandSize;
  }
  if (offset !== commandEnd) throw new Error(`${label} Mach-O load commands are not canonical`);
  if (imageKind === "executable" &&
      (dylinkers.length !== 1 || dylinkers[0] !== "/usr/lib/dyld")) {
    throw new Error(`${label} must bind exactly one trusted dynamic loader`);
  }
  if (imageKind === "dylib" && dylinkers.length !== 0) {
    throw new Error(`${label} dynamic library cannot declare a dynamic loader`);
  }
  return {
    dependencies: [...new Set(dependencies)],
    dylinkers,
    rpaths: [...new Set(rpaths)],
  };
}

function isSystemMachODependency(value) {
  return path.posix.normalize(value) === value &&
    (value.startsWith("/usr/lib/") || value.startsWith("/System/Library/"));
}

function assertPrivateRuntimeRelative(relative) {
  const normalized = path.posix.normalize(relative);
  assertSafeRelative(normalized);
  if (!normalized.startsWith("bin/") && !normalized.startsWith("lib/")) {
    throw new Error(`Node runtime dependency has an unsupported private layout: ${relative}`);
  }
  return normalized;
}

function resolveRunpath(value, image, executable) {
  if (path.isAbsolute(value)) {
    return { original: path.resolve(value), private: "lib" };
  }
  for (const [prefix, originalRoot, privateRoot] of [
    ["@loader_path", path.dirname(image.source), path.posix.dirname(image.relative)],
    ["@executable_path", path.dirname(executable.source), path.posix.dirname(executable.relative)],
  ]) {
    if (value === prefix || value.startsWith(`${prefix}/`)) {
      const suffix = value.slice(prefix.length).replace(/^\//, "");
      return {
        original: path.resolve(originalRoot, suffix),
        private: path.posix.normalize(path.posix.join(privateRoot, suffix)),
      };
    }
  }
  throw new Error(`Node runtime contains an unsupported Mach-O runpath: ${value}`);
}

async function resolveMachODependency(value, image, executable, searchRpaths) {
  if (isSystemMachODependency(value)) return null;
  const candidates = [];
  if (path.isAbsolute(value)) {
    candidates.push({ original: value, private: `lib/${path.posix.basename(value)}` });
  } else if (value === "@loader_path" || value.startsWith("@loader_path/")) {
    const suffix = value.slice("@loader_path".length).replace(/^\//, "");
    candidates.push({
      original: path.resolve(path.dirname(image.source), suffix),
      private: path.posix.join(path.posix.dirname(image.relative), suffix),
    });
  } else if (value === "@executable_path" || value.startsWith("@executable_path/")) {
    const suffix = value.slice("@executable_path".length).replace(/^\//, "");
    candidates.push({
      original: path.resolve(path.dirname(executable.source), suffix),
      private: path.posix.join(path.posix.dirname(executable.relative), suffix),
    });
  } else if (value === "@rpath" || value.startsWith("@rpath/")) {
    const suffix = value.slice("@rpath".length).replace(/^\//, "");
    for (const runpath of searchRpaths) {
      candidates.push({
        original: path.resolve(runpath.original, suffix),
        private: path.posix.join(runpath.private, suffix),
      });
    }
  } else {
    throw new Error(`Node runtime contains an unsupported Mach-O dependency: ${value}`);
  }

  const resolvedCandidates = [];
  for (const candidate of candidates) {
    let canonical;
    try {
      canonical = await realpath(candidate.original);
    } catch (error) {
      if (error?.code === "ENOENT") continue;
      throw error;
    }
    if (!isInside("/opt/homebrew/Cellar", canonical) &&
        !isInside("/usr/local/Cellar", canonical)) {
      throw new Error(`Node runtime dependency is outside the sealed Homebrew closure: ${value}`);
    }
    resolvedCandidates.push({
      private: assertPrivateRuntimeRelative(candidate.private),
      source: canonical,
    });
  }
  if (resolvedCandidates.length === 0) {
    throw new Error(`Node runtime dependency cannot be resolved: ${value}`);
  }
  const sources = new Set(resolvedCandidates.map((candidate) => candidate.source));
  if (sources.size !== 1) {
    throw new Error(`Node runtime dependency resolves ambiguously: ${value}`);
  }
  return resolvedCandidates[0];
}

function nodeClosureEntries(files) {
  const directories = new Set();
  for (const file of files) {
    let current = path.posix.dirname(file.relative);
    while (current !== ".") {
      directories.add(current);
      current = path.posix.dirname(current);
    }
  }
  return [
    ...[...directories].map((relative) => ({ kind: "directory", mode: 0o500, path: relative })),
    ...files.map((file) => ({
      kind: "file",
      mode: file.identity.mode & ~0o222,
      path: file.relative,
      sha256: file.identity.sha256,
      size: file.identity.size,
    })),
  ].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
}

export async function collectNodeRuntimeClosure(nodeExecutable, options = {}) {
  const inspectFile = options.inspectFile ?? inspectToolFile;
  const readImage = options.readImage ?? readFile;
  const resolveDependency = options.resolveDependency ?? resolveMachODependency;
  const node = await inspectFile(nodeExecutable, "Node executable");
  const executable = { relative: "bin/node", source: node.canonical };
  const queue = [{ ...executable, depth: 0, inheritedRpaths: [] }];
  const files = new Map([[executable.relative, { ...executable, identity: node.identity }]]);
  const pathIdentities = new Map();
  assertNoIdentityCollision(executable.relative, pathIdentities);
  const systemLeaves = new Set();
  let totalBytes = node.identity.size;
  const processed = new Set();
  while (queue.length > 0) {
    const image = queue.shift();
    if (image.depth > NODE_RUNTIME_MAX_DEPTH) {
      throw new Error("Node runtime dependency closure exceeds its maximum depth");
    }
    const commands = parseMachOLoadCommands(
      await readImage(image.source),
      `Node runtime image ${image.relative}`,
      image.relative === "bin/node" ? "executable" : "dylib",
    );
    const ownRpaths = commands.rpaths.map((value) => resolveRunpath(value, image, executable));
    const searchRpaths = [...ownRpaths, ...image.inheritedRpaths].filter(
      (candidate, index, values) => values.findIndex(
        (value) => value.original === candidate.original && value.private === candidate.private,
      ) === index,
    );
    const contextKey = `${image.relative}\0${searchRpaths.map(
      (value) => `${value.original}\0${value.private}`,
    ).join("\0")}`;
    if (processed.has(contextKey)) continue;
    processed.add(contextKey);
    if (processed.size > NODE_RUNTIME_MAX_CONTEXTS) {
      throw new Error("Node runtime dependency closure exceeds its bounded context count");
    }
    for (const dependency of commands.dependencies) {
      if (isSystemMachODependency(dependency)) {
        systemLeaves.add(path.posix.basename(dependency).normalize("NFC").toLowerCase());
        continue;
      }
      const resolved = await resolveDependency(dependency, image, executable, searchRpaths);
      const expectedPrivate = `lib/${path.posix.basename(dependency)}`;
      if (resolved.private !== expectedPrivate) {
        throw new Error(`Node runtime dependency cannot use the private flat layout: ${dependency}`);
      }
      const inspected = await inspectFile(resolved.source, `Node runtime dependency ${dependency}`);
      const previous = files.get(resolved.private);
      if (previous) {
        if (previous.source !== inspected.canonical ||
            JSON.stringify(previous.identity) !== JSON.stringify(inspected.identity)) {
          throw new Error(`Node runtime dependency layout collides at ${resolved.private}`);
        }
        queue.push({ ...previous, depth: image.depth + 1, inheritedRpaths: searchRpaths });
        continue;
      }
      const file = {
        identity: inspected.identity,
        relative: resolved.private,
        source: inspected.canonical,
      };
      assertNoIdentityCollision(file.relative, pathIdentities);
      totalBytes += inspected.identity.size;
      if (files.size + 1 > NODE_RUNTIME_MAX_FILES || totalBytes > NODE_RUNTIME_MAX_BYTES) {
        throw new Error("Node runtime dependency closure exceeds its bounded size");
      }
      files.set(file.relative, file);
      queue.push({ ...file, depth: image.depth + 1, inheritedRpaths: searchRpaths });
    }
  }
  const sources = [...files.values()].sort(
    (left, right) => left.relative < right.relative ? -1 : left.relative > right.relative ? 1 : 0,
  );
  const entries = nodeClosureEntries(sources);
  for (const source of sources) {
    if (source.relative.startsWith("lib/") &&
        systemLeaves.has(path.posix.basename(source.relative).normalize("NFC").toLowerCase())) {
      throw new Error(`Node runtime private library collides with a system dependency: ${source.relative}`);
    }
  }
  return {
    entries,
    executable: node.canonical,
    sources,
  };
}

function nodeRuntimeIdentity(nodeIdentity, version, entries) {
  return {
    ...nodeIdentity,
    closure_count: entries.length,
    closure_sha256: sha256(ledgerBytes(entries)),
    path: "bin/node",
    version,
  };
}

async function inspectStagedNodeRuntime(root, version) {
  const entries = await inspectTree(root, {
    allowSymlink: () => false,
    include: (relative) => ["bin", "lib"].includes(relative.split("/")[0]),
  });
  const node = await inspectToolFile(path.join(root, "bin", "node"), "private Node executable");
  return {
    entries,
    executable: node.canonical,
    identity: nodeRuntimeIdentity(node.identity, version, entries),
    root,
  };
}

async function assertStagedNodeRuntimeClosed(root, entries) {
  const filePaths = new Set(entries.filter((entry) => entry.kind === "file").map((entry) => entry.path));
  const systemLeaves = new Set();
  for (const entry of entries) {
    if (entry.kind !== "file") continue;
    const commands = parseMachOLoadCommands(
      await readFile(path.join(root, ...entry.path.split("/"))),
      `sealed Node runtime image ${entry.path}`,
      entry.path === "bin/node" ? "executable" : "dylib",
    );
    for (const dependency of commands.dependencies) {
      if (isSystemMachODependency(dependency)) {
        systemLeaves.add(path.posix.basename(dependency).normalize("NFC").toLowerCase());
        continue;
      }
      if (path.isAbsolute(dependency) &&
          !dependency.startsWith("/opt/homebrew/") && !dependency.startsWith("/usr/local/")) {
        throw new Error(`sealed Node runtime contains an untrusted absolute dependency: ${dependency}`);
      }
      if (!path.isAbsolute(dependency) &&
          !["@loader_path", "@executable_path", "@rpath"].some(
            (prefix) => dependency === prefix || dependency.startsWith(`${prefix}/`),
          )) {
        throw new Error(`sealed Node runtime contains an unsupported dependency: ${dependency}`);
      }
      const privatePath = `lib/${path.posix.basename(dependency)}`;
      assertSafeRelative(privatePath);
      if (!filePaths.has(privatePath)) {
        throw new Error(`sealed Node runtime dependency is missing from its closure: ${privatePath}`);
      }
    }
  }
  for (const filename of filePaths) {
    if (filename.startsWith("lib/") &&
        systemLeaves.has(path.posix.basename(filename).normalize("NFC").toLowerCase())) {
      throw new Error(`sealed Node runtime library collides with a system dependency: ${filename}`);
    }
  }
}

async function inspectNpmPackage(npmCliPath) {
  const npmCli = await inspectToolFile(npmCliPath, "npm CLI");
  let packageRoot = path.dirname(npmCli.canonical);
  let packageFile;
  for (;;) {
    const candidate = path.join(packageRoot, "package.json");
    try {
      const details = await lstat(candidate);
      if (details.isSymbolicLink() || !details.isFile() || details.nlink !== 1) {
        throw new Error("npm package manifest must be one regular, unlinked file");
      }
      packageFile = candidate;
      break;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    const parent = path.dirname(packageRoot);
    if (parent === packageRoot) throw new Error("npm CLI is not inside an npm package root");
    packageRoot = parent;
  }
  packageRoot = await realpath(packageRoot);
  const packageBytes = await readFile(packageFile);
  const manifest = JSON.parse(packageBytes.toString("utf8"));
  if (manifest?.name !== "npm") throw new Error("npm package name must be exactly npm");
  if (!manifest.bin || typeof manifest.bin !== "object" || Array.isArray(manifest.bin) ||
      typeof manifest.bin.npm !== "string") {
    throw new Error("npm package bin must identify the selected npm CLI");
  }
  assertSafeRelative(manifest.bin.npm);
  let declaredCli;
  try {
    declaredCli = await realpath(path.join(packageRoot, ...manifest.bin.npm.split("/")));
  } catch (error) {
    throw new Error("npm package bin does not match the selected npm CLI", { cause: error });
  }
  if (declaredCli !== npmCli.canonical) {
    throw new Error("npm package bin does not match the selected npm CLI");
  }
  if (typeof manifest.version !== "string" || !/^[A-Za-z0-9._+-]{1,80}$/.test(manifest.version)) {
    throw new Error("npm package version is unsafe");
  }
  const entries = await inspectTree(packageRoot, { allowSymlink: () => true });
  if (entries.length === 0) throw new Error("npm package closure is empty");
  return {
    cli: npmCli,
    entries,
    packageBytes,
    packageRoot,
    relativeCli: path.relative(packageRoot, npmCli.canonical).split(path.sep).join("/"),
    version: manifest.version,
  };
}

function npmPackageIdentity(npmPackage, version = npmPackage.version) {
  return {
    ...npmPackage.cli.identity,
    closure_count: npmPackage.entries.length,
    closure_sha256: sha256(ledgerBytes(readonlyEntries(npmPackage.entries))),
    package_json_sha256: sha256(npmPackage.packageBytes),
    package_version: npmPackage.version,
    path: npmPackage.relativeCli,
    version,
  };
}

async function inspectRuntimeFiles(npmCliPath, nodeExecutable, versions = {}, nodeRoot) {
  const nodeClosure = nodeRoot
    ? await inspectStagedNodeRuntime(nodeRoot, versions.node)
    : await collectNodeRuntimeClosure(nodeExecutable);
  const node = await inspectToolFile(nodeExecutable, "Node executable");
  const npmPackage = await inspectNpmPackage(npmCliPath);
  const runtime = {
    arch: process.arch,
    node: nodeRuntimeIdentity(node.identity, versions.node, nodeClosure.entries),
    npm_cli: npmPackageIdentity(npmPackage, versions.npm ?? npmPackage.version),
    platform: process.platform,
  };
  if (runtime.npm_cli.version !== runtime.npm_cli.package_version) {
    throw new Error("npm CLI version does not match its package manifest");
  }
  assertRuntimeIdentity(runtime);
  return {
    node: node.canonical,
    node_entries: nodeClosure.entries,
    node_root: nodeRoot,
    node_sources: nodeClosure.sources,
    npm_cli: npmPackage.cli.canonical,
    npm_entries: npmPackage.entries,
    npm_root: npmPackage.packageRoot,
    runtime,
  };
}

async function inspectRuntime(npmCliPath, nodeExecutable = process.execPath, nodeRoot) {
  const before = await inspectRuntimeFiles(npmCliPath, nodeExecutable, {
    node: process.version,
  }, nodeRoot);
  const closedEnvironment = {
    CI: "1",
    ...(nodeRoot ? { DYLD_LIBRARY_PATH: path.join(nodeRoot, "lib") } : {}),
    PATH: path.dirname(before.node),
    npm_config_audit: "false",
    npm_config_fund: "false",
    npm_config_update_notifier: "false",
  };
  const [{ stdout: nodeStdout }, { stdout: npmStdout }] = await Promise.all([
    executeFile(before.node, ["--version"], { env: closedEnvironment, timeout: 10_000 }),
    executeFile(before.node, [before.npm_cli, "--version"], {
      env: closedEnvironment,
      timeout: 10_000,
    }),
  ]);
  if (nodeStdout.trim() !== before.runtime.node.version ||
      npmStdout.trim() !== before.runtime.npm_cli.package_version) {
    throw new Error("npm CLI version does not match its package manifest");
  }
  const after = await inspectRuntimeFiles(npmCliPath, nodeExecutable, {
    node: nodeStdout.trim(),
    npm: npmStdout.trim(),
  }, nodeRoot);
  assertRuntimeEqual(after.runtime, before.runtime, "release runtime execution probe");
  return after;
}

async function inspectSandboxExecutable() {
  const sandbox = await inspectToolFile(SANDBOX_EXECUTABLE, "Darwin sandbox executable");
  return { ...sandbox.identity, path: sandbox.canonical };
}

async function resolveRuntimeIdentity(npmCliPath) {
  return inspectRuntime(npmCliPath);
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function assertSafeRelative(relative) {
  const parts = relative.split("/");
  if (!relative || path.posix.isAbsolute(relative) || relative.includes("\\") ||
      /[\u0000-\u001f\u007f]/u.test(relative) ||
      parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error(`release closure contains an unsafe path: ${relative}`);
  }
}

function assertNoIdentityCollision(relative, identities) {
  const identity = relative.normalize("NFC").toLowerCase();
  const previous = identities.get(identity);
  if (previous && previous !== relative) {
    throw new Error(`release closure has a case-folded or Unicode path collision: ${previous} / ${relative}`);
  }
  identities.set(identity, relative);
}

function ledgerBytes(entries) {
  return Buffer.from(`${entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`, "utf8");
}

async function inspectTree(root, options = {}) {
  const resolvedRoot = path.resolve(root);
  const rootDetails = await lstat(resolvedRoot);
  if (rootDetails.isSymbolicLink() || !rootDetails.isDirectory()) {
    throw new Error("release closure root must be a real directory");
  }
  const canonicalRoot = await realpath(resolvedRoot);
  const identities = new Map();
  const entries = [];

  async function visit(directory, parentRelative = "") {
    const children = await readdir(directory, { withFileTypes: true });
    children.sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0);
    for (const child of children) {
      const relative = parentRelative ? `${parentRelative}/${child.name}` : child.name;
      if (!parentRelative && options.excludedTopLevel?.has(relative)) continue;
      if (options.include && !options.include(relative, child.isDirectory())) continue;
      assertSafeRelative(relative);
      assertNoIdentityCollision(relative, identities);
      const filename = path.join(directory, child.name);
      const details = await lstat(filename);
      if (details.isDirectory()) {
        entries.push({ kind: "directory", mode: details.mode & 0o777, path: relative });
        await visit(filename, relative);
      } else if (details.isFile()) {
        if (details.nlink !== 1 && !options.allowHardlink?.(relative)) {
          throw new Error(`release closure contains a hardlinked file: ${relative}`);
        }
        const contents = await readFile(filename);
        entries.push({
          kind: "file",
          mode: details.mode & 0o777,
          path: relative,
          sha256: sha256(contents),
          size: contents.byteLength,
        });
      } else if (details.isSymbolicLink()) {
        if (!options.allowSymlink?.(relative)) {
          throw new Error(`release closure contains a disallowed symlink: ${relative}`);
        }
        const target = await readlink(filename);
        if (!target || path.isAbsolute(target) || target.includes("\0")) {
          throw new Error(`release dependency symlink is unsafe: ${relative}`);
        }
        const lexicalTarget = path.resolve(path.dirname(filename), target);
        if (!isInside(resolvedRoot, lexicalTarget)) {
          throw new Error(`release dependency symlink escapes the dependency root: ${relative}`);
        }
        const canonicalTarget = await realpath(filename).catch(() => null);
        if (!canonicalTarget || !isInside(canonicalRoot, canonicalTarget)) {
          throw new Error(`release dependency symlink escapes the dependency root: ${relative}`);
        }
        const targetBytes = Buffer.from(target, "utf8");
        entries.push({
          kind: "symlink",
          path: relative,
          sha256: sha256(targetBytes),
          size: targetBytes.byteLength,
          target,
        });
      } else {
        throw new Error(`release closure contains a special file: ${relative}`);
      }
    }
  }

  await visit(resolvedRoot);
  entries.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return entries;
}

function includesReleaseSource(relative) {
  if (SOURCE_FILES.has(relative)) return true;
  if ([...SOURCE_FILES].some((filename) => filename.startsWith(`${relative}/`))) return true;
  if (/^(?:middleware\.(?:js|jsx|ts|tsx)|instrumentation(?:-client)?\.(?:js|ts))$/.test(relative)) {
    return true;
  }
  return SOURCE_DIRECTORIES.has(relative.split("/")[0]);
}

function classifiesExcludedReleaseSource(relative) {
  const topLevel = relative.split("/")[0];
  return SOURCE_EXCLUSIONS.has(topLevel) || SOURCE_EXCLUDED_FILES.has(relative) ||
    SOURCE_EXCLUDED_DIRECTORIES.has(topLevel);
}

function includeClassifiedReleaseSource(relative) {
  if (includesReleaseSource(relative)) return true;
  if (classifiesExcludedReleaseSource(relative)) return false;
  throw new Error(`release source contract has an unclassified tracked source path: ${relative}`);
}

async function containsPrivateKeyMarker(filename) {
  const begin = "-----BEGIN ";
  const ending = "PRIVATE KEY-----";
  let active = false;
  let beginTail = "";
  let endingTail = "";
  for await (const chunk of createReadStream(filename, { highWaterMark: 64 * 1024 })) {
    for (const character of chunk.toString("latin1")) {
      if (!active) {
        beginTail = `${beginTail}${character}`.slice(-begin.length);
        if (beginTail === begin) {
          active = true;
          beginTail = "";
          endingTail = "";
        }
        continue;
      }
      if (character === "\n" || character === "\r") {
        active = false;
        endingTail = "";
        beginTail = "";
        continue;
      }
      endingTail = `${endingTail}${character}`.slice(-ending.length);
      if (endingTail === ending) return true;
    }
  }
  return false;
}

async function assertNoSensitiveSource(root) {
  const candidates = await inspectTree(root, {
    allowSymlink: () => false,
    excludedTopLevel: SOURCE_EXCLUSIONS,
  });
  for (const entry of candidates) {
    if (entry.kind !== "file") continue;
    const basename = path.posix.basename(entry.path);
    if (/^\.env(?:\.|$)/i.test(basename)) {
      throw new Error(`release source closure contains a dotenv input: ${entry.path}`);
    }
    if (!includesReleaseSource(entry.path)) continue;
    if (/\.(?:key|pem)$/i.test(basename)) {
      throw new Error(`release source closure contains a private key input: ${entry.path}`);
    }
    if (await containsPrivateKeyMarker(path.join(root, ...entry.path.split("/")))) {
      throw new Error(`release source closure contains private key material: ${entry.path}`);
    }
  }
}

async function gitOutput(args, options = {}) {
  const { stdout } = await executeFile(GIT_EXECUTABLE, args, {
    encoding: options.encoding ?? "utf8",
    env: {
      HOME: "",
      LANG: "C",
      LC_ALL: "C",
      PATH: "/usr/bin:/bin",
    },
    maxBuffer: options.maxBuffer ?? 8 * 1024 * 1024,
    timeout: 30_000,
  });
  return stdout;
}

async function exportTrackedSource(source, destination) {
  const gitRoot = (await gitOutput(["-C", source, "rev-parse", "--show-toplevel"])).trim();
  const canonicalGitRoot = await canonicalDirectory(gitRoot, "release source Git root");
  const sourceRelative = path.relative(canonicalGitRoot, source).split(path.sep).join("/");
  assertSafeRelative(sourceRelative);
  const status = await gitOutput([
    "-C", canonicalGitRoot,
    "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no",
    "--", sourceRelative,
  ]);
  if (status.trim()) {
    throw new Error("release source closure has tracked dirty or untracked inputs");
  }
  const commit = (await gitOutput(["-C", canonicalGitRoot, "rev-parse", "HEAD^{commit}"])).trim();
  const tree = (await gitOutput(["-C", canonicalGitRoot, "rev-parse", `${commit}:${sourceRelative}`])).trim();
  assertGitObjectId(commit, "release source commit");
  assertGitObjectId(tree, "release source tree");
  const treeOutput = await gitOutput([
    "-C", canonicalGitRoot,
    "ls-tree", "-rz", "--full-tree", commit, "--", sourceRelative,
  ], { encoding: "buffer" });
  const records = [];
  for (const row of treeOutput.toString("utf8").split("\0")) {
    if (!row) continue;
    const match = /^(100644|100755) blob ([0-9a-f]{40,64})\t(.+)$/.exec(row);
    if (!match) throw new Error("release source Git tree contains a non-file or unsafe mode");
    const relative = path.posix.relative(sourceRelative, match[3]);
    assertSafeRelative(relative);
    if (includeClassifiedReleaseSource(relative)) {
      records.push({ mode: match[1] === "100755" ? 0o755 : 0o644, oid: match[2], path: relative });
    }
  }
  records.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const observed = new Set(records.map((record) => record.path));
  for (const required of SOURCE_FILES) {
    if (!observed.has(required)) throw new Error(`release source Git tree is missing ${required}`);
  }
  await mkdir(destination, { mode: 0o700 });
  for (const record of records) {
    const target = path.join(destination, ...record.path.split("/"));
    await mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
    const contents = await gitOutput(
      ["-C", canonicalGitRoot, "cat-file", "blob", record.oid],
      { encoding: "buffer", maxBuffer: 16 * 1024 * 1024 },
    );
    await writeFile(target, contents, { flag: "wx", mode: record.mode });
  }
  return { commit, evidence: "git-tracked-blobs", tree };
}

async function prepareSourceExport(source, destination, production) {
  await assertNoSensitiveSource(source);
  let identity;
  if (production) {
    identity = await exportTrackedSource(source, destination);
  } else {
    const entries = await inspectTree(source, {
      allowSymlink: () => false,
      include: includeClassifiedReleaseSource,
    });
    await copyInspectedTree(source, destination, entries);
    identity = { commit: null, evidence: "test-fixture", tree: null };
  }
  await assertNoSensitiveSource(destination);
  const entries = await inspectTree(destination, { allowSymlink: () => false });
  const observed = new Set(entries.filter((entry) => entry.kind === "file").map((entry) => entry.path));
  for (const required of SOURCE_FILES) {
    if (!observed.has(required)) throw new Error(`release source closure is missing ${required}`);
  }
  const ledger = ledgerBytes(entries);
  return {
    entries,
    identity: {
      ...identity,
      count: entries.length,
      sha256: sha256(ledger),
    },
    ledger,
  };
}

async function copyInspectedTree(sourceRoot, destinationRoot, entries) {
  await mkdir(destinationRoot, { mode: 0o700 });
  for (const entry of entries) {
    if (entry.kind !== "directory") continue;
    const destination = path.join(destinationRoot, ...entry.path.split("/"));
    await mkdir(destination, { recursive: true, mode: 0o700 });
  }
  for (const entry of entries) {
    if (entry.kind === "directory") continue;
    const source = path.join(sourceRoot, ...entry.path.split("/"));
    const destination = path.join(destinationRoot, ...entry.path.split("/"));
    await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
    if (entry.kind === "file") {
      await copyFile(source, destination);
      await chmod(destination, entry.mode);
    } else {
      await symlink(entry.target, destination);
    }
  }
  const directories = entries
    .filter((entry) => entry.kind === "directory")
    .sort((left, right) => right.path.length - left.path.length);
  for (const entry of directories) {
    await chmod(path.join(destinationRoot, ...entry.path.split("/")), entry.mode);
  }
}

async function copyNodeRuntimeClosure(destinationRoot, entries, sources) {
  const sourceByPath = new Map((sources ?? []).map((source) => [source.relative, source.source]));
  await mkdir(destinationRoot, { mode: 0o700 });
  for (const entry of entries) {
    const destination = path.join(destinationRoot, ...entry.path.split("/"));
    if (entry.kind === "directory") {
      await mkdir(destination, { recursive: true, mode: 0o700 });
      continue;
    }
    const source = sourceByPath.get(entry.path);
    if (!source) throw new Error(`Node runtime closure source is missing: ${entry.path}`);
    await mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
    await copyFile(source, destination, constants.COPYFILE_EXCL);
    await chmod(destination, entry.mode);
  }
  const directories = entries
    .filter((entry) => entry.kind === "directory")
    .sort((left, right) => right.path.length - left.path.length);
  for (const entry of directories) {
    await chmod(path.join(destinationRoot, ...entry.path.split("/")), entry.mode);
  }
}

function readonlyEntries(entries) {
  return entries.map((entry) => entry.kind === "symlink"
    ? entry
    : { ...entry, mode: entry.mode & ~0o222 });
}

async function hardenTree(root, entries) {
  for (const entry of entries) {
    if (entry.kind !== "file") continue;
    await chmod(path.join(root, ...entry.path.split("/")), entry.mode & ~0o222);
  }
  const directories = entries
    .filter((entry) => entry.kind === "directory")
    .sort((left, right) => right.path.length - left.path.length);
  for (const entry of directories) {
    await chmod(path.join(root, ...entry.path.split("/")), entry.mode & ~0o222);
  }
  await chmod(root, 0o500);
}

async function makeDirectoriesWritable(root) {
  let details;
  try {
    details = await lstat(root);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw error;
  }
  if (details.isSymbolicLink() || !details.isDirectory()) return;
  await chmod(root, 0o700);
  for (const child of await readdir(root, { withFileTypes: true })) {
    if (child.isDirectory()) await makeDirectoriesWritable(path.join(root, child.name));
  }
}

function expectedStagedRuntime(runtime, npmEntries) {
  return {
    ...runtime,
    node: { ...runtime.node, mode: runtime.node.mode & ~0o222 },
    npm_cli: {
      ...runtime.npm_cli,
      closure_sha256: sha256(ledgerBytes(readonlyEntries(npmEntries))),
      mode: runtime.npm_cli.mode & ~0o222,
    },
  };
}

async function assertRuntimeStable(inspected, label) {
  let observed;
  try {
    observed = inspected.execution_probe
      ? await inspectRuntime(inspected.npm_cli, inspected.node, inspected.node_root)
      : await inspectRuntimeFiles(inspected.npm_cli, inspected.node, {
        node: inspected.runtime.node.version,
        npm: inspected.runtime.npm_cli.version,
      }, inspected.node_root);
  } catch (error) {
    throw new Error(`${label} runtime identity changed`, { cause: error });
  }
  assertRuntimeEqual(observed.runtime, inspected.runtime, `${label} runtime identity`);
  return observed;
}

async function stageRuntime(inspected, parent, requireExecutable) {
  const runtimeRoot = path.join(parent, "sealed-runtime");
  const stagedNode = path.join(runtimeRoot, "bin", "node");
  const stagedNpm = path.join(runtimeRoot, "npm");
  await copyNodeRuntimeClosure(runtimeRoot, inspected.node_entries, inspected.node_sources);
  await copyInspectedTree(inspected.npm_root, stagedNpm, inspected.npm_entries);
  await hardenTree(stagedNpm, inspected.npm_entries);
  const stagedCli = path.join(stagedNpm, ...inspected.runtime.npm_cli.path.split("/"));
  const staged = requireExecutable
    ? await inspectRuntime(stagedCli, stagedNode, runtimeRoot)
    : await inspectRuntimeFiles(stagedCli, stagedNode, {
      node: inspected.runtime.node.version,
      npm: inspected.runtime.npm_cli.version,
    }, runtimeRoot);
  const expected = expectedStagedRuntime(inspected.runtime, inspected.npm_entries);
  assertRuntimeEqual(staged.runtime, expected, "private staged runtime measured source");
  await chmod(runtimeRoot, 0o500);
  return { ...staged, execution_probe: requireExecutable, root: runtimeRoot };
}

async function stageBuildNode(expectedNode, sourceRoot, sourceEntries, parent, requireExecutable) {
  const runtimeRoot = path.join(parent, "sealed-runtime");
  await copyInspectedTree(sourceRoot, runtimeRoot, sourceEntries);
  const before = await inspectStagedNodeRuntime(runtimeRoot, expectedNode.version);
  assertRuntimeComponentEqual(
    before.identity,
    expectedNode,
    NODE_RUNTIME_IDENTITY_FIELDS,
    "private build Node verified supply runtime",
  );
  if (requireExecutable) {
    let stdout;
    try {
      ({ stdout } = await executeFile(before.executable, ["--version"], {
        env: {
          CI: "1",
          DYLD_LIBRARY_PATH: path.join(runtimeRoot, "lib"),
          PATH: path.join(runtimeRoot, "bin"),
        },
        timeout: 10_000,
      }));
    } catch (error) {
      throw new Error("private build Node executable proof is pending", { cause: error });
    }
    if (stdout.trim() !== expectedNode.version) {
      throw new Error("private build Node version does not match the verified supply runtime");
    }
  }
  const after = await inspectStagedNodeRuntime(runtimeRoot, expectedNode.version);
  assertRuntimeComponentEqual(
    after.identity,
    before.identity,
    NODE_RUNTIME_IDENTITY_FIELDS,
    "private build Node execution probe",
  );
  await chmod(runtimeRoot, 0o500);
  return {
    execution_probe: requireExecutable,
    identity: expectedNode,
    library_path: path.join(runtimeRoot, "lib"),
    path: after.executable,
    root: runtimeRoot,
  };
}

async function assertBuildNodeStable(stagedNode) {
  const observed = await inspectStagedNodeRuntime(stagedNode.root, stagedNode.identity.version);
  try {
    assertRuntimeComponentEqual(
      observed.identity,
      stagedNode.identity,
      NODE_RUNTIME_IDENTITY_FIELDS,
      "private build Node release build",
    );
  } catch (error) {
    throw new Error("private build Node identity changed during the release build", { cause: error });
  }
}

async function assertRegularFile(filename, label) {
  let details;
  try {
    details = await lstat(filename);
  } catch (error) {
    if (error?.code === "ENOENT") throw new Error(`${label} is missing`, { cause: error });
    throw error;
  }
  if (details.isSymbolicLink() || !details.isFile() || details.nlink !== 1) {
    throw new Error(`${label} must be one regular, unlinked file`);
  }
}

async function canonicalDirectory(directory, label) {
  const resolved = path.resolve(directory);
  const details = await lstat(resolved);
  if (details.isSymbolicLink() || !details.isDirectory()) {
    throw new Error(`${label} must be a real directory`);
  }
  return realpath(resolved);
}

async function canonicalPathCandidate(filename) {
  const suffix = [];
  let cursor = path.resolve(filename);
  for (;;) {
    try {
      const canonical = await realpath(cursor);
      return path.join(canonical, ...suffix.reverse());
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      try {
        const details = await lstat(cursor);
        if (details.isSymbolicLink()) {
          throw new Error("release artifact path contains a dangling symlink");
        }
      } catch (detailsError) {
        if (detailsError?.code !== "ENOENT") throw detailsError;
      }
      const parent = path.dirname(cursor);
      if (parent === cursor) throw error;
      suffix.push(path.basename(cursor));
      cursor = parent;
    }
  }
}

function stableDirectoryIdentity(details) {
  return {
    dev: details.dev,
    ino: details.ino,
    mode: details.mode,
    uid: details.uid,
  };
}

function sameStableDirectory(left, right) {
  return left.dev === right.dev && left.ino === right.ino &&
    left.mode === right.mode && left.uid === right.uid;
}

async function acquirePublication(destination, label) {
  const parent = path.dirname(destination);
  const parentCanonical = await realpath(parent);
  if (parentCanonical !== parent) {
    throw new Error(`${label} publication parent must be canonical`);
  }
  const parentHandle = await open(
    parent,
    constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) |
      (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
  );
  let lockHandle;
  let lockIdentity;
  let lockPath;
  let destinationHandle;
  let destinationIdentity;
  try {
    const parentDetails = await parentHandle.stat({ bigint: true });
    if (!parentDetails.isDirectory() || Number(parentDetails.mode & 0o777n) !== 0o700) {
      throw new Error(`${label} publication parent must have mode 0700`);
    }
    if (typeof process.getuid === "function" && parentDetails.uid !== BigInt(process.getuid())) {
      throw new Error(`${label} publication parent must be owned by the current user`);
    }
    lockPath = path.join(parent, `.${path.basename(destination)}.lock`);
    try {
      lockHandle = await open(
        lockPath,
        constants.O_RDWR | constants.O_CREAT | constants.O_EXCL |
          (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
        0o600,
      );
    } catch (error) {
      if (error?.code === "EEXIST") {
        throw new Error(`${label} publication lock is already held`);
      }
      throw error;
    }
    const lockDetails = await lockHandle.stat({ bigint: true });
    lockIdentity = stableDirectoryIdentity(lockDetails);
    try {
      await mkdir(destination, { mode: 0o700 });
    } catch (error) {
      if (error?.code === "EEXIST") throw new Error(`${label} already exists`);
      throw error;
    }
    destinationHandle = await open(
      destination,
      constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) |
        (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
    );
    const destinationDetails = await destinationHandle.stat({ bigint: true });
    if (!destinationDetails.isDirectory() || Number(destinationDetails.mode & 0o777n) !== 0o700) {
      throw new Error(`${label} reserved destination must have mode 0700`);
    }
    destinationIdentity = stableDirectoryIdentity(destinationDetails);
    return {
      completion: null,
      destination,
      destinationHandle,
      destinationIdentity,
      label,
      lockHandle,
      lockIdentity,
      lockPath,
      parent,
      parentHandle,
      parentIdentity: stableDirectoryIdentity(parentDetails),
    };
  } catch (error) {
    await destinationHandle?.close();
    if (destinationIdentity) {
      try {
        const details = await lstat(destination, { bigint: true });
        if (details.isDirectory() &&
            sameStableDirectory(stableDirectoryIdentity(details), destinationIdentity)) {
          await rm(destination, { recursive: true, force: true });
        }
      } catch {
        // A replaced reserved destination is not owned by this failed acquisition.
      }
    }
    if (lockHandle && lockIdentity && lockPath) {
      try {
        const details = await lstat(lockPath, { bigint: true });
        if (sameStableDirectory(stableDirectoryIdentity(details), lockIdentity)) {
          await rm(lockPath);
        }
      } catch {
        // A replaced lock path is not owned by this failed acquisition.
      }
    }
    await lockHandle?.close();
    await parentHandle.close();
    throw error;
  }
}

async function assertPublicationControlStable(publication) {
  const details = await lstat(publication.parent, { bigint: true });
  if (details.isSymbolicLink() || !details.isDirectory() ||
      !sameStableDirectory(stableDirectoryIdentity(details), publication.parentIdentity) ||
      await realpath(publication.parent) !== publication.parent) {
    throw new Error(`${publication.label} publication parent identity changed`);
  }
  const lockDetails = await lstat(publication.lockPath, { bigint: true });
  if (!lockDetails.isFile() ||
      !sameStableDirectory(stableDirectoryIdentity(lockDetails), publication.lockIdentity)) {
    throw new Error(`${publication.label} publication lock identity changed`);
  }
}

async function assertPublicationStable(publication) {
  await assertPublicationControlStable(publication);
  const pathDetails = await lstat(publication.destination, { bigint: true });
  const handleDetails = await publication.destinationHandle.stat({ bigint: true });
  if (!pathDetails.isDirectory() || !handleDetails.isDirectory() ||
      !sameStableDirectory(stableDirectoryIdentity(pathDetails), publication.destinationIdentity) ||
      !sameStableDirectory(stableDirectoryIdentity(handleDetails), publication.destinationIdentity) ||
      await realpath(publication.destination) !== publication.destination) {
    throw new Error(`${publication.label} reserved destination identity changed`);
  }
}

async function releasePublication(publication) {
  let mayUnlink = false;
  try {
    if (publication.destinationRemoved) await assertPublicationControlStable(publication);
    else await assertPublicationStable(publication);
    mayUnlink = true;
  } catch {
    // A replaced parent is intentionally left untouched; the displaced lock fails closed.
  }
  try {
    if (mayUnlink) await rm(publication.lockPath);
  } finally {
    if (!publication.destinationRemoved) await publication.destinationHandle.close();
    await publication.lockHandle.close();
    await publication.parentHandle.close();
  }
}

async function cleanupPublicationDestination(publication) {
  try {
    await assertPublicationStable(publication);
    if (publication.completion) {
      try {
        const markerDetails = await lstat(
          path.join(publication.destination, COMPLETION_FILE),
          { bigint: true },
        );
        const marker = await inspectToolFile(
          path.join(publication.destination, COMPLETION_FILE),
          "publication completion marker",
        );
        if (markerDetails.isFile() &&
            sameStableDirectory(
              stableDirectoryIdentity(markerDetails),
              publication.completion.stableIdentity,
            ) && sameStableDirectory(marker.stable_identity, publication.completion.stableIdentity) &&
            JSON.stringify(marker.identity) === JSON.stringify(publication.completion.identity)) {
          return;
        }
      } catch {
        // An invalid marker in the still-owned destination remains incomplete.
      }
    }
    await publication.destinationHandle.close();
    await makeDirectoriesWritable(publication.destination);
    await rm(publication.destination, { recursive: true, force: true });
    publication.destinationRemoved = true;
  } catch {
    // Never clean a completed artifact or through a replaced parent/destination.
  }
}

async function recordPublicationSync(publication, event, synchronize) {
  await synchronize();
  await publication.syncRecorder?.(event);
}

async function syncPublicationContents(publication) {
  await assertPublicationStable(publication);
  const before = await inspectTree(publication.destination, { allowSymlink: () => true });
  for (const entry of before.filter((candidate) => candidate.kind === "file")) {
    const filename = path.join(publication.destination, ...entry.path.split("/"));
    const handle = await open(
      filename,
      constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
    );
    try {
      const first = await handle.stat({ bigint: true });
      if (!first.isFile() || first.nlink !== 1n) {
        throw new Error(`publication content file is not one regular file: ${entry.path}`);
      }
      await recordPublicationSync(publication, `content-file:${entry.path}`, () => handle.sync());
      const second = await handle.stat({ bigint: true });
      const pathDetails = await lstat(filename, { bigint: true });
      if (!sameFileIdentity(first, second) || !pathDetails.isFile() ||
          !sameStableDirectory(stableDirectoryIdentity(second), stableDirectoryIdentity(pathDetails))) {
        throw new Error(`publication content file identity changed during sync: ${entry.path}`);
      }
    } finally {
      await handle.close();
    }
  }
  const directories = before
    .filter((entry) => entry.kind === "directory")
    .sort((left, right) => {
      const depth = right.path.split("/").length - left.path.split("/").length;
      return depth || (left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
    });
  for (const entry of directories) {
    const filename = path.join(publication.destination, ...entry.path.split("/"));
    const handle = await open(
      filename,
      constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) |
        (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
    );
    try {
      const first = await handle.stat({ bigint: true });
      if (!first.isDirectory()) {
        throw new Error(`publication content directory changed before sync: ${entry.path}`);
      }
      await recordPublicationSync(publication, `content-directory:${entry.path}`, () => handle.sync());
      const second = await handle.stat({ bigint: true });
      const pathDetails = await lstat(filename, { bigint: true });
      if (!second.isDirectory() || !pathDetails.isDirectory() ||
          !sameStableDirectory(stableDirectoryIdentity(first), stableDirectoryIdentity(second)) ||
          !sameStableDirectory(stableDirectoryIdentity(second), stableDirectoryIdentity(pathDetails))) {
        throw new Error(`publication content directory identity changed during sync: ${entry.path}`);
      }
    } finally {
      await handle.close();
    }
  }
  const after = await inspectTree(publication.destination, { allowSymlink: () => true });
  if (!ledgerBytes(after).equals(ledgerBytes(before))) {
    throw new Error("publication content tree changed during durability sync");
  }
  await assertPublicationStable(publication);
}

async function writeCompletionMarker(publication, value) {
  await syncPublicationContents(publication);
  await assertPublicationStable(publication);
  const expected = Buffer.from(canonicalJson(value), "utf8");
  const markerPath = path.join(publication.destination, COMPLETION_FILE);
  const handle = await open(
    markerPath,
    constants.O_RDWR | constants.O_CREAT | constants.O_EXCL |
      (constants.O_NOFOLLOW ?? 0) | (constants.O_CLOEXEC ?? 0),
    0o600,
  );
  let markerIdentity;
  let markerStableIdentity;
  try {
    await handle.writeFile(expected);
    await recordPublicationSync(publication, `marker-file:${COMPLETION_FILE}`, () => handle.sync());
    const before = await handle.stat({ bigint: true });
    const observed = Buffer.alloc(expected.byteLength);
    const { bytesRead } = await handle.read(observed, 0, observed.byteLength, 0);
    const after = await handle.stat({ bigint: true });
    if (!before.isFile() || before.nlink !== 1n || before.size !== BigInt(expected.byteLength) ||
        bytesRead !== expected.byteLength ||
        !observed.equals(expected) || !sameFileIdentity(before, after)) {
      throw new Error("publication completion marker failed exact verification");
    }
    markerIdentity = {
      mode: Number(after.mode & 0o777n),
      sha256: sha256(observed),
      size: Number(after.size),
    };
    markerStableIdentity = stableDirectoryIdentity(after);
  } finally {
    await handle.close();
  }
  await recordPublicationSync(
    publication,
    "destination-directory:.",
    () => publication.destinationHandle.sync(),
  );
  await recordPublicationSync(
    publication,
    "publication-parent:..",
    () => publication.parentHandle.sync(),
  );
  await assertPublicationStable(publication);
  const markerDetails = await lstat(markerPath, { bigint: true });
  const inspected = await inspectToolFile(markerPath, "publication completion marker");
  if (!markerDetails.isFile() ||
      !sameStableDirectory(stableDirectoryIdentity(markerDetails), markerStableIdentity) ||
      !sameStableDirectory(inspected.stable_identity, markerStableIdentity) ||
      JSON.stringify(inspected.identity) !== JSON.stringify(markerIdentity)) {
    throw new Error("publication completion marker changed before publication");
  }
  publication.completion = { identity: markerIdentity, stableIdentity: markerStableIdentity };
}

async function sealDependencyTree({
  dependencyRoot,
  evidence,
  originalRuntime,
  publication,
  sourceExport,
  sourceIdentity,
  stagedRuntime,
}) {
  assertRuntimeIdentity(originalRuntime.runtime);
  assertRuntimeIdentity(stagedRuntime.runtime);
  await assertPublicationStable(publication);
  const packageFile = path.join(sourceExport, "package.json");
  const lockFile = path.join(sourceExport, "package-lock.json");
  await assertRegularFile(packageFile, "Web package manifest");
  await assertRegularFile(lockFile, "Web package lock");
  const packageBytes = await readFile(packageFile);
  const lockBytes = await readFile(lockFile);
  const dependencyEntries = await inspectTree(dependencyRoot, {
    allowHardlink: () => true,
    allowSymlink: () => true,
  });
  if (dependencyEntries.length === 0) {
    throw new Error("release dependency closure is empty");
  }
  const metadata = {
    acquisition: {
      command: ["sealed-node", "sealed-npm-cli", "ci"],
      evidence,
      network: "acquisition-only",
      source: "isolated",
    },
    closure: "complete",
    dependency_root: "node_modules",
    lock_sha256: sha256(lockBytes),
    original_runtime: originalRuntime.runtime,
    package_sha256: sha256(packageBytes),
    runtime: stagedRuntime.runtime,
    schema: SUPPLY_SCHEMA,
    source: sourceIdentity,
    source_root: "source",
    tooling_node_root: "tooling/node",
    tooling_root: "tooling/npm",
  };
  const root = publication.destination;
  await mkdir(path.join(root, "inputs"), { mode: 0o700 });
  await mkdir(path.join(root, "tooling"), { mode: 0o700 });
  await writeFile(path.join(root, METADATA_FILE), canonicalJson(metadata), { flag: "wx", mode: 0o600 });
  await writeFile(path.join(root, "inputs", "package.json"), packageBytes, { flag: "wx", mode: 0o600 });
  await writeFile(path.join(root, "inputs", "package-lock.json"), lockBytes, { flag: "wx", mode: 0o600 });
  const sourceEntries = await inspectTree(sourceExport, { allowSymlink: () => false });
  await copyInspectedTree(sourceExport, path.join(root, "source"), sourceEntries);
  await copyInspectedTree(dependencyRoot, path.join(root, "node_modules"), dependencyEntries);
  await copyInspectedTree(stagedRuntime.node_root, path.join(root, "tooling", "node"), stagedRuntime.node_entries);
  await copyInspectedTree(stagedRuntime.npm_root, path.join(root, "tooling", "npm"), stagedRuntime.npm_entries);
  const entries = await inspectTree(root, {
    allowSymlink: (relative) => relative.startsWith("node_modules/") || relative.startsWith("tooling/npm/"),
    excludedTopLevel: new Set([COMPLETION_FILE, LEDGER_FILE]),
  });
  const ledger = ledgerBytes(entries);
  await writeFile(path.join(root, LEDGER_FILE), ledger, { flag: "wx", mode: 0o600 });
  const supplyDigest = sha256(ledger);
  const releaseEligible = evidence === "darwin-acquisition" && sourceIdentity.evidence === "git-tracked-blobs";
  await verifyReleaseSupplyContents({
    expectedLockDigest: metadata.lock_sha256,
    expectedSupplyDigest: supplyDigest,
    supplyRoot: root,
  });
  await writeCompletionMarker(publication, {
    kind: "release-supply",
    release_eligible: releaseEligible,
    schema: SUPPLY_SCHEMA,
    supply_sha256: supplyDigest,
  });
  return {
    evidence,
    lock_sha256: metadata.lock_sha256,
    package_sha256: metadata.package_sha256,
    release_eligible: releaseEligible,
    source_sha256: sourceIdentity.sha256,
    supply_sha256: supplyDigest,
  };
}

export async function acquireReleaseSupply(options) {
  if (Object.hasOwn(options ?? {}, "runtimeIdentity")) {
    throw new Error("runtimeIdentity is not an accepted production input");
  }
  assertOptionKeys(
    options,
    ["destination", "installRunner", "npmCliPath", "sourceRoot", "syncRecorder"],
    "release supply acquisition",
  );
  const {
    destination,
    installRunner = executeFile,
    npmCliPath,
    sourceRoot,
    syncRecorder,
  } = options;
  if (!destination || !sourceRoot) throw new Error("release supply acquisition requires source and destination roots");
  if (!npmCliPath) throw new Error("release supply acquisition requires an absolute npm CLI path");
  if (syncRecorder !== undefined &&
      (installRunner === executeFile || typeof syncRecorder !== "function")) {
    throw new Error("publication sync recorder is available only to an injected acquisition fixture");
  }
  const source = await canonicalDirectory(sourceRoot, "Web source root");
  const output = await canonicalPathCandidate(destination);
  if (isInside(source, output) || isInside(output, source)) {
    throw new Error("release supply canonical roots must be disjoint");
  }
  const inspectedRuntime = await resolveRuntimeIdentity(npmCliPath);
  const evidence = installRunner === executeFile ? "darwin-acquisition" : "test-fixture";
  if (evidence === "darwin-acquisition" && process.platform !== "darwin") {
    throw new Error("R0 release supply acquisition requires Darwin");
  }
  let acquisitionRoot;
  let failure;
  let publication;
  let published = false;
  let result;
  try {
    acquisitionRoot = await realpath(await mkdtemp(path.join(os.tmpdir(), "cortex-web-supply-acquire-")));
    const sourceExport = path.join(acquisitionRoot, "source-export");
    const sourceSnapshot = await prepareSourceExport(
      source,
      sourceExport,
      evidence === "darwin-acquisition",
    );
    const sourcePackageBytes = await readFile(path.join(sourceExport, "package.json"));
    const sourceLockBytes = await readFile(path.join(sourceExport, "package-lock.json"));
    const stagedRuntime = await stageRuntime(
      inspectedRuntime,
      acquisitionRoot,
      evidence === "darwin-acquisition",
    );
    publication = await acquirePublication(output, "release supply destination");
    publication.syncRecorder = syncRecorder;
    await writeFile(path.join(acquisitionRoot, "package.json"), sourcePackageBytes, { flag: "wx", mode: 0o600 });
    await writeFile(path.join(acquisitionRoot, "package-lock.json"), sourceLockBytes, { flag: "wx", mode: 0o600 });
    const cacheRoot = path.join(acquisitionRoot, "empty-cache");
    const emptyHome = path.join(acquisitionRoot, "empty-home");
    await mkdir(cacheRoot, { mode: 0o700 });
    await mkdir(emptyHome, { mode: 0o700 });
    await assertRuntimeStable(inspectedRuntime, "original acquisition");
    await assertRuntimeStable(stagedRuntime, "private acquisition");
    await installRunner(
      stagedRuntime.node,
      [stagedRuntime.npm_cli, "ci", "--cache", cacheRoot, "--no-audit", "--no-fund"],
      {
        cwd: acquisitionRoot,
        env: {
          CI: "1",
          DYLD_LIBRARY_PATH: path.join(stagedRuntime.node_root, "lib"),
          HOME: emptyHome,
          PATH: [path.dirname(stagedRuntime.node), "/usr/bin", "/bin"].join(path.delimiter),
          npm_config_audit: "false",
          npm_config_cache: cacheRoot,
          npm_config_fund: "false",
          npm_config_update_notifier: "false",
        },
        maxBuffer: 4 * 1024 * 1024,
        timeout: 300_000,
      },
    );
    const acquiredPackageBytes = await readFile(path.join(acquisitionRoot, "package.json"));
    const acquiredLockBytes = await readFile(path.join(acquisitionRoot, "package-lock.json"));
    if (!acquiredPackageBytes.equals(sourcePackageBytes) || !acquiredLockBytes.equals(sourceLockBytes)) {
      throw new Error("Web package inputs changed during dependency acquisition");
    }
    await assertRuntimeStable(inspectedRuntime, "original acquisition");
    const observedStagedRuntime = await assertRuntimeStable(stagedRuntime, "private acquisition");
    result = await sealDependencyTree({
      dependencyRoot: path.join(acquisitionRoot, "node_modules"),
      evidence,
      originalRuntime: inspectedRuntime,
      publication,
      sourceExport,
      sourceIdentity: sourceSnapshot.identity,
      stagedRuntime: observedStagedRuntime,
    });
    published = true;
  } catch (error) {
    failure = new Error(`release supply acquisition failed: ${error.message}`, { cause: error });
  } finally {
    try {
      if (!published && publication) await cleanupPublicationDestination(publication);
      if (acquisitionRoot) {
        await makeDirectoriesWritable(acquisitionRoot);
        await rm(acquisitionRoot, { recursive: true, force: true });
      }
      if (published) {
        await assertPublicationStable(publication);
      }
      if (publication) await releasePublication(publication);
    } catch (error) {
      failure ??= new Error("release supply cleanup failed", { cause: error });
    }
  }
  if (failure) throw failure;
  return result;
}

function assertExactObjectKeys(value, expected, label) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join("\0") !== [...expected].sort().join("\0")) {
    throw new Error(`${label} has unknown or missing fields`);
  }
}

function assertOptionKeys(value, expected, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} options are required`);
  }
  const unknown = Object.keys(value).filter((key) => !expected.includes(key));
  if (unknown.length > 0) {
    throw new Error(`${label} has unknown option: ${unknown.sort().join(", ")}`);
  }
}

async function verifyReleaseSupplyContents(options) {
  const { expectedLockDigest, expectedSupplyDigest, supplyRoot } = options;
  assertDigest(expectedLockDigest, "expected Web lock digest");
  assertDigest(expectedSupplyDigest, "expected supply identity");
  const root = await canonicalDirectory(supplyRoot, "release supply root");
  await assertRegularFile(path.join(root, LEDGER_FILE), "release supply checksum ledger");
  const entries = await inspectTree(root, {
    allowSymlink: (relative) => relative.startsWith("node_modules/") || relative.startsWith("tooling/npm/"),
    excludedTopLevel: new Set([COMPLETION_FILE, LEDGER_FILE]),
  });
  const observedLedger = await readFile(path.join(root, LEDGER_FILE));
  const expectedLedger = ledgerBytes(entries);
  if (!observedLedger.equals(expectedLedger)) {
    throw new Error("release supply checksum ledger does not match its closed contents");
  }
  const observedSupplyDigest = sha256(observedLedger);
  if (observedSupplyDigest !== expectedSupplyDigest) {
    throw new Error("release supply identity does not match the caller pin");
  }
  const metadataBytes = await readFile(path.join(root, METADATA_FILE));
  const metadata = JSON.parse(metadataBytes.toString("utf8"));
  assertExactObjectKeys(
    metadata,
    [
      "acquisition",
      "closure",
      "dependency_root",
      "lock_sha256",
      "original_runtime",
      "package_sha256",
      "runtime",
      "schema",
      "source",
      "source_root",
      "tooling_node_root",
      "tooling_root",
    ],
    "release supply metadata",
  );
  assertExactObjectKeys(
    metadata.acquisition,
    ["command", "evidence", "network", "source"],
    "release supply acquisition",
  );
  if (!metadataBytes.equals(Buffer.from(canonicalJson(metadata), "utf8")) ||
      metadata.schema !== SUPPLY_SCHEMA || metadata.closure !== "complete" ||
      metadata.dependency_root !== "node_modules" ||
      metadata.source_root !== "source" || metadata.tooling_node_root !== "tooling/node" ||
      metadata.tooling_root !== "tooling/npm" ||
      JSON.stringify(metadata.acquisition) !== JSON.stringify({
        command: ["sealed-node", "sealed-npm-cli", "ci"],
        evidence: metadata.acquisition.evidence,
        network: "acquisition-only",
        source: "isolated",
      }) || !["darwin-acquisition", "test-fixture"].includes(metadata.acquisition.evidence)) {
    throw new Error("release supply metadata is not canonical or closed");
  }
  assertRuntimeIdentity(metadata.runtime);
  assertRuntimeIdentity(metadata.original_runtime);
  assertStagedRuntimeMatchesOrigin(
    metadata.runtime,
    metadata.original_runtime,
    "release supply private runtime origin",
  );
  assertExactObjectKeys(
    metadata.source,
    ["commit", "count", "evidence", "sha256", "tree"],
    "release supply source identity",
  );
  if (!Number.isSafeInteger(metadata.source.count) || metadata.source.count <= 0) {
    throw new Error("release supply source entry count is invalid");
  }
  assertDigest(metadata.source.sha256, "release supply source digest");
  if (metadata.source.evidence === "git-tracked-blobs") {
    assertGitObjectId(metadata.source.commit, "release source commit");
    assertGitObjectId(metadata.source.tree, "release source tree");
  } else if (metadata.source.evidence !== "test-fixture" ||
      metadata.source.commit !== null || metadata.source.tree !== null) {
    throw new Error("release supply source evidence is invalid");
  }
  const lockBytes = await readFile(path.join(root, "inputs", "package-lock.json"));
  const packageBytes = await readFile(path.join(root, "inputs", "package.json"));
  if (sha256(lockBytes) !== metadata.lock_sha256 || metadata.lock_sha256 !== expectedLockDigest) {
    throw new Error("release supply lock digest does not match the Web source");
  }
  if (sha256(packageBytes) !== metadata.package_sha256) {
    throw new Error("release supply package digest does not match its metadata");
  }
  const sourceEntries = await inspectTree(path.join(root, metadata.source_root), {
    allowSymlink: () => false,
  });
  if (sourceEntries.length !== metadata.source.count ||
      sha256(ledgerBytes(sourceEntries)) !== metadata.source.sha256) {
    throw new Error("release supply source closure does not match its metadata");
  }
  if (!(await readFile(path.join(root, metadata.source_root, "package.json"))).equals(packageBytes) ||
      !(await readFile(path.join(root, metadata.source_root, "package-lock.json"))).equals(lockBytes)) {
    throw new Error("release supply source package inputs do not match their sealed copies");
  }
  const npmCliPath = path.join(root, metadata.tooling_root, ...metadata.runtime.npm_cli.path.split("/"));
  const npmPackage = await inspectNpmPackage(npmCliPath);
  const observedNpmIdentity = npmPackageIdentity(npmPackage);
  assertRuntimeComponentEqual(
    observedNpmIdentity,
    metadata.runtime.npm_cli,
    NPM_RUNTIME_IDENTITY_FIELDS,
    "release supply npm package closure metadata",
  );
  const sealedNodeRoot = path.join(root, metadata.tooling_node_root);
  const sealedNode = await inspectStagedNodeRuntime(sealedNodeRoot, metadata.runtime.node.version);
  assertRuntimeComponentEqual(
    sealedNode.identity,
    metadata.runtime.node,
    NODE_RUNTIME_IDENTITY_FIELDS,
    "release supply Node closure metadata",
  );
  await assertStagedNodeRuntimeClosed(sealedNodeRoot, sealedNode.entries);
  const { stderr: loadedLibraries, stdout: nodeVersion } = await executeFile(
    sealedNode.executable,
    ["--version"],
    {
    env: {
      CI: "1",
      DYLD_LIBRARY_PATH: path.join(sealedNodeRoot, "lib"),
      DYLD_PRINT_LIBRARIES: "1",
      PATH: path.dirname(sealedNode.executable),
    },
    timeout: 10_000,
    },
  );
  // A Node linked only to system libraries has no private dylib to load.
  // The static closure check above still validates every declared dependency.
  const hasPrivateLibraries = sealedNode.entries.some((entry) => entry.kind === "file" && entry.path.startsWith("lib/"));
  if ((hasPrivateLibraries && !loadedLibraries.includes(`${sealedNodeRoot}${path.sep}lib${path.sep}`)) ||
      /\/opt\/homebrew\/(?:Cellar|opt)\//.test(loadedLibraries) ||
      /\/usr\/local\/(?:Cellar|opt)\//.test(loadedLibraries)) {
    throw new Error("release supply Node execution escaped its private library closure");
  }
  const sealedNodeAfter = await inspectStagedNodeRuntime(sealedNodeRoot, nodeVersion.trim());
  if (nodeVersion.trim() !== metadata.runtime.node.version) {
    throw new Error("release supply Node closure failed its private execution proof");
  }
  assertRuntimeComponentEqual(
    sealedNodeAfter.identity,
    metadata.runtime.node,
    NODE_RUNTIME_IDENTITY_FIELDS,
    "release supply Node private execution proof",
  );
  const dependencyEntries = await inspectTree(path.join(root, "node_modules"), {
    allowSymlink: () => true,
  });
  return {
    dependency_entries: dependencyEntries,
    lock_sha256: metadata.lock_sha256,
    metadata,
    node_entries: sealedNode.entries,
    package_sha256: metadata.package_sha256,
    release_eligible: metadata.acquisition.evidence === "darwin-acquisition" &&
      metadata.source.evidence === "git-tracked-blobs",
    source_entries: sourceEntries,
    supply_sha256: observedSupplyDigest,
  };
}

export async function verifyReleaseSupply(options) {
  if (Object.hasOwn(options ?? {}, "runtimeIdentity")) {
    throw new Error("runtimeIdentity is not an accepted production input");
  }
  assertOptionKeys(
    options,
    ["expectedLockDigest", "expectedSupplyDigest", "supplyRoot"],
    "release supply verification",
  );
  const root = await canonicalDirectory(options.supplyRoot, "release supply root");
  const completionPath = path.join(root, COMPLETION_FILE);
  await assertRegularFile(completionPath, "release supply completion marker");
  const completionBytes = await readFile(completionPath);
  const completion = JSON.parse(completionBytes.toString("utf8"));
  assertExactObjectKeys(
    completion,
    ["kind", "release_eligible", "schema", "supply_sha256"],
    "release supply completion marker",
  );
  if (!completionBytes.equals(Buffer.from(canonicalJson(completion), "utf8")) ||
      completion.kind !== "release-supply" || completion.schema !== SUPPLY_SCHEMA ||
      typeof completion.release_eligible !== "boolean") {
    throw new Error("release supply completion marker is invalid");
  }
  const result = await verifyReleaseSupplyContents(options);
  if (
      completion.supply_sha256 !== result.supply_sha256 ||
      completion.release_eligible !== result.release_eligible) {
    throw new Error("release supply completion marker is invalid");
  }
  return result;
}

async function readDependencyManifest(dependencyRoot, packageName) {
  const packageLabel = packageName === "vite" ? "Vite" : "Vinext";
  const manifestPath = path.join(dependencyRoot, packageName, "package.json");
  await assertRegularFile(manifestPath, `sealed ${packageLabel} package manifest`);
  const bytes = await readFile(manifestPath);
  let manifest;
  try {
    manifest = JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    throw new Error(`sealed ${packageLabel} package manifest is invalid`, { cause: error });
  }
  if (manifest?.name !== packageName) {
    throw new Error(`sealed ${packageLabel} package identity is invalid`);
  }
  return manifest;
}

async function resolveSealedBuildCommand(isolatedSource, dependencyRoot, dependencyEntries) {
  const canonicalDependencyRoot = await realpath(dependencyRoot);
  const packageBytes = await readFile(path.join(isolatedSource, "package.json"));
  const manifest = JSON.parse(packageBytes.toString("utf8"));
  const expectedVinext = manifest.devDependencies?.vinext;
  const expectedVite = manifest.devDependencies?.vite;
  if (manifest.type !== "module" ||
      typeof expectedVinext !== "string" || typeof expectedVite !== "string") {
    throw new Error("sealed Web package does not declare its exact Vinext build runtime");
  }
  const vinextManifest = await readDependencyManifest(dependencyRoot, "vinext");
  const viteManifest = await readDependencyManifest(dependencyRoot, "vite");
  if (vinextManifest.version !== expectedVinext || viteManifest.version !== expectedVite) {
    throw new Error("sealed Vinext build runtime does not match the Web package declaration");
  }
  const vinextBin = typeof vinextManifest.bin === "string"
    ? vinextManifest.bin
    : vinextManifest.bin?.vinext;
  if (typeof vinextBin !== "string") throw new Error("sealed Vinext package does not declare its CLI");
  assertSafeRelative(vinextBin);
  const expectedCli = path.join(dependencyRoot, "vinext", ...vinextBin.split("/"));
  let inspectedCli;
  try {
    inspectedCli = await inspectToolFile(expectedCli, "sealed Vinext CLI");
  } catch (error) {
    throw new Error("sealed Vinext CLI is missing or invalid", { cause: error });
  }
  const relativeCli = path.relative(canonicalDependencyRoot, inspectedCli.canonical).split(path.sep).join("/");
  const ledgerIdentity = dependencyEntries.find(
    (entry) => entry.kind === "file" && entry.path === relativeCli,
  );
  if (!ledgerIdentity || JSON.stringify(inspectedCli.identity) !== JSON.stringify({
    mode: ledgerIdentity.mode,
    sha256: ledgerIdentity.sha256,
    size: ledgerIdentity.size,
  })) {
    throw new Error("sealed Vinext CLI does not match the verified dependency closure");
  }
  return {
    cli: inspectedCli.canonical,
    identity: ledgerIdentity,
    path: relativeCli,
    version: vinextManifest.version,
    vite: viteManifest.version,
  };
}

function adapterIdentity(entries) {
  const adapter = entries.find((entry) => entry.path === "server/node-adapter.mjs");
  if (!adapter) throw new Error("release payload is missing its Node adapter");
  const text = adapter.contents.toString("utf8");
  const versions = [...text.matchAll(/^const ADAPTER_VERSION = ([1-9][0-9]*);$/gm)];
  if (versions.length !== 1) {
    throw new Error("release payload must contain one exact Node adapter version declaration");
  }
  return { sha256: adapter.sha256, version: Number.parseInt(versions[0][1], 10) };
}

function sandboxPath(value) {
  return JSON.stringify(path.resolve(value));
}

function sandboxAncestors(value) {
  const ancestors = [];
  let current = path.resolve(value);
  for (;;) {
    ancestors.push(current);
    const parent = path.dirname(current);
    if (parent === current) return ancestors.reverse();
    current = parent;
  }
}

async function writeSandboxProfile({
  buildRoot,
  cacheRoot,
  configTempRoot,
  isolatedSource,
}) {
  const sandbox = await inspectSandboxExecutable();
  const profilePath = path.join(buildRoot, "release-build.sb");
  const descriptor = {
    metadata_paths: sandboxAncestors(buildRoot),
    read_paths: ["/"],
    read_roots: [
      "/System",
      "/usr/bin",
      "/usr/lib",
      "/private/var/db",
      "/dev",
      buildRoot,
    ],
    write_roots: [
      path.join(isolatedSource, ".vinext"),
      path.join(isolatedSource, ".wrangler"),
      path.join(isolatedSource, "dist"),
      configTempRoot,
      cacheRoot,
      path.join(buildRoot, "temp"),
      path.join(buildRoot, "xdg-cache"),
      path.join(buildRoot, "xdg-config"),
      path.join(buildRoot, "xdg-data"),
    ],
  };
  const profile = sandboxProfile(descriptor);
  const profileBytes = Buffer.from(profile, "utf8");
  await writeFile(profilePath, profileBytes, { flag: "wx", mode: 0o600 });
  const inspectedProfile = await inspectToolFile(profilePath, "Darwin sandbox profile");
  if (inspectedProfile.identity.mode !== 0o600 ||
      inspectedProfile.identity.sha256 !== sha256(profileBytes)) {
    throw new Error("Darwin sandbox profile failed stable identity inspection");
  }
  return {
    identity: { mode: sandbox.mode, sha256: sandbox.sha256, size: sandbox.size },
    path: sandbox.path,
    policy: {
      descriptor,
      descriptor_sha256: canonicalDigest(descriptor),
      schema: SANDBOX_POLICY_SCHEMA,
    },
    profile: inspectedProfile,
    profilePath,
  };
}

function sandboxProfile(descriptor) {
  return [
    "(version 1)",
    "; cooperative release lock; this profile is not a hostile kernel-race claim",
    "(deny default)",
    "(deny network*)",
    "(allow process*)",
    "(allow sysctl-read)",
    "(allow mach-lookup)",
    `(allow file-read-metadata ${descriptor.metadata_paths.map((filename) => `(literal ${sandboxPath(filename)})`).join(" ")})`,
    `(allow file-read* ${descriptor.read_paths.map((filename) => `(literal ${sandboxPath(filename)})`).join(" ")} ${descriptor.read_roots.map((root) => `(subpath ${sandboxPath(root)})`).join(" ")})`,
    `(allow file-write* (literal "/dev/null") ${descriptor.write_roots.map((root) => `(subpath ${sandboxPath(root)})`).join(" ")})`,
    "",
  ].join("\n");
}

async function assertSandboxStable(sandbox) {
  const observed = await inspectToolFile(sandbox.path, "Darwin sandbox executable");
  if (JSON.stringify(observed.identity) !== JSON.stringify(sandbox.identity)) {
    throw new Error("Darwin sandbox executable identity changed during the release build");
  }
  const profile = await inspectToolFile(sandbox.profilePath, "Darwin sandbox profile");
  if (JSON.stringify(profile.identity) !== JSON.stringify(sandbox.profile.identity) ||
      !sameStableDirectory(profile.stable_identity, sandbox.profile.stable_identity) ||
      profile.identity.sha256 !== sha256(Buffer.from(sandboxProfile(sandbox.policy.descriptor), "utf8"))) {
    throw new Error("Darwin sandbox profile identity changed during the release build");
  }
}

async function assertNetworkGuardStable(networkGuard) {
  const observed = await inspectToolFile(networkGuard.path, "release network guard");
  if (JSON.stringify(observed.identity) !== JSON.stringify(networkGuard.identity) ||
      !sameStableDirectory(observed.stable_identity, networkGuard.stable_identity) ||
      observed.identity.sha256 !== sha256(Buffer.from(NETWORK_GUARD, "utf8"))) {
    throw new Error("release network guard identity changed during the release build");
  }
}

function sandboxedNodeArguments(sandbox, stagedNode, nodeArguments) {
  return [
    "-f",
    sandbox.profilePath,
    ENV_EXECUTABLE,
    `DYLD_LIBRARY_PATH=${stagedNode.library_path}`,
    stagedNode.path,
    ...nodeArguments,
  ];
}

function sandboxEvidence({ application, arguments: commandArguments, environment, networkGuard, sandbox }) {
  const closedEnvironment = closedEnvironmentDescriptor(environment);
  const evidence = {
    application,
    argv: [sandbox.path, ...commandArguments],
    closed_environment: closedEnvironment,
    closed_environment_sha256: canonicalDigest(closedEnvironment),
    mode: sandbox.identity.mode,
    network_guard_sha256: networkGuard.identity.sha256,
    path: sandbox.path,
    policy: sandbox.policy,
    profile_sha256: sandbox.profile.identity.sha256,
    sha256: sandbox.identity.sha256,
    size: sandbox.identity.size,
  };
  return evidence;
}

function assertSandboxEvidence(sandbox, evidence, releaseEligible, vinextPath) {
  assertExactObjectKeys(
    sandbox,
    [
      "application",
      "argv",
      "closed_environment",
      "closed_environment_sha256",
      "mode",
      "network_guard_sha256",
      "path",
      "policy",
      "profile_sha256",
      "sha256",
      "size",
    ],
    "release sandbox evidence",
  );
  assertExactObjectKeys(
    sandbox.policy,
    ["descriptor", "descriptor_sha256", "schema"],
    "release sandbox policy",
  );
  assertExactObjectKeys(
    sandbox.policy.descriptor,
    ["metadata_paths", "read_paths", "read_roots", "write_roots"],
    "release sandbox policy descriptor",
  );
  for (const [key, values] of Object.entries(sandbox.policy.descriptor)) {
    if (!Array.isArray(values) || values.length === 0 ||
        values.some((value) => typeof value !== "string" || !path.isAbsolute(value) ||
          /[\u0000-\u001f\u007f]/u.test(value)) ||
        new Set(values).size !== values.length) {
      throw new Error(`release sandbox policy ${key} is invalid`);
    }
  }
  if (sandbox.policy.schema !== SANDBOX_POLICY_SCHEMA ||
      sandbox.policy.descriptor_sha256 !== canonicalDigest(sandbox.policy.descriptor) ||
      sandbox.profile_sha256 !== sha256(Buffer.from(sandboxProfile(sandbox.policy.descriptor), "utf8"))) {
    throw new Error("release sandbox policy or profile identity is invalid");
  }
  assertDigest(sandbox.sha256, "release sandbox executable digest");
  assertDigest(sandbox.network_guard_sha256, "release network guard digest");
  assertDigest(sandbox.closed_environment_sha256, "release closed environment digest");
  if (sandbox.path !== SANDBOX_EXECUTABLE ||
      !Number.isSafeInteger(sandbox.mode) || sandbox.mode < 0 || sandbox.mode > 0o777 ||
      !Number.isSafeInteger(sandbox.size) || sandbox.size <= 0 ||
      sandbox.network_guard_sha256 !== sha256(Buffer.from(NETWORK_GUARD, "utf8"))) {
    throw new Error("release sandbox executable or network guard identity is invalid");
  }
  if (!Array.isArray(sandbox.closed_environment) || sandbox.closed_environment.length === 0 ||
      sandbox.closed_environment.some((entry) => !Array.isArray(entry) || entry.length !== 2 ||
        typeof entry[0] !== "string" || typeof entry[1] !== "string") ||
      JSON.stringify([...sandbox.closed_environment].sort(([left], [right]) =>
        left < right ? -1 : left > right ? 1 : 0)) !== JSON.stringify(sandbox.closed_environment) ||
      sandbox.closed_environment_sha256 !== canonicalDigest(sandbox.closed_environment)) {
    throw new Error("release sandbox closed environment identity is invalid");
  }
  const opensslConfiguration = sandbox.closed_environment.filter(([name]) => name === "OPENSSL_CONF");
  if (opensslConfiguration.length !== 1 || opensslConfiguration[0][1] !== "/dev/null") {
    throw new Error("release sandbox OpenSSL configuration is invalid");
  }
  assertSafeRelative(vinextPath);
  const profilePath = sandbox.argv?.[2];
  const buildRoot = typeof profilePath === "string" ? path.dirname(profilePath) : "";
  if (!Array.isArray(sandbox.argv) || sandbox.argv.length !== 8 ||
      sandbox.argv[0] !== sandbox.path || sandbox.argv[1] !== "-f" ||
      profilePath !== path.join(buildRoot, "release-build.sb") ||
      sandbox.argv[3] !== ENV_EXECUTABLE ||
      sandbox.argv[4] !== `DYLD_LIBRARY_PATH=${path.join(buildRoot, "sealed-runtime", "lib")}` ||
      sandbox.argv[5] !== path.join(buildRoot, "sealed-runtime", "bin", "node") ||
      sandbox.argv[6] !== path.join(buildRoot, "source", "node_modules", ...vinextPath.split("/")) ||
      sandbox.argv[7] !== "build" ||
      JSON.stringify(sandbox.policy.descriptor.read_paths) !== JSON.stringify(["/"]) ||
      !sandbox.policy.descriptor.read_roots.includes(buildRoot) ||
      JSON.stringify(sandbox.policy.descriptor.metadata_paths) !==
        JSON.stringify(sandboxAncestors(buildRoot))) {
    throw new Error("release sandbox argv identity is invalid");
  }
  const serialized = canonicalJson(sandbox);
  if (BUILD_SECRETS.some((secret) => serialized.includes(secret))) {
    throw new Error("release sandbox evidence contains a build secret");
  }
  if (![
    "allow-and-deny-probed",
    "fixture-unverified",
  ].includes(sandbox.application) ||
      (releaseEligible && (evidence !== "darwin-sandbox" ||
        sandbox.application !== "allow-and-deny-probed"))) {
    throw new Error("release sandbox application evidence is invalid");
  }
  return true;
}

function assertReleaseBuildProvenance(provenance) {
  assertExactObjectKeys(
    provenance,
    [
      "adapter",
      "build_command",
      "build_id",
      "build_sha256",
      "evidence",
      "lock_sha256",
      "release_eligible",
      "runtime",
      "sandbox",
      "schema",
      "source_sha256",
      "supply_sha256",
      "vinext",
    ],
    "release build provenance",
  );
  if (provenance.schema !== 1 ||
      !["darwin-sandbox", "test-fixture"].includes(provenance.evidence) ||
      typeof provenance.release_eligible !== "boolean" ||
      JSON.stringify(provenance.build_command) !==
        JSON.stringify(["sealed-node", "sealed-vinext", "build"])) {
    throw new Error("release build provenance is invalid");
  }
  for (const [value, label] of [
    [provenance.build_sha256, "release build digest"],
    [provenance.lock_sha256, "release build lock digest"],
    [provenance.source_sha256, "release build source digest"],
    [provenance.supply_sha256, "release build supply digest"],
  ]) {
    assertDigest(value, label);
  }
  assertRuntimeIdentity(provenance.runtime);
  assertExactObjectKeys(
    provenance.vinext,
    ["kind", "mode", "path", "sha256", "size", "version", "vite"],
    "release Vinext identity",
  );
  if (provenance.vinext.kind !== "file" ||
      !Number.isSafeInteger(provenance.vinext.mode) || provenance.vinext.mode < 0 ||
      provenance.vinext.mode > 0o777 || !Number.isSafeInteger(provenance.vinext.size) ||
      provenance.vinext.size <= 0) {
    throw new Error("release Vinext identity is invalid");
  }
  assertSafeRelative(provenance.vinext.path);
  assertDigest(provenance.vinext.sha256, "release Vinext digest");
  assertSandboxEvidence(
    provenance.sandbox,
    provenance.evidence,
    provenance.release_eligible,
    provenance.vinext.path,
  );
}

export async function verifyReleaseBuildOutput(options) {
  assertOptionKeys(options, ["outputRoot"], "release build output verification");
  const root = await canonicalDirectory(options.outputRoot, "release build output root");
  const completionPath = path.join(root, COMPLETION_FILE);
  const provenancePath = path.join(root, "provenance.json");
  const ledgerPath = path.join(root, "release-payload.sha256");
  await assertRegularFile(completionPath, "release build completion marker");
  await assertRegularFile(provenancePath, "release build provenance");
  await assertRegularFile(ledgerPath, "release payload checksum ledger");
  const completionBytes = await readFile(completionPath);
  const provenanceBytes = await readFile(provenancePath);
  const payloadLedger = await readFile(ledgerPath);
  const completion = JSON.parse(completionBytes.toString("utf8"));
  const provenance = JSON.parse(provenanceBytes.toString("utf8"));
  assertExactObjectKeys(
    completion,
    ["kind", "provenance_sha256", "release_eligible", "schema"],
    "release build completion marker",
  );
  if (!completionBytes.equals(Buffer.from(canonicalJson(completion), "utf8")) ||
      !provenanceBytes.equals(Buffer.from(canonicalJson(provenance), "utf8")) ||
      completion.kind !== "offline-release-build" || completion.schema !== 1 ||
      typeof completion.release_eligible !== "boolean" ||
      completion.provenance_sha256 !== sha256(provenanceBytes)) {
    throw new Error("release build completion marker or provenance is invalid");
  }
  assertReleaseBuildProvenance(provenance);
  if (completion.release_eligible !== provenance.release_eligible ||
      provenance.build_sha256 !== sha256(payloadLedger)) {
    throw new Error("release build completion or payload identity is invalid");
  }
  return { completion, provenance };
}

async function probeDarwinSandbox({ environment, isolatedSource, sandbox, stagedNode, supply }) {
  const probeEnvironment = {
    CI: "1",
    HOME: environment.HOME,
    LANG: "C",
    LC_ALL: "C",
    OPENSSL_CONF: environment.OPENSSL_CONF,
    PATH: environment.PATH,
    TMPDIR: environment.TMPDIR,
  };
  try {
    const allowed = await executeFile(sandbox.path, sandboxedNodeArguments(sandbox, stagedNode, [
      "--input-type=module",
      "--eval",
      "import { readFileSync } from 'node:fs'; process.stdout.write(String(readFileSync('package.json').byteLength));",
    ]), {
      cwd: isolatedSource,
      env: probeEnvironment,
      timeout: 10_000,
    });
    if (!/^[1-9][0-9]*$/.test(allowed.stdout)) {
      throw new Error("allowed read probe returned an invalid result");
    }
  } catch (error) {
    throw new Error("Darwin sandbox application evidence is pending: allowed read probe failed", {
      cause: error,
    });
  }

  const deniedRead = await executeFile(sandbox.path, sandboxedNodeArguments(sandbox, stagedNode, [
    "--input-type=module",
    "--eval",
    "import { readFileSync } from 'node:fs'; try { readFileSync(process.argv[1]); process.exit(91); } catch (error) { process.stdout.write(error.code ?? 'UNKNOWN'); }",
    path.join(supply, METADATA_FILE),
  ]), {
    cwd: isolatedSource,
    env: probeEnvironment,
    timeout: 10_000,
  }).catch((error) => {
    throw new Error("Darwin sandbox denied-read evidence is pending", { cause: error });
  });
  if (!/^(?:EACCES|EPERM)$/.test(deniedRead.stdout)) {
    throw new Error("Darwin sandbox did not deny the sealed supply read probe");
  }

  const deniedNetwork = await executeFile(sandbox.path, sandboxedNodeArguments(sandbox, stagedNode, [
    "--input-type=module",
    "--eval",
    "import net from 'node:net'; const server = net.createServer(); server.once('error', (error) => process.stdout.write(error.code ?? 'UNKNOWN')); server.listen(0, '127.0.0.1', () => process.exit(91));",
  ]), {
    cwd: isolatedSource,
    env: probeEnvironment,
    timeout: 10_000,
  }).catch((error) => {
    throw new Error("Darwin sandbox network-denial evidence is pending", { cause: error });
  });
  if (!/^(?:EACCES|EPERM)$/.test(deniedNetwork.stdout)) {
    throw new Error("Darwin sandbox did not deny the network probe");
  }
  await assertSandboxStable(sandbox);
  await assertBuildNodeStable(stagedNode);
  return "allow-and-deny-probed";
}

export async function buildReleaseOffline(options) {
  if (Object.hasOwn(options ?? {}, "runtimeIdentity")) {
    throw new Error("runtimeIdentity is not an accepted production input");
  }
  assertOptionKeys(
    options,
    ["buildId", "buildRunner", "expectedLockDigest", "expectedSupplyDigest", "outputRoot", "supplyRoot"],
    "offline release build",
  );
  const {
    buildId,
    buildRunner = executeFile,
    expectedLockDigest,
    expectedSupplyDigest,
    outputRoot,
    supplyRoot,
  } = options;
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$/.test(buildId ?? "")) {
    throw new Error("offline release build requires a safe 8-64 character build identifier");
  }
  if (!outputRoot || !supplyRoot) {
    throw new Error("offline release build requires supply and output roots");
  }
  assertDigest(expectedLockDigest, "expected Web lock digest");
  assertDigest(expectedSupplyDigest, "expected supply identity");
  const supply = await canonicalDirectory(supplyRoot, "release supply root");
  const output = await canonicalPathCandidate(outputRoot);
  if (isInside(supply, output) || isInside(output, supply)) {
    throw new Error("offline release canonical roots must be disjoint");
  }
  const evidence = buildRunner === executeFile ? "darwin-sandbox" : "test-fixture";
  if (evidence === "darwin-sandbox" && process.platform !== "darwin") {
    throw new Error("R0 offline release build requires Darwin sandbox-exec");
  }
  const verifiedSupply = await verifyReleaseSupply({
    expectedLockDigest,
    expectedSupplyDigest,
    supplyRoot: supply,
  });
  if (evidence === "darwin-sandbox" && !verifiedSupply.release_eligible) {
    throw new Error("production offline build refuses a test-fixture supply");
  }
  const sourceLedger = ledgerBytes(verifiedSupply.source_entries);
  const sourceDigest = verifiedSupply.metadata.source.sha256;
  const publication = await acquirePublication(output, "offline release output");
  let buildRoot;
  let stageOutput;
  let published = false;
  try {
    buildRoot = await realpath(await mkdtemp(path.join(os.tmpdir(), "cortex-web-offline-build-")));
    const isolatedSource = path.join(buildRoot, "source");
    stageOutput = publication.destination;
    await copyInspectedTree(
      path.join(supply, verifiedSupply.metadata.source_root),
      isolatedSource,
      verifiedSupply.source_entries,
    );
    const copiedSourceEntries = await inspectTree(isolatedSource, {
      allowSymlink: () => false,
    });
    if (!ledgerBytes(copiedSourceEntries).equals(sourceLedger)) {
      throw new Error("isolated Web source does not match its source identity");
    }
    const isolatedDependencies = path.join(isolatedSource, "node_modules");
    await copyInspectedTree(
      path.join(supply, "node_modules"),
      isolatedDependencies,
      verifiedSupply.dependency_entries,
    );
    const configTempRoot = path.join(isolatedDependencies, ".vite-temp");
    await mkdir(configTempRoot, { mode: 0o700 });
    await hardenTree(isolatedDependencies, verifiedSupply.dependency_entries);
    const expectedDependencyEntries = readonlyEntries(verifiedSupply.dependency_entries);
    const copiedDependencyEntries = await inspectTree(isolatedDependencies, {
      allowSymlink: () => true,
      excludedTopLevel: new Set([".vite-temp"]),
    });
    if (!ledgerBytes(copiedDependencyEntries).equals(ledgerBytes(expectedDependencyEntries))) {
      throw new Error("isolated dependency tree does not match the sealed supply");
    }
    const stagedNode = await stageBuildNode(
      verifiedSupply.metadata.runtime.node,
      path.join(supply, verifiedSupply.metadata.tooling_node_root),
      verifiedSupply.node_entries,
      buildRoot,
      evidence === "darwin-sandbox",
    );
    const sealedBuildCommand = await resolveSealedBuildCommand(
      isolatedSource,
      isolatedDependencies,
      expectedDependencyEntries,
    );
    const cacheRoot = path.join(buildRoot, "empty-cache");
    const emptyHome = path.join(buildRoot, "empty-home");
    const guardPath = path.join(buildRoot, "cortex-network-guard.mjs");
    const tempRoot = path.join(buildRoot, "temp");
    await mkdir(cacheRoot, { mode: 0o700 });
    await mkdir(emptyHome, { mode: 0o700 });
    await mkdir(tempRoot, { mode: 0o700 });
    await writeFile(guardPath, NETWORK_GUARD, { flag: "wx", mode: 0o600 });
    const inspectedNetworkGuard = await inspectToolFile(guardPath, "release network guard");
    const networkGuard = { ...inspectedNetworkGuard, path: inspectedNetworkGuard.canonical };
    if (networkGuard.identity.mode !== 0o600 ||
        networkGuard.identity.sha256 !== sha256(Buffer.from(NETWORK_GUARD, "utf8"))) {
      throw new Error("release network guard failed stable identity inspection");
    }
    const sandbox = await writeSandboxProfile({
      buildRoot,
      cacheRoot,
      configTempRoot,
      isolatedSource,
    });
    const buildPath = [
      path.join(isolatedDependencies, ".bin"),
      path.dirname(stagedNode.path),
      "/usr/bin",
      "/bin",
    ].join(path.delimiter);
    const buildEnvironment = {
      ALL_PROXY: "http://127.0.0.1:9",
      CI: "1",
      CORTEX_ACCESS_BOOTSTRAP_TOKEN: BUILD_SECRETS[1],
      CORTEX_CONTROL_TOKEN: BUILD_SECRETS[0],
      CORTEX_EMPTY_HOME: emptyHome,
      CORTEX_WEB_BUILD_ID: buildId,
      CORTEX_WEB_DRAFT_SECRET: BUILD_SECRETS[2],
      CORTEX_WEB_RELEASE_BUILD: "1",
      DYLD_LIBRARY_PATH: stagedNode.library_path,
      HOME: emptyHome,
      HTTPS_PROXY: "http://127.0.0.1:9",
      HTTP_PROXY: "http://127.0.0.1:9",
      LANG: "C",
      LC_ALL: "C",
      NODE_OPTIONS: `--import=${pathToFileURL(guardPath).href}`,
      NODE_PATH: "",
      NO_PROXY: "",
      OPENSSL_CONF: "/dev/null",
      PATH: buildPath,
      TMPDIR: tempRoot,
      TZ: "UTC",
      WRANGLER_LOG_PATH: ".wrangler/wrangler.log",
      XDG_CACHE_HOME: path.join(buildRoot, "xdg-cache"),
      XDG_CONFIG_HOME: path.join(buildRoot, "xdg-config"),
      XDG_DATA_HOME: path.join(buildRoot, "xdg-data"),
      npm_config_audit: "false",
      npm_config_cache: cacheRoot,
      npm_config_fund: "false",
      npm_config_offline: "true",
      npm_config_update_notifier: "false",
    };
    const sandboxApplication = evidence === "darwin-sandbox"
      ? await probeDarwinSandbox({
        environment: buildEnvironment,
        isolatedSource,
        sandbox,
        stagedNode,
        supply,
      })
      : "fixture-unverified";
    const buildArguments = sandboxedNodeArguments(sandbox, stagedNode, [
      sealedBuildCommand.cli,
      "build",
    ]);
    await assertSandboxStable(sandbox);
    await assertNetworkGuardStable(networkGuard);
    await buildRunner(sandbox.path, buildArguments, {
      cwd: isolatedSource,
      env: buildEnvironment,
      maxBuffer: 4 * 1024 * 1024,
      timeout: 120_000,
    });
    await assertPublicationStable(publication);
    await assertSandboxStable(sandbox);
    await assertNetworkGuardStable(networkGuard);
    await assertBuildNodeStable(stagedNode);
    await inspectTree(configTempRoot, { allowSymlink: () => false });
    await makeDirectoriesWritable(configTempRoot);
    await chmod(isolatedDependencies, 0o700);
    await rm(configTempRoot, { recursive: true, force: true });
    await chmod(isolatedDependencies, 0o500);
    const dependenciesAfterBuild = await inspectTree(isolatedDependencies, {
      allowSymlink: () => true,
    });
    if (!ledgerBytes(dependenciesAfterBuild).equals(ledgerBytes(expectedDependencyEntries))) {
      throw new Error("isolated dependency tree changed during the release build");
    }
    const sourceAfterBuild = await inspectTree(isolatedSource, {
      allowSymlink: () => false,
      excludedTopLevel: SOURCE_EXCLUSIONS,
    });
    if (!ledgerBytes(sourceAfterBuild).equals(sourceLedger)) {
      throw new Error("release build mutated its sealed source inputs");
    }
    const entries = await inspectReleasePayload(path.join(isolatedSource, "dist"), {
      forbiddenPrefixes: [supply, buildRoot, isolatedSource, process.env.HOME ?? ""],
      secretValues: BUILD_SECRETS,
      sourceRoot: isolatedSource,
    });
    const payloadLedger = Buffer.from(releasePayloadLedger(entries), "utf8");
    const sandboxIdentity = sandboxEvidence({
      application: sandboxApplication,
      arguments: buildArguments,
      environment: buildEnvironment,
      networkGuard,
      sandbox,
    });
    const sandboxIdentityVerified = assertSandboxEvidence(
      sandboxIdentity,
      evidence,
      evidence === "darwin-sandbox",
      sealedBuildCommand.path,
    );
    const provenance = {
      adapter: adapterIdentity(entries),
      build_command: ["sealed-node", "sealed-vinext", "build"],
      build_id: buildId,
      build_sha256: sha256(payloadLedger),
      evidence,
      lock_sha256: expectedLockDigest,
      release_eligible: evidence === "darwin-sandbox" &&
        verifiedSupply.release_eligible && stagedNode.execution_probe &&
        sandboxApplication === "allow-and-deny-probed" && sandboxIdentityVerified,
      runtime: verifiedSupply.metadata.runtime,
      sandbox: sandboxIdentity,
      schema: 1,
      source_sha256: sourceDigest,
      supply_sha256: verifiedSupply.supply_sha256,
      vinext: {
        ...sealedBuildCommand.identity,
        path: sealedBuildCommand.path,
        version: sealedBuildCommand.version,
        vite: sealedBuildCommand.vite,
      },
    };
    assertReleaseBuildProvenance(provenance);
    await writeReleasePayload(entries, path.join(stageOutput, "payload"));
    await writeFile(path.join(stageOutput, "release-payload.sha256"), payloadLedger, { flag: "wx", mode: 0o600 });
    const provenanceBytes = Buffer.from(canonicalJson(provenance), "utf8");
    await writeFile(path.join(stageOutput, "provenance.json"), provenanceBytes, { flag: "wx", mode: 0o600 });
    await verifyReleaseSupply({
      expectedLockDigest,
      expectedSupplyDigest,
      supplyRoot: supply,
    });
    await writeCompletionMarker(publication, {
      kind: "offline-release-build",
      provenance_sha256: sha256(provenanceBytes),
      release_eligible: provenance.release_eligible,
      schema: 1,
    });
    published = true;
    return { output_root: output, provenance };
  } finally {
    if (!published) await cleanupPublicationDestination(publication);
    await releasePublication(publication);
    if (buildRoot) {
      await makeDirectoriesWritable(buildRoot);
      await rm(buildRoot, { recursive: true, force: true });
    }
  }
}
