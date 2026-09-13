// One loopback Web instance, started the same way for every acceptance that
// needs the shell in a real browser.
//
// The Web front door opens only for a request that carries the boundary its
// adapter mints: the bootstrap token, the forwarded host and protocol of a
// configured door, and -- for the public door -- the identity the adapter sets
// only after it verified a Cloudflare Access assertion. `next dev` has no
// adapter in front of it, so the proxy below stands in for one: it strips both
// boundary headers from every caller and sets them itself.

import { spawn } from "node:child_process";
import { createHmac } from "node:crypto";
import { createServer as createHttpServer, request as httpRequest } from "node:http";
import { connect, createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { MOCK_CONTROL_TOKEN, startMockControlServer } from "./mock-control-server.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export const PUBLIC_ORIGIN = "https://cortex.test.ts.net";
export const ACCESS_IDENTITY = "operator@cortex.test.ts.net";
export const ACCESS_BOOTSTRAP_TOKEN = createHmac(
  "sha256",
  "test-only-private-access-secret-value",
)
  .update("cortex-private-access-bootstrap-v1", "ascii")
  .digest("base64url");

// Every browser acceptance takes its three ports from here. The harness binds
// them to 127.0.0.1 itself, so `verify:safety` checks one registered contract
// instead of matching a port literal inside each caller -- which is what broke
// when the mobile acceptance started accepting environment-selected ports. Each
// caller keeps a distinct default set so two acceptances may run side by side;
// the CORTEX_TEST_*_PORT variables move a run onto free ports without changing
// the loopback binding, which no caller chooses.
export const LOOPBACK_PORTS = Object.freeze({
  "verify-mobile-pwa": Object.freeze({ controlPort: 8799, proxyPort: 3000, upstreamPort: 3001 }),
  "capture-shell-screenshots": Object.freeze({ controlPort: 8797, proxyPort: 3002, upstreamPort: 3003 }),
  "verify-markdown-math": Object.freeze({ controlPort: 8897, proxyPort: 3102, upstreamPort: 3103 }),
});

const PORT_VARIABLES = Object.freeze({
  controlPort: "CORTEX_TEST_CONTROL_PORT",
  proxyPort: "CORTEX_TEST_PROXY_PORT",
  upstreamPort: "CORTEX_TEST_UPSTREAM_PORT",
});

export function loopbackPorts(caller, env = process.env) {
  assertLoopbackCaller(caller);
  const defaults = LOOPBACK_PORTS[caller];
  const ports = {};
  for (const [name, variable] of Object.entries(PORT_VARIABLES)) {
    const override = env[variable];
    const port = override === undefined || override === "" ? defaults[name] : Number(override);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) {
      throw new Error(`${variable} must be a TCP port number, got ${JSON.stringify(override)}`);
    }
    ports[name] = port;
  }
  if (new Set(Object.values(ports)).size !== Object.keys(ports).length) {
    throw new Error(`${caller} needs three distinct ports, got ${JSON.stringify(ports)}`);
  }
  return ports;
}

export function assertLoopbackCaller(caller) {
  if (!Object.hasOwn(LOOPBACK_PORTS, caller)) {
    throw new Error(`${caller} is not a registered loopback caller; add it to LOOPBACK_PORTS`);
  }
}

export async function assertPortAvailable(label, port) {
  await new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", (error) => reject(new Error(`${label}: TCP ${port} must be free: ${error.message}`)));
    probe.listen(port, "127.0.0.1", () => probe.close(resolve));
  });
}

async function waitForServer(child, origin, output) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Loopback dev server exited early:\n${output.value}`);
    try {
      const response = await fetch(origin);
      if (response.ok) return;
    } catch {
      // The loopback server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Loopback dev server did not become ready:\n${output.value}`);
}

async function stopServer(child) {
  if (child.exitCode !== null) return;
  process.kill(-child.pid, "SIGTERM");
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, 5_000)),
  ]);
  if (child.exitCode === null) process.kill(-child.pid, "SIGKILL");
}

function startAccessBoundaryProxy(proxyPort, upstreamPort) {
  const publicAuthority = new URL(PUBLIC_ORIGIN).host;
  const server = createHttpServer((request, response) => {
    const headers = { ...request.headers };
    for (const name of Object.keys(headers)) {
      if (
        name === "forwarded" ||
        name === "x-real-ip" ||
        name === "x-cortex-access-bootstrap" ||
        name === "x-cortex-access-identity" ||
        name.startsWith("x-forwarded-")
      ) delete headers[name];
    }
    Object.assign(headers, {
      host: publicAuthority,
      "x-cortex-access-bootstrap": ACCESS_BOOTSTRAP_TOKEN,
      "x-cortex-access-identity": ACCESS_IDENTITY,
      "x-forwarded-host": publicAuthority,
      "x-forwarded-proto": "https",
    });
    if (headers.origin) headers.origin = PUBLIC_ORIGIN;
    const upstream = httpRequest(
      {
        host: "127.0.0.1",
        port: upstreamPort,
        path: request.url,
        method: request.method,
        headers,
      },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
        upstreamResponse.pipe(response);
      },
    );
    upstream.on("error", () => {
      if (!response.headersSent) response.writeHead(502);
      response.end();
    });
    request.pipe(upstream);
  });
  server.on("upgrade", (request, socket, head) => {
    const upstream = connect(upstreamPort, "127.0.0.1", () => {
      upstream.write(`${request.method} ${request.url} HTTP/${request.httpVersion}\r\n`);
      for (let index = 0; index < request.rawHeaders.length; index += 2) {
        upstream.write(`${request.rawHeaders[index]}: ${request.rawHeaders[index + 1]}\r\n`);
      }
      upstream.write("\r\n");
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    upstream.on("error", () => socket.destroy());
    socket.on("error", () => upstream.destroy());
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(proxyPort, "127.0.0.1", () => resolve(server));
  });
}

async function stopAccessBoundaryProxy(server) {
  if (!server) return;
  await new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
    server.closeAllConnections();
  });
}

// Start the mock Control daemon, the Web dev server and the boundary proxy, and
// hand back the origin a browser may use. `close` releases all three and then
// proves every port it took is free again.
export async function startLoopbackWeb({ controlPort = 8799, proxyPort = 3000, upstreamPort = 3001 } = {}) {
  const origin = `http://127.0.0.1:${proxyPort}`;
  const mockControl = await startMockControlServer(controlPort);
  const output = { value: "" };
  const server = spawn(
    process.execPath,
    [path.join(webRoot, "node_modules/next/dist/bin/next"), "dev", "-H", "127.0.0.1", "-p", String(upstreamPort)],
    {
      cwd: webRoot,
      detached: true,
      env: {
        ...process.env,
        CORTEX_ACCESS_BOOTSTRAP_TOKEN: ACCESS_BOOTSTRAP_TOKEN,
        CORTEX_CONTROL_API_URL: `http://127.0.0.1:${controlPort}`,
        CORTEX_CONTROL_TOKEN: MOCK_CONTROL_TOKEN,
        CORTEX_PUBLIC_ORIGIN: PUBLIC_ORIGIN,
        NEXT_TELEMETRY_DISABLED: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  for (const stream of [server.stdout, server.stderr]) {
    stream.on("data", (chunk) => {
      output.value = `${output.value}${chunk}`.slice(-8_000);
    });
  }
  let proxy;
  try {
    proxy = await startAccessBoundaryProxy(proxyPort, upstreamPort);
    await waitForServer(server, origin, output);
  } catch (error) {
    await stopAccessBoundaryProxy(proxy);
    await stopServer(server);
    await mockControl.close();
    throw error;
  }
  return {
    origin,
    output,
    close: async () => {
      await stopAccessBoundaryProxy(proxy);
      await stopServer(server);
      await mockControl.close();
      for (const [label, port] of [["boundary proxy", proxyPort], ["Web upstream", upstreamPort], ["mock Control", controlPort]]) {
        await assertPortAvailable(`after ${label}`, port);
      }
    },
  };
}

export function launchOptions() {
  return process.env.CAPTURE_BROWSER_EXECUTABLE
    ? { executablePath: process.env.CAPTURE_BROWSER_EXECUTABLE, headless: true }
    : { channel: process.env.CAPTURE_BROWSER_CHANNEL ?? "msedge", headless: true };
}

export { MOCK_CONTROL_TOKEN };
