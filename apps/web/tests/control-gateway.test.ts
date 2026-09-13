import { createHash, createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { GET, POST } from "../app/api/cortex/[...path]/route";
import { GET as GET_ACCESS_HEALTH } from "../app/api/cortex/access-boundary/health/route";

const TOKEN = "server-only-control-token-value-000000000000";
const BOOTSTRAP_SECRET = "test-only-private-access-secret-value";
const BOOTSTRAP_TOKEN = createHmac("sha256", BOOTSTRAP_SECRET)
  .update("cortex-private-access-bootstrap-v1", "ascii")
  .digest("base64url");
const PUBLIC_ORIGIN = "https://cortex.owner.ts.net";
const LOCAL_ORIGIN = "http://127.0.0.1:3000";

function configureAccessBoundary(publicOrigin = PUBLIC_ORIGIN) {
  process.env.CORTEX_PUBLIC_ORIGIN = publicOrigin;
  process.env.CORTEX_ACCESS_BOOTSTRAP_TOKEN = BOOTSTRAP_TOKEN;
}

function configureLocalAccessBoundary(localOrigin = LOCAL_ORIGIN) {
  process.env.CORTEX_LOCAL_ORIGIN = localOrigin;
  process.env.CORTEX_ACCESS_BOOTSTRAP_TOKEN = BOOTSTRAP_TOKEN;
}

const ACCESS_IDENTITY = "operator@example.test";

function boundaryHeaders(origin = PUBLIC_ORIGIN): Record<string, string> {
  const parsed = new URL(origin);
  return {
    Host: parsed.host,
    "Sec-Fetch-Site": "same-origin",
    "X-Cortex-Access-Bootstrap": BOOTSTRAP_TOKEN,
    "X-Forwarded-Host": parsed.host,
    "X-Forwarded-Proto": parsed.protocol.slice(0, -1),
    // ⟦P7⟧ The public door is opened by the identity the adapter verified and
    // set; the local door has no such header.
    ...(parsed.protocol === "https:" ? { "X-Cortex-Access-Identity": ACCESS_IDENTITY } : {}),
  };
}

function localBoundaryHeaders(): Record<string, string> {
  return {
    ...boundaryHeaders(LOCAL_ORIGIN),
    "X-Forwarded-For": "127.0.0.1",
    "X-Forwarded-Port": "3000",
  };
}

function configureControlGateway() {
  configureAccessBoundary();
  process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
  process.env.CORTEX_CONTROL_TOKEN = TOKEN;
}

function mutationHeaders(): Record<string, string> {
  return {
    ...boundaryHeaders(),
    Origin: PUBLIC_ORIGIN,
    "X-Cortex-Web-Client": "v1",
    "Idempotency-Key": "web-research-command-0001",
    "Content-Type": "application/json",
  };
}

afterEach(() => {
  delete process.env.CORTEX_CONTROL_API_URL;
  delete process.env.CORTEX_CONTROL_TOKEN;
  delete process.env.CORTEX_LOCAL_ORIGIN;
  delete process.env.CORTEX_PUBLIC_ORIGIN;
  delete process.env.CORTEX_ACCESS_BOOTSTRAP_TOKEN;
  delete process.env.CORTEX_DEV_ORIGIN;
  vi.unstubAllGlobals();
});

describe("research catalog gateway", () => {
  it("forwards bounded catalog and exact document reads through the authenticated boundary", async () => {
    configureControlGateway();
    const upstream = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", upstream);
    const item = `ri_${"a".repeat(32)}`;
    const document = `rdv_${"b".repeat(32)}`;
    for (const [parts, query] of [
      [["research-items"], "?kind=project&status=monitored&limit=100&offset=0"],
      [["research-items", item], ""],
      [["research-documents", document, "content"], ""],
    ] as const) {
      const response = await GET(new NextRequest(`${PUBLIC_ORIGIN}/api/cortex/${parts.join("/")}${query}`, {
        headers: boundaryHeaders(),
      }), { params: Promise.resolve({ path: [...parts] }) });
      expect(response.status).toBe(200);
    }
    expect(upstream).toHaveBeenCalledTimes(3);
    upstream.mockClear();
    for (const query of ["?limit=201", "?offset=-1", "?offset=0&offset=1", "?file=/etc/passwd", "?kind=other"]) {
      const response = await GET(new NextRequest(`${PUBLIC_ORIGIN}/api/cortex/research-items${query}`, {
        headers: boundaryHeaders(),
      }), { params: Promise.resolve({ path: ["research-items"] }) });
      expect(response.status).toBe(404);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("admits only the research-thread command fields", async () => {
    configureControlGateway();
    const upstream = vi.fn(async () => new Response("{}", { status: 201 }));
    vi.stubGlobal("fetch", upstream);
    const item = `ri_${"a".repeat(32)}`;
    const context = { params: Promise.resolve({ path: ["research-items", item, "thread"] }) };
    for (const body of [
      { workspace_id: "ws_1", expected_revision: 0 },
      { workspace_id: "ws_1", expected_revision: 0, start_run: true },
    ]) {
      const response = await POST(new NextRequest(`${PUBLIC_ORIGIN}/api/cortex/research-items/${item}/thread`, {
        method: "POST", headers: mutationHeaders(), body: JSON.stringify(body),
      }), context);
      expect(response.status).toBe("start_run" in body ? 400 : 201);
    }
    expect(upstream).toHaveBeenCalledOnce();
  });
});

describe("server-only Control gateway", () => {
  it("allows an exact authenticated local boundary to reach Control", async () => {
    configureLocalAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async () => new Response(
      JSON.stringify({ items: [], next_cursor: null }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: localBoundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["workspaces"] }) },
    );

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it("rejects incomplete or cross-site local boundary evidence before Control", async () => {
    configureLocalAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };
    const missingProof = Object.fromEntries(
      Object.entries(localBoundaryHeaders())
        .filter(([name]) => name !== "X-Cortex-Access-Bootstrap"),
    );

    for (const headers of [
      missingProof,
      Object.fromEntries(
        Object.entries(localBoundaryHeaders())
          .filter(([name]) => name !== "X-Forwarded-For"),
      ),
      Object.fromEntries(
        Object.entries(localBoundaryHeaders())
          .filter(([name]) => name !== "X-Forwarded-Port"),
      ),
      { ...localBoundaryHeaders(), "X-Forwarded-Port": "3001" },
      { ...localBoundaryHeaders(), "Sec-Fetch-Site": "cross-site" },
    ]) {
      const response = await GET(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", { headers }),
        context,
      );
      expect(response.status).toBe(403);
      await expect(response.json()).resolves.toMatchObject({
        category: "access_boundary_rejected",
      });
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects local mutations with a mismatched Origin before Control", async () => {
    configureLocalAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        method: "POST",
        headers: {
          ...localBoundaryHeaders(),
          Origin: "http://127.0.0.1:3001",
          "X-Cortex-Web-Client": "v1",
        },
        body: JSON.stringify({ title: "Research" }),
      }),
      { params: Promise.resolve({ path: ["workspaces"] }) },
    );

    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects ambiguous or malformed local origin configuration", async () => {
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const invalidOrigins = [
      "http://localhost:3000",
      "http://127.0.0.1",
      "http://127.0.0.1:0",
      "http://127.0.0.1:65536",
      "http://127.0.0.1:not-a-port",
      "https://127.0.0.1:3000",
      "http://user:password@127.0.0.1:3000",
      "http://127.0.0.1:3000/path",
    ];

    for (const origin of invalidOrigins) {
      configureLocalAccessBoundary(origin);
      const response = await GET(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
          headers: localBoundaryHeaders(),
        }),
        { params: Promise.resolve({ path: ["workspaces"] }) },
      );
      expect(response.status).toBe(503);
      await expect(response.json()).resolves.toMatchObject({
        category: "access_boundary_unconfigured",
      });
    }

    expect(upstream).not.toHaveBeenCalled();
  });

  it("serves two doors from one configuration and opens the public one only to a verified identity", async () => {
    // ⟦P7⟧ The local door of before and the public door `[web]` names are
    // configured together; which one a request came through is decided by
    // its exact `Host`, and the public one additionally demands the identity
    // header only the adapter can set.
    configureLocalAccessBoundary();
    process.env.CORTEX_PUBLIC_ORIGIN = "https://cortex.example.test";
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async () => new Response(
      JSON.stringify({ items: [], next_cursor: null }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", upstream);
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };

    const local = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: localBoundaryHeaders(),
      }),
      context,
    );
    expect(local.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(1);

    const publicHeaders = boundaryHeaders("https://cortex.example.test");
    const identified = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: { ...publicHeaders, "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Port": "443" },
      }),
      context,
    );
    expect(identified.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(2);

    const rejected: Record<string, string>[] = [
      Object.fromEntries(Object.entries(publicHeaders).filter(([name]) => name !== "X-Cortex-Access-Identity")),
      { ...publicHeaders, "X-Cortex-Access-Identity": "" },
      { ...publicHeaders, "X-Cortex-Access-Identity": "not-an-email" },
      { ...publicHeaders, "X-Cortex-Access-Identity": "a@b.test, c@d.test" },
      { ...publicHeaders, "X-Forwarded-Proto": "http" },
      { ...publicHeaders, Host: "127.0.0.1:3000", "X-Forwarded-Host": "127.0.0.1:3000" },
      { ...publicHeaders, Origin: LOCAL_ORIGIN },
    ];
    for (const headers of rejected) {
      const response = await GET(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", { headers }),
        context,
      );
      expect(response.status).toBe(403);
      await expect(response.json()).resolves.toMatchObject({ category: "access_boundary_rejected" });
    }
    expect(upstream).toHaveBeenCalledTimes(2);
  });

  it("accepts public mutations whose Origin and Referer are the public origin and no other", async () => {
    configureLocalAccessBoundary();
    process.env.CORTEX_PUBLIC_ORIGIN = "https://cortex.example.test";
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async () => new Response(
      JSON.stringify({ id: "ws_1", title: "Research", revision: 0, created_at: "now", updated_at: "now" }),
      { status: 201 },
    ));
    vi.stubGlobal("fetch", upstream);
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };
    const body = JSON.stringify({ title: "Research" });
    const base = {
      ...boundaryHeaders("https://cortex.example.test"),
      "Content-Type": "application/json",
      "Idempotency-Key": "web-command-00000007",
      Origin: "https://cortex.example.test",
      Referer: "https://cortex.example.test/workspaces",
      "X-Cortex-Web-Client": "v1",
    };
    const accepted = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", { body, headers: base, method: "POST" }),
      context,
    );
    expect(accepted.status).toBe(201);
    expect(upstream).toHaveBeenCalledTimes(1);

    const rejected: Record<string, string>[] = [
      { ...base, Origin: LOCAL_ORIGIN },
      { ...base, Origin: "https://evil.example.test" },
      { ...base, Referer: "https://evil.example.test/" },
      { ...base, Referer: "not a url" },
      Object.fromEntries(Object.entries(base).filter(([name]) => name !== "X-Cortex-Access-Identity")),
    ];
    for (const headers of rejected) {
      const response = await POST(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", { body, headers, method: "POST" }),
        context,
      );
      expect(response.status).toBe(403);
      await expect(response.json()).resolves.toMatchObject({ category: "access_boundary_rejected" });
    }
    expect(upstream).toHaveBeenCalledTimes(1);
  });

  it("fails closed when access or loopback configuration is absent or invalid", async () => {
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };
    const missing = await GET(new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces"), context);
    expect(missing.status).toBe(503);
    await expect(missing.json()).resolves.toMatchObject({ category: "access_boundary_unconfigured" });

    configureAccessBoundary();
    const missingControl = await GET(new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
      headers: boundaryHeaders(),
    }), context);
    expect(missingControl.status).toBe(503);
    await expect(missingControl.json()).resolves.toMatchObject({ category: "control_gateway_unconfigured" });

    process.env.CORTEX_CONTROL_API_URL = "https://public.example";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const publicTarget = await GET(new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
      headers: boundaryHeaders(),
    }), context);
    expect(publicTarget.status).toBe(503);
  });

  it("keeps the token server-side and forwards only allowlisted routes", async () => {
    configureAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("X-Cortex-Control-Token")).toBe(TOKEN);
      expect(new Headers(init?.headers).get("Origin")).toBeNull();
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads?workspace_id=ws_1", {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["threads"] }) },
    );
    expect(response.status).toBe(200);
    expect(await response.text()).not.toContain(TOKEN);
    expect(upstream).toHaveBeenCalledOnce();
    expect(String(upstream.mock.calls[0][0])).toBe("http://127.0.0.1:8799/api/v1/threads?workspace_id=ws_1");

    const denied = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/internal/secrets", {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["internal", "secrets"] }) },
    );
    expect(denied.status).toBe(404);
    expect(upstream).toHaveBeenCalledOnce();

    const injectedQuery = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads?workspace_id=ws_1&target=http://evil.test", {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["threads"] }) },
    );
    expect(injectedQuery.status).toBe(404);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it("forwards the health probe the composer reads the dispatch gate from", async () => {
    // P8 V6-1: `getRuntimeDispatchGate()` reads `/api/cortex/health` through
    // this proxy. A route missing from GET_ROUTES is a 404 the client reads
    // as "unknown", so this drives the real handler and its allowlist.
    configureControlGateway();
    const upstream = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () =>
      new Response(JSON.stringify({ api_version: "v1", runtime_dispatch_enabled: false, status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/health", { headers: boundaryHeaders() }),
      { params: Promise.resolve({ path: ["health"] }) },
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ runtime_dispatch_enabled: false });
    expect(upstream).toHaveBeenCalledOnce();
    expect(String(upstream.mock.calls[0][0])).toBe("http://127.0.0.1:8799/api/v1/health");

    const withQuery = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/health?verbose=1", { headers: boundaryHeaders() }),
      { params: Promise.resolve({ path: ["health"] }) },
    );
    expect(withQuery.status).toBe(404);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it.each([
    {
      label: "run history",
      path: ["threads", "thread_1", "runs"],
      query: "?after_id=run_1&limit=25",
      upstream: "http://127.0.0.1:8799/api/v1/threads/thread_1/runs?after_id=run_1&limit=25",
    },
    {
      label: "research workflow",
      path: ["runs", "run_1", "research-workflow"],
      query: "",
      upstream: "http://127.0.0.1:8799/api/v1/runs/run_1/research-workflow",
    },
    {
      label: "artifact content",
      path: ["artifact-versions", "artifact_version_1", "content"],
      query: "",
      upstream: "http://127.0.0.1:8799/api/v1/artifact-versions/artifact_version_1/content",
    },
    {
      label: "source search",
      path: ["sources", "search"],
      query: "?q=memory&limit=10",
      upstream: "http://127.0.0.1:8799/api/v1/sources/search?q=memory&limit=10",
    },
    {
      label: "source content",
      path: ["sources", "source_1", "content"],
      query: "?kind=grounding&cursor=next&limit=20000",
      upstream: "http://127.0.0.1:8799/api/v1/sources/source_1/content?kind=grounding&cursor=next&limit=20000",
    },
    {
      label: "sources list",
      path: ["sources"],
      query: "",
      upstream: "http://127.0.0.1:8799/api/v1/sources",
    },
    {
      label: "source detail",
      path: ["sources", "source_1"],
      query: "",
      upstream: "http://127.0.0.1:8799/api/v1/sources/source_1",
    },
    {
      label: "capture inbox",
      path: ["captures"],
      query: "",
      upstream: "http://127.0.0.1:8799/api/v1/captures",
    },
    {
      label: "filtered capture inbox",
      path: ["captures"],
      query: "?state=pending",
      upstream: "http://127.0.0.1:8799/api/v1/captures?state=pending",
    },
    {
      label: "capture detail",
      path: ["captures", "capture_1"],
      query: "",
      upstream: "http://127.0.0.1:8799/api/v1/captures/capture_1",
    },
  ])("forwards the exact $label read route", async ({ path, query, upstream: expectedUpstream }) => {
    configureControlGateway();
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("X-Cortex-Control-Token")).toBe(TOKEN);
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${path.join("/")}${query}`, {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(String(upstream.mock.calls[0][0])).toBe(expectedUpstream);
  });

  it.each([
    { path: ["source-intents", "intent_1", "resolve"], upstreamPath: "source-intents/intent_1/resolve" },
    { path: ["decisions", "decision_1", "resolve"], upstreamPath: "decisions/decision_1/resolve" },
  ])("forwards only an exact resolve body to $upstreamPath", async ({ path, upstreamPath }) => {
    configureControlGateway();
    const body = '{"choice":"keep_both","expected_revision":3}';
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.body).toBe(body);
      expect(new Headers(init?.headers).get("X-Cortex-Control-Token")).toBe(TOKEN);
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${path.join("/")}`, {
        method: "POST",
        headers: mutationHeaders(),
        body,
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(200);
    expect(String(upstream.mock.calls[0][0])).toBe(`http://127.0.0.1:8799/api/v1/${upstreamPath}`);
  });

  it.each([
    ["missing choice", '{"expected_revision":3}'],
    ["missing revision", '{"choice":"keep_both"}'],
    ["extra key", '{"choice":"keep_both","expected_revision":3,"target":"http://evil.test"}'],
    ["array", '["choice","expected_revision"]'],
    ["null", "null"],
    ["malformed JSON", "{"],
  ])("rejects a resolve body with %s before upstream fetch", async (_label, body) => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const path = ["source-intents", "intent_1", "resolve"];

    const response = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/source-intents/intent_1/resolve", {
        method: "POST",
        headers: mutationHeaders(),
        body,
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ category: "invalid_request" });
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([
    ["missing choice", '{"expected_revision":3}'],
    ["extra key", '{"choice":"create_successor","expected_revision":3,"run_id":"run_1"}'],
  ])("rejects a Decision resolve body with %s before upstream fetch", async (_label, body) => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const path = ["decisions", "decision_1", "resolve"];

    const response = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/decisions/decision_1/resolve", {
        method: "POST",
        headers: mutationHeaders(),
        body,
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ category: "invalid_request" });
    expect(upstream).not.toHaveBeenCalled();
  });

  it("forwards every capture state the enum allows and no other", async () => {
    configureControlGateway();
    const forwarded: string[] = [];
    const upstream = vi.fn(async (input: RequestInfo | URL) => {
      forwarded.push(String(input));
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    for (const state of ["pending", "approved", "claimed", "uncertain", "consumed", "dismissed", "failed"]) {
      const response = await GET(
        new NextRequest(`http://127.0.0.1:3000/api/cortex/captures?state=${state}`, {
          headers: boundaryHeaders(),
        }),
        { params: Promise.resolve({ path: ["captures"] }) },
      );
      expect(response.status).toBe(200);
    }

    expect(forwarded).toEqual([
      "pending",
      "approved",
      "claimed",
      "uncertain",
      "consumed",
      "dismissed",
      "failed",
    ].map((state) => `http://127.0.0.1:8799/api/v1/captures?state=${state}`));
  });

  it.each([
    {
      label: "capture creation",
      path: ["captures"],
      body: '{"payload":"https://example.com/post","note":"read later"}',
      upstreamPath: "captures",
    },
    {
      label: "a multi-line capture at the payload bound",
      path: ["captures"],
      body: JSON.stringify({ payload: `first line\n${"a".repeat(16_372)}`, note: "" }),
      upstreamPath: "captures",
    },
    {
      label: "a 16,384-character multi-byte capture",
      path: ["captures"],
      body: JSON.stringify({ payload: "\u00e9".repeat(16_384), note: "" }),
      upstreamPath: "captures",
    },
    {
      // A surrogate pair is one code point and two UTF-16 units, so this row
      // also refuses a bound measured with `String.length`.
      label: "a 16,384-code-point astral capture",
      path: ["captures"],
      body: JSON.stringify({ payload: "\uD83D\uDE00".repeat(16_384), note: "" }),
      upstreamPath: "captures",
    },
    {
      label: "capture approval",
      path: ["captures", "capture_1", "approve"],
      body: '{"expected_revision":0}',
      upstreamPath: "captures/capture_1/approve",
    },
    {
      label: "capture dismissal",
      path: ["captures", "capture_1", "dismiss"],
      body: '{"expected_revision":1}',
      upstreamPath: "captures/capture_1/dismiss",
    },
    {
      label: "capture reopen",
      path: ["captures", "capture_1", "reopen"],
      body: '{"expected_revision":2,"acknowledged":true}',
      upstreamPath: "captures/capture_1/reopen",
    },
  ])("forwards only an exact $label body", async ({ path, body, upstreamPath }) => {
    configureControlGateway();
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.body).toBe(body);
      expect(new Headers(init?.headers).get("X-Cortex-Control-Token")).toBe(TOKEN);
      expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("web-research-command-0001");
      return new Response("{}", { status: 201, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${path.join("/")}`, {
        method: "POST",
        headers: mutationHeaders(),
        body,
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(201);
    expect(String(upstream.mock.calls[0][0])).toBe(`http://127.0.0.1:8799/api/v1/${upstreamPath}`);
  });

  it.each([
    ["creation with an extra key", ["captures"], '{"payload":"https://example.com","note":"","extra":true}'],
    ["creation without a note", ["captures"], '{"payload":"https://example.com"}'],
    ["creation without a payload", ["captures"], '{"note":""}'],
    ["creation with an empty body", ["captures"], "{}"],
    ["creation with a non-string payload", ["captures"], '{"payload":7,"note":""}'],
    ["creation with a null note", ["captures"], '{"payload":"https://example.com","note":null}'],
    [
      "creation above the payload bound",
      ["captures"],
      JSON.stringify({ payload: "a".repeat(16_385), note: "" }),
    ],
    [
      "creation one code point above the payload bound",
      ["captures"],
      JSON.stringify({ payload: "\uD83D\uDE00".repeat(16_385), note: "" }),
    ],
    ["creation as an array", ["captures"], '["payload","note"]'],
    ["creation as malformed JSON", ["captures"], "{"],
    ["approval with an extra key", ["captures", "capture_1", "approve"], '{"expected_revision":0,"acknowledged":true}'],
    ["approval without a revision", ["captures", "capture_1", "approve"], "{}"],
    ["approval as null", ["captures", "capture_1", "approve"], "null"],
    ["dismissal with an extra key", ["captures", "capture_1", "dismiss"], '{"expected_revision":0,"note":"stale"}'],
    ["dismissal without a revision", ["captures", "capture_1", "dismiss"], '{"acknowledged":true}'],
    ["reopen without an acknowledgement", ["captures", "capture_1", "reopen"], '{"expected_revision":0}'],
    ["reopen acknowledged false", ["captures", "capture_1", "reopen"], '{"expected_revision":0,"acknowledged":false}'],
    ["reopen acknowledged as a string", ["captures", "capture_1", "reopen"], '{"expected_revision":0,"acknowledged":"true"}'],
    ["reopen acknowledged as one", ["captures", "capture_1", "reopen"], '{"expected_revision":0,"acknowledged":1}'],
    [
      "reopen with an extra key",
      ["captures", "capture_1", "reopen"],
      '{"expected_revision":0,"acknowledged":true,"extra":true}',
    ],
  ])("rejects a capture body with %s before upstream fetch", async (_label, path, body) => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${(path as string[]).join("/")}`, {
        method: "POST",
        headers: mutationHeaders(),
        body: body as string,
      }),
      { params: Promise.resolve({ path: path as string[] }) },
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({ category: "invalid_request" });
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([
    ["capture creation query", "captures?state=pending", ["captures"]],
    ["capture approve query", "captures/capture_1/approve?state=pending", ["captures", "capture_1", "approve"]],
    ["capture detail POST", "captures/capture_1", ["captures", "capture_1"]],
  ])("rejects $0 as a POST route", async (_label, requestPath, path) => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${requestPath}`, {
        method: "POST",
        headers: mutationHeaders(),
        body: '{"expected_revision":0}',
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("verifies the access boundary before resolve-body inspection or upstream configuration", async () => {
    configureAccessBoundary();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/source-intents/intent_1/resolve", {
        method: "POST",
        headers: {
          ...mutationHeaders(),
          Origin: "https://evil.test",
        },
        body: "{",
      }),
      { params: Promise.resolve({ path: ["source-intents", "intent_1", "resolve"] }) },
    );

    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([
    ["duplicate after_id", "threads/thread_1/runs?after_id=run_1&after_id=run_2", ["threads", "thread_1", "runs"]],
    ["duplicate limit", "threads/thread_1/runs?limit=1&limit=2", ["threads", "thread_1", "runs"]],
    ["blank after_id", "threads/thread_1/runs?after_id=", ["threads", "thread_1", "runs"]],
    ["blank limit", "threads/thread_1/runs?limit=", ["threads", "thread_1", "runs"]],
    ["zero limit", "threads/thread_1/runs?limit=0", ["threads", "thread_1", "runs"]],
    ["oversized limit", "threads/thread_1/runs?limit=101", ["threads", "thread_1", "runs"]],
    ["non-canonical limit", "threads/thread_1/runs?limit=01", ["threads", "thread_1", "runs"]],
    ["unknown history query", "threads/thread_1/runs?state=completed", ["threads", "thread_1", "runs"]],
    ["target injection", "threads/thread_1/runs?target=http://evil.test", ["threads", "thread_1", "runs"]],
    ["workflow query", "runs/run_1/research-workflow?limit=1", ["runs", "run_1", "research-workflow"]],
    ["content query", "artifact-versions/artifact_version_1/content?limit=1", ["artifact-versions", "artifact_version_1", "content"]],
    ["alternate content collection", "artifacts/artifact_1/content", ["artifacts", "artifact_1", "content"]],
    ["extra content segment", "artifact-versions/artifact_version_1/content/raw", ["artifact-versions", "artifact_version_1", "content", "raw"]],
    ["encoded slash", "artifact-versions/artifact%2Fversion/content", ["artifact-versions", "artifact%2Fversion", "content"]],
    ["encoded backslash", "artifact-versions/artifact%5Cversion/content", ["artifact-versions", "artifact%5Cversion", "content"]],
    // The Control sources endpoints define no query parameters; the front
    // door stays fail-closed rather than inheriting Control's silent-ignore.
    ["missing search query", "sources/search", ["sources", "search"]],
    ["empty search query", "sources/search?q=%20", ["sources", "search"]],
    ["duplicate search query", "sources/search?q=a&q=b", ["sources", "search"]],
    ["search limit", "sources/search?q=memory&limit=51", ["sources", "search"]],
    ["search byte bound", `sources/search?q=${"中".repeat(342)}`, ["sources", "search"]],
    ["search unknown field", "sources/search?q=memory&path=/tmp/file", ["sources", "search"]],
    ["content kind", "sources/source_1/content?kind=pdf", ["sources", "source_1", "content"]],
    ["content duplicate kind", "sources/source_1/content?kind=notes&kind=full_text", ["sources", "source_1", "content"]],
    ["content cursor", "sources/source_1/content?cursor=/tmp/file", ["sources", "source_1", "content"]],
    ["content blank cursor", "sources/source_1/content?cursor=", ["sources", "source_1", "content"]],
    ["content oversized limit", "sources/source_1/content?limit=20001", ["sources", "source_1", "content"]],
    ["content unknown field", "sources/source_1/content?path=/tmp/file", ["sources", "source_1", "content"]],
    ["sources list query", "sources?limit=10", ["sources"]],
    ["source detail query", "sources/source_1?verbose=1", ["sources", "source_1"]],
    ["extra sources segment", "sources/source_1/aliases", ["sources", "source_1", "aliases"]],
    // The capture list takes at most one `state` out of the seven-value enum,
    // so an unknown, blank, repeated or accompanied parameter is refused here
    // rather than forwarded for Control to argue about.
    ["unknown capture query", "captures?limit=10", ["captures"]],
    ["capture query beside state", "captures?state=pending&limit=10", ["captures"]],
    ["unknown capture state", "captures?state=staged", ["captures"]],
    ["blank capture state", "captures?state=", ["captures"]],
    ["duplicate capture state", "captures?state=pending&state=approved", ["captures"]],
    ["capture detail query", "captures/capture_1?state=pending", ["captures", "capture_1"]],
    // The capture commands are POST-only; a method mismatch is a 404 here.
    ["capture approve through GET", "captures/capture_1/approve", ["captures", "capture_1", "approve"]],
    ["capture dismiss through GET", "captures/capture_1/dismiss", ["captures", "capture_1", "dismiss"]],
    ["capture reopen through GET", "captures/capture_1/reopen", ["captures", "capture_1", "reopen"]],
    ["extra capture segment", "captures/capture_1/aliases", ["captures", "capture_1", "aliases"]],
  ])("rejects $0 without reaching upstream", async (_label, requestPath, path) => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${requestPath}`, {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([
    ["content POST", "artifact-versions/artifact_version_1/content", ["artifact-versions", "artifact_version_1", "content"]],
    ["Source resolve query", "source-intents/intent_1/resolve?target=http://evil.test", ["source-intents", "intent_1", "resolve"]],
    ["Decision resolve query", "decisions/decision_1/resolve?limit=1", ["decisions", "decision_1", "resolve"]],
  ])("rejects $0 as a POST route", async (_label, requestPath, path) => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest(`http://127.0.0.1:3000/api/cortex/${requestPath}`, {
        method: "POST",
        headers: mutationHeaders(),
        body: '{"choice":"keep_both","expected_revision":3}',
      }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("does not expose the Source resolve path through GET", async () => {
    configureControlGateway();
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);

    const response = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/source-intents/intent_1/resolve", {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["source-intents", "intent_1", "resolve"] }) },
    );

    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("requires same-origin CSRF evidence for mutations and preserves idempotency", async () => {
    configureAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("web-command-00000001");
      return new Response(JSON.stringify({ id: "ws_1", title: "Research", revision: 0, created_at: "now", updated_at: "now" }), { status: 201 });
    });
    vi.stubGlobal("fetch", upstream);
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };

    const rejected = await POST(new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
      method: "POST",
      headers: {
        ...boundaryHeaders(),
        Origin: "https://evil.test",
        "X-Cortex-Web-Client": "v1",
      },
      body: JSON.stringify({ title: "Research" }),
    }), context);
    expect(rejected.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();

    const accepted = await POST(new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
      method: "POST",
      headers: {
        ...boundaryHeaders(),
        Origin: PUBLIC_ORIGIN,
        "X-Cortex-Web-Client": "v1",
        "Idempotency-Key": "web-command-00000001",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ title: "Research" }),
    }), context);
    expect(accepted.status).toBe(201);
    expect(upstream).toHaveBeenCalledOnce();

    const oversized = await POST(new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
      method: "POST",
      headers: {
        ...boundaryHeaders(),
        Origin: PUBLIC_ORIGIN,
        "X-Cortex-Web-Client": "v1",
        "Idempotency-Key": "web-command-00000002",
        "Content-Length": "1048577",
      },
      body: "{}",
    }), context);
    expect(oversized.status).toBe(400);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it("rejects DNS rebinding, bootstrap failures, and forged forwarding before upstream", async () => {
    configureAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };
    const attacks: Record<string, string>[] = [
      { ...boundaryHeaders(), Host: "attacker.example", "X-Forwarded-Host": "attacker.example" },
      { ...boundaryHeaders(), "X-Cortex-Access-Bootstrap": "A".repeat(43) },
      Object.fromEntries(Object.entries(boundaryHeaders()).filter(([name]) => name !== "X-Cortex-Access-Bootstrap")),
      { ...boundaryHeaders(), "X-Forwarded-Host": "attacker.example" },
      { ...boundaryHeaders(), "X-Forwarded-Proto": "http" },
      { ...boundaryHeaders(), Origin: "https://attacker.example" },
      { ...boundaryHeaders(), Forwarded: "for=127.0.0.1;host=cortex.owner.ts.net;proto=https" },
      { ...boundaryHeaders(), "X-Forwarded-For": "203.0.113.10" },
      { ...boundaryHeaders(), "X-Forwarded-Port": "443, 8443" },
      { ...boundaryHeaders(), "Sec-Fetch-Site": "cross-site" },
    ];

    for (const headers of attacks) {
      const response = await GET(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", { headers }),
        context,
      );
      expect(response.status).toBe(403);
      await expect(response.json()).resolves.toMatchObject({ category: "access_boundary_rejected" });
    }

    const duplicateHeaders = new Headers(boundaryHeaders());
    duplicateHeaders.append("X-Cortex-Access-Bootstrap", BOOTSTRAP_TOKEN);
    const duplicate = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: duplicateHeaders,
      }),
      context,
    );
    expect(duplicate.status).toBe(403);

    const duplicateHostHeaders = new Headers(boundaryHeaders());
    duplicateHostHeaders.append("Host", "attacker.example");
    const duplicateHost = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: duplicateHostHeaders,
      }),
      context,
    );
    expect(duplicateHost.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects malformed origin configuration instead of trusting the request Host", async () => {
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const invalidOrigins = [
      "*",
      "http://cortex.owner.ts.net",
      "https://cortex",
      "https://localhost",
      "https://127.0.0.1",
      "https://CORTEX.example.com",
      "https://cortex.example.com:8443",
      "https://user:password@cortex.owner.ts.net",
      "https://cortex.owner.ts.net/path",
      `${PUBLIC_ORIGIN},https://other.owner.ts.net`,
    ];
    for (const origin of invalidOrigins) {
      configureAccessBoundary(origin);
      const response = await GET(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
          headers: boundaryHeaders(),
        }),
        { params: Promise.resolve({ path: ["workspaces"] }) },
      );
      expect(response.status).toBe(503);
      await expect(response.json()).resolves.toMatchObject({ category: "access_boundary_unconfigured" });
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects malformed bootstrap configuration", async () => {
    configureAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const context = { params: Promise.resolve({ path: ["workspaces"] }) };

    for (const token of ["short", "A".repeat(44), `${"A".repeat(42)}+`]) {
      process.env.CORTEX_ACCESS_BOOTSTRAP_TOKEN = token;
      const response = await GET(
        new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
          headers: boundaryHeaders(),
        }),
        context,
      );
      expect(response.status).toBe(503);
    }

    expect(upstream).not.toHaveBeenCalled();
  });

  it("does not create a local-origin bypass from a development environment value", async () => {
    configureAccessBoundary();
    process.env.CORTEX_DEV_ORIGIN = LOCAL_ORIGIN;
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", upstream);
    const response = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: boundaryHeaders(LOCAL_ORIGIN),
      }),
      { params: Promise.resolve({ path: ["workspaces"] }) },
    );
    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("accepts only the unused loopback forwarding metadata added by Next", async () => {
    configureAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", upstream);
    const response = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/workspaces", {
        headers: {
          ...boundaryHeaders(),
          "X-Forwarded-For": "127.0.0.1",
          "X-Forwarded-Port": "3001",
        },
      }),
      { params: Promise.resolve({ path: ["workspaces"] }) },
    );
    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
  });

  // ⟦P8-08⟧ Before this, every turn through the public door was recorded by
  // the daemon as `local-operator`, because the API is loopback-only and the
  // adapter told it nothing about who had knocked.
  it("carries the verified Access identity upstream from the public door", async () => {
    configureControlGateway();
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(headers.get("X-Cortex-Access-Identity")).toBe(ACCESS_IDENTITY);
      expect(headers.get("X-Cortex-Control-Token")).toBe(TOKEN);
      return new Response("{}", { status: 201, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads/thread_1/runs", {
        method: "POST",
        headers: mutationHeaders(),
        body: '{"expected_revision":1}',
      }),
      { params: Promise.resolve({ path: ["threads", "thread_1", "runs"] }) },
    );

    expect(response.status).toBe(201);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it("sends no identity from the local door, even one the caller supplied", async () => {
    configureLocalAccessBoundary();
    process.env.CORTEX_CONTROL_API_URL = "http://127.0.0.1:8799";
    process.env.CORTEX_CONTROL_TOKEN = TOKEN;
    const upstream = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      // The local door has no assertion to verify, so it has nothing to say
      // about who acted; a header the caller invented is not evidence and is
      // never relayed. The upstream headers are built from nothing.
      expect(new Headers(init?.headers).has("X-Cortex-Access-Identity")).toBe(false);
      return new Response("{}", { status: 201, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", upstream);

    const response = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads/thread_1/runs", {
        method: "POST",
        headers: {
          ...localBoundaryHeaders(),
          Origin: LOCAL_ORIGIN,
          "X-Cortex-Web-Client": "v1",
          "X-Cortex-Access-Identity": "attacker@example.test",
          "Idempotency-Key": "web-research-command-0001",
          "Content-Type": "application/json",
        },
        body: '{"expected_revision":1}',
      }),
      { params: Promise.resolve({ path: ["threads", "thread_1", "runs"] }) },
    );

    expect(response.status).toBe(201);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it("forwards rename and archive actions and the include_archived flag", async () => {
    configureControlGateway();
    const upstream = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () => new Response(
      JSON.stringify({ items: [], next_cursor: null }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", upstream);

    const archive = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads/thread_1/archive", {
        body: JSON.stringify({ expected_revision: 2 }),
        headers: mutationHeaders(),
        method: "POST",
      }),
      { params: Promise.resolve({ path: ["threads", "thread_1", "archive"] }) },
    );
    expect(archive.status).toBe(200);
    expect(String(upstream.mock.calls[0][0])).toBe("http://127.0.0.1:8799/api/v1/threads/thread_1/archive");

    const included = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads?workspace_id=ws_1&include_archived=true", {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["threads"] }) },
    );
    expect(included.status).toBe(200);
    expect(String(upstream.mock.calls[1][0])).toBe(
      "http://127.0.0.1:8799/api/v1/threads?workspace_id=ws_1&include_archived=true",
    );

    const bogusFlag = await GET(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads?workspace_id=ws_1&include_archived=1", {
        headers: boundaryHeaders(),
      }),
      { params: Promise.resolve({ path: ["threads"] }) },
    );
    expect(bogusFlag.status).toBe(404);

    const unlisted = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads/thread_1/delete", {
        body: JSON.stringify({ expected_revision: 2 }),
        headers: mutationHeaders(),
        method: "POST",
      }),
      { params: Promise.resolve({ path: ["threads", "thread_1", "delete"] }) },
    );
    expect(unlisted.status).toBe(404);
    expect(upstream).toHaveBeenCalledTimes(2);
    const extraField = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads/thread_1/rename", {
        body: JSON.stringify({ title: "Sharper question", expected_revision: 2, force: true }),
        headers: mutationHeaders(),
        method: "POST",
      }),
      { params: Promise.resolve({ path: ["threads", "thread_1", "rename"] }) },
    );
    expect(extraField.status).toBe(400);
    await expect(extraField.json()).resolves.toMatchObject({ category: "invalid_request" });

    const rename = await POST(
      new NextRequest("http://127.0.0.1:3000/api/cortex/threads/thread_1/rename", {
        body: JSON.stringify({ title: "Sharper question", expected_revision: 2 }),
        headers: mutationHeaders(),
        method: "POST",
      }),
      { params: Promise.resolve({ path: ["threads", "thread_1", "rename"] }) },
    );
    expect(rename.status).toBe(200);
    expect(String(upstream.mock.calls[2][0])).toBe("http://127.0.0.1:8799/api/v1/threads/thread_1/rename");
    expect(upstream).toHaveBeenCalledTimes(3);
  });
});

describe("Web access boundary health", () => {
  it("returns a challenge-bound attestation without exposing reusable secrets", async () => {
    configureAccessBoundary();
    const challenge = "Q".repeat(43);
    const request = new NextRequest(
      "http://127.0.0.1:3000/api/cortex/access-boundary/health",
      {
        headers: {
          ...boundaryHeaders(),
          "Sec-Fetch-Site": "none",
          "X-Cortex-Access-Attestation-Challenge": challenge,
        },
      },
    );
    const response = await GET_ACCESS_HEALTH(request);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const payload = await response.json();
    const fingerprint = createHash("sha256")
      .update(PUBLIC_ORIGIN, "utf8")
      .digest("base64url");
    const expectedAttestation = createHmac("sha256", BOOTSTRAP_TOKEN)
      .update(["cortex-web-access-attestation-v1", challenge, fingerprint].join("\0"), "utf8")
      .digest("base64url");
    expect(payload).toEqual({
      attestation: expectedAttestation,
      challenge,
      origin_fingerprint: fingerprint,
      service: "cortex-web-access-boundary",
      status: "ok",
      version: 1,
      web_access_boundary_verified: true,
    });
    expect(JSON.stringify(payload)).not.toContain(BOOTSTRAP_TOKEN);
  });

  it("rejects missing or duplicate challenges without an attestation oracle", async () => {
    configureAccessBoundary();
    const baseHeaders = new Headers({
      ...boundaryHeaders(),
      "Sec-Fetch-Site": "none",
    });
    const missing = await GET_ACCESS_HEALTH(new NextRequest(
      "http://127.0.0.1:3000/api/cortex/access-boundary/health",
      { headers: baseHeaders },
    ));
    expect(missing.status).toBe(403);

    baseHeaders.append("X-Cortex-Access-Attestation-Challenge", "Q".repeat(43));
    baseHeaders.append("X-Cortex-Access-Attestation-Challenge", "R".repeat(43));
    const duplicate = await GET_ACCESS_HEALTH(new NextRequest(
      "http://127.0.0.1:3000/api/cortex/access-boundary/health",
      { headers: baseHeaders },
    ));
    expect(duplicate.status).toBe(403);
  });
});
