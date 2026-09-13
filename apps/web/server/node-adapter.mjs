import { constants as cryptoConstants, createPublicKey, verify as verifySignature } from "node:crypto";
import { constants, realpathSync } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { Writable } from "node:stream";
import { fileURLToPath } from "node:url";

import { ACCESS_IDENTITY_MAX_LENGTH } from "./access-identity-bound.mjs";
import webHandler from "./index.js";

const ADAPTER_VERSION = 1;
const LISTEN_HOST = "127.0.0.1";
const MAX_REQUEST_BYTES = 1024 * 1024;
const MAX_STATIC_BYTES = 16 * 1024 * 1024;
const SHUTDOWN_TIMEOUT_MS = 2_000;
const SAFE_BUILD_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$/;
const BOOTSTRAP_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
// ⟦P7⟧ The public door. Exactly `https://<fqdn>`: lowercase labels, at least
// one dot, no port, no path. The issuer is the Cloudflare Access team domain
// (or a loopback stand-in for an acceptance on this machine); the audience is
// the Access application's 64-hex AUD tag. All three are public identifiers.
const PUBLIC_ORIGIN_PATTERN =
  /^https:\/\/(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/;
const ACCESS_ISSUER_PATTERN =
  /^(?:https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com|http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4})$/;
const ACCESS_AUDIENCE_PATTERN = /^[0-9a-f]{64}$/;
const ACCESS_ASSERTION_HEADER = "cf-access-jwt-assertion";
const ACCESS_IDENTITY_HEADER = "x-cortex-access-identity";
// The headers the app's boundary reads as ADAPTER evidence. Exactly these are
// stripped from every caller and set by the adapter; the browser's own
// `x-cortex-web-client` and the attestation challenge pass through untouched.
const ADAPTER_EVIDENCE_HEADERS = new Set(["x-cortex-access-bootstrap", ACCESS_IDENTITY_HEADER]);
// Caller-authored client-address claims. None is read by the app today; all
// are stripped on both doors so that the first code to reach for a client IP
// cannot be handed an attacker-authored one (`x-real-ip` is additionally in
// the app boundary's always-rejected set, so passing it through produced a
// self-inflicted 403). The edge's `cf-*` telemetry goes the same way: the
// only edge header the adapter reads is the assertion, and it reads it
// before this strip.
const CLIENT_ADDRESS_HEADERS = new Set(["true-client-ip", "x-real-ip"]);
// The Access session cookie is the assertion's twin: a bearer credential the
// edge sets beside the header. It never crosses into the app, where it would
// sit in logs and request dumps; every other cookie does.
const ACCESS_COOKIE_NAMES = new Set(["CF_Authorization", "CF_AppSession"]);
const JWT_SEGMENT = /^[A-Za-z0-9_-]+$/;
const MAX_ASSERTION_BYTES = 8 * 1024;
const MAX_JWKS_BYTES = 64 * 1024;
const MAX_JWKS_KEYS = 16;
const MIN_RSA_MODULUS_BITS = 2048;
const JWKS_CACHE_TTL_MS = 60 * 60 * 1000;
// A cached key may answer for at most this long after its fetch when the
// issuer cannot be consulted: a deliberate grace for an issuer outage, not an
// unbounded one. Past it the door is closed, whatever is cached, so a retired
// key is honoured for at most 2 h and revocation never depends on the cache.
const JWKS_STALE_CEILING_MS = 2 * JWKS_CACHE_TTL_MS;
const JWKS_REFRESH_MIN_INTERVAL_MS = 30 * 1000;
const JWKS_FETCH_TIMEOUT_MS = 5_000;
const ACCESS_CLOCK_SKEW_SECONDS = 60;
const ACCESS_EMAIL_PATTERN = /^[^\s@,;"'<>()[\]\\]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/i;
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
const PUBLIC_FILES = new Map([
  ["/manifest.webmanifest", { relative: "manifest.webmanifest", type: "application/manifest+json; charset=utf-8", cache: "no-cache" }],
  ["/offline.html", { relative: "offline.html", type: "text/html; charset=utf-8", cache: "no-cache" }],
  ["/sw.js", { relative: "sw.js", type: "text/javascript; charset=utf-8", cache: "no-cache" }],
  ["/icons/cortex-180.png", { relative: "icons/cortex-180.png", type: "image/png", cache: "public, max-age=31536000, immutable" }],
  ["/icons/cortex-192.png", { relative: "icons/cortex-192.png", type: "image/png", cache: "public, max-age=31536000, immutable" }],
  ["/icons/cortex-512.png", { relative: "icons/cortex-512.png", type: "image/png", cache: "public, max-age=31536000, immutable" }],
]);

const moduleDirectory = path.dirname(fileURLToPath(import.meta.url));
const defaultClientRoot = path.resolve(moduleDirectory, "../client");

function jsonLine(stream, value) {
  stream.write(`${JSON.stringify(value)}\n`);
}

async function writeJsonLine(stream, value) {
  const line = `${JSON.stringify(value)}\n`;
  if (!(stream instanceof Writable)) {
    stream.write(line);
    return;
  }
  await new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => stream.off("error", onError);
    const onError = (error) => {
      if (settled) {
        cleanup();
        return;
      }
      settled = true;
      cleanup();
      reject(error);
    };
    stream.once("error", onError);
    try {
      stream.write(line, (error) => {
        if (settled) return;
        settled = true;
        if (error) {
          reject(error);
          setImmediate(cleanup);
          return;
        }
        cleanup();
        resolve();
      });
    } catch (error) {
      settled = true;
      cleanup();
      reject(error);
    }
  });
}

function parseConfiguration(environment) {
  if (environment.CORTEX_WEB_LISTEN_HOST !== LISTEN_HOST) {
    throw new Error("invalid_listen_host");
  }
  const portText = environment.CORTEX_WEB_LISTEN_PORT;
  if (!portText || !/^\d{1,5}$/.test(portText)) {
    throw new Error("invalid_listen_port");
  }
  const port = Number(portText);
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new Error("invalid_listen_port");
  }
  const buildId = environment.CORTEX_WEB_BUILD_ID;
  if (!buildId || !SAFE_BUILD_ID.test(buildId)) {
    throw new Error("invalid_build_id");
  }
  let localAccess = null;
  if (environment.CORTEX_LOCAL_ACCESS_ENABLED !== undefined) {
    const bootstrapToken = environment.CORTEX_ACCESS_BOOTSTRAP_TOKEN;
    if (
      environment.CORTEX_LOCAL_ACCESS_ENABLED !== "1" ||
      !bootstrapToken ||
      !BOOTSTRAP_TOKEN_PATTERN.test(bootstrapToken)
    ) throw new Error("invalid_local_access");
    localAccess = { bootstrapToken, enabled: true };
  }
  let publicAccess = null;
  const publicOrigin = environment.CORTEX_PUBLIC_ORIGIN;
  const issuer = environment.CORTEX_ACCESS_ISSUER;
  const audience = environment.CORTEX_ACCESS_AUDIENCE;
  if (publicOrigin !== undefined || issuer !== undefined || audience !== undefined) {
    // A public origin without the identity check it must pass, or without the
    // local bootstrap the app's boundary demands of every door, is not a
    // weaker door: it is a configuration error, refused before listening.
    if (
      localAccess === null ||
      typeof publicOrigin !== "string" ||
      !PUBLIC_ORIGIN_PATTERN.test(publicOrigin) ||
      typeof issuer !== "string" ||
      !ACCESS_ISSUER_PATTERN.test(issuer) ||
      typeof audience !== "string" ||
      !ACCESS_AUDIENCE_PATTERN.test(audience)
    ) throw new Error("invalid_public_access");
    publicAccess = {
      audience,
      authority: new URL(publicOrigin).host,
      issuer,
      origin: publicOrigin,
    };
  }
  return { buildId, localAccess, port, publicAccess };
}

// ⟦P7⟧ Cloudflare Access identity re-verification. The tunnel only carries
// traffic that passed Access at the edge, but the adapter does not take that
// on faith: every public-door request must carry the `Cf-Access-Jwt-Assertion`
// the edge adds, an RS256 JWT signed by the team's keys, and the adapter checks
// the signature against the issuer's JWKS and the claims against its own
// configuration. Node's `crypto` is enough; there is no JWT dependency.

function parseJwks(body) {
  if (!body || typeof body !== "object" || !Array.isArray(body.keys)) {
    throw new Error("jwks_invalid");
  }
  if (body.keys.length > MAX_JWKS_KEYS) throw new Error("jwks_invalid");
  const keys = new Map();
  for (const candidate of body.keys) {
    if (
      !candidate ||
      typeof candidate !== "object" ||
      candidate.kty !== "RSA" ||
      typeof candidate.kid !== "string" ||
      !candidate.kid ||
      candidate.kid.length > 256 ||
      (candidate.alg !== undefined && candidate.alg !== "RS256") ||
      (candidate.use !== undefined && candidate.use !== "sig") ||
      typeof candidate.n !== "string" ||
      typeof candidate.e !== "string"
    ) continue;
    let key;
    try {
      key = createPublicKey({
        format: "jwk",
        key: { e: candidate.e, kty: "RSA", n: candidate.n },
      });
    } catch {
      continue;
    }
    if (
      key.asymmetricKeyType !== "rsa" ||
      (key.asymmetricKeyDetails?.modulusLength ?? 0) < MIN_RSA_MODULUS_BITS
    ) continue;
    keys.set(candidate.kid, key);
  }
  return keys;
}

async function readBoundedJson(response) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("jwks_invalid");
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_JWKS_BYTES) throw new Error("jwks_oversized");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

export function createJwksCache(issuer, options = {}) {
  const fetchImplementation = options.fetchImplementation ?? fetch;
  const now = options.now ?? Date.now;
  const url = `${issuer}/cdn-cgi/access/certs`;
  let keys = new Map();
  let fetchedAt = Number.NEGATIVE_INFINITY;
  let attemptedAt = Number.NEGATIVE_INFINITY;
  let inFlight = null;
  let fetches = 0;

  function refresh() {
    if (inFlight) return inFlight;
    inFlight = (async () => {
      try {
        fetches += 1;
        const response = await fetchImplementation(url, {
          headers: { accept: "application/json" },
          redirect: "error",
          signal: AbortSignal.timeout(JWKS_FETCH_TIMEOUT_MS),
        });
        if (!response.ok) throw new Error("jwks_unavailable");
        keys = parseJwks(await readBoundedJson(response));
        fetchedAt = now();
      } finally {
        attemptedAt = now();
        inFlight = null;
      }
    })();
    return inFlight;
  }

  return {
    get fetches() {
      return fetches;
    },
    // Resolves to the key for `kid`, or null when a completed fetch does not
    // publish it. Throws when the issuer could not be consulted and the cache
    // is past its staleness ceiling (or empty) -- the caller reports that as
    // unverifiable, not as invalid. Freshness is recomputed from `fetchedAt`
    // at every return, so a key fetched by this very call is fresh, and a
    // stale cache is never served without at least one failed consultation.
    async keyFor(kid) {
      const fresh = () => now() - fetchedAt < JWKS_CACHE_TTL_MS;
      const withinCeiling = () => now() - fetchedAt < JWKS_STALE_CEILING_MS;
      if (fresh() && keys.has(kid)) return keys.get(kid);
      // A stale cache is always re-consulted (coalesced on the one in-flight
      // fetch). A fresh cache that does not publish this kid is re-consulted
      // at most every JWKS_REFRESH_MIN_INTERVAL_MS: an attacker sending
      // unknown kids must not turn the adapter into a JWKS flood.
      let consulted = false;
      if (!fresh() || inFlight || now() - attemptedAt >= JWKS_REFRESH_MIN_INTERVAL_MS) {
        try {
          await refresh();
          consulted = true;
        } catch (error) {
          if (!withinCeiling()) throw error;
        }
      }
      if (!withinCeiling()) throw new Error("jwks_stale");
      const key = keys.get(kid);
      if (key !== undefined) return key;
      // "Invalid" is the answer only when a COMPLETED fetch does not publish
      // the kid. A kid the rate limit (or a failed fetch) kept us from asking
      // about is unverifiable -- retryable, not a verdict on the token.
      if (!consulted) throw new Error("jwks_refresh_suppressed");
      return null;
    },
  };
}

function decodeSegment(segment) {
  if (!JWT_SEGMENT.test(segment)) return null;
  const decoded = Buffer.from(segment, "base64url");
  if (decoded.toString("base64url") !== segment) return null;
  return decoded;
}

function decodeJsonSegment(segment) {
  const decoded = decodeSegment(segment);
  if (decoded === null) return null;
  try {
    const value = JSON.parse(decoded.toString("utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function claimsAccepted(payload, { audience, issuer, nowSeconds }) {
  if (payload.iss !== issuer) return false;
  const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (
    audiences.length === 0 ||
    audiences.length > 16 ||
    !audiences.every((item) => typeof item === "string") ||
    !audiences.includes(audience)
  ) return false;
  const { exp, iat, nbf, email } = payload;
  if (!Number.isFinite(exp) || !Number.isFinite(iat)) return false;
  if (nowSeconds >= exp + ACCESS_CLOCK_SKEW_SECONDS) return false;
  if (iat > nowSeconds + ACCESS_CLOCK_SKEW_SECONDS) return false;
  if (nbf !== undefined && (!Number.isFinite(nbf) || nbf > nowSeconds + ACCESS_CLOCK_SKEW_SECONDS)) {
    return false;
  }
  return (
    typeof email === "string" &&
    email.length <= ACCESS_IDENTITY_MAX_LENGTH &&
    ACCESS_EMAIL_PATTERN.test(email)
  );
}

export function createAccessVerifier({ audience, issuer, fetchImplementation, now = Date.now }) {
  if (
    typeof issuer !== "string" ||
    !ACCESS_ISSUER_PATTERN.test(issuer) ||
    typeof audience !== "string" ||
    !ACCESS_AUDIENCE_PATTERN.test(audience)
  ) throw new Error("invalid_public_access");
  const jwks = createJwksCache(issuer, { fetchImplementation, now });
  const invalid = { category: "access_identity_invalid", ok: false };
  return {
    jwks,
    async verify(assertion) {
      if (
        typeof assertion !== "string" ||
        assertion.length === 0 ||
        Buffer.byteLength(assertion, "utf8") > MAX_ASSERTION_BYTES
      ) return invalid;
      const segments = assertion.split(".");
      if (segments.length !== 3) return invalid;
      const [encodedHeader, encodedPayload, encodedSignature] = segments;
      const header = decodeJsonSegment(encodedHeader);
      const payload = decodeJsonSegment(encodedPayload);
      const signature = decodeSegment(encodedSignature);
      if (
        header === null ||
        payload === null ||
        signature === null ||
        header.alg !== "RS256" ||
        typeof header.kid !== "string" ||
        !header.kid ||
        (header.typ !== undefined && header.typ !== "JWT")
      ) return invalid;
      let key;
      try {
        key = await jwks.keyFor(header.kid);
      } catch {
        return { category: "access_identity_unverifiable", ok: false };
      }
      if (!key) return invalid;
      const signed = Buffer.from(`${encodedHeader}.${encodedPayload}`, "ascii");
      let valid = false;
      try {
        valid = verifySignature(
          "sha256",
          signed,
          { key, padding: cryptoConstants.RSA_PKCS1_PADDING },
          signature,
        );
      } catch {
        valid = false;
      }
      if (!valid) return invalid;
      if (!claimsAccepted(payload, { audience, issuer, nowSeconds: Math.floor(now() / 1000) })) {
        return invalid;
      }
      return {
        identity: { email: payload.email, expiresAt: payload.exp, issuedAt: payload.iat },
        ok: true,
      };
    },
  };
}

function headerValues(request, name) {
  const values = [];
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    if (request.rawHeaders[index].toLowerCase() === name) values.push(request.rawHeaders[index + 1]);
  }
  return values;
}

function connectionHeaderNames(request) {
  const result = new Set();
  for (const value of request.headersDistinct?.connection ?? []) {
    for (const token of value.split(",")) {
      const normalized = token.trim().toLowerCase();
      if (normalized) result.add(normalized);
    }
  }
  return result;
}

function withoutAccessCookies(value) {
  return value
    .split(";")
    .map((pair) => pair.trim())
    .filter((pair) => pair !== "" && !ACCESS_COOKIE_NAMES.has(pair.split("=", 1)[0].trim()))
    .join("; ");
}

function incomingHeaders(request, door) {
  const headers = new Headers();
  const connectionNames = connectionHeaderNames(request);
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    const name = request.rawHeaders[index].toLowerCase();
    const value = request.rawHeaders[index + 1];
    // Every header the app's boundary reads as adapter evidence is stripped
    // from the caller and set below, whichever door the request came through.
    // Nothing the edge adds crosses either: not the assertion, not the
    // `cf-*` telemetry, not a client-address claim, not the Access cookie.
    if (
      name === "host" ||
      name === "forwarded" ||
      ADAPTER_EVIDENCE_HEADERS.has(name) ||
      CLIENT_ADDRESS_HEADERS.has(name) ||
      name.startsWith("x-forwarded-") ||
      name.startsWith("cf-") ||
      HOP_BY_HOP_HEADERS.has(name) ||
      connectionNames.has(name)
    ) continue;
    if (name === "cookie") {
      const kept = withoutAccessCookies(value);
      if (kept !== "") headers.append(name, kept);
      continue;
    }
    headers.append(name, value);
  }
  headers.set("host", door.authority);
  if (door.localAccess?.enabled === true) {
    headers.set("x-cortex-access-bootstrap", door.localAccess.bootstrapToken);
    headers.set("x-forwarded-for", LISTEN_HOST);
    headers.set("x-forwarded-host", door.authority);
    if (door.kind === "public") {
      headers.set("x-forwarded-port", "443");
      headers.set("x-forwarded-proto", "https");
      headers.set(ACCESS_IDENTITY_HEADER, door.identity.email);
    } else {
      headers.set("x-forwarded-port", new URL(`http://${door.authority}`).port);
      headers.set("x-forwarded-proto", "http");
    }
  }
  return headers;
}

function declaredContentLength(request) {
  const values = [];
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    if (request.rawHeaders[index].toLowerCase() === "content-length") {
      values.push(request.rawHeaders[index + 1]);
    }
  }
  if (values.length === 0) return null;
  if (values.length !== 1 || !/^(?:0|[1-9]\d*)$/.test(values[0])) return Number.NaN;
  const length = Number(values[0]);
  return Number.isSafeInteger(length) ? length : Number.NaN;
}

function limitedRequestBody(request) {
  const state = { exceeded: false };
  let observed = 0;
  let consumerOpen = true;
  let streamController;
  let settleCompletion;
  let signalExceeded;
  const completion = new Promise((resolve) => {
    settleCompletion = resolve;
  });
  const limitExceeded = new Promise((resolve) => {
    signalExceeded = resolve;
  });
  let settled = false;

  const cleanup = () => {
    request.off("data", onData);
    request.off("end", onEnd);
    request.off("aborted", onAborted);
    request.off("error", onAborted);
  };
  const settle = (outcome) => {
    if (settled) return;
    settled = true;
    cleanup();
    settleCompletion(outcome);
  };
  const failConsumer = (reason) => {
    if (!consumerOpen) return;
    consumerOpen = false;
    streamController.error(reason);
  };
  const onData = (chunk) => {
    observed += chunk.byteLength;
    if (observed > MAX_REQUEST_BYTES) {
      state.exceeded = true;
      signalExceeded();
      failConsumer(new Error("request_body_too_large"));
      settle("exceeded");
      request.resume();
      return;
    }
    if (consumerOpen) streamController.enqueue(new Uint8Array(chunk));
  };
  const onEnd = () => {
    if (consumerOpen) {
      consumerOpen = false;
      streamController.close();
    }
    settle("complete");
  };
  const onAborted = () => {
    failConsumer(new Error("request_aborted"));
    settle("aborted");
  };
  const body = new ReadableStream({
    start(controller) {
      streamController = controller;
    },
    cancel() {
      consumerOpen = false;
    },
  });
  request.on("data", onData);
  request.once("end", onEnd);
  request.once("aborted", onAborted);
  request.once("error", onAborted);
  return { body, completion, limitExceeded, state };
}

function requestUrl(rawTarget, origin) {
  if (
    typeof rawTarget !== "string" ||
    !rawTarget.startsWith("/") ||
    rawTarget.startsWith("//") ||
    rawTarget.includes("\\") ||
    rawTarget.includes("\0") ||
    rawTarget.includes("#")
  ) return null;
  const rawPath = rawTarget.split("?", 1)[0];
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(rawPath);
  } catch {
    return null;
  }
  if (
    decodedPath.includes("\\") ||
    decodedPath.includes("\0") ||
    decodedPath.split("/").some((component) => component === "." || component === "..")
  ) return null;
  const url = new URL(rawTarget, origin);
  return url.origin === origin ? url : null;
}

function exactHost(request, authority) {
  let count = 0;
  let value = null;
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    if (request.rawHeaders[index].toLowerCase() === "host") {
      count += 1;
      value = request.rawHeaders[index + 1];
    }
  }
  return count === 1 && value === authority;
}

function isLoopbackSocket(request) {
  return request.socket.localAddress === LISTEN_HOST &&
    request.socket.remoteAddress === LISTEN_HOST;
}

function publicEntry(pathname) {
  const fixed = PUBLIC_FILES.get(pathname);
  if (fixed) return fixed;
  if (/^\/assets\/[A-Za-z0-9][A-Za-z0-9._/-]*\.css$/.test(pathname)) {
    return {
      relative: pathname.slice(1),
      type: "text/css; charset=utf-8",
      cache: "public, max-age=31536000, immutable",
    };
  }
  if (/^\/assets\/[A-Za-z0-9][A-Za-z0-9._/-]*\.js$/.test(pathname)) {
    return {
      relative: pathname.slice(1),
      type: "text/javascript; charset=utf-8",
      cache: "public, max-age=31536000, immutable",
    };
  }
  return null;
}

function sameStableStat(left, right) {
  return left.dev === right.dev &&
    left.ino === right.ino &&
    left.mode === right.mode &&
    left.nlink === right.nlink &&
    left.uid === right.uid &&
    left.gid === right.gid &&
    left.size === right.size &&
    left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs;
}

async function bindClientRoot(configuredRoot) {
  let handle;
  try {
    const configured = await lstat(configuredRoot, { bigint: true });
    if (configured.isSymbolicLink() || !configured.isDirectory()) {
      throw new Error("invalid_client_root");
    }
    const rootPath = await realpath(configuredRoot);
    handle = await open(
      rootPath,
      constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) | (constants.O_NOFOLLOW ?? 0),
    );
    const held = await handle.stat({ bigint: true });
    const named = await lstat(rootPath, { bigint: true });
    if (
      !held.isDirectory() ||
      named.isSymbolicLink() ||
      !named.isDirectory() ||
      !sameStableStat(configured, held) ||
      !sameStableStat(held, named)
    ) throw new Error("invalid_client_root");
    return { handle, rootPath, stat: held };
  } catch {
    await handle?.close();
    throw new Error("invalid_client_root");
  }
}

async function verifyClientRoot(binding) {
  try {
    const held = await binding.handle.stat({ bigint: true });
    const named = await lstat(binding.rootPath, { bigint: true });
    return held.isDirectory() &&
      named.isDirectory() &&
      !named.isSymbolicLink() &&
      sameStableStat(binding.stat, held) &&
      sameStableStat(binding.stat, named);
  } catch {
    return false;
  }
}

async function readPublicFile(clientRoot, entry) {
  const components = entry.relative.split("/");
  if (components.some((component) => !component || component === "." || component === "..")) {
    return null;
  }
  if (!await verifyClientRoot(clientRoot)) return null;

  let cursor = clientRoot.rootPath;
  const namedPath = [];
  for (let index = 0; index < components.length; index += 1) {
    cursor = path.join(cursor, components[index]);
    let details;
    try {
      details = await lstat(cursor, { bigint: true });
    } catch {
      return null;
    }
    if (details.isSymbolicLink()) return null;
    if (index < components.length - 1 ? !details.isDirectory() : !details.isFile()) {
      return null;
    }
    namedPath.push({ cursor, details });
  }

  let handle;
  try {
    handle = await open(cursor, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const opened = await handle.stat({ bigint: true });
    const named = await lstat(cursor, { bigint: true });
    if (
      !opened.isFile() ||
      named.isSymbolicLink() ||
      !sameStableStat(opened, named) ||
      !sameStableStat(namedPath.at(-1).details, named) ||
      opened.size > BigInt(MAX_STATIC_BYTES)
    ) return null;
    const contents = await handle.readFile();
    const closed = await handle.stat({ bigint: true });
    if (
      contents.byteLength > MAX_STATIC_BYTES ||
      !sameStableStat(opened, closed) ||
      BigInt(contents.byteLength) !== opened.size ||
      !await verifyClientRoot(clientRoot)
    ) return null;
    for (const snapshot of namedPath) {
      const current = await lstat(snapshot.cursor, { bigint: true });
      if (current.isSymbolicLink() || !sameStableStat(snapshot.details, current)) return null;
    }
    return contents;
  } catch {
    return null;
  } finally {
    await handle?.close();
  }
}

async function staticResponse(request, clientRoot) {
  if (!new Set(["GET", "HEAD"]).has(request.method)) {
    return new Response("Method Not Allowed\n", {
      status: 405,
      headers: { Allow: "GET, HEAD", "Cache-Control": "no-store" },
    });
  }
  const entry = publicEntry(new URL(request.url).pathname);
  if (!entry) return new Response("Not Found\n", { status: 404 });
  const contents = await readPublicFile(clientRoot, entry);
  if (contents === null) return new Response("Not Found\n", { status: 404 });
  const headers = {
    "Cache-Control": entry.cache,
    "Content-Length": String(contents.byteLength),
    "Content-Type": entry.type,
    "X-Content-Type-Options": "nosniff",
  };
  return new Response(request.method === "HEAD" ? null : contents, { status: 200, headers });
}

function healthResponse(method, buildId, publicDoor) {
  if (!["GET", "HEAD"].includes(method)) {
    return new Response("Method Not Allowed\n", {
      status: 405,
      headers: { Allow: "GET, HEAD", "Cache-Control": "no-store" },
    });
  }
  // The public door appears here as one boolean and nothing else: no origin,
  // no issuer, no identity. The lifecycle's health check tolerates the key.
  const body = Buffer.from(`${JSON.stringify({
    adapter_version: ADAPTER_VERSION,
    build_id: buildId,
    public_door: publicDoor,
    service: "cortex-web",
    status: "ok",
  })}\n`);
  return new Response(method === "HEAD" ? null : body, {
    status: 200,
    headers: {
      "Cache-Control": "no-store",
      "Content-Length": String(body.byteLength),
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function responseHeaders(response) {
  const result = new Map();
  const connectionNames = new Set();
  for (const value of response.headers.get("connection")?.split(",") ?? []) {
    const normalized = value.trim().toLowerCase();
    if (normalized) connectionNames.add(normalized);
  }
  for (const [name, value] of response.headers) {
    const normalized = name.toLowerCase();
    if (
      normalized === "set-cookie" ||
      HOP_BY_HOP_HEADERS.has(normalized) ||
      connectionNames.has(normalized)
    ) continue;
    result.set(name, value);
  }
  const cookies = typeof response.headers.getSetCookie === "function"
    ? response.headers.getSetCookie()
    : [];
  if (cookies.length) result.set("set-cookie", cookies);
  return result;
}

async function writeFetchResponse(request, response, nodeResponse) {
  nodeResponse.statusCode = response.status;
  for (const [name, value] of responseHeaders(response)) {
    nodeResponse.setHeader(name, value);
  }
  if (request.method === "HEAD" || response.body === null) {
    await response.body?.cancel();
    nodeResponse.end();
    return;
  }

  const reader = response.body.getReader();
  const disconnected = () => {
    if (!nodeResponse.writableEnded) void reader.cancel("client_disconnected");
  };
  nodeResponse.once("close", disconnected);
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!nodeResponse.write(value)) {
        await new Promise((resolve, reject) => {
          nodeResponse.once("drain", resolve);
          nodeResponse.once("error", reject);
        });
      }
    }
    nodeResponse.end();
  } finally {
    nodeResponse.off("close", disconnected);
    reader.releaseLock();
  }
}

function requestFailure(response, status, body) {
  if (response.headersSent) {
    response.destroy();
    return;
  }
  const encoded = Buffer.from(`${body}\n`);
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Length": String(encoded.byteLength),
    "Content-Type": "text/plain; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(encoded);
}

function accessProblem(response, status, category) {
  if (response.headersSent) {
    response.destroy();
    return;
  }
  const encoded = Buffer.from(`${JSON.stringify({
    category,
    owner: "cortex-web",
    retryable: status >= 500,
    status,
    title: status === 401
      ? "The public front door requires a verified Cloudflare Access identity"
      : "The public front door could not verify the Cloudflare Access identity",
    type: `urn:cortex:web-problem:${category}`,
  })}\n`);
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Length": String(encoded.byteLength),
    "Content-Type": "application/problem+json",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(encoded);
}

function makeRequestHandler({
  authority,
  buildId,
  clientRoot,
  fetchHandler,
  localAccess,
  origin,
  publicAccess,
  verifier,
}) {
  const assets = { fetch: (request) => staticResponse(request, clientRoot) };
  return async (request, response) => {
    // Two doors, one loopback listener, told apart by the exact `Host`: the
    // local authority is the door of before; the public authority exists only
    // when a public origin is configured, and is opened by a verified Access
    // identity and nothing else. Any other `Host` is misdirected, as always.
    let door = null;
    if (isLoopbackSocket(request)) {
      if (exactHost(request, authority)) {
        door = { authority, kind: "local", localAccess };
      } else if (publicAccess !== null && exactHost(request, publicAccess.authority)) {
        door = { authority: publicAccess.authority, kind: "public", localAccess };
      }
    }
    if (door === null) {
      requestFailure(response, 421, "Misdirected Request");
      return;
    }
    if (door.kind === "public") {
      const assertions = headerValues(request, ACCESS_ASSERTION_HEADER);
      if (assertions.length !== 1) {
        request.resume();
        accessProblem(response, 401, "access_identity_missing");
        return;
      }
      const verified = await verifier.verify(assertions[0]);
      if (!verified.ok) {
        request.resume();
        accessProblem(
          response,
          verified.category === "access_identity_unverifiable" ? 503 : 401,
          verified.category,
        );
        return;
      }
      door.identity = verified.identity;
    }
    if (!["GET", "HEAD", "POST"].includes(request.method ?? "")) {
      requestFailure(response, 405, "Method Not Allowed");
      return;
    }
    const url = requestUrl(request.url, origin);
    if (url === null) {
      requestFailure(response, 400, "Bad Request");
      return;
    }

    const controller = new AbortController();
    const abort = () => controller.abort(new Error("request_aborted"));
    request.once("aborted", abort);
    request.once("error", abort);
    response.once("close", () => {
      if (!response.writableEnded) abort();
    });
    const headers = incomingHeaders(request, door);
    const contentLength = declaredContentLength(request);
    if (Number.isNaN(contentLength)) {
      requestFailure(response, 400, "Bad Request");
      return;
    }
    if (contentLength !== null && contentLength > MAX_REQUEST_BYTES) {
      response.setHeader("Connection", "close");
      request.resume();
      requestFailure(response, 413, "Content Too Large");
      return;
    }
    const init = {
      method: request.method,
      headers,
      signal: controller.signal,
    };
    let bodyGuard = null;
    if (!new Set(["GET", "HEAD"]).has(request.method)) {
      bodyGuard = limitedRequestBody(request);
      init.body = bodyGuard.body;
      init.duplex = "half";
    }
    try {
      const fetchRequest = new Request(url, init);
      let responsePromise;
      const entry = publicEntry(url.pathname);
      if (entry) {
        responsePromise = staticResponse(fetchRequest, clientRoot);
      } else if (url.pathname === "/_cortex/health") {
        responsePromise = Promise.resolve(
          healthResponse(request.method, buildId, publicAccess !== null),
        );
      } else {
        responsePromise = Promise.resolve(fetchHandler.fetch(
          fetchRequest,
          { ASSETS: assets },
          { passThroughOnException() {}, waitUntil() {} },
        ));
      }
      let fetchResponse;
      if (bodyGuard) {
        const first = await Promise.race([
          responsePromise.then(
            (value) => ({ outcome: "response", value }),
            (error) => ({ error, outcome: "error" }),
          ),
          bodyGuard.limitExceeded.then(() => ({ outcome: "exceeded" })),
        ]);
        if (first.outcome === "exceeded") {
          controller.abort(new Error("request_body_too_large"));
          response.setHeader("Connection", "close");
          request.resume();
          requestFailure(response, 413, "Content Too Large");
          void responsePromise.then((lateResponse) => lateResponse?.body?.cancel()).catch(() => {});
          return;
        }
        if (first.outcome === "error") throw first.error;
        fetchResponse = first.value;
        const bodyOutcome = await bodyGuard.completion;
        if (bodyOutcome === "exceeded") {
          controller.abort(new Error("request_body_too_large"));
          const cancellation = fetchResponse?.body?.cancel();
          if (cancellation) void cancellation.catch(() => {});
          response.setHeader("Connection", "close");
          request.resume();
          requestFailure(response, 413, "Content Too Large");
          return;
        }
        if (bodyOutcome !== "complete") throw new Error("request_aborted");
      } else {
        fetchResponse = await responsePromise;
      }
      if (!(fetchResponse instanceof Response)) {
        throw new Error("invalid_fetch_response");
      }
      await writeFetchResponse(request, fetchResponse, response);
    } catch {
      const oversized = bodyGuard?.state.exceeded === true;
      if (oversized) {
        response.setHeader("Connection", "close");
        request.resume();
      }
      requestFailure(
        response,
        oversized ? 413 : 500,
        oversized ? "Content Too Large" : "Internal Server Error",
      );
    } finally {
      request.off("aborted", abort);
      request.off("error", abort);
    }
  };
}

function closeServer(server, sockets) {
  let closePromise;
  return () => {
    if (closePromise) return closePromise;
    closePromise = new Promise((resolve) => {
      let complete = false;
      const deadline = setTimeout(() => {
        server.closeAllConnections?.();
        for (const socket of sockets) socket.destroy();
        finish(true);
      }, SHUTDOWN_TIMEOUT_MS);
      deadline.unref();
      const finish = (forced) => {
        if (complete) return;
        complete = true;
        clearTimeout(deadline);
        resolve({ forced });
      };
      try {
        server.close((error) => {
          if (error && error.code !== "ERR_SERVER_NOT_RUNNING") {
            server.closeAllConnections?.();
            for (const socket of sockets) socket.destroy();
            finish(true);
            return;
          }
          finish(false);
        });
        server.closeIdleConnections?.();
      } catch {
        for (const socket of sockets) socket.destroy();
        finish(true);
      }
    });
    return closePromise;
  };
}

export async function startNodeAdapter(options = {}) {
  const environment = options.environment ?? process.env;
  const output = options.output ?? process.stdout;
  const fetchHandler = options.fetchHandler ?? webHandler;
  if (!fetchHandler || typeof fetchHandler.fetch !== "function") {
    throw new Error("invalid_fetch_handler");
  }
  const { buildId, localAccess, port, publicAccess } = parseConfiguration(environment);
  const verifier = publicAccess === null
    ? null
    : createAccessVerifier({
      audience: publicAccess.audience,
      fetchImplementation: options.fetchImplementation,
      issuer: publicAccess.issuer,
      now: options.now,
    });
  const clientRoot = await bindClientRoot(path.resolve(options.clientRoot ?? defaultClientRoot));
  const serverFactory = options.serverFactory ?? createServer;
  const sockets = new Set();
  let closeResources = null;
  let transferred = false;
  try {
    const server = serverFactory();
    const closeListener = closeServer(server, sockets);
    let closePromise;
    closeResources = () => {
      if (!closePromise) {
        closePromise = (async () => {
          try {
            return await closeListener();
          } finally {
            await clientRoot.handle.close();
          }
        })();
      }
      return closePromise;
    };
    server.on("connection", (socket) => {
      sockets.add(socket);
      socket.once("close", () => sockets.delete(socket));
    });
    server.on("clientError", (_error, socket) => {
      if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
      else socket.destroy();
    });
    server.headersTimeout = 10_000;
    server.requestTimeout = 30_000;
    server.keepAliveTimeout = 5_000;

    await new Promise((resolve, reject) => {
      const onError = (error) => {
        server.off("listening", onListening);
        reject(error);
      };
      const onListening = () => {
        server.off("error", onError);
        resolve();
      };
      server.once("error", onError);
      server.once("listening", onListening);
      server.listen({ host: LISTEN_HOST, port });
    });
    const address = server.address();
    if (!address || typeof address === "string" || address.address !== LISTEN_HOST) {
      throw new Error("invalid_listener_address");
    }
    const origin = `http://${LISTEN_HOST}:${address.port}`;
    const authority = new URL(origin).host;
    if (localAccess?.enabled === true) environment.CORTEX_LOCAL_ORIGIN = origin;
    server.on("request", makeRequestHandler({
      authority,
      buildId,
      clientRoot,
      fetchHandler,
      localAccess,
      origin,
      publicAccess,
      verifier,
    }));
    await writeJsonLine(output, {
      adapter_version: ADAPTER_VERSION,
      build_id: buildId,
      event: "ready",
      host: LISTEN_HOST,
      port: address.port,
      service: "cortex-web",
    });
    const runtime = { address, close: closeResources, origin, server };
    transferred = true;
    return runtime;
  } finally {
    if (!transferred) {
      if (closeResources) await closeResources();
      else await clientRoot.handle.close();
    }
  }
}

export function installSignalHandlers(runtime, options = {}) {
  const output = options.output ?? process.stdout;
  const processObject = options.processObject ?? process;
  let stopping = false;
  for (const signal of ["SIGINT", "SIGTERM"]) {
    processObject.once(signal, async () => {
      if (stopping) return;
      stopping = true;
      const result = await runtime.close();
      const exitCode = result.forced ? 1 : 0;
      output.write(`${JSON.stringify({
        event: "stopped",
        forced: result.forced,
        service: "cortex-web",
        signal,
      })}\n`, () => processObject.exit(exitCode));
    });
  }
}

async function main() {
  const runtime = await startNodeAdapter();
  installSignalHandlers(runtime);
}

const invokedPath = process.argv[1] ? realpathSync(process.argv[1]) : null;
if (invokedPath === realpathSync(fileURLToPath(import.meta.url))) {
  main().catch(() => {
    jsonLine(process.stderr, {
      code: "startup_failed",
      event: "fatal",
      service: "cortex-web",
    });
    process.exitCode = 1;
  });
}
