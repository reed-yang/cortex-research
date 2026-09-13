// A read-only stand-in for the Control daemon, seeded with one world that is
// wide enough for every shell surface: two projects (one of them empty), a
// live thread with a pending decision, a failed thread, an archived thread,
// three adopted sources and two captures. Every response is a fixed literal,
// so a screenshot taken against it is the same bytes on every machine.

import { createServer } from "node:http";

export const MOCK_CONTROL_TOKEN = "test-only-control-token-000000000000000000";

const now = "2026-07-23T12:00:00Z";
const earlier = "2026-07-23T09:30:00Z";

const workspace = { id: "ws_mobile", title: "Echo × Helios Lab", engine_owned: false, revision: 1, created_at: now, updated_at: now };
const emptyWorkspace = { id: "ws_empty", title: "Successor Lab", engine_owned: false, revision: 1, created_at: now, updated_at: now };
const workspaces = [workspace, emptyWorkspace];

const thread = { id: "thread_mobile", workspace_id: workspace.id, title: "Helios-14B memory plan", status: "running", active_run_id: "run_mobile", archived_at: null, engine_owned: false, revision: 2, created_at: now, updated_at: now };
const failedThread = { id: "thread_failed", workspace_id: workspace.id, title: "Wan2.2 cache ablation", status: "idle", active_run_id: null, archived_at: null, engine_owned: false, revision: 4, created_at: earlier, updated_at: earlier };
const archivedThread = { id: "thread_archived", workspace_id: workspace.id, title: "Retired context sweep", status: "idle", active_run_id: null, archived_at: earlier, engine_owned: false, revision: 6, created_at: earlier, updated_at: earlier };
const threadsByWorkspace = new Map([
  [workspace.id, [thread, failedThread, archivedThread]],
  [emptyWorkspace.id, []],
]);

const run = { id: "run_mobile", thread_id: thread.id, state: "waiting_for_decision", active_attempt_id: "attempt_mobile", stage: "source review", latest_sequence: 2, engine_owned: false, revision: 3, created_at: now, updated_at: now };
const failedRun = { id: "run_failed", thread_id: failedThread.id, state: "failed", active_attempt_id: "attempt_failed", stage: "evidence sweep", latest_sequence: 4, engine_owned: false, revision: 5, created_at: earlier, updated_at: earlier };

const message = { id: "msg_mobile", thread_id: thread.id, role: "assistant", content: "Echo-Infinity and Helios evidence is durable. One source decision still needs your review.", position: 1, created_at: now };
const failedAsk = { id: "msg_failed", thread_id: failedThread.id, role: "user", content: "Compare the recurrent memory controller against a frozen Wan2.2 cache.", position: 1, created_at: earlier };
const archivedAsk = { id: "msg_archived", thread_id: archivedThread.id, role: "user", content: "Sweep the retired context notes for anything still cited.", position: 1, created_at: earlier };
const messagesByThread = new Map([
  [thread.id, [message]],
  [failedThread.id, [failedAsk]],
  [archivedThread.id, [archivedAsk]],
]);
const runsByThread = new Map([
  [thread.id, [run]],
  [failedThread.id, [failedRun]],
  [archivedThread.id, []],
]);
const runsById = new Map([[run.id, run], [failedRun.id, failedRun]]);

const decision = { id: "decision_mobile", run_id: run.id, attempt_id: "attempt_mobile", kind: "source_conflict", prompt: "Keep Echo-Infinity and the supplied model page as separate canonical sources?", options: [{ id: "keep_both", label: "Keep both sources" }, { id: "replace", label: "Use Echo-Infinity only" }], state: "pending", resolution: null, revision: 0, created_at: now, resolved_at: null };
const event = { cursor: "djE6Mi5kZXRlcm1pbmlzdGlj", schema_version: 1, id: "event_mobile", run_id: run.id, attempt_id: "attempt_mobile", sequence: 2, type: "decision.required", occurred_at: now, causation_id: null, durability: "durable", payload: { decision_id: decision.id } };
const failureEvent = { cursor: "djE6MS5kZXRlcm1pbmlzdGlj", schema_version: 1, id: "event_failed", run_id: failedRun.id, attempt_id: "attempt_failed", sequence: 4, type: "run.failed", occurred_at: earlier, causation_id: null, durability: "durable", payload: { failure_category: "managed_worker_unavailable" } };

const NOTES_TEXT = "# Echo-Infinity\n\nEvolving memory keeps a long video coherent by writing only what the\nreconstruction error says is new.\n\n- Preserve causal temporal updates.\n- Measure memory drift before the context is scaled.\n";
const NOTES_SHA256 = "0ff9d955ff542319ce2792bdaf6b81692d6083a4a105f411c77c6eaa91852035";

function source(id, authority, authorityId, kind, title, importState, aliases) {
  return {
    id,
    authority,
    authority_id: authorityId,
    canonical_id: `${authority}:${authorityId}`,
    source_kind: kind,
    official_title: title,
    import_state: importState,
    revision: 1,
    aliases: aliases.map((value, index) => ({ id: `${id}_alias_${index}`, authority: "project", value, created_at: earlier })),
    created_at: earlier,
    updated_at: now,
  };
}

const sources = [
  source("src_echo", "arxiv", "2606.04527", "paper", "Echo-Infinity: evolving memory for long video", "imported", ["Echo-Infinity"]),
  source("src_lingbot", "arxiv", "2607.07675", "paper", "LingBot-World: a supplied model page", "imported", ["LingBot"]),
  source("src_helios", "doi", "10.5555/helios-14b", "paper", "Helios-14B: a bounded memory controller", "pending", []),
];
const sourcesById = new Map(sources.map((item) => [item.id, item]));

function capture(id, payload, kind, note, state, extra = {}) {
  return {
    id,
    capture_key: `key_${id}`,
    payload,
    kind,
    note,
    state,
    known_source_id: null,
    consumed_source_ids: null,
    failure_category: null,
    blocked_by: null,
    revision: 1,
    created_at: earlier,
    updated_at: now,
    ...extra,
  };
}

const captures = [
  capture("cap_ttt", "https://arxiv.org/abs/2607.07675", "url", "Compare with the frozen cache baseline.", "pending"),
  capture("cap_note", "Memory drift should be measured before the context is scaled.", "text", "", "approved"),
];

// One readable document, so the Library's reader has something to show and the
// digest is the digest of the text actually served.
const sourceNotes = new Map(sources.map((item) => [item.id, {
  source_id: item.id,
  canonical_id: item.canonical_id,
  kind: "notes",
  text: NOTES_TEXT,
  content_sha256: NOTES_SHA256,
  start_line: 1,
  end_line: 7,
  next_cursor: null,
}]));

const health = {
  status: "ok",
  api_version: "1.4.0",
  runtime_dispatch_enabled: false,
  capabilities: {
    control_store: true,
    event_replay: true,
    event_stream: true,
    source_resolution: true,
    artifact_metadata: true,
    research_pipeline: true,
    runtime_dispatch: true,
    telegram_adapter: false,
  },
};

const researchWorkflow = {
  schema_version: 1,
  run,
  workflow: null,
  source_gates: [{
    id: "source_intent_mobile",
    run_id: run.id,
    attempt_id: "attempt_mobile",
    state: "pending",
    revision: 0,
    created_at: now,
    updated_at: now,
    title_observation: "Echo-Infinity",
    locator_observation: "https://arxiv.org/abs/2607.07675",
    candidates: [{
      id: "candidate_mobile",
      claim_kind: "title",
      canonical_id: "arxiv:2607.07675",
      official_title: "Echo-Infinity",
      source_kind: "paper",
      version: null,
    }],
    decision,
  }],
  sources: [],
  lineage: { nodes: [], links: [], successor_node_id: null },
  decisions: [decision],
  artifacts: [],
  snapshots: [],
};

function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
    "Content-Type": "application/json",
  });
  response.end(body);
}

function page(items) {
  return { items, next_cursor: null };
}

export async function startMockControlServer(port = 8799) {
  const server = createServer((request, response) => {
    if (request.headers["x-cortex-control-token"] !== MOCK_CONTROL_TOKEN) {
      send(response, 403, { type: "urn:cortex:problem:authentication_required", title: "Not authorized", status: 403, category: "authentication_required", retryable: false, owner: "mock-cortexd" });
      return;
    }
    const url = new URL(request.url ?? "/", `http://127.0.0.1:${port}`);
    const threadRoute = /^\/api\/v1\/threads\/([^/]+)(\/messages|\/runs)?$/.exec(url.pathname);
    const runRoute = /^\/api\/v1\/runs\/([^/]+)(\/research-workflow)?$/.exec(url.pathname);
    const sourceRoute = /^\/api\/v1\/sources\/([^/]+)$/.exec(url.pathname);
    const contentRoute = /^\/api\/v1\/sources\/([^/]+)\/content$/.exec(url.pathname);
    const workspaceId = url.searchParams.get("workspace_id");
    if (request.method !== "GET") {
      send(response, 405, { type: "urn:cortex:problem:invalid_request", title: "Mock is read-only", status: 405, category: "invalid_request", retryable: false, owner: "mock-cortexd" });
    } else if (url.pathname === "/api/v1/health") send(response, 200, health);
    else if (url.pathname === "/api/v1/workspaces") send(response, 200, page(workspaces));
    else if (url.pathname === `/api/v1/workspaces/${workspace.id}`) send(response, 200, workspace);
    else if (url.pathname === `/api/v1/workspaces/${emptyWorkspace.id}`) send(response, 200, emptyWorkspace);
    else if (url.pathname === "/api/v1/threads" && threadsByWorkspace.has(workspaceId)) {
      send(response, 200, page(threadsByWorkspace.get(workspaceId)));
    } else if (threadRoute && messagesByThread.has(threadRoute[1])) {
      const id = threadRoute[1];
      if (threadRoute[2] === "/messages") send(response, 200, page(messagesByThread.get(id)));
      else if (threadRoute[2] === "/runs") send(response, 200, page(runsByThread.get(id)));
      else send(response, 200, [thread, failedThread, archivedThread].find((item) => item.id === id));
    } else if (runRoute && runsById.has(runRoute[1])) {
      if (runRoute[2] !== "/research-workflow") send(response, 200, runsById.get(runRoute[1]));
      else if (runRoute[1] === run.id) send(response, 200, researchWorkflow);
      else send(response, 404, { type: "urn:cortex:problem:not_found", title: "Not found", status: 404, category: "not_found", retryable: false, owner: "mock-cortexd" });
    } else if (url.pathname === "/api/v1/sources") send(response, 200, page(sources));
    else if (sourceRoute && sourcesById.has(sourceRoute[1])) send(response, 200, sourcesById.get(sourceRoute[1]));
    else if (contentRoute && sourceNotes.has(contentRoute[1]) && url.searchParams.get("kind") === "notes") {
      send(response, 200, sourceNotes.get(contentRoute[1]));
    }
    else if (url.pathname === "/api/v1/captures") send(response, 200, page(captures));
    else if (url.pathname === "/api/v1/decisions" && url.searchParams.get("state") === "pending") send(response, 200, page([decision]));
    else if (url.pathname === "/api/v1/events") send(response, 200, { items: url.searchParams.has("after_cursor") ? [] : [failureEvent, event], next_cursor: event.cursor });
    else send(response, 404, { type: "urn:cortex:problem:not_found", title: "Not found", status: 404, category: "not_found", retryable: false, owner: "mock-cortexd" });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return {
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}
