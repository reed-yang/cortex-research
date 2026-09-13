import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { LOOPBACK_PORTS, loopbackPorts } from "./loopback-web-harness.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageJson = JSON.parse(await readFile(path.join(webRoot, "package.json"), "utf8"));
const nextConfig = await readFile(path.join(webRoot, "next.config.ts"), "utf8");
const hosting = JSON.parse(await readFile(path.join(webRoot, ".openai/hosting.json"), "utf8"));
const captureRunner = await readFile(path.join(webRoot, "scripts/capture-screenshots.mjs"), "utf8");
// The dev server, the boundary proxy and the mock daemon that every browser
// acceptance starts now live in one harness, so the binding is checked there
// once and every caller's ports are checked to be the harness's own.
const harness = await readFile(path.join(webRoot, "scripts/loopback-web-harness.mjs"), "utf8");
const mockControl = await readFile(path.join(webRoot, "scripts/mock-control-server.mjs"), "utf8");
// The harness itself declares this and this verifier reads it, so the two files
// that define the loopback contract are the only ones that state a port.
const harnessSources = new Set(["loopback-web-harness.mjs", "verify-loopback.mjs"]);

function verifyScript(name, executable, command) {
  const script = packageJson.scripts[name];
  assert.equal(typeof script, "string", `${name} script is required`);
  assert.match(script, new RegExp(`\\b${executable}\\s+${command}\\b`));
  assert.match(script, /(?:-H|--hostname)\s+127\.0\.0\.1(?:\s|$)/);
  assert.match(script, /(?:-p|--port)\s+3000(?:\s|$)/);
  assert.doesNotMatch(script, /0\.0\.0\.0|\[?::\]?|--hostname\s+localhost/);
}

verifyScript("dev", "next", "dev");
verifyScript("start", "vinext", "start");
assert.match(nextConfig, /allowedDevOrigins:\s*\["127\.0\.0\.1"\]/);
assert.deepEqual(hosting, { d1: null, r2: null });
assert.match(
  captureRunner,
  /"dev",\s*"-H",\s*"127\.0\.0\.1",\s*"-p",\s*"3000"/,
);
assert.doesNotMatch(captureRunner, /0\.0\.0\.0|\[?::\]?|"localhost"/);
assert.match(
  harness,
  /"dev",\s*"-H",\s*"127\.0\.0\.1",\s*"-p",\s*String\(upstreamPort\)/,
);
assert.match(harness, /server\.listen\(proxyPort, "127\.0\.0\.1", \(\) => resolve\(server\)\)/);
assert.match(harness, /host: "127\.0\.0\.1",\n\s*port: upstreamPort,/);
assert.match(harness, /connect\(upstreamPort, "127\.0\.0\.1",/);
assert.match(harness, /CORTEX_CONTROL_API_URL: `http:\/\/127\.0\.0\.1:\$\{controlPort\}`/);
assert.match(mockControl, /server\.listen\(port, "127\.0\.0\.1", resolve\)/);

// Each caller asks the registry for its ports rather than restating literals,
// so a caller can be moved onto free ports -- which is what the mobile
// acceptance already needed -- without any of them being able to name a
// listening address. Every registered set stays disjoint, so two acceptances
// running side by side cannot take each other's listener.
const takenPorts = new Map();
for (const [caller, defaults] of Object.entries(LOOPBACK_PORTS)) {
  const relative = `scripts/${caller}.mjs`;
  const source = await readFile(path.join(webRoot, relative), "utf8");
  assert.ok(source.includes(`loopbackPorts("${caller}")`), `${relative} must resolve its ports through the registry`);
  assert.ok(source.includes("startLoopbackWeb("), `${relative} must start the loopback harness`);
  assert.doesNotMatch(source, /0\.0\.0\.0|\[?::\]?|"localhost"/, relative);
  assert.deepEqual(loopbackPorts(caller, {}), defaults, `${relative} must default to its registered ports`);
  for (const [name, port] of Object.entries(defaults)) {
    assert.ok(Number.isInteger(port) && port > 0 && port < 65_536, `${relative} ${name} must be a TCP port`);
    assert.ok(!takenPorts.has(port), `${relative} ${name} reuses TCP ${port}, taken by ${takenPorts.get(port)}`);
    takenPorts.set(port, relative);
  }
}

// A new browser acceptance must register instead of quietly starting its own
// world: `verify-markdown-math.mjs` had been running outside this checked set.
for (const entry of await readdir(path.join(webRoot, "scripts"))) {
  if (harnessSources.has(entry) || !entry.endsWith(".mjs")) continue;
  const source = await readFile(path.join(webRoot, "scripts", entry), "utf8");
  if (!source.includes("loopback-web-harness.mjs")) continue;
  assert.ok(
    Object.hasOwn(LOOPBACK_PORTS, entry.replace(/\.mjs$/, "")),
    `scripts/${entry} uses the loopback harness but is not a registered caller`,
  );
}

// A selected port is still only a port: the resolver refuses anything that is
// not a TCP number, so no override can smuggle an address into a listener.
for (const override of ["0.0.0.0", "::", "localhost", " ", "3000.5", "-1", "65536"]) {
  assert.throws(
    () => loopbackPorts("verify-mobile-pwa", { CORTEX_TEST_PROXY_PORT: override }),
    /must be a TCP port number/,
    `override ${JSON.stringify(override)} must be rejected`,
  );
}
assert.throws(() => loopbackPorts("unregistered-acceptance", {}), /not a registered loopback caller/);
assert.deepEqual(
  loopbackPorts("verify-mobile-pwa", { CORTEX_TEST_PROXY_PORT: "3900", CORTEX_TEST_UPSTREAM_PORT: "3901" }),
  { controlPort: 8799, proxyPort: 3900, upstreamPort: 3901 },
);
assert.throws(
  () => loopbackPorts("verify-mobile-pwa", { CORTEX_TEST_PROXY_PORT: "8799" }),
  /three distinct ports/,
);
for (const source of [harness, mockControl]) {
  assert.doesNotMatch(source, /0\.0\.0\.0|\[?::\]?|"localhost"/);
}

console.log("Loopback-only dev/start/desktop/mobile capture and null persistence bindings verified.");
