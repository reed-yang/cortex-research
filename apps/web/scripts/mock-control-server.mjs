// A read-only stand-in for the Control daemon, seeded with one world that is
// wide enough for every shell surface: two projects (one of them empty), a
// live thread with a pending decision, a failed thread, an archived thread,
// three adopted papers, one blog, one XHS note that recommends them, two
// captures and two saved ideas. Every response is a fixed literal, so a
// screenshot taken against it is the same bytes on every machine. The note,
// its blogger, its captions and its images are invented and drawn in code.

import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { encodeRgbPng } from "./screenshot-contract.mjs";

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

// The notes carry what the Library's Preview has to show: inline math, one
// figure the stored copy holds and one it does not.
const NOTES_TEXT = "# Echo-Infinity\n\nEvolving memory keeps a long video coherent by writing only what the\nreconstruction error says is new.\n\n- Preserve causal temporal updates.\n- Measure memory drift before the context is scaled.\n\n" +
  String.raw`The write gate opens when $e_t = \lVert x_t - \hat{x}_t \rVert_2 > \tau$.` +
  "\n\n![Memory drift by layer](assets/drift.png)\n\n![Ablation grid](assets/ablation.png)\n";
const NOTES_SHA256 = "6e8031bcf0c777116bcfa6fdd65cba846ebb637412e43a876fffb9b06bf50ccd";

// A small bar chart drawn in code, so the figure is the same bytes everywhere
// and no binary fixture is committed.
function driftFigure() {
  const width = 480;
  const height = 200;
  const pixels = Buffer.alloc(width * height * 3);
  const fill = (x0, y0, w, h, color) => {
    for (let y = y0; y < y0 + h; y += 1) {
      for (let x = x0; x < x0 + w; x += 1) pixels.set(color, (y * width + x) * 3);
    }
  };
  fill(0, 0, width, height, [246, 244, 238]);
  fill(40, 20, 2, 160, [60, 68, 64]);
  fill(40, 178, 420, 2, [60, 68, 64]);
  [38, 64, 90, 118, 132, 150].forEach((value, index) => {
    fill(64 + index * 66, 178 - value, 40, value, index % 2 ? [53, 105, 88] : [197, 108, 38]);
  });
  return encodeRgbPng({ width, height, pixels });
}

const SOURCE_FIGURES = new Map([["assets/drift.png", driftFigure()]]);

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

// The note and the blog were added the day before the papers, so the papers
// stay at the top of the newest-first list the screenshots open.
const dayBefore = "2026-07-22T18:00:00Z";
const NOTE_ID = "0000000000000000000000c1";
const BLOGGER_ID = "00000000000000000000c0c1";
const BLOG_URL = "https://blog.example.org/memory-drift";
const blogSource = { ...source("src_blog_drift", "url", createHash("sha256").update(BLOG_URL).digest("hex"), "blog", "Memory drift, measured layer by layer", "imported", []), created_at: dayBefore };
const noteSource = { ...source("src_note_weekly", "xhs", NOTE_ID, "xhs_note", "本周记忆论文 Weekly memory papers", "imported", []), created_at: dayBefore };

const sources = [
  source("src_echo", "arxiv", "2606.04527", "paper", "Echo-Infinity: evolving memory for long video", "imported", ["Echo-Infinity"]),
  source("src_lingbot", "arxiv", "2607.07675", "paper", "LingBot-World: a supplied model page", "imported", ["LingBot"]),
  source("src_helios", "doi", "10.5555/helios-14b", "paper", "Helios-14B: a bounded memory controller", "pending", []),
  blogSource,
  noteSource,
];
const sourcesById = new Map(sources.map((item) => [item.id, item]));

// One carousel slide: a title bar and three text bars on a tinted card, so
// the note detail scene has images to show without a real screenshot.
function slideFigure(accent) {
  const width = 270;
  const height = 360;
  const pixels = Buffer.alloc(width * height * 3);
  const fill = (x0, y0, w, h, color) => {
    for (let y = y0; y < y0 + h; y += 1) {
      for (let x = x0; x < x0 + w; x += 1) pixels.set(color, (y * width + x) * 3);
    }
  };
  fill(0, 0, width, height, [250, 247, 240]);
  fill(18, 24, 234, 44, accent);
  [96, 136, 176].forEach((top, index) => fill(18, top, 234 - index * 48, 14, [96, 104, 100]));
  fill(18, 228, 234, 104, [232, 226, 214]);
  return encodeRgbPng({ width, height, pixels });
}

const NOTE_IMAGES = [
  { ordinal: 1, asset_name: "1-00000000c0c1.png", bytes: slideFigure([197, 108, 38]) },
  { ordinal: 2, asset_name: "2-00000000c0c2.png", bytes: slideFigure([53, 105, 88]) },
];
const NOTE_FIGURES = new Map(NOTE_IMAGES.map((image) => [`assets/${image.asset_name}`, image.bytes]));

const noteRecommendations = [
  {
    id: "xhs_rec_echo", image_ordinal: 1, kind: "paper", title: "Echo-Infinity: evolving memory for long video",
    quote: "Echo-Infinity (arXiv 2606.04527): memory that only writes what is new", arxiv_id: "2606.04527",
    url: null, url_state: "none", url_checked_title: null, origin: "rule+model", identify_run: "xhs_task_identify_c1",
    capture_id: null, capture_state: null, capture_revision: null, import_state: "imported",
    imported_source_id: "src_echo", imported_source_kind: "paper", revision: 2, created_at: dayBefore, updated_at: dayBefore,
  },
  {
    id: "xhs_rec_blog", image_ordinal: 2, kind: "blog", title: "Memory drift, measured layer by layer",
    quote: "博客：Memory drift, measured layer by layer", arxiv_id: null, url: BLOG_URL, url_state: "auto_matched",
    url_checked_title: "Memory drift, measured layer by layer | Example blog", origin: "model", identify_run: "xhs_task_identify_c1",
    capture_id: null, capture_state: null, capture_revision: null, import_state: "imported",
    imported_source_id: blogSource.id, imported_source_kind: "blog", revision: 2, created_at: dayBefore, updated_at: dayBefore,
  },
  {
    id: "xhs_rec_other", image_ordinal: null, kind: "other", title: "A weekly reading group",
    quote: "每周读书会 weekly reading group", arxiv_id: null, url: null, url_state: "none", url_checked_title: null,
    origin: "model", identify_run: "xhs_task_identify_c1", capture_id: null, capture_state: null, capture_revision: null,
    import_state: "none", imported_source_id: null, imported_source_kind: null, revision: 0, created_at: dayBefore, updated_at: dayBefore,
  },
];

const noteProjection = {
  source_id: noteSource.id, note_id: NOTE_ID, title: noteSource.official_title, state: "saved", last_error: null,
  content_version: 1, revision: 5,
  blogger: { user_id: BLOGGER_ID, name: "Synthetic Curator 合成", role: "curator" },
  permalink: `https://www.xiaohongshu.com/explore/${NOTE_ID}`,
  published_at: "2026-07-22T09:00:00Z",
  caption: "Two memory papers and one blog post this week.\n本周两篇记忆论文和一篇博客。",
  caption_complete: true,
  images: NOTE_IMAGES.map((image) => ({
    ordinal: image.ordinal, asset_path: `assets/${image.asset_name}`, media_type: "image/png", width: 270, height: 360,
    download_state: "ok", download_error: null, ocr_state: "ok", ocr_error: null, ocr_engine: "deepseek-ocr-2", ocr_flags: [],
  })),
  recommendations: noteRecommendations,
};

function linkEntry(item, imageOrdinal, recommendationId) {
  return { source_id: item.id, source_kind: item.source_kind, title: item.official_title, image_ordinal: imageOrdinal, recommendation_id: recommendationId, created_at: dayBefore };
}

const sourceLinks = new Map([
  [noteSource.id, { recommended_in: [], recommends: [linkEntry(sourcesById.get("src_echo"), 1, "xhs_rec_echo"), linkEntry(blogSource, 2, "xhs_rec_blog")] }],
  ["src_echo", { recommended_in: [linkEntry(noteSource, 1, "xhs_rec_echo")], recommends: [] }],
  [blogSource.id, { recommended_in: [linkEntry(noteSource, 2, "xhs_rec_blog")], recommends: [] }],
]);

const xhsStatus = {
  enabled: true, enabled_in_config: true, roots_ready: true, refusal: null,
  roots: { "xhs-notes": "ready", blogs: "ready" },
  schedules: {
    "xhs-pull": { enabled: true, revision: 1, interval_seconds: 86400, next_due_at: "2026-07-24T06:00:00Z", last_outcome: "ran" },
    "xhs-drain": { enabled: true, revision: 1, interval_seconds: 300, next_due_at: "2026-07-23T12:05:00Z", last_outcome: "ran" },
  },
  bloggers: [
    { user_id: BLOGGER_ID, display_name: "Synthetic Curator 合成", role: "curator", followed: true, last_scan_at: "2026-07-23T06:00:00Z", last_scan_outcome: "ok", last_scan_error: null, last_new_note_at: "2026-07-23T06:00:00Z" },
    { user_id: "00000000000000000000c0c2", display_name: "Quiet Author", role: "author", followed: true, last_scan_at: "2026-07-23T06:01:00Z", last_scan_outcome: "no_new_notes", last_scan_error: null, last_new_note_at: null },
    { user_id: "00000000000000000000c0c3", display_name: "Throttled Curator", role: "curator", followed: true, last_scan_at: "2026-07-23T06:02:00Z", last_scan_outcome: "failed", last_scan_error: "rate_limited", last_new_note_at: null },
  ],
  tasks: { canceled: 0, done: 14, failed: 1, pending: 2, running: 0 },
  usage: { gpt: { calls: 3, cap: 300 }, ocr: { calls: 4, cap: 1000 }, tikhub: { calls: 5, cap: 100 } },
  last_failures: { tikhub: "rate_limited", cdn: null, ocr: null, gpt: null, blog: null },
};

// The stored files of the note and the blog, in the layout Control saves them
// (cortex_platform/product/xhs/layout.py), by `${source_id}:${kind}`.
const NOTE_MD = `# ${noteSource.official_title}\n\n- Blogger: Synthetic Curator 合成 (curator)\n- Published: 2026-07-22T09:00:00Z\n- Permalink: <https://www.xiaohongshu.com/explore/${NOTE_ID}>\n- Images: 2\n\n## Caption\n\n${noteProjection.caption}\n\n## Recommendations\n\n### 1. Echo-Infinity: evolving memory for long video\n\nIdentified (auto): paper, from the image 1\n\n> Echo-Infinity (arXiv 2606.04527): memory that only writes what is new\n\narXiv: 2606.04527\n`;
const TRANSCRIPTION_MD = `# Transcription\n\n## Image 1\n\n![Image 1](assets/${NOTE_IMAGES[0].asset_name})\n\nEcho-Infinity (arXiv 2606.04527): memory that only writes what is new\n\n## Image 2\n\n![Image 2](assets/${NOTE_IMAGES[1].asset_name})\n\n博客：Memory drift, measured layer by layer\n`;
const ARTICLE_MD = "# Memory drift, measured layer by layer\n\nA synthetic article: drift is measured per layer before the context is scaled.\n";
const BLOG_NOTES_MD = `# Memory drift, measured layer by layer\n\n- Source: <${BLOG_URL}>\n- Article text: extracted from the page\n- Not peer-reviewed\n\n## Recommended in\n\n### ${noteSource.official_title} · image 2\n\n> 博客：Memory drift, measured layer by layer\n`;
const contentDocuments = new Map([
  [`${noteSource.id}:notes`, NOTE_MD],
  [`${noteSource.id}:full_text`, TRANSCRIPTION_MD],
  [`${blogSource.id}:full_text`, ARTICLE_MD],
  [`${blogSource.id}:notes`, BLOG_NOTES_MD],
]);

function contentPage(item, kind, text) {
  return {
    source_id: item.id, canonical_id: item.canonical_id, kind, text,
    content_sha256: createHash("sha256").update(text).digest("hex"),
    start_line: 1, end_line: text.replace(/\n$/, "").split("\n").length, next_cursor: null,
  };
}

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
    available_source_id: null,
    payload_note: null,
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

// Saved ideas, newest first as the list route answers: one sent from Telegram
// in the live thread and one typed in the Web Inbox.
const fragments = [
  {
    id: "fragment_tg", text: "Try a smaller memory window first,\nthen compare drift.", note: "",
    origin: "telegram", thread_id: thread.id, context_item_id: null, created_at: now,
  },
  {
    id: "fragment_web", text: "Measure memory drift per layer.", note: "From the reading group.",
    origin: "web", thread_id: null, context_item_id: null, created_at: earlier,
  },
];

// One readable document, so the Library's reader has something to show and the
// digest is the digest of the text actually served.
const sourceNotes = new Map(sources.filter((item) => item.source_kind === "paper").map((item) => [item.id, {
  source_id: item.id,
  canonical_id: item.canonical_id,
  kind: "notes",
  text: NOTES_TEXT,
  content_sha256: NOTES_SHA256,
  start_line: 1,
  end_line: 13,
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
    const documentRoute = /^\/api\/v1\/sources\/([^/]+)\/document$/.exec(url.pathname);
    const assetRoute = /^\/api\/v1\/sources\/([^/]+)\/asset$/.exec(url.pathname);
    const noteRoute = /^\/api\/v1\/sources\/([^/]+)\/note$/.exec(url.pathname);
    const linksRoute = /^\/api\/v1\/sources\/([^/]+)\/links$/.exec(url.pathname);
    // A blog's or a note's stored file, for the paged and the whole-document reads.
    const storedRoute = contentRoute ?? documentRoute;
    const storedKind = url.searchParams.get("kind") ?? "notes";
    const stored = storedRoute && sourcesById.has(storedRoute[1]) ? contentDocuments.get(`${storedRoute[1]}:${storedKind}`) : undefined;
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
    } else if (url.pathname === "/api/v1/sources") {
      const kind = url.searchParams.get("kind");
      send(response, 200, page(sources.filter((item) => !kind || item.source_kind === kind)));
    } else if (url.pathname === "/api/v1/xhs/status") send(response, 200, xhsStatus);
    else if (noteRoute && noteRoute[1] === noteSource.id) send(response, 200, noteProjection);
    else if (linksRoute && sourcesById.has(linksRoute[1])) send(response, 200, sourceLinks.get(linksRoute[1]) ?? { recommended_in: [], recommends: [] });
    else if (stored !== undefined) {
      const item = sourcesById.get(storedRoute[1]);
      const content = contentPage(item, storedKind, stored);
      if (contentRoute) send(response, 200, content);
      else send(response, 200, { source_id: item.id, canonical_id: item.canonical_id, kind: storedKind, text: stored, content_sha256: content.content_sha256, retained_bytes: Buffer.byteLength(stored), redacted: false });
    } else if (sourceRoute && sourcesById.has(sourceRoute[1])) send(response, 200, sourcesById.get(sourceRoute[1]));
    else if (contentRoute && sourceNotes.has(contentRoute[1]) && url.searchParams.get("kind") === "notes") {
      send(response, 200, sourceNotes.get(contentRoute[1]));
    } else if (documentRoute && sourceNotes.has(documentRoute[1]) && (url.searchParams.get("kind") ?? "notes") === "notes") {
      const notes = sourceNotes.get(documentRoute[1]);
      send(response, 200, { source_id: notes.source_id, canonical_id: notes.canonical_id, kind: "notes", text: notes.text, content_sha256: notes.content_sha256, retained_bytes: Buffer.byteLength(notes.text), redacted: false });
    } else if (assetRoute && sourcesById.has(assetRoute[1])) {
      const figure = (assetRoute[1] === noteSource.id ? NOTE_FIGURES : SOURCE_FIGURES).get(url.searchParams.get("path") ?? "");
      if (!figure) send(response, 404, { type: "urn:cortex:problem:source_asset_unavailable", title: "The figure is not available", status: 404, category: "source_asset_unavailable", retryable: false, owner: "mock-cortexd" });
      else {
        response.writeHead(200, {
          "Cache-Control": "no-store",
          "Content-Length": figure.byteLength,
          "Content-Type": "image/png",
          "Cross-Origin-Resource-Policy": "same-origin",
          "X-Content-Type-Options": "nosniff",
        });
        response.end(figure);
      }
    }
    else if (url.pathname === "/api/v1/captures") send(response, 200, page(captures));
    else if (url.pathname === "/api/v1/fragments") send(response, 200, page(fragments));
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
