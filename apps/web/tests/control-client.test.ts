import { describe, expect, it, vi } from "vitest";
import { ContractDecodeError, decodeCapture, decodeRun, decodeThread, decodeWorkspace, type Capture } from "../app/control/contracts";
import { alreadyCapturedCurrent, ControlProblemError, CortexControlClient } from "../app/control/client";
import {
  decodeArtifactVersionContent,
  decodeResearchWorkflow,
  type SourceGateProjection,
} from "../app/control/research-contracts";
import {
  decodeResearchDocumentContent,
  decodeResearchItem,
  decodeResearchItemPage,
} from "../app/control/research-items-contracts";

const thread = {
  id: "thread_1",
  workspace_id: "ws_1",
  title: "Echo memory",
  status: "idle",
  active_run_id: null,
  archived_at: null,
  engine_owned: false,
  revision: 2,
  created_at: "2026-07-23T12:00:00Z",
  updated_at: "2026-07-23T12:01:00Z",
};

const now = "2026-07-23T12:00:00Z";
const pendingDecision = {
  id: "decision_1",
  run_id: "run_1",
  attempt_id: "attempt_1",
  kind: "source_conflict",
  prompt: "Keep both canonical sources?",
  options: [
    { id: "keep_both", label: "Keep both sources" },
    { id: "cancel", label: "Cancel" },
  ],
  state: "pending",
  resolution: null,
  revision: 0,
  created_at: now,
  resolved_at: null,
};
const sourceGate = {
  id: "source_intent_1",
  run_id: "run_1",
  attempt_id: "attempt_1",
  state: "pending",
  revision: 0,
  created_at: now,
  updated_at: now,
  title_observation: "Echo-Infinity",
  locator_observation: "https://arxiv.org/abs/2607.07675",
  candidates: [{
    id: "candidate_1",
    claim_kind: "title",
    canonical_id: "arxiv:2606.04527",
    official_title: "Echo-Infinity",
    source_kind: "paper",
    version: null as number | null,
  }],
  decision: pendingDecision,
};
const run = {
  id: "run_1",
  thread_id: thread.id,
  state: "waiting_for_decision",
  active_attempt_id: "attempt_1",
  stage: "source review",
  latest_sequence: 2,
  engine_owned: false,
  revision: 3,
  created_at: now,
  updated_at: now,
};
const workflow = {
  id: "workflow_1",
  definition_id: "golden_research",
  definition_version: 1,
  state: "waiting",
  current_stage_key: "await_source_decision",
  revision: 1,
  created_at: now,
  updated_at: now,
  stages: [{
    key: "await_source_decision",
    position: 0,
    effect: "decision",
    state: "waiting",
    revision: 1,
    attempt: 1,
    checkpoint_enabled: false,
    started_at: now,
    completed_at: null,
  }],
};
const g0Projection = {
  schema_version: 1,
  run,
  workflow,
  source_gates: [sourceGate],
  sources: [],
  lineage: { nodes: [], links: [], successor_node_id: null },
  decisions: [pendingDecision],
  artifacts: [],
  snapshots: [],
};

const generator = { name: "cortex", version: "1.0.0" };
const tool = { name: "research_writer", version: "1.0.0" };
const sha1 = "a".repeat(64);
const sha2 = "b".repeat(64);

function artifactVersion(
  id: string,
  logicalVersion: number,
  versionRunId: string,
  attemptId: string,
  sha256: string,
  parents: Array<{ artifact_version_id: string; sha256: string }>,
) {
  const committedAt = logicalVersion === 1 ? now : "2026-07-23T12:05:00Z";
  const sourceIds = ["source_echo", "source_lingbot"];
  return {
    id,
    artifact_id: "artifact_brief",
    logical_version: logicalVersion,
    resource_uri: `cortex://artifacts/artifact_brief/${id}`,
    sha256,
    byte_length: logicalVersion === 1 ? 80 : 120,
    media_type: "text/markdown",
    run_id: versionRunId,
    attempt_id: attemptId,
    parents,
    source_ids: sourceIds,
    generator,
    tool,
    state: "committed",
    provenance: {
      schema_version: 1,
      run_id: versionRunId,
      attempt_id: attemptId,
      source_ids: sourceIds,
      generator,
      tool,
      parents,
      media_type: "text/markdown",
      byte_length: logicalVersion === 1 ? 80 : 120,
      sha256,
      committed_at: committedAt,
    },
    created_at: committedAt,
    committed_at: committedAt,
  };
}

const version1 = artifactVersion("version_1", 1, "run_previous", "attempt_previous", sha1, []);
const version2 = artifactVersion(
  "version_2",
  2,
  "run_1",
  "attempt_1",
  sha2,
  [{ artifact_version_id: "version_1", sha256: sha1 }],
);
const resolvedDecision = {
  ...pendingDecision,
  state: "resolved",
  resolution: { choice: "keep_both" },
  revision: 1,
  resolved_at: "2026-07-23T12:02:00Z",
};
const g1Projection = {
  schema_version: 1,
  run: { ...run, state: "completed", active_attempt_id: null, stage: null, revision: 8 },
  workflow: {
    ...workflow,
    state: "completed",
    current_stage_key: null,
    revision: 8,
    stages: [{ ...workflow.stages[0], state: "completed", completed_at: "2026-07-23T12:04:00Z" }],
  },
  source_gates: [{ ...sourceGate, state: "resolved", revision: 1, decision: resolvedDecision }],
  sources: [
    {
      id: "binding_echo",
      disposition: "reused",
      created_at: now,
      source: {
        id: "source_echo",
        authority: "arxiv",
        authority_id: "2606.04527",
        canonical_id: "arxiv:2606.04527",
        source_kind: "paper",
        official_title: "Echo-Infinity",
        import_state: "existing",
        revision: 0,
        aliases: [{ id: "alias_echo", authority: "project", value: "Echo-Infinity", created_at: now }],
        created_at: now,
        updated_at: now,
      },
    },
    {
      id: "binding_lingbot",
      disposition: "imported",
      created_at: now,
      source: {
        id: "source_lingbot",
        authority: "arxiv",
        authority_id: "2607.07675",
        canonical_id: "arxiv:2607.07675",
        source_kind: "paper",
        official_title: "LingBot-World",
        import_state: "imported",
        revision: 1,
        aliases: [],
        created_at: now,
        updated_at: now,
      },
    },
  ],
  lineage: {
    nodes: [
      { id: "lineage_dormant", status: "dormant", title: "Dormant memory", revision: 2 },
      { id: "lineage_graduated", status: "graduated", title: "Graduated memory", revision: 4 },
      { id: "lineage_successor", status: "active", title: "Successor", revision: 1 },
    ],
    links: [
      { id: "link_1", from_node_id: "lineage_dormant", to_node_id: "lineage_successor", relation: "successor_reuses" },
      { id: "link_2", from_node_id: "lineage_graduated", to_node_id: "lineage_successor", relation: "successor_reuses" },
    ],
    successor_node_id: "lineage_successor",
  },
  decisions: [resolvedDecision],
  artifacts: [{
    id: "artifact_brief",
    workspace_id: "workspace_1",
    thread_id: thread.id,
    kind: "living_brief",
    title: "Living Brief",
    head_artifact_version_id: "version_2",
    head_revision: 2,
    created_at: now,
    updated_at: "2026-07-23T12:05:00Z",
    versions: [version1, version2],
  }],
  snapshots: [{
    id: "snapshot_1",
    workspace_id: "workspace_1",
    name: "Research handoff",
    members: [{
      artifact_id: "artifact_brief",
      artifact_version_id: "version_2",
      logical_version: 2,
      sha256: sha2,
    }],
    created_at: "2026-07-23T12:06:00Z",
  }],
};

const adoptedSource = {
  id: "source_echo",
  authority: "arxiv",
  authority_id: "2606.04527",
  canonical_id: "arxiv:2606.04527",
  source_kind: "paper",
  official_title: "Echo-Infinity",
  import_state: "existing",
  revision: 0,
  aliases: [{ id: "alias_echo", authority: "project", value: "echo-infinity-2026", created_at: now }],
  created_at: now,
  updated_at: now,
};
const importedSource = {
  ...adoptedSource,
  id: "source_lingbot",
  authority_id: "2607.07675",
  canonical_id: "arxiv:2607.07675",
  official_title: "LingBot-World",
  import_state: "imported",
  revision: 1,
  aliases: [],
};

const capture = {
  id: "capture_1",
  capture_key: "https://example.com/post",
  payload: "https://example.com/post",
  kind: "url",
  note: "read later",
  state: "pending",
  known_source_id: null,
  consumed_source_ids: null,
  failure_category: null,
  blocked_by: null,
  revision: 0,
  created_at: now,
  updated_at: now,
};

const uncertainCapture = {
  ...capture,
  id: "capture_2",
  capture_key: "echo-infinity-2026",
  payload: "echo-infinity-2026",
  kind: "text",
  note: "",
  state: "uncertain",
  known_source_id: "source_echo",
  failure_category: "outcome_unknown",
  blocked_by: null,
  revision: 3,
};

function servingCapture(payload: unknown) {
  return new CortexControlClient({
    fetcher: (async () => new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })) as unknown as typeof fetch,
  });
}

function captureProblem(category: string, current: unknown) {
  return new ControlProblemError({
    type: `urn:cortex:problem:${category}`,
    title: "The payload is already staged",
    status: 409,
    category,
    retryable: false,
    owner: "cortexd",
    current: current as Capture,
  });
}

function invalidG1(mutator: (value: typeof g1Projection) => void) {
  const value = structuredClone(g1Projection);
  mutator(value);
  return value;
}

describe("Cortex Control client contract", () => {
  it("rejects malformed DTOs before they enter UI state", () => {
    expect(() => decodeThread({ ...thread, revision: "2" })).toThrowError(ContractDecodeError);
    expect(() => decodeThread({ ...thread, active_run_id: 7 })).toThrowError(/active_run_id/);
    expect(() => decodeThread({ ...thread, engine_owned: undefined })).toThrowError(/engine_owned/);
    // P9-2: the workspace carries the same flag, and it is required for the
    // same reason -- the web ships with its daemon, so a payload without it is
    // not a payload this cockpit can filter honestly.
    const workspace = { id: "ws_1", title: "Memory research", engine_owned: false, revision: 0, created_at: thread.created_at, updated_at: thread.updated_at };
    expect(decodeWorkspace(workspace).engine_owned).toBe(false);
    expect(() => decodeWorkspace({ ...workspace, engine_owned: undefined })).toThrowError(/engine_owned/);
    expect(() => decodeWorkspace({ ...workspace, engine_owned: "true" })).toThrowError(/engine_owned/);
    // ADJ-A: and the run carries it too, decided by RUN ownership.
    expect(decodeRun(run).engine_owned).toBe(false);
    expect(decodeRun({ ...run, engine_owned: true }).engine_owned).toBe(true);
    expect(() => decodeRun({ ...run, engine_owned: undefined })).toThrowError(/engine_owned/);
  });

  it("reuses one idempotency key when an ambiguous command is retried", async () => {
    const keys: string[] = [];
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      keys.push(new Headers(init?.headers).get("Idempotency-Key") ?? "");
      if (keys.length === 1) throw new TypeError("socket closed after write");
      return new Response(JSON.stringify({
        id: "msg_1",
        thread_id: thread.id,
        role: "user",
        content: "Persist this",
        position: 1,
        created_at: "2026-07-23T12:02:00Z",
      }), { status: 201, headers: { "Content-Type": "application/json", "Idempotency-Replayed": "true" } });
    });
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch, idempotencyKeyFactory: () => "web-deterministic-command-0001" });
    const command = client.prepareAppendMessage(thread, "Persist this");

    await expect(command.execute()).rejects.toThrow("unreachable");
    await expect(command.execute()).resolves.toMatchObject({ replayed: true, value: { id: "msg_1" } });
    expect(keys).toEqual(["web-deterministic-command-0001", "web-deterministic-command-0001"]);
    expect(fetcher.mock.calls.every((call) => !new Headers(call[1]?.headers).has("X-Cortex-Control-Token"))).toBe(true);
  });

  it("surfaces a validated revision conflict with the current resource", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      type: "urn:cortex:problem:revision_conflict",
      title: "Revision conflict",
      status: 409,
      category: "revision_conflict",
      retryable: false,
      owner: "cortexd",
      current: { ...thread, revision: 3 },
    }), { status: 409, headers: { "Content-Type": "application/problem+json" } }));
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch, idempotencyKeyFactory: () => "web-deterministic-command-0002" });

    try {
      await client.prepareCreateRun(thread).execute();
      throw new Error("Expected conflict");
    } catch (error) {
      expect(error).toBeInstanceOf(ControlProblemError);
      expect((error as ControlProblemError).problem).toMatchObject({ category: "revision_conflict", current: { revision: 3 } });
    }
  });

  it("decodes complete G0 and G1 projections while discarding additive fields", () => {
    expect(decodeResearchWorkflow(g0Projection)).toMatchObject({
      schema_version: 1,
      workflow: { current_stage_key: "await_source_decision" },
      source_gates: [{ decision: { state: "pending" } }],
      artifacts: [],
    });
    const additiveProjection = structuredClone(g1Projection);
    Object.assign(additiveProjection.decisions[0].options[0], { future_option_field: "discard me" });
    Object.assign(additiveProjection.decisions[0].resolution, { future_resolution_field: "discard me" });
    const decoded = decodeResearchWorkflow({
      ...additiveProjection,
      future_top_level: "discard me",
      workflow: { ...additiveProjection.workflow, future_workflow_field: true },
    });
    expect(decoded).toMatchObject({
      run: { state: "completed" },
      lineage: { successor_node_id: "lineage_successor" },
      artifacts: [{ head_artifact_version_id: "version_2", versions: [{ logical_version: 1 }, { logical_version: 2 }] }],
      snapshots: [{ members: [{ artifact_version_id: "version_2" }] }],
    });
    expect(decoded).not.toHaveProperty("future_top_level");
    expect(decoded.workflow).not.toHaveProperty("future_workflow_field");
    expect(decoded.decisions[0].options[0]).not.toHaveProperty("future_option_field");
    expect(decoded.decisions[0].resolution).not.toHaveProperty("future_resolution_field");
  });

  it.each([
    ["wrong schema", (value: typeof g1Projection) => { value.schema_version = 2; }],
    ["negative revision", (value: typeof g1Projection) => { value.run.revision = -1; }],
    ["invalid decision state", (value: typeof g1Projection) => { value.decisions[0].state = "unknown"; }],
    ["malformed Source candidate", (value: typeof g1Projection) => { value.source_gates[0].candidates[0].version = -1; }],
    ["duplicate lineage node", (value: typeof g1Projection) => { value.lineage.nodes.push({ ...value.lineage.nodes[0] }); }],
    ["dangling lineage link", (value: typeof g1Projection) => { value.lineage.links[0].from_node_id = "missing_node"; }],
    ["unknown artifact head", (value: typeof g1Projection) => { value.artifacts[0].head_artifact_version_id = "version_missing"; }],
    ["unordered artifact versions", (value: typeof g1Projection) => { value.artifacts[0].versions.reverse(); }],
    ["duplicate logical version", (value: typeof g1Projection) => { value.artifacts[0].versions[1].logical_version = 1; }],
    ["invalid digest", (value: typeof g1Projection) => { value.artifacts[0].versions[1].sha256 = "NOT-A-DIGEST"; }],
    ["unsafe resource URI", (value: typeof g1Projection) => { value.artifacts[0].versions[1].resource_uri = "file:///private/output.md"; }],
    ["provenance mismatch", (value: typeof g1Projection) => { value.artifacts[0].versions[1].provenance.byte_length += 1; }],
    ["projected parent hash mismatch", (value: typeof g1Projection) => {
      value.artifacts[0].versions[1].parents[0].sha256 = "c".repeat(64);
      value.artifacts[0].versions[1].provenance.parents[0].sha256 = "c".repeat(64);
    }],
    ["empty artifact sources", (value: typeof g1Projection) => {
      value.artifacts[0].versions[1].source_ids = [];
      value.artifacts[0].versions[1].provenance.source_ids = [];
    }],
    ["non-canonical artifact source order", (value: typeof g1Projection) => {
      const sourceIds = [...value.artifacts[0].versions[1].source_ids].reverse();
      value.artifacts[0].versions[1].source_ids = [...sourceIds];
      value.artifacts[0].versions[1].provenance.source_ids = [...sourceIds];
    }],
    ["non-canonical artifact parent order", (value: typeof g1Projection) => {
      const parent = { artifact_version_id: "version_0", sha256: "c".repeat(64) };
      const parents = [...value.artifacts[0].versions[1].parents, parent];
      value.artifacts[0].versions[1].parents = structuredClone(parents);
      value.artifacts[0].versions[1].provenance.parents = structuredClone(parents);
    }],
    ["unbound selected-run artifact source", (value: typeof g1Projection) => {
      const sourceIds = ["source_echo", "source_missing"];
      value.artifacts[0].versions[1].source_ids = [...sourceIds];
      value.artifacts[0].versions[1].provenance.source_ids = [...sourceIds];
    }],
    ["snapshot member outside the run", (value: typeof g1Projection) => { value.snapshots[0].members[0].artifact_version_id = "version_1"; }],
    ["oversized collection", (value: typeof g1Projection) => {
      value.artifacts.push(...Array.from({ length: 200 }, () => structuredClone(value.artifacts[0])));
    }],
  ])("rejects a projection with %s", (_label, mutate) => {
    expect(() => decodeResearchWorkflow(invalidG1(mutate))).toThrowError(ContractDecodeError);
  });

  it("validates exact bounded artifact content", () => {
    const content = "# Echo\n\nVerified output.\n";
    const valid = {
      artifact_version_id: "version_2",
      media_type: "text/markdown",
      byte_length: new TextEncoder().encode(content).byteLength,
      sha256: sha2,
      content,
    };
    expect(decodeArtifactVersionContent(valid)).toEqual(valid);
    for (const invalid of [
      { ...valid, artifact_version_id: "bad/version" },
      { ...valid, media_type: "application/json" },
      { ...valid, byte_length: valid.byte_length + 1 },
      { ...valid, sha256: sha2.toUpperCase() },
      { ...valid, content: 7 },
      { ...valid, byte_length: 1_048_577, content: "x".repeat(1_048_577) },
    ]) {
      expect(() => decodeArtifactVersionContent(invalid)).toThrowError(ContractDecodeError);
    }
  });

  it("uses exact typed research read routes without browser credentials", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const content = "# Echo\n";
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      const payload = url.includes("/research-workflow")
        ? g1Projection
        : url.includes("/artifact-versions/")
          ? {
              artifact_version_id: "version_2",
              media_type: "text/markdown",
              byte_length: new TextEncoder().encode(content).byteLength,
              sha256: sha2,
              content,
            }
          : { items: [g1Projection.run], next_cursor: null };
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch });

    await expect(client.listRuns(thread.id)).resolves.toMatchObject({ items: [{ id: "run_1" }] });
    await client.listRuns(thread.id, "run_older");
    await expect(client.getResearchWorkflow("run_1")).resolves.toMatchObject({ schema_version: 1 });
    await expect(client.getArtifactVersionContent("version_2")).resolves.toMatchObject({ content });

    expect(requests.map((request) => request.url)).toEqual([
      `/api/cortex/threads/${thread.id}/runs`,
      `/api/cortex/threads/${thread.id}/runs?after_id=run_older`,
      "/api/cortex/runs/run_1/research-workflow",
      "/api/cortex/artifact-versions/version_2/content",
    ]);
    expect(requests.every((request) => request.init?.cache === "no-store")).toBe(true);
    expect(requests.every((request) => !new Headers(request.init?.headers).has("X-Cortex-Control-Token"))).toBe(true);
  });

  it("reads the adopted Source corpus through exact typed routes without private fields", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      const payload = url.endsWith("/sources")
        ? {
            items: [
              { ...importedSource, engine_ref: "paper:20260719-lingbot-world" },
              { ...adoptedSource, engine_ref: "paper:20260618-echo-infinity" },
            ],
            next_cursor: null,
          }
        : { ...adoptedSource, engine_ref: "paper:20260618-echo-infinity", future_source_field: "discard me" };
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch });

    const listed = await client.listSources();
    expect(listed.items.map((item) => item.id)).toEqual(["source_lingbot", "source_echo"]);
    expect(listed.next_cursor).toBeNull();
    expect(listed.items[0]).not.toHaveProperty("engine_ref");

    const detail = await client.getSource("source_echo");
    expect(detail).toMatchObject({
      canonical_id: "arxiv:2606.04527",
      import_state: "existing",
      aliases: [{ authority: "project", value: "echo-infinity-2026" }],
    });
    expect(detail).not.toHaveProperty("engine_ref");
    expect(detail).not.toHaveProperty("future_source_field");

    expect(requests.map((request) => request.url)).toEqual([
      "/api/cortex/sources",
      "/api/cortex/sources/source_echo",
    ]);
    expect(requests.every((request) => request.init?.cache === "no-store")).toBe(true);
    expect(requests.every((request) => !new Headers(request.init?.headers).has("X-Cortex-Control-Token"))).toBe(true);
  });

  it("rejects Source payloads that are malformed or answer another identity", async () => {
    const serving = (payload: unknown) => new CortexControlClient({
      fetcher: (async () => new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })) as unknown as typeof fetch,
    });

    await expect(serving(adoptedSource).getSource("source_other")).rejects.toBeInstanceOf(ContractDecodeError);
    await expect(serving({ ...adoptedSource, official_title: "" }).getSource("source_echo"))
      .rejects.toBeInstanceOf(ContractDecodeError);
    await expect(serving({ items: [{ ...adoptedSource, revision: -1 }], next_cursor: null }).listSources())
      .rejects.toBeInstanceOf(ContractDecodeError);
    await expect(serving({
      items: [{ ...adoptedSource, aliases: [adoptedSource.aliases[0], adoptedSource.aliases[0]] }],
      next_cursor: null,
    }).listSources()).rejects.toBeInstanceOf(ContractDecodeError);
  });

  it("resolves a Source gate with CAS and one idempotency key across an ambiguous retry", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      if (requests.length === 1) throw new TypeError("socket closed after write");
      return new Response(JSON.stringify({
        ...sourceGate,
        state: "resolved",
        revision: 1,
        decision: resolvedDecision,
        ignored_internal_field: "discard",
      }), { status: 200, headers: { "Content-Type": "application/json", "Idempotency-Replayed": "true" } });
    });
    const client = new CortexControlClient({
      fetcher: fetcher as typeof fetch,
      idempotencyKeyFactory: () => "web-source-resolution-0001",
    });
    const command = client.prepareResolveSourceIntent(
      sourceGate as SourceGateProjection,
      "keep_both",
    );

    await expect(command.execute()).rejects.toThrow("unreachable");
    await expect(command.execute()).resolves.toMatchObject({
      replayed: true,
      value: { id: sourceGate.id, state: "resolved", revision: 1 },
    });
    expect(requests.map((request) => request.url)).toEqual([
      "/api/cortex/source-intents/source_intent_1/resolve",
      "/api/cortex/source-intents/source_intent_1/resolve",
    ]);
    expect(requests.map((request) => JSON.parse(String(request.init?.body)))).toEqual([
      { choice: "keep_both", expected_revision: 0 },
      { choice: "keep_both", expected_revision: 0 },
    ]);
    expect(requests.map((request) => new Headers(request.init?.headers).get("Idempotency-Key"))).toEqual([
      "web-source-resolution-0001",
      "web-source-resolution-0001",
    ]);
    expect(requests.every((request) => !new Headers(request.init?.headers).has("X-Cortex-Control-Token"))).toBe(true);
  });


  it("reads the capture inbox through exact typed routes", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      const payload = url.includes("/captures/")
        ? capture
        : { items: [capture, uncertainCapture], next_cursor: null };
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch });

    const listed = await client.listCaptures();
    expect(listed.items.map((item) => item.id)).toEqual(["capture_1", "capture_2"]);
    expect(listed.next_cursor).toBeNull();
    expect(listed.items[1]).toMatchObject({
      state: "uncertain",
      kind: "text",
      known_source_id: "source_echo",
      failure_category: "outcome_unknown",
    });

    const filtered = await client.listCaptures("uncertain");
    expect(filtered.items).toHaveLength(2);
    const detail = await client.getCapture("capture_1");
    expect(detail).toEqual(capture);

    expect(requests.map((request) => request.url)).toEqual([
      "/api/cortex/captures",
      "/api/cortex/captures?state=uncertain",
      "/api/cortex/captures/capture_1",
    ]);
    expect(requests.every((request) => request.init?.cache === "no-store")).toBe(true);
    expect(requests.every((request) => !new Headers(request.init?.headers).has("X-Cortex-Control-Token"))).toBe(true);
  });

  it("carries the run that fences a blocked capture", () => {
    const blocked = decodeCapture({
      ...capture,
      state: "uncertain",
      failure_category: "carrier_thread_busy",
      blocked_by: "run_fencing_the_carrier",
    });
    expect(blocked.blocked_by).toBe("run_fencing_the_carrier");
    expect(decodeCapture(capture).blocked_by).toBeNull();
  });

  it("refuses a capture that answers another identity", async () => {
    await expect(servingCapture({ ...capture, id: "capture_other" }).getCapture("capture_1"))
      .rejects.toBeInstanceOf(ContractDecodeError);
    await expect(servingCapture({ items: [{ ...capture, revision: -1 }], next_cursor: null }).listCaptures())
      .rejects.toBeInstanceOf(ContractDecodeError);
  });

  it.each([
    ["a lease owner", { claim_owner: "p4-worker" }],
    ["a lease epoch", { claim_epoch: 2 }],
    ["a lease expiry", { claim_expires_at: now }],
    ["an unenumerated field", { future_capture_field: "discard me" }],
  ])("refuses a capture carrying %s", (_label, extra) => {
    expect(() => decodeCapture({ ...capture, ...extra })).toThrowError(ContractDecodeError);
  });

  it.each([
    ["a missing field", (value: Record<string, unknown>) => { delete value.note; }],
    ["an unknown state", (value: Record<string, unknown>) => { value.state = "staged"; }],
    ["an unknown kind", (value: Record<string, unknown>) => { value.kind = "pdf"; }],
    ["a negative revision", (value: Record<string, unknown>) => { value.revision = -1; }],
    ["a stringified source list", (value: Record<string, unknown>) => { value.consumed_source_ids = "source_echo"; }],
    ["a non-string source id", (value: Record<string, unknown>) => { value.consumed_source_ids = [7]; }],
    ["an empty consumed source list", (value: Record<string, unknown>) => { value.consumed_source_ids = []; }],
    ["a non-string note", (value: Record<string, unknown>) => { value.note = null; }],
    // ⟦P9-3⟧ `blocked_by` is required-present, not optional: the daemon
    // projects it onto every capture from every route, so a DTO without it is
    // one this web did not ship with and must not be rendered a field short.
    ["no blocked_by at all", (value: Record<string, unknown>) => { delete value.blocked_by; }],
    ["a non-string blocked_by", (value: Record<string, unknown>) => { value.blocked_by = 7; }],
  ])("refuses a capture with %s", (_label, mutate) => {
    const value: Record<string, unknown> = { ...capture };
    mutate(value);
    expect(() => decodeCapture(value)).toThrowError(ContractDecodeError);
  });

  it("stages a capture and records each decision as its own CAS command", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    let key = 0;
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      const state = url.endsWith("/approve") || url.endsWith("/reopen")
        ? "approved"
        : url.endsWith("/dismiss")
          ? "dismissed"
          : "pending";
      return new Response(JSON.stringify({ ...capture, state, revision: capture.revision + requests.length }), {
        status: url.endsWith("/captures") ? 201 : 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    const client = new CortexControlClient({
      fetcher: fetcher as typeof fetch,
      idempotencyKeyFactory: () => `web-capture-command-${String(++key).padStart(4, "0")}`,
    });

    const created = await client.prepareCreateCapture({ payload: "https://example.com/post ", note: "read later" }).execute();
    expect(created.value.state).toBe("pending");
    await client.prepareApproveCapture(capture as Capture).execute();
    await client.prepareDismissCapture({ ...capture, revision: 4 } as Capture).execute();
    await client.prepareReopenCapture({ ...uncertainCapture, id: capture.id } as Capture).execute();

    expect(requests.map((request) => request.url)).toEqual([
      "/api/cortex/captures",
      "/api/cortex/captures/capture_1/approve",
      "/api/cortex/captures/capture_1/dismiss",
      "/api/cortex/captures/capture_1/reopen",
    ]);
    // The payload leaves the browser exactly as it was typed: trimming is the
    // server's key derivation, not a client rewrite of the stored submission.
    expect(requests.map((request) => JSON.parse(String(request.init?.body)))).toEqual([
      { payload: "https://example.com/post ", note: "read later" },
      { expected_revision: 0 },
      { expected_revision: 4 },
      { expected_revision: 3, acknowledged: true },
    ]);
    expect(requests.map((request) => new Headers(request.init?.headers).get("Idempotency-Key"))).toEqual([
      "web-capture-command-0001",
      "web-capture-command-0002",
      "web-capture-command-0003",
      "web-capture-command-0004",
    ]);
    expect(requests.every((request) => new Headers(request.init?.headers).get("X-Cortex-Web-Client") === "v1")).toBe(true);
    expect(requests.every((request) => !new Headers(request.init?.headers).has("X-Cortex-Control-Token"))).toBe(true);
  });

  it("reuses one capture idempotency key across an ambiguous retry", async () => {
    const keys: string[] = [];
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      keys.push(new Headers(init?.headers).get("Idempotency-Key") ?? "");
      if (keys.length === 1) throw new TypeError("socket closed after write");
      return new Response(JSON.stringify(capture), {
        status: 201,
        headers: { "Content-Type": "application/json", "Idempotency-Replayed": "true" },
      });
    });
    const client = new CortexControlClient({
      fetcher: fetcher as typeof fetch,
      idempotencyKeyFactory: () => "web-capture-staging-0001",
    });
    const command = client.prepareCreateCapture({ payload: "https://example.com/post", note: "" });

    await expect(command.execute()).rejects.toThrow("unreachable");
    await expect(command.execute()).resolves.toMatchObject({ replayed: true, value: { id: "capture_1" } });
    expect(keys).toEqual(["web-capture-staging-0001", "web-capture-staging-0001"]);
  });

  it("refuses a decided capture that answers for another row", async () => {
    const client = new CortexControlClient({
      fetcher: (async () => new Response(JSON.stringify({ ...capture, id: "capture_other", state: "approved", revision: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })) as unknown as typeof fetch,
    });

    await expect(client.prepareApproveCapture(capture as Capture).execute())
      .rejects.toBeInstanceOf(ContractDecodeError);
  });

  it("surfaces an already_captured conflict as the existing inbox row", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      type: "urn:cortex:problem:already_captured",
      title: "The payload is already staged",
      status: 409,
      category: "already_captured",
      retryable: false,
      owner: "cortexd",
      current: capture,
    }), { status: 409, headers: { "Content-Type": "application/problem+json" } }));
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch });

    try {
      await client.prepareCreateCapture({ payload: "HTTPS://Example.com/post", note: "again" }).execute();
      throw new Error("Expected an already_captured conflict");
    } catch (error) {
      expect(error).toBeInstanceOf(ControlProblemError);
      expect((error as ControlProblemError).problem.category).toBe("already_captured");
      expect(alreadyCapturedCurrent(error)).toEqual(capture);
    }

    expect(alreadyCapturedCurrent(captureProblem("revision_conflict", capture))).toBeNull();
    expect(alreadyCapturedCurrent(new TypeError("socket closed after write"))).toBeNull();
    // A conflict body that leaks the consumer lease is a server defect, not a
    // row this client will hand to the inbox.
    expect(() => alreadyCapturedCurrent(captureProblem("already_captured", { ...capture, claim_owner: "p4" })))
      .toThrowError(ContractDecodeError);
  });

  it("rejects valid response DTOs that do not match the requested identity", async () => {
    const content = "# Echo\n";
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const payload = url.includes("/research-workflow")
        ? g1Projection
        : url.includes("/artifact-versions/")
          ? {
              artifact_version_id: "version_2",
              media_type: "text/markdown",
              byte_length: new TextEncoder().encode(content).byteLength,
              sha256: sha2,
              content,
            }
          : { ...sourceGate, id: "source_intent_other" };
      return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = new CortexControlClient({
      fetcher: fetcher as typeof fetch,
      idempotencyKeyFactory: () => "web-source-resolution-0002",
    });

    await expect(client.getResearchWorkflow("run_other")).rejects.toBeInstanceOf(ContractDecodeError);
    await expect(client.getArtifactVersionContent("version_other")).rejects.toBeInstanceOf(ContractDecodeError);
    await expect(client.prepareResolveSourceIntent(
      sourceGate as SourceGateProjection,
      "keep_both",
    ).execute()).rejects.toBeInstanceOf(ContractDecodeError);
  });
  it("sends rename and archive as action posts with the row's revision", async () => {
    const calls: Array<{ url: string; body: unknown; key: string | null }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: JSON.parse(String(init?.body)), key: new Headers(init?.headers).get("Idempotency-Key") });
      const path = String(input);
      const row = path.includes("/workspaces/")
        ? { id: "ws_1", title: "Echo v2", engine_owned: false, revision: 3, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z" }
        : { id: "thread_1", workspace_id: "ws_1", title: "Q", status: "idle", active_run_id: null, engine_owned: false, archived_at: path.endsWith("/archive") ? "2026-09-06T00:00:00Z" : null, revision: 3, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z" };
      return new Response(JSON.stringify(row), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch, idempotencyKeyFactory: () => "web-k" });
    const workspace = { id: "ws_1", title: "Echo", engine_owned: false, revision: 2, created_at: "", updated_at: "" };
    const thread = { id: "thread_1", workspace_id: "ws_1", title: "Q", status: "idle", active_run_id: null, engine_owned: false, archived_at: null, revision: 2, created_at: "", updated_at: "" };
    expect((await client.prepareRenameWorkspace(workspace, "Echo v2").execute()).value.title).toBe("Echo v2");
    expect((await client.prepareRenameThread(thread, "Q2").execute()).value.revision).toBe(3);
    expect((await client.prepareArchiveThread(thread).execute()).value.archived_at).not.toBeNull();
    expect((await client.prepareUnarchiveThread(thread).execute()).value.archived_at).toBeNull();
    expect(calls.map((c) => c.url.replace(/^.*\/api\/cortex/, ""))).toEqual(["/workspaces/ws_1/rename", "/threads/thread_1/rename", "/threads/thread_1/archive", "/threads/thread_1/unarchive"]);
    expect(calls[0].body).toEqual({ title: "Echo v2", expected_revision: 2 });
    expect(calls[2].body).toEqual({ expected_revision: 2 });
    expect(calls.every((c) => c.key === "web-k")).toBe(true);
  });

  it("asks for archived threads only when told to and requires archived_at", async () => {
    const fetcher = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(async () => new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch });
    await client.listThreads("ws_1");
    await client.listThreads("ws_1", { includeArchived: true });
    expect(String(fetcher.mock.calls[0][0])).toContain("/threads?workspace_id=ws_1");
    expect(String(fetcher.mock.calls[0][0])).not.toContain("include_archived");
    expect(String(fetcher.mock.calls[1][0])).toContain("/threads?workspace_id=ws_1&include_archived=true");
    expect(() => decodeThread({ id: "thread_1", workspace_id: "ws_1", title: "Q", status: "idle", active_run_id: null, engine_owned: false, revision: 0, created_at: "x", updated_at: "x" })).toThrow(/archived_at/);
  });

  it("reads the health projection defensively: only boolean capabilities, never a guessed gate", async () => {
    const served = async (payload: unknown) => {
      const client = new CortexControlClient({
        fetcher: (async () => new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } })) as unknown as typeof fetch,
      });
      return client.getRuntimeStatus();
    };
    await expect(served({ api_version: "v1", runtime_dispatch_enabled: false, capabilities: { event_replay: true, source_resolution: false, runtime_dispatch: "yes", nested: {} } }))
      .resolves.toEqual({ apiVersion: "v1", dispatchGate: false, capabilities: { event_replay: true, source_resolution: false } });
    // Nothing reported is `null`, which the Status view says out loud; it is
    // never an empty map that would read as "this build can do nothing".
    await expect(served({})).resolves.toEqual({ apiVersion: null, dispatchGate: null, capabilities: null });
    await expect(served({ capabilities: ["event_replay"] })).resolves.toMatchObject({ capabilities: null });
  });
});

// R1c: the research catalog, its dossier and the one command that opens a
// conversation for an item.
const researchItemId = `ri_${"a1".repeat(16)}`;
const researchItem = {
  id: researchItemId,
  kind: "idea",
  origin_id: "idea.memory-decay",
  title: "Memory decay in long-horizon agents",
  status: "awaiting_human",
  summary: "Where this was left.",
  pause_reason: "Waiting on your answer about scope.",
  round_count: 3,
  updated_at: now,
};
const researchDocument = {
  id: "rd_1",
  document_id: "rd_1.document",
  title: "Dossier",
  version: 2,
  media_type: "text/markdown",
  byte_length: 10,
  sha256: "c".repeat(64),
};
const researchDetail = {
  ...researchItem,
  history: [{ kind: "round", label: "Round 1", text: "Framed the question.", created_at: now }],
  documents: [researchDocument],
  thread_id: null,
  continuation_ready: true,
  unavailable_reason: null,
};
const documentContent = {
  document_version_id: "rd_1",
  media_type: "text/markdown",
  byte_length: 10,
  sha256: "c".repeat(64),
  content: "# Decay\n\nx",
};

function servedBy(payload: unknown) {
  const requests: string[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push(String(input));
    void init;
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  return { requests, client: new CortexControlClient({ fetcher: fetcher as typeof fetch }) };
}

describe("research catalog contract", () => {
  it("pages the catalog on exact typed routes and drops fields it was not promised", async () => {
    const { client, requests } = servedBy({
      items: [{ ...researchItem, future_item_field: "discard me" }],
      total: 7,
      limit: 100,
      offset: 0,
      future_page_field: "discard me",
    });
    const page = await client.listResearchItems({ kind: "idea", status: "awaiting_human" });
    expect(requests[0]).toContain("/api/cortex/research-items?kind=idea&status=awaiting_human&limit=100&offset=0");
    expect(page).toEqual({ items: [researchItem], total: 7, limit: 100, offset: 0 });
    expect(page.items[0]).not.toHaveProperty("future_item_field");

    const browsed = servedBy({ items: [], total: 7, limit: 100, offset: 200 });
    await browsed.client.listResearchItems({ offset: 200 });
    expect(browsed.requests[0]).toContain("/api/cortex/research-items?limit=100&offset=200");
  });

  it("refuses a page that carries more than it says, or the same item twice", () => {
    const page = { items: [researchItem, { ...researchItem }], total: 2, limit: 2, offset: 0 };
    expect(() => decodeResearchItemPage(page)).toThrowError(/unique/);
    expect(() => decodeResearchItemPage({ ...page, items: [researchItem], limit: 0 })).toThrowError(/limit/);
    expect(() => decodeResearchItemPage({ ...page, items: [researchItem, { ...researchItem, id: `ri_${"b2".repeat(16)}` }], limit: 1 }))
      .toThrowError(/more rows than it says/);
  });

  it("refuses an identity it cannot trust: a host path, a wrong kind or a foreign answer", async () => {
    expect(() => decodeResearchItem({ ...researchItem, origin_id: "/Users/private/notes/idea.md" })).toThrowError(/origin_id/);
    expect(() => decodeResearchItem({ ...researchItem, id: "ri_not-hex" })).toThrowError(/id/);
    expect(() => decodeResearchItem({ ...researchItem, kind: "paper" })).toThrowError(/kind/);
    // A truthful status this build has never heard of is kept, in the words the
    // backend used.
    expect(decodeResearchItem({ ...researchItem, status: "resumed_elsewhere" }).status).toBe("resumed_elsewhere");

    const { client } = servedBy({ ...researchDetail, id: `ri_${"b2".repeat(16)}` });
    await expect(client.getResearchItem(researchItemId)).rejects.toThrowError(/does not match the request/);
  });

  it("reads a dossier and its document version by their own identities", async () => {
    const detail = servedBy({ ...researchDetail, future_detail_field: "discard me" });
    const decoded = await detail.client.getResearchItem(researchItemId);
    expect(detail.requests[0]).toContain(`/api/cortex/research-items/${researchItemId}`);
    expect(decoded).toMatchObject({ continuation_ready: true, thread_id: null, documents: [researchDocument] });
    expect(decoded).not.toHaveProperty("future_detail_field");

    const content = servedBy(documentContent);
    const bytes = await content.client.getResearchDocumentContent("rd_1");
    expect(content.requests[0]).toContain("/api/cortex/research-documents/rd_1/content");
    expect(bytes).toEqual({ ...documentContent, redacted: false, retained_byte_length: null });

    const foreign = servedBy({ ...documentContent, document_version_id: "rd_2" });
    await expect(foreign.client.getResearchDocumentContent("rd_1")).rejects.toThrowError(/does not match the request/);
  });

  it("keeps a redacted projection honest about what it is", () => {
    const redacted = decodeResearchDocumentContent({ ...documentContent, redacted: true, retained_byte_length: 4096 });
    // The bytes are the projection's; the digest still names the retained
    // version they were projected from, so the two are not compared.
    expect(redacted).toMatchObject({ redacted: true, retained_byte_length: 4096, byte_length: 10, sha256: "c".repeat(64) });
    // A redaction can make the answer LONGER than what it was projected from --
    // "/tmp/x" replaced by "[redacted]" is six bytes replaced by ten -- so a
    // retained length below the delivered one is an ordinary answer, not a
    // malformed one.
    const expanded = { content: "[redacted]", byte_length: 10, retained_byte_length: 6, redacted: true };
    expect(decodeResearchDocumentContent({ ...documentContent, ...expanded }))
      .toMatchObject({ redacted: true, retained_byte_length: 6, byte_length: 10, content: "[redacted]" });
    expect(() => decodeResearchDocumentContent({ ...documentContent, retained_byte_length: -1 }))
      .toThrowError(/retained_byte_length/);
    expect(() => decodeResearchDocumentContent({ ...documentContent, retained_byte_length: 1_048_577 }))
      .toThrowError(/retained_byte_length/);
    expect(() => decodeResearchDocumentContent({ ...documentContent, retained_byte_length: "4096" }))
      .toThrowError(/retained_byte_length/);
    expect(() => decodeResearchDocumentContent({ ...documentContent, redacted: "yes" })).toThrowError(/redacted/);
    // Truncated or re-encoded bytes are refused rather than read as the document.
    expect(() => decodeResearchDocumentContent({ ...documentContent, byte_length: 11 })).toThrowError(/byte length/);
    expect(() => decodeResearchDocumentContent({ ...documentContent, media_type: "application/pdf" })).toThrowError(/media_type/);
  });

  it("opens an item's conversation with the project the operator has open, and starts no run", async () => {
    const calls: Array<{ url: string; body: unknown; key: string | null }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        body: JSON.parse(String(init?.body ?? "{}")),
        key: new Headers(init?.headers).get("Idempotency-Key"),
      });
      return new Response(JSON.stringify(thread), { status: 201, headers: { "Content-Type": "application/json" } });
    });
    const client = new CortexControlClient({ fetcher: fetcher as typeof fetch, idempotencyKeyFactory: () => "web-research-0001" });
    const workspace = { id: "ws_1", title: "Echo", engine_owned: false, revision: 4, created_at: now, updated_at: now };

    const opened = await client.prepareOpenResearchThread({ id: researchItemId }, workspace).execute();
    expect(opened.value.id).toBe("thread_1");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain(`/api/cortex/research-items/${researchItemId}/thread`);
    expect(calls[0].body).toEqual({ workspace_id: "ws_1", expected_revision: 4 });
    expect(calls[0].key).toBe("web-research-0001");

    // A thread answered for another project is refused rather than opened.
    const foreign = servedBy({ ...thread, workspace_id: "ws_2" });
    await expect(foreign.client.prepareOpenResearchThread({ id: researchItemId }, workspace).execute())
      .rejects.toThrowError(/does not match the request/);
  });
});
