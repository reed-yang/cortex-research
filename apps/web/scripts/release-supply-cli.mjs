import path from "node:path";

import {
  acquireReleaseSupply,
  buildReleaseOffline,
  verifyReleaseSupply,
} from "./release-supply.mjs";

function parseOptions(values, allowed) {
  const options = {};
  for (let index = 0; index < values.length; index += 2) {
    const flag = values[index];
    const value = values[index + 1];
    if (!flag?.startsWith("--") || value === undefined || value.startsWith("--")) {
      throw new Error("release command options must be --name value pairs");
    }
    const name = flag.slice(2);
    if (!allowed.has(name)) throw new Error(`unknown release command option: --${name}`);
    if (Object.hasOwn(options, name)) throw new Error(`duplicate release command option: --${name}`);
    options[name] = value;
  }
  return options;
}

function requireOption(options, name) {
  const value = options[name];
  if (!value) throw new Error(`release command requires --${name}`);
  return value;
}

function npmCliPath(options) {
  const candidate = options["npm-cli"] ?? process.env.npm_execpath;
  if (!candidate || !path.isAbsolute(candidate)) {
    throw new Error("release command requires an absolute --npm-cli path");
  }
  return candidate;
}

async function run(command, values) {
  if (command === "acquire") {
    const options = parseOptions(values, new Set(["destination", "npm-cli", "source"]));
    return acquireReleaseSupply({
      destination: requireOption(options, "destination"),
      npmCliPath: npmCliPath(options),
      sourceRoot: requireOption(options, "source"),
    });
  }
  if (command === "verify") {
    const options = parseOptions(values, new Set(["lock-digest", "supply", "supply-digest"]));
    return verifyReleaseSupply({
      expectedLockDigest: requireOption(options, "lock-digest"),
      expectedSupplyDigest: requireOption(options, "supply-digest"),
      supplyRoot: requireOption(options, "supply"),
    });
  }
  if (command === "build") {
    const options = parseOptions(
      values,
      new Set(["build-id", "lock-digest", "output", "supply", "supply-digest"]),
    );
    return buildReleaseOffline({
      buildId: requireOption(options, "build-id"),
      expectedLockDigest: requireOption(options, "lock-digest"),
      expectedSupplyDigest: requireOption(options, "supply-digest"),
      outputRoot: requireOption(options, "output"),
      supplyRoot: requireOption(options, "supply"),
    });
  }
  throw new Error("release command must be acquire, verify, or build");
}

try {
  const result = await run(process.argv[2], process.argv.slice(3));
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
