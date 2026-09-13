import { NextRequest, NextResponse } from "next/server";
import { verifyAccessBoundary } from "../access-security";

const ID = "[A-Za-z0-9_-]{1,200}";
const GET_ROUTES = [
  // P8 V6-1: the composer reads the runtime dispatch gate off the daemon's
  // health payload; a route the proxy does not forward is a 404 the client
  // reads as "unknown", and the copy the gate decides never appears.
  /^health$/,
  /^workspaces$/,
  new RegExp(`^workspaces\\/${ID}$`),
  /^threads$/,
  new RegExp(`^threads\\/${ID}$`),
  new RegExp(`^threads\\/${ID}\\/messages$`),
  new RegExp(`^threads\\/${ID}\\/runs$`),
  new RegExp(`^runs\\/${ID}$`),
  new RegExp(`^runs\\/${ID}\\/events$`),
  new RegExp(`^runs\\/${ID}\\/research-workflow$`),
  new RegExp(`^artifact-versions\\/${ID}\\/content$`),
  /^research-items$/,
  /^research-items\/ri_[a-f0-9]{32}$/,
  /^research-documents\/rdv_[a-f0-9]{32}\/content$/,
  /^sources$/,
  /^sources\/search$/,
  new RegExp(`^sources\\/${ID}\\/content$`),
  new RegExp(`^sources\\/${ID}$`),
  /^events$/,
  /^decisions$/,
  /^captures$/,
  new RegExp(`^captures\\/${ID}$`),
];

const CAPTURE_STATES = [
  "pending",
  "approved",
  "claimed",
  "uncertain",
  "consumed",
  "dismissed",
  "failed",
];

const POST_ROUTES = [
  /^research-items\/ri_[a-f0-9]{32}\/thread$/,
  /^workspaces$/,
  new RegExp(`^workspaces\\/${ID}\\/threads$`),
  new RegExp(`^workspaces\\/${ID}\\/rename$`),
  new RegExp(`^threads\\/${ID}\\/(?:rename|archive|unarchive)$`),
  new RegExp(`^threads\\/${ID}\\/messages$`),
  new RegExp(`^threads\\/${ID}\\/runs$`),
  new RegExp(`^runs\\/${ID}\\/(?:pause|resume|cancel|retry)$`),
  new RegExp(`^source-intents\\/${ID}\\/resolve$`),
  new RegExp(`^decisions\\/${ID}\\/resolve$`),
  /^captures$/,
  new RegExp(`^captures\\/${ID}\\/(?:approve|dismiss|reopen)$`),
];

type RouteContext = { params: Promise<{ path: string[] }> };

function problem(status: number, category: string, title: string) {
  return NextResponse.json(
    {
      type: `urn:cortex:web-problem:${category}`,
      title,
      status,
      category,
      retryable: status >= 500,
      owner: "cortex-web",
    },
    { status, headers: { "Cache-Control": "no-store", "Content-Type": "application/problem+json" } },
  );
}

function upstreamConfiguration(): { base: URL; token: string } | null {
  const rawBase = process.env.CORTEX_CONTROL_API_URL;
  const token = process.env.CORTEX_CONTROL_TOKEN;
  if (!rawBase || !token || token.length < 32) return null;
  let base: URL;
  try {
    base = new URL(rawBase);
  } catch {
    return null;
  }
  if (
    base.protocol !== "http:" ||
    base.hostname !== "127.0.0.1" ||
    base.username ||
    base.password ||
    base.search ||
    base.hash ||
    (base.pathname !== "/" && base.pathname !== "")
  ) return null;
  return { base, token };
}

function validQuery(path: string, searchParams: URLSearchParams): boolean {
  const keys = [...searchParams.keys()];
  const only = (...allowed: string[]) => keys.every((key) => allowed.includes(key));
  const once = (name: string) => searchParams.getAll(name).length === 1;
  const boundedLimit = (maximum: number) => !searchParams.has("limit") ||
    (once("limit") && /^[1-9][0-9]*$/.test(searchParams.get("limit") ?? "") && Number(searchParams.get("limit")) <= maximum);
  if (path === "research-items") {
    const offset = searchParams.get("offset");
    return only("kind", "status", "limit", "offset") && boundedLimit(200) &&
      (!searchParams.has("kind") || (once("kind") && ["idea", "exploration", "project"].includes(searchParams.get("kind") ?? ""))) &&
      (!searchParams.has("status") || (once("status") && /^[a-z_]{1,40}$/.test(searchParams.get("status") ?? ""))) &&
      (!searchParams.has("offset") || (once("offset") && /^(0|[1-9][0-9]*)$/.test(offset ?? "") && Number.isSafeInteger(Number(offset))));
  }
  if (path === "sources/search") {
    const q = searchParams.get("q")?.trim() ?? "";
    return only("q", "limit") && once("q") && [...q].length > 0 && new TextEncoder().encode(q).length <= 1_024 &&
      !/[\u0000-\u001f]/.test(q) && boundedLimit(50);
  }
  if (new RegExp(`^sources\\/${ID}\\/content$`).test(path)) {
    return only("kind", "cursor", "limit") && boundedLimit(20_000) &&
      (!searchParams.has("kind") || (once("kind") && ["notes", "full_text", "grounding"].includes(searchParams.get("kind") ?? ""))) &&
      (!searchParams.has("cursor") || (once("cursor") && /^[A-Za-z0-9_-]{1,512}$/.test(searchParams.get("cursor") ?? "")));
  }
  if (path === "threads") {
    const workspaceId = searchParams.get("workspace_id");
    const include = searchParams.get("include_archived");
    return only("workspace_id", "include_archived") && once("workspace_id") && Boolean(workspaceId?.match(new RegExp(`^${ID}$`))) &&
      (!searchParams.has("include_archived") || (once("include_archived") && (include === "true" || include === "false")));
  }
  if (new RegExp(`^threads\\/${ID}\\/runs$`).test(path)) {
    const afterId = searchParams.get("after_id");
    const limit = searchParams.get("limit");
    return only("after_id", "limit") &&
      (!searchParams.has("after_id") || (once("after_id") && Boolean(afterId?.match(new RegExp(`^${ID}$`))))) &&
      (!searchParams.has("limit") || (once("limit") && Boolean(limit?.match(/^(?:[1-9]|[1-9][0-9]|100)$/))));
  }
  if (path === "events") {
    const cursor = searchParams.get("after_cursor");
    const limit = searchParams.get("limit");
    return only("after_cursor", "limit") &&
      (!searchParams.has("after_cursor") || (once("after_cursor") && Boolean(cursor?.match(/^[A-Za-z0-9_-]{1,512}$/)))) &&
      (!searchParams.has("limit") || (once("limit") && Boolean(limit?.match(/^\d{1,4}$/))));
  }
  if (path === "captures") {
    const state = searchParams.get("state");
    return only("state") &&
      (!searchParams.has("state") || (once("state") && CAPTURE_STATES.includes(state ?? "")));
  }
  if (path === "decisions") {
    const state = searchParams.get("state");
    return only("state") && once("state") && ["pending", "resolved", "expired"].includes(state ?? "");
  }
  if (/^runs\/[A-Za-z0-9_-]+\/events$/.test(path)) {
    const sequence = searchParams.get("after_sequence");
    return only("after_sequence") && (!searchParams.has("after_sequence") || (once("after_sequence") && Boolean(sequence?.match(/^\d+$/))));
  }
  return keys.length === 0;
}

function exactObjectBody(body: string, names: string[]): Record<string, unknown> | null {
  let decoded: unknown;
  try {
    decoded = JSON.parse(body);
  } catch {
    return null;
  }
  if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) return null;
  const keys = Object.keys(decoded);
  if (keys.length !== names.length || !names.every((name) => keys.includes(name))) return null;
  return decoded as Record<string, unknown>;
}

function hasExactResolveBody(body: string): boolean {
  return exactObjectBody(body, ["choice", "expected_revision"]) !== null;
}

// A capture stages raw operator text, so the edge pins the submission shape as
// hard as it pins a resolve: exactly two string fields and the same payload
// bound Control enforces, checked before anything reaches the daemon. The
// bound counts code points, because that is what contract R-3, the store's
// `_required_text` and SQLite's `length()` all count -- measuring bytes here
// would refuse a legal payload the moment it stopped being ASCII, and
// measuring `String.length` would refuse it the moment it left the BMP. The
// separate memory ceiling is the 1 MiB body cap enforced below.
function hasExactCaptureBody(body: string): boolean {
  const decoded = exactObjectBody(body, ["payload", "note"]);
  if (!decoded) return false;
  return typeof decoded.payload === "string" &&
    typeof decoded.note === "string" &&
    [...decoded.payload].length <= 16_384;
}

function hasExactRevisionBody(body: string): boolean {
  return exactObjectBody(body, ["expected_revision"]) !== null;
}

// `acknowledged` must be literally true: it is part of the canonical request
// hash upstream, so a replay cannot re-open an uncertain capture silently.
function hasExactReopenBody(body: string): boolean {
  const decoded = exactObjectBody(body, ["expected_revision", "acknowledged"]);
  return decoded !== null && decoded.acknowledged === true;
}

// A rename carries exactly the new title and the revision it is replacing, so
// an extra field never reaches Control through the Web door. Archive and
// unarchive carry the revision alone, which `hasExactRevisionBody` already
// pins for the capture commands.
function hasExactRenameBody(body: string): boolean {
  const decoded = exactObjectBody(body, ["title", "expected_revision"]);
  return decoded !== null && typeof decoded.title === "string";
}

function hasExactResearchThreadBody(body: string): boolean {
  const value = exactObjectBody(body, ["workspace_id", "expected_revision"]);
  return value !== null && typeof value.workspace_id === "string" &&
    new RegExp(`^${ID}$`).test(value.workspace_id) &&
    typeof value.expected_revision === "number" && Number.isSafeInteger(value.expected_revision) && value.expected_revision >= 0;
}

const EXACT_BODY_ROUTES: Array<[RegExp, (body: string) => boolean, string]> = [
  [/^research-items\/ri_[a-f0-9]{32}\/thread$/, hasExactResearchThreadBody, "The research conversation body is invalid"],
  [new RegExp(`^source-intents\\/${ID}\\/resolve$`), hasExactResolveBody, "The resolve command body is invalid"],
  [new RegExp(`^decisions\\/${ID}\\/resolve$`), hasExactResolveBody, "The resolve command body is invalid"],
  [/^captures$/, hasExactCaptureBody, "The capture command body is invalid"],
  [new RegExp(`^captures\\/${ID}\\/(?:approve|dismiss)$`), hasExactRevisionBody, "The capture command body is invalid"],
  [new RegExp(`^captures\\/${ID}\\/reopen$`), hasExactReopenBody, "The capture command body is invalid"],
  [new RegExp(`^workspaces\\/${ID}\\/rename$`), hasExactRenameBody, "The rename command body is invalid"],
  [new RegExp(`^threads\\/${ID}\\/rename$`), hasExactRenameBody, "The rename command body is invalid"],
  [new RegExp(`^threads\\/${ID}\\/(?:archive|unarchive)$`), hasExactRevisionBody, "The archive command body is invalid"],
];

async function proxy(request: NextRequest, context: RouteContext, method: "GET" | "POST") {
  const accessBoundary = verifyAccessBoundary(request, method);
  if (!accessBoundary.allowed) {
    const status = accessBoundary.category === "access_boundary_unconfigured" ? 503 : 403;
    return problem(
      status,
      accessBoundary.category,
      status === 503
        ? "The private access boundary is not configured"
        : "The private access boundary rejected the request",
    );
  }
  const segments = (await context.params).path;
  const canonicalPath = segments.join("/");
  const allowlist = method === "GET" ? GET_ROUTES : POST_ROUTES;
  const incoming = new URL(request.url);
  if (
    !segments.length ||
    !allowlist.some((pattern) => pattern.test(canonicalPath)) ||
    (method === "GET" ? !validQuery(canonicalPath, incoming.searchParams) : Boolean(incoming.search))
  ) {
    return problem(404, "not_found", "The Cortex route is not available through the Web client");
  }
  const configuration = upstreamConfiguration();
  if (!configuration) {
    return problem(503, "control_gateway_unconfigured", "The local Cortex Control gateway is not configured");
  }

  const upstream = new URL(`/api/v1/${canonicalPath}${incoming.search}`, configuration.base);
  const headers = new Headers({
    Accept: "application/json",
    "X-Cortex-Control-Token": configuration.token,
  });
  // ⟦P8-08⟧ The upstream headers are built from nothing, so no caller header
  // is ever forwarded. The identity travels only when this adapter verified
  // it against the Cloudflare Access assertion; the daemon records the turn
  // as that person rather than as the machine's local operator.
  if (accessBoundary.identity) {
    headers.set("X-Cortex-Access-Identity", accessBoundary.identity);
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);
  if (method === "POST") headers.set("Content-Type", "application/json");

  let body: string | undefined;
  if (method === "POST") {
    const declaredLength = Number(request.headers.get("content-length") ?? "0");
    if (!Number.isFinite(declaredLength) || declaredLength < 0 || declaredLength > 1_048_576) {
      return problem(400, "invalid_request", "The command body is invalid or exceeds 1 MiB");
    }
    body = await request.text();
    if (!body || new TextEncoder().encode(body).byteLength > 1_048_576) {
      return problem(400, "invalid_request", "The command body is required and must not exceed 1 MiB");
    }
    const exactBody = EXACT_BODY_ROUTES.find(([pattern]) => pattern.test(canonicalPath));
    if (exactBody && !exactBody[1](body)) {
      return problem(400, "invalid_request", exactBody[2]);
    }
  }

  let response: Response;
  try {
    response = await fetch(upstream, {
      method,
      headers,
      body,
      cache: "no-store",
      redirect: "error",
    });
  } catch {
    return problem(503, "control_gateway_unavailable", "The local Cortex Control gateway is unavailable");
  }

  const responseHeaders = new Headers({
    "Cache-Control": "no-store",
    "Content-Type": response.headers.get("content-type") ?? "application/json",
  });
  const replayed = response.headers.get("idempotency-replayed");
  if (replayed === "true") responseHeaders.set("Idempotency-Replayed", "true");
  return new NextResponse(await response.arrayBuffer(), {
    status: response.status,
    headers: responseHeaders,
  });
}

export function GET(request: NextRequest, context: RouteContext) {
  return proxy(request, context, "GET");
}

export function POST(request: NextRequest, context: RouteContext) {
  return proxy(request, context, "POST");
}
