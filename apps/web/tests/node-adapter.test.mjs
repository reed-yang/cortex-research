import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createServer, request as httpRequest } from "node:http";
import {
  cp,
  mkdtemp,
  mkdir,
  readdir,
  readlink,
  realpath,
  rename,
  rm,
  symlink,
  unlink,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Writable } from "node:stream";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  inspectReleasePayload,
  releasePayloadLedger,
  writeReleasePayload,
} from "../scripts/release-payload.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BUILD_ID = "cortex-adapter-test";
const LOCAL_BOOTSTRAP_TOKEN = "B".repeat(43);
const SENTINELS = [
  "adapter-control-secret-sentinel-000000000000000000",
  "adapter-bootstrap-secret-sentinel-00000000000000",
  "adapter-draft-secret-sentinel-000000000000000000",
];

function fixtureLauncher(payload) {
  const adapterUrl = pathToFileURL(path.join(payload, "server/node-adapter.mjs")).href;
  const handlerUrl = pathToFileURL(path.join(payload, "server/index.js")).href;
  return `
import webHandler from ${JSON.stringify(handlerUrl)};
import { installSignalHandlers, startNodeAdapter } from ${JSON.stringify(adapterUrl)};

const encoder = new TextEncoder();
const fixtureHandler = {
  async fetch(request, environment, context) {
    const url = new URL(request.url);
    if (url.pathname === "/__adapter-test/echo") {
      return Response.json({
        body: await request.text(),
        bootstrap: request.headers.get("x-cortex-access-bootstrap"),
        forwarded: request.headers.get("x-forwarded-host"),
        host: request.headers.get("host"),
        method: request.method,
        removed: request.headers.get("x-remove-me"),
        url: request.url,
      });
    }
    if (url.pathname === "/__adapter-test/ignore-body") {
      return new Response("body-was-not-read\\n", {
        headers: { "Content-Type": "text/plain" },
      });
    }
    if (url.pathname === "/__adapter-test/stream") {
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode("first\\n"));
          setTimeout(() => {
            controller.enqueue(encoder.encode("second\\n"));
            controller.close();
          }, 80);
        },
      }), { headers: { "Content-Type": "text/plain" } });
    }
    if (url.pathname === "/__adapter-test/head") {
      return new Response("body-must-not-cross-head", {
        headers: { "Content-Type": "text/plain", "X-Head-Fixture": "yes" },
      });
    }
    if (url.pathname === "/__adapter-test/cookies") {
      const headers = new Headers({ "Content-Type": "text/plain" });
      headers.append("Set-Cookie", "first=one; HttpOnly; SameSite=Strict");
      headers.append("Set-Cookie", "second=two; Secure; SameSite=Lax");
      return new Response("cookies", { headers });
    }
    if (url.pathname === "/__adapter-test/hang") {
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode("open\\n"));
        },
      }), { headers: { "Content-Type": "text/plain" } });
    }
    return webHandler.fetch(request, environment, context);
  },
};

const runtime = await startNodeAdapter({ fetchHandler: fixtureHandler });
installSignalHandlers(runtime);
`;
}

async function copiedPayload(t) {
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-node-adapter-"));
  t.after(() => rm(parent, { recursive: true, force: true }));
  const candidate = path.join(parent, "candidate");
  await cp(path.join(webRoot, "dist"), candidate, { recursive: true });
  await rm(path.join(candidate, "client/assets/_vinext_fonts"), {
    recursive: true,
    force: true,
  });
  const payload = path.join(parent, "payload");
  const entries = await inspectReleasePayload(candidate, {
    forbiddenPrefixes: [webRoot, parent, process.env.HOME ?? ""],
    secretValues: SENTINELS,
    sourceRoot: webRoot,
  });
  assert.ok(entries.some((entry) => entry.path === "server/node-adapter.mjs"));
  assert.match(releasePayloadLedger(entries), /  server\/node-adapter\.mjs\n/);
  await writeReleasePayload(entries, payload);
  await mkdir(path.join(parent, "empty-home"), { mode: 0o700 });
  const launcher = path.join(parent, "launcher.mjs");
  await writeFile(launcher, fixtureLauncher(payload), { mode: 0o600 });
  await symlink(
    path.join(payload, "server/index.js"),
    path.join(payload, "client/assets/linked.js"),
  );
  for (let cursor = payload; cursor !== path.dirname(cursor); cursor = path.dirname(cursor)) {
    assert.notEqual(path.basename(cursor), "node_modules");
  }
  return { entries, launcher, parent, payload };
}

function captureLines(stream, lines) {
  stream.setEncoding("utf8");
  let pending = "";
  stream.on("data", (chunk) => {
    pending += chunk;
    while (pending.includes("\n")) {
      const index = pending.indexOf("\n");
      lines.push(pending.slice(0, index));
      pending = pending.slice(index + 1);
    }
  });
  return () => pending;
}

function waitForReady(child, lines, errorLines) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("adapter ready timeout")), 10_000);
    const inspect = () => {
      for (const line of lines) {
        try {
          const value = JSON.parse(line);
          if (value.event === "ready") {
            clearTimeout(timeout);
            child.off("exit", exited);
            resolve(value);
            return;
          }
        } catch {
          // Ignore incomplete or non-JSON output until the timeout reports a failure.
        }
      }
    };
    const exited = (code, signal) => {
      clearTimeout(timeout);
      reject(new Error(
        `adapter exited before ready: ${code ?? signal}: ${errorLines.join(" | ")}`,
      ));
    };
    child.once("exit", exited);
    child.stdout.on("data", inspect);
    inspect();
  });
}

function request(port, options = {}) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    const times = [];
    const headers = options.rawHeaders ?? {
      Host: `127.0.0.1:${port}`,
      ...(options.headers ?? {}),
    };
    const outgoing = httpRequest({
      host: "127.0.0.1",
      port,
      method: options.method ?? "GET",
      path: options.path ?? "/",
      headers,
    }, (response) => {
      response.on("data", (chunk) => {
        chunks.push(chunk);
        times.push(Date.now());
      });
      response.on("end", () => resolve({
        body: Buffer.concat(chunks),
        headers: response.headers,
        rawHeaders: response.rawHeaders,
        status: response.statusCode,
        times,
      }));
    });
    outgoing.once("error", reject);
    if (options.body !== undefined) outgoing.write(options.body);
    outgoing.end();
  });
}

function waitForExit(child) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("adapter shutdown timeout")), 5_000);
    child.once("exit", (code, signal) => {
      clearTimeout(timeout);
      resolve({ code, signal });
    });
  });
}

async function assertPortReusable(port) {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port }, resolve);
  });
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

function adapterEnvironment(port) {
  return {
    CORTEX_WEB_BUILD_ID: BUILD_ID,
    CORTEX_WEB_LISTEN_HOST: "127.0.0.1",
    CORTEX_WEB_LISTEN_PORT: String(port),
  };
}

async function openDescriptorCount(target) {
  const canonical = await realpath(target);
  if (process.platform === "linux") {
    let count = 0;
    for (const descriptor of await readdir("/proc/self/fd")) {
      try {
        const linked = await readlink(`/proc/self/fd/${descriptor}`);
        if (linked.replace(/ \(deleted\)$/, "") === canonical) count += 1;
      } catch {
        // File descriptors may close between directory enumeration and readlink.
      }
    }
    return count;
  }
  if (process.platform === "darwin") {
    const result = spawnSync(
      "/usr/sbin/lsof",
      ["-Fn", "-a", "-p", String(process.pid), "--", canonical],
      { encoding: "utf8" },
    );
    if (result.status === 1 && result.stdout === "") return 0;
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.split("\n").filter((line) => /^f\d+$/.test(line)).length;
  }
  throw new Error(`unsupported_descriptor_platform:${process.platform}`);
}

function tcpServerCount() {
  return process.getActiveResourcesInfo().filter((entry) => entry === "TCPServerWrap").length;
}

async function assertTcpServerBaseline(expected) {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (tcpServerCount() === expected) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(tcpServerCount(), expected);
}

test("adapter startup transfers or releases every listener and client-root handle", async (t) => {
  const { payload } = await copiedPayload(t);
  const clientRoot = path.join(payload, "client");
  const adapterUrl = pathToFileURL(path.join(payload, "server/node-adapter.mjs")).href;
  const { startNodeAdapter } = await import(adapterUrl);
  const baselineServers = tcpServerCount();
  assert.equal(await openDescriptorCount(clientRoot), 0);

  let synchronousFailurePort = null;
  await assert.rejects(
    startNodeAdapter({
      clientRoot,
      environment: adapterEnvironment(0),
      output: {
        write(line) {
          synchronousFailurePort = JSON.parse(line).port;
          throw new Error("synchronous_ready_write_failure");
        },
      },
    }),
    /synchronous_ready_write_failure/,
  );
  assert.ok(Number.isInteger(synchronousFailurePort) && synchronousFailurePort > 0);
  assert.equal(await openDescriptorCount(clientRoot), 0);
  await assertPortReusable(synchronousFailurePort);
  await assertTcpServerBaseline(baselineServers);

  let asynchronousFailurePort = null;
  const failedOutput = new Writable({
    write(chunk, _encoding, callback) {
      asynchronousFailurePort = JSON.parse(chunk.toString("utf8")).port;
      setImmediate(() => callback(new Error("asynchronous_ready_write_failure")));
    },
  });
  await assert.rejects(
    startNodeAdapter({
      clientRoot,
      environment: adapterEnvironment(0),
      output: failedOutput,
    }),
    /asynchronous_ready_write_failure/,
  );
  assert.ok(Number.isInteger(asynchronousFailurePort) && asynchronousFailurePort > 0);
  assert.equal(await openDescriptorCount(clientRoot), 0);
  await assertPortReusable(asynchronousFailurePort);
  await assertTcpServerBaseline(baselineServers);

  const occupied = createServer();
  await new Promise((resolve, reject) => {
    occupied.once("error", reject);
    occupied.listen({ host: "127.0.0.1", port: 0 }, resolve);
  });
  const occupiedAddress = occupied.address();
  assert.ok(occupiedAddress && typeof occupiedAddress !== "string");
  await assert.rejects(
    startNodeAdapter({
      clientRoot,
      environment: adapterEnvironment(occupiedAddress.port),
      output: { write() {} },
    }),
    (error) => error?.code === "EADDRINUSE",
  );
  assert.equal(await openDescriptorCount(clientRoot), 0);
  await new Promise((resolve, reject) => {
    occupied.close((error) => error ? reject(error) : resolve());
  });
  await assertPortReusable(occupiedAddress.port);
  await assertTcpServerBaseline(baselineServers);

  let invalidAddressPort = null;
  await assert.rejects(
    startNodeAdapter({
      clientRoot,
      environment: adapterEnvironment(0),
      output: { write() {} },
      serverFactory() {
        const server = createServer();
        const address = server.address.bind(server);
        server.address = () => {
          const actual = address();
          if (!actual || typeof actual === "string") return actual;
          invalidAddressPort = actual.port;
          return { ...actual, address: "0.0.0.0" };
        };
        return server;
      },
    }),
    /invalid_listener_address/,
  );
  assert.ok(Number.isInteger(invalidAddressPort) && invalidAddressPort > 0);
  assert.equal(await openDescriptorCount(clientRoot), 0);
  await assertPortReusable(invalidAddressPort);
  await assertTcpServerBaseline(baselineServers);

  let ready = null;
  const runtime = await startNodeAdapter({
    clientRoot,
    environment: adapterEnvironment(0),
    output: { write(line) { ready = JSON.parse(line); } },
  });
  assert.equal(await openDescriptorCount(clientRoot), 1);
  const firstClose = runtime.close();
  const secondClose = runtime.close();
  assert.equal(firstClose, secondClose);
  assert.deepEqual(await firstClose, { forced: false });
  assert.deepEqual(await secondClose, { forced: false });
  assert.equal(await openDescriptorCount(clientRoot), 0);
  await assertPortReusable(ready.port);
  await assertTcpServerBaseline(baselineServers);
});

test("local access injects only adapter-derived loopback boundary headers", async (t) => {
  const { payload } = await copiedPayload(t);
  const adapterUrl = pathToFileURL(path.join(payload, "server/node-adapter.mjs")).href;
  const { startNodeAdapter } = await import(adapterUrl);
  const environment = {
    ...adapterEnvironment(0),
    CORTEX_ACCESS_BOOTSTRAP_TOKEN: LOCAL_BOOTSTRAP_TOKEN,
    CORTEX_CONTROL_TOKEN: SENTINELS[0],
    CORTEX_LOCAL_ACCESS_ENABLED: "1",
  };
  const observed = [];
  const fetchHandler = {
    async fetch(incoming) {
      observed.push(Object.fromEntries(incoming.headers));
      return new Response("adapter-local-ok\n", {
        headers: { "Content-Type": "text/plain" },
      });
    },
  };
  const readyLines = [];
  const runtime = await startNodeAdapter({
    clientRoot: path.join(payload, "client"),
    environment,
    fetchHandler,
    output: { write(line) { readyLines.push(line); } },
  });
  t.after(() => runtime.close());
  const authority = `127.0.0.1:${runtime.address.port}`;

  assert.equal(environment.CORTEX_LOCAL_ORIGIN, `http://${authority}`);
  const valid = await request(runtime.address.port, {
    headers: {
      Forwarded: "for=203.0.113.10;host=evil.test;proto=https",
      "X-Cortex-Access-Bootstrap": "A".repeat(43),
      "X-Forwarded-For": "203.0.113.10",
      "X-Forwarded-Host": "evil.test",
      "X-Forwarded-Port": "443",
      "X-Forwarded-Proto": "https",
    },
    path: "/__adapter-test/local",
  });
  assert.equal(valid.status, 200);
  assert.equal(valid.body.toString("utf8"), "adapter-local-ok\n");
  assert.equal(observed.length, 1);
  assert.equal(observed[0].forwarded, undefined);
  assert.equal(observed[0].host, authority);
  assert.equal(observed[0]["x-cortex-access-bootstrap"], LOCAL_BOOTSTRAP_TOKEN);
  assert.equal(observed[0]["x-forwarded-for"], "127.0.0.1");
  assert.equal(observed[0]["x-forwarded-host"], authority);
  assert.equal(observed[0]["x-forwarded-port"], String(runtime.address.port));
  assert.equal(observed[0]["x-forwarded-proto"], "http");

  const badHost = await request(runtime.address.port, {
    headers: { Host: "evil.test" },
  });
  assert.equal(badHost.status, 421);
  assert.equal(observed.length, 1);
  const duplicateHost = await request(runtime.address.port, {
    rawHeaders: ["Host", authority, "hOsT", "evil.test"],
  });
  assert.equal(duplicateHost.status, 421);
  assert.equal(observed.length, 1);

  const publicOutput = [
    ...readyLines,
    JSON.stringify(valid.headers),
    valid.body.toString("utf8"),
    JSON.stringify(badHost.headers),
    badHost.body.toString("utf8"),
  ].join("\n");
  assert.ok(!publicOutput.includes(LOCAL_BOOTSTRAP_TOKEN));
  assert.ok(!publicOutput.includes(SENTINELS[0]));

  let invalidSocketCalls = 0;
  const invalidSocketRuntime = await startNodeAdapter({
    clientRoot: path.join(payload, "client"),
    environment: {
      ...adapterEnvironment(0),
      CORTEX_ACCESS_BOOTSTRAP_TOKEN: LOCAL_BOOTSTRAP_TOKEN,
      CORTEX_LOCAL_ACCESS_ENABLED: "1",
    },
    fetchHandler: {
      async fetch() {
        invalidSocketCalls += 1;
        return new Response("must-not-run");
      },
    },
    output: { write() {} },
    serverFactory() {
      const server = createServer();
      server.on("connection", (socket) => {
        Object.defineProperty(socket, "remoteAddress", { value: "203.0.113.10" });
      });
      return server;
    },
  });
  t.after(() => invalidSocketRuntime.close());
  const invalidSocket = await request(invalidSocketRuntime.address.port);
  assert.equal(invalidSocket.status, 421);
  assert.equal(invalidSocketCalls, 0);

  await assert.rejects(
    startNodeAdapter({
      clientRoot: path.join(payload, "client"),
      environment: {
        ...adapterEnvironment(0),
        CORTEX_LOCAL_ACCESS_ENABLED: "1",
      },
      fetchHandler,
      output: { write() {} },
    }),
    /invalid_local_access/,
  );
});

test("standalone Node adapter preserves HTTP boundaries and closes its port", async (t) => {
  const { entries, launcher, parent, payload } = await copiedPayload(t);
  const stdoutLines = [];
  const stderrLines = [];
  const child = spawn(process.execPath, [launcher], {
    cwd: payload,
    env: {
      CORTEX_ACCESS_BOOTSTRAP_TOKEN: SENTINELS[1],
      CORTEX_CONTROL_API_URL: "http://127.0.0.1:9",
      CORTEX_CONTROL_TOKEN: SENTINELS[0],
      CORTEX_WEB_BUILD_ID: BUILD_ID,
      CORTEX_WEB_DRAFT_SECRET: SENTINELS[2],
      CORTEX_WEB_LISTEN_HOST: "127.0.0.1",
      CORTEX_WEB_LISTEN_PORT: "0",
      HOME: path.join(parent, "empty-home"),
      NODE_PATH: "",
      PATH: path.dirname(process.execPath),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const pendingStdout = captureLines(child.stdout, stdoutLines);
  const pendingStderr = captureLines(child.stderr, stderrLines);
  t.after(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  });
  const ready = await waitForReady(child, stdoutLines, stderrLines);
  assert.deepEqual(ready, {
    adapter_version: 1,
    build_id: BUILD_ID,
    event: "ready",
    host: "127.0.0.1",
    port: ready.port,
    service: "cortex-web",
  });
  assert.ok(Number.isInteger(ready.port) && ready.port > 0);

  const page = await request(ready.port);
  assert.equal(page.status, 200);
  assert.match(page.body.toString("utf8"), /^<!DOCTYPE html>/);

  const health = await request(ready.port, { path: "/_cortex/health" });
  assert.equal(health.status, 200);
  assert.deepEqual(JSON.parse(health.body), {
    adapter_version: 1,
    build_id: BUILD_ID,
    public_door: false,
    service: "cortex-web",
    status: "ok",
  });
  assert.equal(health.headers["cache-control"], "no-store");
  const healthPost = await request(ready.port, {
    body: "{}",
    method: "POST",
    path: "/_cortex/health",
  });
  assert.equal(healthPost.status, 405);
  assert.equal(healthPost.headers.allow, "GET, HEAD");

  const assetEntry = entries.find(
    (entry) => entry.path.startsWith("client/assets/") && entry.path.endsWith(".js"),
  );
  assert.ok(assetEntry);
  const assetPath = `/${assetEntry.path.slice("client/".length)}`;
  const asset = await request(ready.port, { path: assetPath });
  assert.equal(asset.status, 200);
  assert.deepEqual(asset.body, assetEntry.contents);
  assert.equal(asset.headers["content-type"], "text/javascript; charset=utf-8");
  assert.equal(asset.headers["cache-control"], "public, max-age=31536000, immutable");
  const assetHead = await request(ready.port, { method: "HEAD", path: assetPath });
  assert.equal(assetHead.status, 200);
  assert.equal(assetHead.body.byteLength, 0);
  assert.equal(assetHead.headers["content-length"], String(assetEntry.size));

  const postBody = "streamed-request-body-sentinel";
  const echo = await request(ready.port, {
    body: postBody,
    headers: {
      Connection: "x-remove-me",
      "Content-Type": "text/plain",
      Forwarded: "for=203.0.113.10;host=evil.test",
      "X-Cortex-Access-Bootstrap": "browser-forged-bootstrap",
      "X-Forwarded-Host": "evil.test",
      "X-Remove-Me": "connection-header-sentinel",
    },
    method: "POST",
    path: "/__adapter-test/echo",
  });
  assert.equal(echo.status, 200);
  assert.deepEqual(JSON.parse(echo.body), {
    body: postBody,
    bootstrap: null,
    forwarded: null,
    host: `127.0.0.1:${ready.port}`,
    method: "POST",
    removed: null,
    url: `http://127.0.0.1:${ready.port}/__adapter-test/echo`,
  });
  const oversized = await request(ready.port, {
    body: "",
    headers: { "Content-Length": String(1024 * 1024 + 1) },
    method: "POST",
    path: "/__adapter-test/echo",
  });
  assert.equal(oversized.status, 413);
  assert.equal(oversized.headers.connection, "close");
  const chunkedOversized = await request(ready.port, {
    body: Buffer.alloc(1024 * 1024 + 1, 0x61),
    headers: { "Transfer-Encoding": "chunked" },
    method: "POST",
    path: "/__adapter-test/ignore-body",
  });
  assert.equal(chunkedOversized.status, 413);
  assert.equal(chunkedOversized.headers.connection, "close");
  assert.equal(chunkedOversized.body.toString("utf8"), "Content Too Large\n");

  const streamed = await request(ready.port, { path: "/__adapter-test/stream" });
  assert.equal(streamed.status, 200);
  assert.equal(streamed.body.toString("utf8"), "first\nsecond\n");
  assert.ok(streamed.times.length >= 2);
  assert.ok(streamed.times.at(-1) - streamed.times[0] >= 40);

  const dynamicHead = await request(ready.port, {
    method: "HEAD",
    path: "/__adapter-test/head",
  });
  assert.equal(dynamicHead.status, 200);
  assert.equal(dynamicHead.body.byteLength, 0);
  assert.equal(dynamicHead.headers["x-head-fixture"], "yes");

  const cookies = await request(ready.port, { path: "/__adapter-test/cookies" });
  assert.deepEqual(cookies.headers["set-cookie"], [
    "first=one; HttpOnly; SameSite=Strict",
    "second=two; Secure; SameSite=Lax",
  ]);

  const badHost = await request(ready.port, {
    headers: { Host: "evil.test" },
  });
  assert.equal(badHost.status, 421);
  const traversal = await request(ready.port, {
    path: "/assets/%2e%2e/server/index.js",
  });
  assert.equal(traversal.status, 400);
  const symlinked = await request(ready.port, { path: "/assets/linked.js" });
  assert.equal(symlinked.status, 404);
  assert.doesNotMatch(symlinked.body.toString("utf8"), /server\/index|node-adapter/);

  const clientRoot = path.join(payload, "client");
  const originalClientRoot = path.join(payload, "client-bound-at-startup");
  const replacementClientRoot = path.join(parent, "replacement-client");
  const replacementSentinel = "replacement-client-root-must-not-be-served";
  await rename(clientRoot, originalClientRoot);
  await mkdir(replacementClientRoot, { mode: 0o700 });
  await writeFile(
    path.join(replacementClientRoot, "manifest.webmanifest"),
    replacementSentinel,
    { mode: 0o600 },
  );
  await symlink(replacementClientRoot, clientRoot, "dir");
  const replacedBySymlink = await request(ready.port, { path: "/manifest.webmanifest" });
  assert.equal(replacedBySymlink.status, 404);
  assert.ok(!replacedBySymlink.body.includes(replacementSentinel));
  await unlink(clientRoot);
  await rename(replacementClientRoot, clientRoot);
  const replacedByDirectory = await request(ready.port, { path: "/manifest.webmanifest" });
  assert.equal(replacedByDirectory.status, 404);
  assert.ok(!replacedByDirectory.body.includes(replacementSentinel));

  const hangOpened = new Promise((resolve, reject) => {
    const outgoing = httpRequest({
      host: "127.0.0.1",
      port: ready.port,
      path: "/__adapter-test/hang",
      headers: { Host: `127.0.0.1:${ready.port}` },
    }, (response) => {
      response.once("data", () => resolve(response));
      response.once("error", reject);
    });
    outgoing.once("error", reject);
    outgoing.end();
  });
  await hangOpened;
  const shutdownStarted = Date.now();
  child.kill("SIGTERM");
  const exited = await waitForExit(child);
  assert.ok(Date.now() - shutdownStarted < 4_000);
  assert.equal(exited.signal, null);
  assert.ok([0, 1].includes(exited.code));
  await assertPortReusable(ready.port);

  const allOutput = [
    ...stdoutLines,
    pendingStdout(),
    ...stderrLines,
    pendingStderr(),
  ].join("\n");
  assert.doesNotMatch(allOutput, new RegExp(parent.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  for (const sentinel of SENTINELS) assert.ok(!allOutput.includes(sentinel));
  assert.ok(stdoutLines.some((line) => JSON.parse(line).event === "stopped"));

  const unsafeOutput = [];
  const unsafeErrors = [];
  const unsafe = spawn(
    process.execPath,
    [path.join(payload, "server/node-adapter.mjs")],
    {
      cwd: payload,
      env: {
        CORTEX_WEB_BUILD_ID: BUILD_ID,
        CORTEX_WEB_LISTEN_HOST: "0.0.0.0",
        CORTEX_WEB_LISTEN_PORT: "0",
        HOME: path.join(parent, "empty-home"),
        NODE_PATH: "",
        PATH: path.dirname(process.execPath),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  captureLines(unsafe.stdout, unsafeOutput);
  captureLines(unsafe.stderr, unsafeErrors);
  const unsafeExit = await waitForExit(unsafe);
  assert.equal(unsafeExit.code, 1);
  assert.equal(unsafeExit.signal, null);
  assert.deepEqual(unsafeOutput, []);
  assert.deepEqual(unsafeErrors.map((line) => JSON.parse(line)), [{
    code: "startup_failed",
    event: "fatal",
    service: "cortex-web",
  }]);
});

// ⟦P7⟧ ---------------------------------------------------------------------
// The public door: same loopback listener, told apart by `Host`, opened only
// by a Cloudflare Access assertion the adapter verified against a JWKS.

test("public door verifies the Access assertion, injects the public boundary, and leaves the local door as it was", async (t) => {
  const { accessKey, jwksFetch, signAssertion } = await import("./access-identity-helpers.mjs");
  const { payload } = await copiedPayload(t);
  const adapterUrl = pathToFileURL(path.join(payload, "server/node-adapter.mjs")).href;
  const { startNodeAdapter } = await import(adapterUrl);
  const PUBLIC_AUTHORITY = "cortex.example.test";
  const key = accessKey("kid-live");
  const rotated = accessKey("kid-rotated");
  const fetcher = jwksFetch([[key], [key, rotated]]);
  const clock = { value: Date.now() };
  const environment = {
    ...adapterEnvironment(0),
    CORTEX_ACCESS_AUDIENCE: "ab".repeat(32),
    CORTEX_ACCESS_BOOTSTRAP_TOKEN: LOCAL_BOOTSTRAP_TOKEN,
    CORTEX_ACCESS_ISSUER: "http://127.0.0.1:8123",
    CORTEX_CONTROL_TOKEN: SENTINELS[0],
    CORTEX_LOCAL_ACCESS_ENABLED: "1",
    CORTEX_PUBLIC_ORIGIN: `https://${PUBLIC_AUTHORITY}`,
  };
  const observed = [];
  const fetchHandler = {
    async fetch(incoming) {
      observed.push({ headers: Object.fromEntries(incoming.headers), method: incoming.method, url: incoming.url });
      return new Response("adapter-door-ok\n", { headers: { "Content-Type": "text/plain" } });
    },
  };
  const readyLines = [];
  const runtime = await startNodeAdapter({
    clientRoot: path.join(payload, "client"),
    environment,
    fetchHandler,
    fetchImplementation: fetcher.fetchImplementation,
    now: () => clock.value,
    output: { write(line) { readyLines.push(line); } },
  });
  t.after(() => runtime.close());
  const port = runtime.address.port;
  const localAuthority = `127.0.0.1:${port}`;
  assert.equal(environment.CORTEX_LOCAL_ORIGIN, `http://${localAuthority}`);
  assert.equal(environment.CORTEX_PUBLIC_ORIGIN, `https://${PUBLIC_AUTHORITY}`);

  // (1) The public door without an assertion: 401, typed, and the app never runs.
  const missing = await request(port, { headers: { Host: PUBLIC_AUTHORITY }, path: "/__adapter-test/public" });
  assert.equal(missing.status, 401);
  assert.equal(missing.headers["content-type"], "application/problem+json");
  assert.deepEqual(JSON.parse(missing.body.toString("utf8")).category, "access_identity_missing");
  assert.equal(observed.length, 0);

  // (2) Bad assertions of every shape: 401 access_identity_invalid, app never runs.
  const badCases = {
    garbage: "not.a.jwt",
    expired: signAssertion(key, { exp: Math.floor(clock.value / 1000) - 600 }),
    wrong_audience: signAssertion(key, { aud: ["cd".repeat(32)] }),
    wrong_issuer: signAssertion(key, { iss: "https://example-team.cloudflareaccess.com" }),
    none_alg: signAssertion(key, {}, { header: { alg: "none" }, signature: "" }),
  };
  for (const [name, assertion] of Object.entries(badCases)) {
    const rejected = await request(port, {
      headers: { "Cf-Access-Jwt-Assertion": assertion, Host: PUBLIC_AUTHORITY },
      path: "/__adapter-test/public",
    });
    assert.equal(rejected.status, 401, name);
    assert.equal(JSON.parse(rejected.body.toString("utf8")).category, "access_identity_invalid", name);
  }
  // An unknown kid inside the JWKS rate-limit window is 503 unverifiable
  // (retryable), not a 401 verdict: nobody asked the issuer about it yet.
  const suppressed = await request(port, {
    headers: { "Cf-Access-Jwt-Assertion": signAssertion(rotated, {}), Host: PUBLIC_AUTHORITY },
    path: "/__adapter-test/public",
  });
  assert.equal(suppressed.status, 503);
  assert.deepEqual(JSON.parse(suppressed.body.toString("utf8")).category, "access_identity_unverifiable");
  assert.equal(JSON.parse(suppressed.body.toString("utf8")).retryable, true);
  const duplicated = await request(port, {
    rawHeaders: ["Host", PUBLIC_AUTHORITY, "Cf-Access-Jwt-Assertion", signAssertion(key, {}), "Cf-Access-Jwt-Assertion", signAssertion(key, {})],
    path: "/__adapter-test/public",
  });
  assert.equal(duplicated.status, 401);
  assert.equal(JSON.parse(duplicated.body.toString("utf8")).category, "access_identity_missing");
  assert.equal(observed.length, 0);

  // (3) A valid assertion opens the door: the app sees the public authority,
  // https forwarding, the verified identity, and nothing the caller supplied
  // for any of those headers.
  const accepted = await request(port, {
    headers: {
      "Cf-Access-Authenticated-User-Email": "forged@example.test",
      "Cf-Access-Jwt-Assertion": signAssertion(key, {}),
      "Cf-Connecting-Ip": "203.0.113.10",
      "Cf-Ipcountry": "XX",
      "Cf-Ray": "0123456789abcdef-DFW",
      "Cf-Visitor": "{\"scheme\":\"https\"}",
      Cookie: `CF_Authorization=${signAssertion(key, {})}; theme=dark; CF_AppSession=abc123`,
      Forwarded: "for=203.0.113.10;host=evil.test;proto=https",
      Host: PUBLIC_AUTHORITY,
      "True-Client-Ip": "203.0.113.10",
      "X-Real-Ip": "203.0.113.10",
      Origin: `https://${PUBLIC_AUTHORITY}`,
      "Sec-Fetch-Site": "same-origin",
      "X-Cortex-Access-Bootstrap": "A".repeat(43),
      "X-Cortex-Access-Identity": "forged@example.test",
      "X-Cortex-Web-Client": "v1",
      "X-Forwarded-For": "203.0.113.10",
      "X-Forwarded-Host": "evil.test",
      "X-Forwarded-Proto": "http",
    },
    path: "/__adapter-test/public",
  });
  assert.equal(accepted.status, 200);
  assert.equal(accepted.body.toString("utf8"), "adapter-door-ok\n");
  assert.equal(observed.length, 1);
  const seen = observed[0].headers;
  assert.equal(seen.host, PUBLIC_AUTHORITY);
  assert.equal(seen["x-cortex-access-bootstrap"], LOCAL_BOOTSTRAP_TOKEN);
  assert.equal(seen["x-cortex-access-identity"], "operator@example.test");
  assert.equal(seen["x-forwarded-for"], "127.0.0.1");
  assert.equal(seen["x-forwarded-host"], PUBLIC_AUTHORITY);
  assert.equal(seen["x-forwarded-port"], "443");
  assert.equal(seen["x-forwarded-proto"], "https");
  assert.equal(seen.forwarded, undefined);
  assert.equal(seen["cf-access-jwt-assertion"], undefined);
  assert.equal(seen["cf-access-authenticated-user-email"], undefined);
  for (const name of ["cf-connecting-ip", "cf-ipcountry", "cf-ray", "cf-visitor", "true-client-ip", "x-real-ip"]) {
    assert.equal(seen[name], undefined, name);
  }
  // The Access session cookie never crosses; every other cookie does.
  assert.equal(seen.cookie, "theme=dark");
  assert.equal(seen.origin, `https://${PUBLIC_AUTHORITY}`);
  assert.equal(seen["sec-fetch-site"], "same-origin");
  // The browser's own mutation evidence is not adapter evidence: it crosses.
  assert.equal(seen["x-cortex-web-client"], "v1");
  assert.ok(observed[0].url.startsWith(`http://${localAuthority}/`));

  // (4) A POST through the public door reaches the app with its body.
  const posted = await request(port, {
    body: "{\"title\":\"Research\"}",
    headers: {
      "Cf-Access-Jwt-Assertion": signAssertion(key, {}),
      "Content-Length": "20",
      "Content-Type": "application/json",
      Host: PUBLIC_AUTHORITY,
      Origin: `https://${PUBLIC_AUTHORITY}`,
    },
    method: "POST",
    path: "/__adapter-test/public",
  });
  assert.equal(posted.status, 200);
  assert.equal(observed.at(-1).method, "POST");

  // (5) The health endpoint says only that a public door exists.
  const health = await request(port, {
    headers: { "Cf-Access-Jwt-Assertion": signAssertion(key, {}), Host: PUBLIC_AUTHORITY },
    path: "/_cortex/health",
  });
  assert.equal(health.status, 200);
  assert.deepEqual(JSON.parse(health.body.toString("utf8")), {
    adapter_version: 1,
    build_id: BUILD_ID,
    public_door: true,
    service: "cortex-web",
    status: "ok",
  });

  // (6) The rotated key verifies once the rate limit has passed and the JWKS refreshed.
  clock.value += 31_000;
  const rotatedAccepted = await request(port, {
    headers: { "Cf-Access-Jwt-Assertion": signAssertion(rotated, {}), Host: PUBLIC_AUTHORITY },
    path: "/__adapter-test/public",
  });
  assert.equal(rotatedAccepted.status, 200);
  assert.equal(fetcher.calls(), 2);

  // (7) The local door is exactly as before: no assertion, local headers,
  // and a caller-supplied identity header never reaches the app.
  const local = await request(port, {
    headers: {
      Cookie: "CF_Authorization=stale; session=keep",
      "X-Cortex-Access-Identity": "forged@example.test",
      "X-Cortex-Web-Client": "v1",
      "X-Real-Ip": "203.0.113.10",
    },
    path: "/__adapter-test/local",
  });
  assert.equal(local.status, 200);
  const localSeen = observed.at(-1).headers;
  assert.equal(localSeen.host, localAuthority);
  assert.equal(localSeen["x-cortex-access-identity"], undefined);
  assert.equal(localSeen["x-cortex-web-client"], "v1");
  assert.equal(localSeen["x-real-ip"], undefined);
  assert.equal(localSeen.cookie, "session=keep");
  assert.equal(localSeen["x-forwarded-proto"], "http");
  assert.equal(localSeen["x-forwarded-host"], localAuthority);
  const localHealth = await request(port, { path: "/_cortex/health" });
  assert.equal(JSON.parse(localHealth.body.toString("utf8")).public_door, true);

  // (8) Any other Host is misdirected, assertion or not.
  const misdirected = await request(port, {
    headers: { "Cf-Access-Jwt-Assertion": signAssertion(key, {}), Host: "other.example.test" },
  });
  assert.equal(misdirected.status, 421);

  // Nothing secret crossed into public output.
  const publicOutput = [...readyLines, accepted.body.toString("utf8"), missing.body.toString("utf8")].join("\n");
  assert.ok(!publicOutput.includes(LOCAL_BOOTSTRAP_TOKEN));
  assert.ok(!publicOutput.includes(SENTINELS[0]));
  assert.ok(!publicOutput.includes("operator@example.test"));

  // (9) A public origin without its identity check, or without local access, is refused at start.
  for (const broken of [
    { CORTEX_ACCESS_AUDIENCE: undefined },
    { CORTEX_ACCESS_ISSUER: "https://evil.example/cloudflareaccess.com" },
    { CORTEX_PUBLIC_ORIGIN: "http://cortex.example.test" },
    { CORTEX_PUBLIC_ORIGIN: "https://cortex.example.test:8443" },
    { CORTEX_ACCESS_BOOTSTRAP_TOKEN: undefined, CORTEX_LOCAL_ACCESS_ENABLED: undefined },
  ]) {
    const brokenEnvironment = { ...environment, ...broken };
    for (const name of Object.keys(broken)) if (broken[name] === undefined) delete brokenEnvironment[name];
    delete brokenEnvironment.CORTEX_LOCAL_ORIGIN;
    await assert.rejects(
      startNodeAdapter({
        clientRoot: path.join(payload, "client"),
        environment: brokenEnvironment,
        fetchHandler,
        output: { write() {} },
      }),
      /invalid_public_access|invalid_local_access/,
    );
  }
});
