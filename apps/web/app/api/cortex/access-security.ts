import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import type { NextRequest } from "next/server";

// ⟦ADJ-H-1⟧ One bound, imported rather than restated: the daemon records a
// verified identity as the actor `access:<id>` against a 200-character
// `actor_id` column, so an identity longer than 193 opens the door and then
// fails every write with 400. `server/node-adapter.mjs` imports the same
// module and refuses the same length before it ever mints the header. The
// door refuses what the daemon refuses.
import { ACCESS_IDENTITY_MAX_LENGTH } from "../../../server/access-identity-bound.mjs";

const ATTESTATION_DERIVATION_DOMAIN = "cortex-web-access-attestation-v1";
const BOOTSTRAP_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const ATTESTATION_CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
// ⟦P7⟧ The public door's origin is exactly `https://<fqdn>`: lowercase labels,
// at least one dot, no port, no path. The adapter opens that door only to a
// request whose Cloudflare Access assertion it verified, and says so with
// one header it strips from every caller and sets itself.
const PUBLIC_HOSTNAME_PATTERN =
  /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/;
const ACCESS_IDENTITY_HEADER = "x-cortex-access-identity";
const ACCESS_IDENTITY_PATTERN =
  /^[^\s@,;"'<>()[\]\\]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/i;
const ALWAYS_REJECTED_FORWARDING_HEADERS = new Set([
  "forwarded",
  "x-forwarded-prefix",
  "x-forwarded-server",
  "x-real-ip",
]);

type AccessDoor = {
  kind: "local" | "public";
  origin: URL;
  originFingerprint: string;
};

type AccessConfiguration = {
  bootstrapToken: string;
  doors: readonly AccessDoor[];
};

export type AccessBoundaryFailure = {
  allowed: false;
  category: "access_boundary_unconfigured" | "access_boundary_rejected";
};

export type AccessBoundaryResult =
  | {
      allowed: true;
      door: "local" | "public";
      origin: string;
      // ⟦P8-08⟧ The Cloudflare Access identity this adapter verified, carried
      // out so the daemon can record who acted instead of only which door
      // opened. `null` for the local door, where the caller is the operator at
      // the machine and there is no assertion to verify.
      identity: string | null;
    }
  | AccessBoundaryFailure;

export type AccessBoundaryAttestation = {
  attestation: string;
  challenge: string;
  origin_fingerprint: string;
  service: "cortex-web-access-boundary";
  status: "ok";
  version: 1;
  web_access_boundary_verified: true;
};

function accessDoor(kind: AccessDoor["kind"], origin: URL): AccessDoor {
  return {
    kind,
    origin,
    originFingerprint: createHash("sha256")
      .update(origin.origin, "utf8")
      .digest("base64url"),
  };
}

function accessConfiguration(): AccessConfiguration | null {
  // Two doors, one listener: the local door is the adapter's own loopback
  // origin, the public door the one public origin `[web]` names. Either or
  // both may be configured; a door whose origin does not parse fails the
  // whole boundary closed rather than leaving the other one open.
  const localOriginValue = process.env.CORTEX_LOCAL_ORIGIN;
  const publicOriginValue = process.env.CORTEX_PUBLIC_ORIGIN;
  if (!localOriginValue && !publicOriginValue) return null;
  const doors: AccessDoor[] = [];
  if (localOriginValue) {
    const origin = parseLocalOrigin(localOriginValue);
    if (!origin) return null;
    doors.push(accessDoor("local", origin));
  }
  if (publicOriginValue) {
    const origin = parsePublicOrigin(publicOriginValue);
    if (!origin) return null;
    doors.push(accessDoor("public", origin));
  }
  const bootstrapToken = process.env.CORTEX_ACCESS_BOOTSTRAP_TOKEN;
  if (!bootstrapToken || !BOOTSTRAP_TOKEN_PATTERN.test(bootstrapToken)) {
    return null;
  }
  return { bootstrapToken, doors };
}

function parsePublicOrigin(value: string): URL | null {
  let origin: URL;
  try {
    origin = new URL(value);
  } catch {
    return null;
  }
  if (
    origin.origin !== value ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  ) return null;
  if (
    origin.protocol !== "https:" ||
    origin.port !== "" ||
    !PUBLIC_HOSTNAME_PATTERN.test(origin.hostname)
  ) return null;
  return origin;
}

function parseLocalOrigin(value: string): URL | null {
  let origin: URL;
  try {
    origin = new URL(value);
  } catch {
    return null;
  }
  if (
    origin.origin !== value ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash ||
    origin.protocol !== "http:" ||
    origin.hostname !== "127.0.0.1" ||
    !/^\d{1,5}$/.test(origin.port)
  ) return null;
  const port = Number(origin.port);
  return port >= 1 && port <= 65_535 ? origin : null;
}

function singleHeader(headers: Headers, name: string): string | null {
  const value = headers.get(name);
  if (!value || value.includes(",") || value.includes("\r") || value.includes("\n")) {
    return null;
  }
  return value;
}

function forwardingHeadersAreTrusted(headers: Headers): boolean {
  for (const [name] of headers) {
    const key = name.toLowerCase();
    if (
      ALWAYS_REJECTED_FORWARDING_HEADERS.has(key) ||
      (key.startsWith("x-forwarded-") &&
        key !== "x-forwarded-host" &&
        key !== "x-forwarded-proto" &&
        key !== "x-forwarded-for" &&
        key !== "x-forwarded-port")
    ) return false;
  }
  const forwardedFor = headers.get("x-forwarded-for");
  if (
    forwardedFor !== null &&
    (singleHeader(headers, "x-forwarded-for") === null ||
      !["127.0.0.1", "::1"].includes(forwardedFor))
  ) return false;
  const forwardedPort = headers.get("x-forwarded-port");
  if (forwardedPort !== null) {
    const canonicalPort = singleHeader(headers, "x-forwarded-port");
    if (
      !canonicalPort ||
      !/^\d{1,5}$/.test(canonicalPort) ||
      Number(canonicalPort) < 1 ||
      Number(canonicalPort) > 65_535
    ) return false;
  }
  return true;
}

function bootstrapMatches(candidate: string, expected: string): boolean {
  if (!BOOTSTRAP_TOKEN_PATTERN.test(candidate)) return false;
  const candidateDigest = createHash("sha256").update(candidate, "ascii").digest();
  const expectedDigest = createHash("sha256").update(expected, "ascii").digest();
  return timingSafeEqual(candidateDigest, expectedDigest);
}

function verifiedIdentity(headers: Headers): string | null {
  const identity = singleHeader(headers, ACCESS_IDENTITY_HEADER);
  if (
    !identity ||
    identity.length > ACCESS_IDENTITY_MAX_LENGTH ||
    !ACCESS_IDENTITY_PATTERN.test(identity)
  ) {
    return null;
  }
  return identity;
}

function configuredDoorForRequest(
  request: NextRequest,
  configuration: AccessConfiguration,
): AccessDoor | null {
  const host = singleHeader(request.headers, "host");
  const forwardedHost = singleHeader(request.headers, "x-forwarded-host");
  const forwardedProto = singleHeader(request.headers, "x-forwarded-proto");
  if (!host || !forwardedHost || !forwardedProto || host !== forwardedHost) return null;
  const door = configuration.doors.find(
    (candidate) =>
      candidate.origin.host === host &&
      candidate.origin.protocol.slice(0, -1) === forwardedProto,
  );
  if (!door) return null;
  if (
    door.kind === "local" &&
    (singleHeader(request.headers, "x-forwarded-for") !== "127.0.0.1" ||
      singleHeader(request.headers, "x-forwarded-port") !== door.origin.port)
  ) return null;
  // The public door is opened by the adapter's verified identity, a header
  // the adapter strips from every caller and sets only after the Cloudflare
  // Access assertion verified. Without it the request is not a public-door
  // request at all, whatever its origin headers say.
  if (door.kind === "public" && verifiedIdentity(request.headers) === null) return null;
  return door;
}

function refererMatches(headers: Headers, origin: URL): boolean {
  const referer = headers.get("referer");
  if (referer === null) return true;
  try {
    return new URL(referer).origin === origin.origin;
  } catch {
    return false;
  }
}

function verifyConfiguredAccessBoundary(
  request: NextRequest,
  method: "GET" | "POST",
  configuration: AccessConfiguration,
): AccessBoundaryResult {
  if (!forwardingHeadersAreTrusted(request.headers)) {
    return { allowed: false, category: "access_boundary_rejected" };
  }

  const bootstrap = singleHeader(request.headers, "x-cortex-access-bootstrap");
  const door = configuredDoorForRequest(request, configuration);
  const fetchSite = singleHeader(request.headers, "sec-fetch-site");
  const requestOrigin = request.headers.get("origin");
  if (
    !bootstrap ||
    !bootstrapMatches(bootstrap, configuration.bootstrapToken) ||
    !door ||
    (fetchSite !== "same-origin" && fetchSite !== "none") ||
    (requestOrigin !== null &&
      (requestOrigin.includes(",") || requestOrigin !== door.origin.origin)) ||
    (method === "POST" &&
      (requestOrigin !== door.origin.origin ||
        !refererMatches(request.headers, door.origin) ||
        singleHeader(request.headers, "x-cortex-web-client") !== "v1"))
  ) return { allowed: false, category: "access_boundary_rejected" };

  // Only the public door has an assertion to carry: `configuredDoorForRequest`
  // already refused a public request without a verified identity, so this
  // re-read cannot be null there, and the local door deliberately has none.
  return {
    allowed: true,
    door: door.kind,
    origin: door.origin.origin,
    identity: door.kind === "public" ? verifiedIdentity(request.headers) : null,
  };
}

export function verifyAccessBoundary(
  request: NextRequest,
  method: "GET" | "POST",
): AccessBoundaryResult {
  const configuration = accessConfiguration();
  if (!configuration) return { allowed: false, category: "access_boundary_unconfigured" };
  return verifyConfiguredAccessBoundary(request, method, configuration);
}

export function createAccessBoundaryAttestation(
  request: NextRequest,
): AccessBoundaryAttestation | AccessBoundaryFailure {
  const configuration = accessConfiguration();
  if (!configuration) return { allowed: false, category: "access_boundary_unconfigured" };
  const boundary = verifyConfiguredAccessBoundary(request, "GET", configuration);
  if (!boundary.allowed) return boundary;
  const door = configuration.doors.find((candidate) => candidate.kind === boundary.door);
  if (!door) return { allowed: false, category: "access_boundary_rejected" };
  const challenge = singleHeader(
    request.headers,
    "x-cortex-access-attestation-challenge",
  );
  if (!challenge || !ATTESTATION_CHALLENGE_PATTERN.test(challenge)) {
    return { allowed: false, category: "access_boundary_rejected" };
  }

  const attestationPayload = [
    ATTESTATION_DERIVATION_DOMAIN,
    challenge,
    door.originFingerprint,
  ].join("\0");
  return {
    attestation: createHmac("sha256", configuration.bootstrapToken)
      .update(attestationPayload, "utf8")
      .digest("base64url"),
    challenge,
    origin_fingerprint: door.originFingerprint,
    service: "cortex-web-access-boundary",
    status: "ok",
    version: 1,
    web_access_boundary_verified: true,
  };
}
