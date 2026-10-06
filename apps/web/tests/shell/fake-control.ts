import { CortexControlClient } from "../../app/control/client";
import { sourceLinkEntry, tinyPng, xhsNoteProjection, xhsStatusProjection } from "./xhs-fixtures";

export const now = "2026-09-06T12:00:00Z";
type Row = Record<string, unknown>;

// The note header Control answers a command or a 409 with.
const NOTE_HEADER_FIELDS = ["source_id", "note_id", "title", "state", "last_error", "content_version", "revision"];
function noteHeader(note: Row): Row {
  return Object.fromEntries(NOTE_HEADER_FIELDS.map((name) => [name, note[name]]));
}

// A plain in-memory Storage for a test that needs the browser to remember
// something. Newer Node releases put their own unconfigured `localStorage` in
// front of jsdom's, so a test installs this with `vi.stubGlobal` instead.
export function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, String(value)); },
  };
}

export class FakeControl {
  workspaces: Row[] = [];
  threads: Row[] = [];
  messages: Row[] = [];
  runs: Row[] = [];
  events: Row[] = [];
  decisions: Row[] = [];
  captures: Row[] = [];
  // Idea fragments in save order; the list route answers newest first.
  fragments: Row[] = [];
  // Control's receipts for fragment saves: a retried key answers the stored row.
  private fragmentReceipts = new Map<string, Row>();
  sources: Row[] = [];
  contents: Row[] = [];
  // Figures a source's stored copy holds, by `${source_id}:${path}`; a path
  // missing here is the route's 404, which is how a missing figure looks.
  assets = new Map<string, { type: string; bytes: Uint8Array<ArrayBuffer> }>();
  // XHS: each saved note's projection by its source id, the links each source
  // has (none unless a test adds them), and the plugin status, off as shipped.
  xhsNotes: Record<string, Row> = {};
  sourceLinks: Record<string, { recommended_in: Row[]; recommends: Row[] }> = {};
  xhsStatus: Row = xhsStatusProjection();
  research: Record<string, Row> = {};
  // R1c: research items hold their detail fields here; the list route answers
  // with the catalog fields only, exactly as the contract says.
  researchItems: Row[] = [];
  researchContents: Record<string, Row> = {};
  // The page size the catalog route is willing to answer with, whatever the
  // client asked for: a test that has to browse sets it small.
  researchPageLimit = 100;
  health: Row = { api_version: "v1", capabilities: { control_store: true }, runtime_dispatch_enabled: false };
  posts: Array<{ path: string; body: Row; key: string | null }> = [];
  // Every read, path and query, in order: a test that asks "did the inbox
  // actually load" needs the request, not just the resulting rows.
  gets: string[] = [];
  failNext: { path: RegExp; status: number; category: string; current?: Row } | null = null;
  // The next matching POST commits and then loses its response, the way a
  // socket that closes after the write does.
  loseNextResponse: RegExp | null = null;
  // The next matching POST commits and the Web gateway then answers for it with
  // its own retryable 503, the way route.ts does when its upstream fetch throws.
  gatewayDropsNextResponse: RegExp | null = null;
  // A test that has to observe the shell WHILE a read is in flight arms this
  // hook; the fetch awaits it before it answers, so the in-flight window is a
  // fact of the test rather than a race with the microtask queue.
  beforeResponse: ((path: string, method: string) => Promise<void> | void) | null = null;
  offline = false;

  workspace(id: string, title: string, extra: Row = {}): Row {
    const row = { id, title, engine_owned: false, revision: 0, created_at: now, updated_at: now, ...extra };
    this.workspaces.push(row);
    return row;
  }
  thread(id: string, workspace_id: string, title: string, extra: Row = {}): Row {
    const row = { id, workspace_id, title, status: "idle", active_run_id: null, engine_owned: false, archived_at: null, revision: 0, created_at: now, updated_at: now, ...extra };
    this.threads.push(row);
    return row;
  }
  message(id: string, thread_id: string, role: string, content: string, position: number): Row {
    const row = { id, thread_id, role, content, position, created_at: now };
    this.messages.push(row);
    return row;
  }
  run(id: string, thread_id: string, state: string, extra: Row = {}): Row {
    const row = { id, thread_id, state, active_attempt_id: null, stage: null, latest_sequence: 0, engine_owned: false, revision: 0, created_at: now, updated_at: now, ...extra };
    this.runs.push(row);
    return row;
  }
  decision(id: string, run_id: string, prompt: string, options: unknown[], extra: Row = {}): Row {
    const row = { id, run_id, attempt_id: "attempt_1", kind: "approval", prompt, options, state: "pending", resolution: null, revision: 0, created_at: now, resolved_at: null, ...extra };
    this.decisions.push(row);
    return row;
  }
  capture(id: string, payload: string, extra: Row = {}): Row {
    const row = { id, capture_key: payload, payload, kind: /^https?:/.test(payload) ? "url" : "text", note: "", state: "pending", known_source_id: null, consumed_source_ids: null, failure_category: null, blocked_by: null, available_source_id: null, payload_note: null, revision: 0, created_at: now, updated_at: now, ...extra };
    this.captures.push(row);
    return row;
  }
  fragment(id: string, text: string, extra: Row = {}): Row {
    const row = { id, text, note: "", origin: "web", thread_id: null, context_item_id: null, created_at: now, ...extra };
    this.fragments.push(row);
    return row;
  }
  // Events carry their own cursor, which is what `/events` pages on.
  event(id: string, run_id: string, extra: Row = {}): Row {
    const sequence = this.events.length + 1;
    const row = { cursor: `event_cursor_${sequence}`, schema_version: 1, id, run_id, attempt_id: null, sequence, type: "run.progressed", occurred_at: now, causation_id: null, durability: "durable", payload: {}, ...extra };
    this.events.push(row);
    return row;
  }
  source(id: string, canonical_id: string, official_title: string, extra: Row = {}): Row {
    const row = { id, authority: "arxiv", authority_id: canonical_id.replace(/^[^:]+:/, ""), canonical_id, source_kind: "paper", official_title, import_state: "imported", revision: 0, aliases: [], created_at: now, updated_at: now, ...extra };
    this.sources.push(row);
    return row;
  }
  // One page of readable text for one kind; the reader asks per kind and pages
  // with the opaque `next_cursor` the page carries.
  sourceContent(source_id: string, kind: string, text: string, extra: Row = {}): Row {
    const source = this.sources.find((s) => s.id === source_id);
    const lines = text.split("\n").length;
    const row = { source_id, canonical_id: String(source?.canonical_id ?? "arxiv:0000.00000"), kind, text, content_sha256: "a".repeat(64), start_line: 1, end_line: lines, next_cursor: null, ...extra };
    this.contents.push(row);
    return row;
  }
  sourceAsset(source_id: string, path: string, type: string, bytes: Uint8Array<ArrayBuffer>): void {
    this.assets.set(`${source_id}:${path}`, { type, bytes });
  }
  // A blog source, identified the way Control identifies one: by the SHA-256
  // of its normalized URL. The digest here is invented.
  blog(id: string, title: string, extra: Row = {}): Row {
    const digest = (id.replace(/[^0-9a-f]/g, "") + "b".repeat(64)).slice(0, 64);
    return this.source(id, `url:${digest}`, title, { authority: "url", source_kind: "blog", ...extra });
  }
  // A saved XHS note: its source row and its note projection, with one tiny
  // generated image under the version's assets for every downloaded one.
  xhsNote(id: string, title: string, extra: Row = {}): Row {
    const note = xhsNoteProjection(id, { title, ...extra });
    this.source(id, `xhs:${String(note.note_id)}`, title, { authority: "xhs", source_kind: "xhs_note" });
    for (const image of note.images as Row[]) {
      if (image.asset_path) this.sourceAsset(id, String(image.asset_path), "image/png", tinyPng());
    }
    this.xhsNotes[id] = note;
    return note;
  }
  // One recommendation link from a note to a source, seen from both ends.
  link(noteId: string, toId: string, imageOrdinal: number | null = 1): void {
    const note = this.sources.find((s) => s.id === noteId)!;
    const to = this.sources.find((s) => s.id === toId)!;
    const recommendation = `xhs_rec_${noteId}_${toId}`;
    const entry = (source: Row) => sourceLinkEntry(String(source.id), String(source.source_kind), String(source.official_title), { image_ordinal: imageOrdinal, recommendation_id: recommendation });
    (this.sourceLinks[toId] ??= { recommended_in: [], recommends: [] }).recommended_in.push(entry(note));
    (this.sourceLinks[noteId] ??= { recommended_in: [], recommends: [] }).recommends.push(entry(to));
  }
  sourceGate(id: string, run: Row, decision: Row | null, extra: Row = {}): Row {
    return { id, run_id: run.id, attempt_id: run.active_attempt_id, state: "pending", revision: 0, created_at: now, updated_at: now, title_observation: null, locator_observation: null, candidates: [{ id: `${id}_candidate`, claim_kind: "title", canonical_id: "arxiv:2606.04527", official_title: "Echo-Infinity", source_kind: "paper", version: null }], decision, ...extra };
  }
  // A schema-complete projection for one run; a caller overrides only the
  // lanes it is exercising.
  researchWorkflow(run: Row, extra: Row = {}): Row {
    const row = { schema_version: 1, run, workflow: null, source_gates: [], sources: [], lineage: { nodes: [], links: [], successor_node_id: null }, decisions: [], artifacts: [], snapshots: [], ...extra };
    this.research[String(run.id)] = row;
    return row;
  }

  // A research item, catalog fields and dossier fields together. `ri_` plus 32
  // hex is the only shape the contract decodes, so the fixture spells it from a
  // short name rather than letting a test invent one.
  researchItem(name: string, kind: string, title: string, extra: Row = {}): Row {
    const id = `ri_${name.padEnd(32, "0").slice(0, 32).replace(/[^0-9a-f]/g, "0")}`;
    const row = {
      id, kind, origin_id: `${kind}.${name}`, title, status: "incubating",
      summary: `What ${title} is about.`, pause_reason: null, round_count: 1, updated_at: now,
      history: [], documents: [], thread_id: null, continuation_ready: true, unavailable_reason: null,
      ...extra,
    };
    this.researchItems.push(row);
    return row;
  }
  // One document version and the bytes it stores. The reference lives on the
  // item; the content is answered by document-version identity, on its own
  // route. `redaction` supplies the authorized projection: the delivered bytes
  // are that projection's, while the digest still names the retained version.
  researchDocument(
    item: Row,
    versionId: string,
    title: string,
    content: string,
    extra: Row = {},
    redaction?: { content: string },
  ): Row {
    const byte_length = new TextEncoder().encode(content).byteLength;
    const ref = {
      id: versionId, document_id: `${versionId}.document`, title, version: 1,
      media_type: "text/markdown", byte_length, sha256: "c".repeat(64), ...extra,
    };
    (item.documents as Row[]).push(ref);
    const delivered = redaction?.content ?? content;
    this.researchContents[versionId] = {
      document_version_id: versionId, media_type: ref.media_type, sha256: ref.sha256,
      byte_length: new TextEncoder().encode(delivered).byteLength, content: delivered,
      ...(redaction ? { redacted: true, retained_byte_length: byte_length } : {}),
    };
    return ref;
  }

  client(): CortexControlClient {
    return new CortexControlClient({ fetcher: this.fetch as typeof fetch, idempotencyKeyFactory: () => `web-test-${this.posts.length + 1}` });
  }

  private json(value: unknown, status = 200) {
    return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  }
  private problem(status: number, category: string, current?: Row) {
    return new Response(JSON.stringify({ category, owner: "cortexd", retryable: false, status, title: category, type: `urn:cortex:problem:${category}`, ...(current ? { current } : {}) }), { status, headers: { "Content-Type": "application/problem+json" } });
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    if (this.offline) throw new TypeError("Failed to fetch");
    const url = new URL(String(input), "http://127.0.0.1");
    const path = url.pathname.replace(/^\/api\/cortex\//, "");
    const method = init?.method ?? "GET";
    if (this.beforeResponse) await this.beforeResponse(path, method);
    if (this.failNext && this.failNext.path.test(path)) {
      const failure = this.failNext;
      this.failNext = null;
      return this.problem(failure.status, failure.category, failure.current);
    }
    if (method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Row;
      const key = new Headers(init?.headers).get("Idempotency-Key");
      this.posts.push({ path, body, key });
      const response = this.handlePost(path, body, key);
      if (this.loseNextResponse && this.loseNextResponse.test(path)) {
        this.loseNextResponse = null;
        throw new TypeError("socket closed after write");
      }
      if (this.gatewayDropsNextResponse && this.gatewayDropsNextResponse.test(path)) {
        this.gatewayDropsNextResponse = null;
        const category = "control_gateway_unavailable";
        return new Response(JSON.stringify({ category, owner: "cortex-web", retryable: true, status: 503, title: "The local Cortex Control gateway is unavailable", type: `urn:cortex:web-problem:${category}` }), { status: 503, headers: { "Content-Type": "application/problem+json" } });
      }
      return response;
    }
    this.gets.push(`${path}${url.search}`);
    return this.handleGet(path, url.searchParams);
  };

  private handleGet(path: string, params: URLSearchParams): Response {
    const list = (items: unknown[]) => this.json({ items, next_cursor: null });
    if (path === "health") return this.json(this.health);
    if (path === "workspaces") return list(this.workspaces);
    if (path === "threads") {
      const rows = this.threads.filter((t) => t.workspace_id === params.get("workspace_id") && (params.get("include_archived") === "true" || t.archived_at === null));
      return list(rows);
    }
    let m = path.match(/^workspaces\/([^/]+)$/);
    if (m) { const w = this.workspaces.find((r) => r.id === m![1]); return w ? this.json(w) : this.problem(404, "not_found"); }
    m = path.match(/^threads\/([^/]+)$/);
    if (m) { const t = this.threads.find((r) => r.id === m![1]); return t ? this.json(t) : this.problem(404, "not_found"); }
    m = path.match(/^threads\/([^/]+)\/messages$/);
    if (m) return list(this.messages.filter((r) => r.thread_id === m![1]));
    m = path.match(/^threads\/([^/]+)\/runs$/);
    if (m) return list(this.runs.filter((r) => r.thread_id === m![1]));
    m = path.match(/^runs\/([^/]+)$/);
    if (m) { const r = this.runs.find((x) => x.id === m![1]); return r ? this.json(r) : this.problem(404, "not_found"); }
    m = path.match(/^runs\/([^/]+)\/research-workflow$/);
    if (m) { const r = this.research[m[1]]; return r ? this.json(r) : this.problem(404, "not_found"); }
    if (path === "decisions") return list(this.decisions.filter((d) => d.state === (params.get("state") ?? "pending")));
    // Same contract as the real route: the events after the supplied cursor,
    // and a `next_cursor` of the last one delivered, or the cursor that came
    // in when there was nothing new.
    if (path === "events") {
      const after = params.get("after_cursor");
      const start = after ? this.events.findIndex((e) => e.cursor === after) + 1 : 0;
      const items = this.events.slice(start);
      const last = items.at(-1);
      return this.json({ items, next_cursor: last ? String(last.cursor) : after });
    }
    // The catalog pages; the list answer carries the catalog fields only.
    if (path === "research-items") {
      const kind = params.get("kind");
      const status = params.get("status");
      const limit = Math.min(Number(params.get("limit") ?? "100"), this.researchPageLimit);
      const offset = Number(params.get("offset") ?? "0");
      const rows = this.researchItems.filter((r) => (!kind || r.kind === kind) && (!status || r.status === status));
      const items = rows.slice(offset, offset + limit).map((r) => ({
        id: r.id, kind: r.kind, origin_id: r.origin_id, title: r.title, status: r.status,
        summary: r.summary, pause_reason: r.pause_reason, round_count: r.round_count, updated_at: r.updated_at,
      }));
      return this.json({ items, total: rows.length, limit, offset });
    }
    m = path.match(/^research-items\/([^/]+)$/);
    if (m) { const r = this.researchItems.find((x) => x.id === m![1]); return r ? this.json(r) : this.problem(404, "not_found"); }
    m = path.match(/^research-documents\/([^/]+)\/content$/);
    if (m) {
      const content = this.researchContents[m[1]];
      return content ? this.json(content) : this.problem(404, "research_document_unavailable");
    }
    if (path === "captures") return list(this.captures);
    if (path === "fragments") return list([...this.fragments].reverse());
    m = path.match(/^fragments\/([^/]+)$/);
    if (m) { const f = this.fragments.find((x) => x.id === m![1]); return f ? this.json(f) : this.problem(404, "not_found"); }
    if (path === "sources") {
      const kind = params.get("kind");
      return list(this.sources.filter((s) => !kind || s.source_kind === kind));
    }
    if (path === "xhs/status") return this.json(this.xhsStatus);
    m = path.match(/^sources\/([^/]+)\/note$/);
    if (m) {
      const note = this.xhsNotes[m[1]];
      if (!note) return this.problem(404, "not_found");
      // A recommendation's Capture state is read from the Capture, as Control does.
      for (const recommendation of note.recommendations as Row[]) {
        const capture = this.captures.find((c) => c.id === recommendation.capture_id);
        if (capture) Object.assign(recommendation, { capture_state: capture.state, capture_revision: capture.revision });
      }
      return this.json(note);
    }
    m = path.match(/^sources\/([^/]+)\/links$/);
    if (m) {
      if (!this.sources.some((s) => s.id === m![1])) return this.problem(404, "not_found");
      return this.json(this.sourceLinks[m[1]] ?? { recommended_in: [], recommends: [] });
    }
    // Search is a sibling route of the record, so it is answered before the
    // `sources/{id}` pattern can claim the word "search" as an identifier.
    if (path === "sources/search") {
      const query = params.get("q") ?? "";
      const results = this.sources
        .filter((s) => String(s.official_title).toLowerCase().includes(query.toLowerCase()))
        .map((s) => ({ source_id: s.id, canonical_id: s.canonical_id, title: s.official_title, evidence_id: `source:${s.id}:chunk:1`, section: "Method", excerpt: `A stored passage from ${s.official_title}.`, content_sha256: "b".repeat(64) }));
      return this.json({ query, retrieval_mode: "fts5_or", results });
    }
    m = path.match(/^sources\/([^/]+)\/content$/);
    if (m) {
      const kind = params.get("kind") ?? "notes";
      const page = this.contents.find((c) => c.source_id === m![1] && c.kind === kind);
      return page ? this.json(page) : this.problem(404, "source_content_unavailable");
    }
    // The whole document is every page of that kind, in order: the same text
    // the paged reader shows, read once.
    m = path.match(/^sources\/([^/]+)\/document$/);
    if (m) {
      const kind = params.get("kind") ?? "notes";
      const pages = this.contents.filter((c) => c.source_id === m![1] && c.kind === kind);
      if (!pages.length) return this.problem(409, "source_content_unavailable");
      const text = pages.map((page) => String(page.text)).join("");
      return this.json({ source_id: m[1], canonical_id: pages[0]!.canonical_id, kind, text, content_sha256: pages[0]!.content_sha256, retained_bytes: new TextEncoder().encode(text).byteLength, redacted: false });
    }
    m = path.match(/^sources\/([^/]+)\/asset$/);
    if (m) {
      const asset = this.assets.get(`${m[1]}:${params.get("path") ?? ""}`);
      if (!asset) return this.problem(404, "source_asset_unavailable");
      return new Response(asset.bytes, { headers: { "Content-Type": asset.type, "Content-Length": String(asset.bytes.byteLength), "Cache-Control": "no-store" } });
    }
    m = path.match(/^sources\/([^/]+)$/);
    if (m) { const s = this.sources.find((x) => x.id === m![1]); return s ? this.json(s) : this.problem(404, "not_found"); }
    return this.problem(404, "not_found");
  }

  private handlePost(path: string, body: Row, key: string | null = null): Response {
    if (path === "fragments") {
      const stored = key ? this.fragmentReceipts.get(key) : undefined;
      if (stored) return new Response(JSON.stringify(stored), { status: 201, headers: { "Content-Type": "application/json", "Idempotency-Replayed": "true" } });
      const f = this.fragment(`fragment_${this.fragments.length + 1}`, String(body.text), { note: String(body.note) });
      if (key) this.fragmentReceipts.set(key, f);
      return this.json(f, 201);
    }
    let m = path.match(/^workspaces\/([^/]+)\/rename$/);
    if (m) { const w = this.workspaces.find((x) => x.id === m![1])!; Object.assign(w, { title: body.title, revision: (w.revision as number) + 1 }); return this.json(w); }
    m = path.match(/^threads\/([^/]+)\/(rename|archive|unarchive)$/);
    if (m) {
      const t = this.threads.find((x) => x.id === m![1])!;
      if (m[2] === "rename") t.title = body.title;
      if (m[2] === "archive") t.archived_at = now;
      if (m[2] === "unarchive") t.archived_at = null;
      t.revision = (t.revision as number) + 1;
      return this.json(t);
    }
    if (path === "workspaces") return this.json(this.workspace(`ws_${this.workspaces.length + 1}`, String(body.title)), 201);
    m = path.match(/^workspaces\/([^/]+)\/threads$/);
    if (m) return this.json(this.thread(`thread_${this.threads.length + 1}`, m[1], String(body.title)), 201);
    m = path.match(/^threads\/([^/]+)\/messages$/);
    if (m) { const t = this.threads.find((x) => x.id === m![1])!; if (t.archived_at !== null) return this.problem(409, "thread_archived", t); t.revision = (t.revision as number) + 1; return this.json(this.message(`msg_${this.messages.length + 1}`, m[1], String(body.role ?? "user"), String(body.content), this.messages.length + 1), 201); }
    m = path.match(/^threads\/([^/]+)\/runs$/);
    if (m) { const t = this.threads.find((x) => x.id === m![1])!; if (t.archived_at !== null) return this.problem(409, "thread_archived", t); const r = this.run(`run_${this.runs.length + 1}`, m[1], "queued"); t.active_run_id = r.id; t.revision = (t.revision as number) + 1; return this.json(r, 201); }
    m = path.match(/^runs\/([^/]+)\/(pause|resume|cancel|retry)$/);
    if (m) { const r = this.runs.find((x) => x.id === m![1])!; r.state = { pause: "paused", resume: "running", cancel: "canceled", retry: "queued" }[m[2]]!; r.revision = (r.revision as number) + 1; return this.json(r); }
    // A source gate is answered on its own route, and the answer is the gate:
    // the client refuses one that names a different gate, run or attempt.
    m = path.match(/^source-intents\/([^/]+)\/resolve$/);
    if (m) {
      const gateId = m[1];
      for (const projection of Object.values(this.research)) {
        const gate = ((projection.source_gates as Row[] | undefined) ?? []).find((g) => g.id === gateId);
        if (!gate) continue;
        const held = gate.decision as Row | null;
        if (held) Object.assign(held, { state: "resolved", resolution: { choice: body.choice, actor_id: "local-operator" }, revision: (held.revision as number) + 1, resolved_at: now });
        Object.assign(gate, { state: "resolved", revision: (gate.revision as number) + 1, updated_at: now });
        return this.json(gate);
      }
      return this.problem(404, "not_found");
    }
    // Opening a research item's conversation: it reuses the linked thread when
    // there is one, starts no run, and refuses a stale project revision.
    m = path.match(/^research-items\/([^/]+)\/thread$/);
    if (m) {
      const item = this.researchItems.find((x) => x.id === m![1]);
      if (!item) return this.problem(404, "not_found");
      const ws = this.workspaces.find((x) => x.id === body.workspace_id);
      if (!ws) return this.problem(404, "not_found");
      if (body.expected_revision !== ws.revision) return this.problem(409, "revision_conflict", ws);
      const existing = this.threads.find((t) => t.id === item.thread_id);
      if (existing) return this.json(existing);
      const created = this.thread(`thread_${this.threads.length + 1}`, String(ws.id), String(item.title));
      item.thread_id = created.id;
      return this.json(created, 201);
    }
    m = path.match(/^decisions\/([^/]+)\/resolve$/);
    if (m) { const d = this.decisions.find((x) => x.id === m![1])!; Object.assign(d, { state: "resolved", resolution: { choice: body.choice }, revision: (d.revision as number) + 1, resolved_at: now }); return this.json(d); }
    // The real client posts only `payload` and `note`: approval is a second
    // command against the staged row, never a flag on the create.
    if (path === "captures") { const c = { id: `capture_${this.captures.length + 1}`, capture_key: String(body.payload), payload: String(body.payload), kind: /^https?:/.test(String(body.payload)) ? "url" : "text", note: String(body.note ?? ""), state: "pending", known_source_id: null, consumed_source_ids: null, failure_category: null, blocked_by: null, available_source_id: null, payload_note: null, revision: 0, created_at: now, updated_at: now }; this.captures.push(c); return this.json(c, 201); }
    m = path.match(/^captures\/([^/]+)\/(approve|dismiss|reopen)$/);
    if (m) { const c = this.captures.find((x) => x.id === m![1])!; c.state = { approve: "approved", dismiss: "dismissed", reopen: "pending" }[m[2]]!; c.revision = (c.revision as number) + 1; return this.json(c); }
    m = path.match(/^sources\/([^/]+)\/recommendations\/import$/);
    if (m) return this.importRecommendations(m[1], body);
    m = path.match(/^sources\/([^/]+)\/recommendations\/([^/]+)\/link$/);
    if (m) {
      const recommendation = ((this.xhsNotes[m[1]]?.recommendations as Row[] | undefined) ?? []).find((r) => r.id === m![2]);
      if (!recommendation) return this.problem(404, "not_found");
      if (body.expected_revision !== recommendation.revision) return this.problem(409, "revision_conflict", recommendation);
      Object.assign(recommendation, { url: body.url, url_state: "operator_set", url_checked_title: null, revision: (recommendation.revision as number) + 1, updated_at: now });
      // As in Control, a changed link moves the note's revision.
      const note = this.xhsNotes[m[1]]!;
      note.revision = (note.revision as number) + 1;
      return this.json({ recommendation });
    }
    m = path.match(/^sources\/([^/]+)\/images\/([0-9]+)\/retry$/);
    if (m) {
      const note = this.xhsNotes[m[1]];
      const image = ((note?.images as Row[] | undefined) ?? []).find((i) => i.ordinal === Number(m![2]));
      if (!note || !image) return this.problem(404, "not_found");
      if (body.expected_revision !== note.revision) return this.problem(409, "revision_conflict", noteHeader(note));
      if (image.download_state === "failed") Object.assign(image, { download_state: "pending", download_error: null });
      else if (image.ocr_state === "failed") Object.assign(image, { ocr_state: "pending", ocr_error: null });
      else return this.problem(409, "invalid_transition");
      Object.assign(note, { state: "detail_ok", revision: (note.revision as number) + 1 });
      return this.json({ note: noteHeader(note), image });
    }
    return this.problem(404, "not_found");
  }

  // Each recommendation gets its own disposition, as Control's import gives:
  // a paper is staged as a pending Capture (an open one of the same payload is
  // reused), a blog with a link is queued, and anything else is refused.
  private importRecommendations(noteSourceId: string, body: Row): Response {
    const note = this.xhsNotes[noteSourceId];
    if (!note) return this.problem(404, "not_found");
    if (body.expected_revision !== note.revision) return this.problem(409, "revision_conflict", noteHeader(note));
    const items = (body.recommendation_ids as string[]).map((id) => {
      const recommendation = (note.recommendations as Row[]).find((r) => r.id === id) ?? null;
      const item = (disposition: string, reason: string | null, capture: Row | null = null) => ({
        recommendation_id: id, disposition, reason, capture_id: capture?.id ?? null,
        capture_revision: capture?.revision ?? null, capture_state: capture?.state ?? null, recommendation,
      });
      if (!recommendation) return item("refused", "not_found");
      if (recommendation.import_state !== "none" && recommendation.import_state !== "failed") return item("refused", "already_imported");
      if (recommendation.kind === "paper") {
        if (!recommendation.arxiv_id) return item("refused", "no_arxiv_id");
        const payload = `https://arxiv.org/abs/${String(recommendation.arxiv_id)}`;
        const open = this.captures.find((c) => c.payload === payload && ["pending", "approved", "claimed"].includes(String(c.state)));
        const capture = open ?? this.capture(`capture_${this.captures.length + 1}`, payload, { note: `Recommended in XHS note ${String(note.title)} · image ${String(recommendation.image_ordinal)}` });
        Object.assign(recommendation, { capture_id: capture.id, capture_state: capture.state, capture_revision: capture.revision, import_state: "staged", revision: (recommendation.revision as number) + 1 });
        return item(open ? "capture_reused" : "capture_staged", null, capture);
      }
      if (recommendation.kind === "blog") {
        if (!recommendation.url) return item("refused", "no_url");
        Object.assign(recommendation, { import_state: "importing", revision: (recommendation.revision as number) + 1 });
        return item("blog_import_queued", null);
      }
      return item("refused", "not_importable");
    });
    return this.json({ note_source_id: noteSourceId, items });
  }
}
