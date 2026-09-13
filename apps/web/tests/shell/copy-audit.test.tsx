import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { BANNED_WORDS, copy, decisionKindLabel, problemSentence, runStateLabel } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl, now } from "./fake-control";

afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

// Spec section 7: the shape of an identifier product chrome may never show.
// The vocabulary it may not carry is `BANNED_WORDS`, imported from the copy
// table so the guard that runs in production and the guard that audits it are
// one list. Ids are `{prefix}_{uuid4().hex}` in the store, but a fixture or a
// shortened id is the same leak, so the suffix is any word body -- plus bare
// uuids and long hashes, which carry no prefix at all.
const RAW_ID = new RegExp([
  "\\b(?:run|attempt|thr|thread|ws|workspace|cap|capture|src|source|dec|decision|msg|message|evt|event)_[A-Za-z0-9-]+",
  "\\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\b",
  "\\b[0-9a-f]{24,}\\b",
].join("|"), "i");

// Everything a person reads: the text nodes plus the attributes that carry
// words (a placeholder, an accessible name, a tooltip). The parts are joined
// with a space because `textContent` welds neighbouring nodes into one word
// and would hide a leak from the `\b` guards below.
const SPOKEN_ATTRIBUTES = ["aria-label", "placeholder", "title", "alt"];

function visibleText(container: HTMLElement): string {
  const clone = container.cloneNode(true) as HTMLElement;
  clone.querySelectorAll("[data-details]").forEach((node) => node.remove());
  const parts: string[] = [];
  const walker = clone.ownerDocument.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) parts.push(walker.currentNode.nodeValue ?? "");
  for (const name of SPOKEN_ATTRIBUTES) {
    for (const node of clone.querySelectorAll(`[${name}]`)) parts.push(node.getAttribute(name) ?? "");
  }
  return parts.join(" ");
}

function bannedWord(text: string): string | null {
  return text.match(BANNED_WORDS)?.[1] ?? null;
}

function seeded(): FakeControl {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1a2b3c4d", "ws_1", "First question", { active_run_id: "run_deadbeef01" });
  control.thread("thread_9f8e7d6c", "ws_1", "Older question", { archived_at: "2026-09-01T00:00:00Z" });
  control.message("msg_00c0ffee", "thread_1a2b3c4d", "user", "hello", 1);
  const run = control.run("run_deadbeef01", "thread_1a2b3c4d", "waiting_for_decision", { active_attempt_id: "attempt_abcdef01" });
  control.run("run_facefeed02", "thread_1a2b3c4d", "failed", { created_at: "2026-09-05T12:00:00Z" });
  control.event("event_1", "run_facefeed02", { type: "run.failed", payload: { failure_category: "revision_conflict" } });
  control.decision("decision_00ff00ff", String(run.id), "Allow the import?", [
    { id: "approve_once", label: "Approve once" },
    { id: "deny", label: "Deny", tone: "danger" },
  ], { attempt_id: "attempt_abcdef01" });
  control.capture("capture_0badc0de", "https://example.com/echo");
  control.source("source_11aabbcc", "arxiv:2401.12345", "Memory in long-horizon agents");
  // A research item whose every identity -- the item, its origin, its document
  // version and its digest -- is one the audit would catch if it reached a
  // screen outside a disclosure.
  const idea = control.researchItem("aaa1", "idea", "Memory decay in long-horizon agents", {
    status: "awaiting_human",
    pause_reason: "Waiting on your answer about scope.",
    history: [{ kind: "round", label: "Round 1", text: "Framed the question.", created_at: now }],
  });
  control.researchDocument(idea, "rd_1", "Dossier", "# Decay\n\nA retained finding.\n");
  // The key list the real `/health` reports, plus one this app has never seen.
  control.health = {
    api_version: "v1",
    capabilities: {
      control_store: true, event_replay: true, event_stream: true, source_resolution: false,
      artifact_metadata: true, research_pipeline: true, runtime_dispatch: true,
      telegram_adapter: false, durable_operation_deduplication: true,
    },
    runtime_dispatch_enabled: false,
  };
  return control;
}

async function open(view: "research" | "library" | "inbox" | "status"): Promise<void> {
  const user = userEvent.setup();
  // The Inbox entry carries a pending-decision badge, so the name is matched
  // from its start rather than whole.
  const label = {
    research: copy.sidebar.research, library: copy.sidebar.library,
    inbox: copy.sidebar.inbox, status: copy.sidebar.status,
  }[view];
  await user.click(screen.getAllByRole("button", { name: new RegExp(`^${label}`) })[0]!);
}

describe("shell copy audit", () => {
  it("shows no Control vocabulary or raw ids outside details", async () => {
    const control = seeded();
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1a2b3c4d");
    const { container } = render(<Shell client={control.client()} />);
    await screen.findByText(copy.strip.waiting);
    // Run history is collapsed by default, and its rows are where a failed
    // run's own category would otherwise reach the screen.
    await userEvent.setup().click(screen.getByRole("button", { name: copy.thread.runs }));
    await screen.findByText(new RegExp(`^${runStateLabel("failed")} · `));

    // Status included: it speaks of dispatch and of the engine, but the words
    // this guard bans have no place there either.
    const check = (view: string) => {
      const text = visibleText(container);
      expect(bannedWord(text), `${view}: banned word`).toBeNull();
      expect(text.match(RAW_ID)?.[0] ?? null, `${view}: raw id`).toBeNull();
    };

    check("thread");
    for (const view of ["research", "library", "inbox", "status"] as const) {
      await open(view);
      await screen.findByRole("heading", { name: copy[view].title });
      check(view);
    }

    // The dossier too: it is the one screen that holds an item's original
    // identity, its document version and that version's digest.
    await open("research");
    await userEvent.setup().click(await screen.findByRole("button", { name: /^Memory decay in long-horizon agents/ }));
    await screen.findByRole("heading", { level: 3, name: "Memory decay in long-horizon agents" });
    await screen.findByText(copy.research.history);
    check("dossier");
  });

  it("never spells a response it could not read, and keeps the wire message under details", async () => {
    const control = seeded();
    // A row this client cannot decode: the listing fails inside the contract
    // decoder, whose message names the field it rejected.
    control.sources[0]!.revision = "one";
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1a2b3c4d&view=library");
    const { container } = render(<Shell client={control.client()} />);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(copy.errors.unreadable)).toBeTruthy();
    await waitFor(() => expect(bannedWord(visibleText(container))).toBeNull());
    expect(container.querySelector("[data-details]")?.textContent ?? "").toContain("revision");
  });

  it("keeps the copy table itself clear of the vocabulary it exists to replace", () => {
    const spoken: string[] = [];
    const walk = (value: unknown): void => {
      if (typeof value === "string") { spoken.push(value); return; }
      if (value && typeof value === "object") for (const entry of Object.values(value)) walk(entry);
    };
    // `copy.details` is rendered only inside a disclosure, which is the one
    // place a code and Control's own nouns are allowed.
    for (const [group, value] of Object.entries(copy)) if (group !== "details") walk(value);
    for (const category of ["revision_conflict", "machine_thread", "machine_workspace", "machine_run",
      "thread_active_run", "thread_archived", "thread_has_no_user_message", "managed_worker_unavailable", "already_captured",
      "pause_unsupported", "worker_unavailable", "not_found"]) spoken.push(problemSentence(category));
    for (const state of ["queued", "starting", "running", "paused", "waiting_for_decision", "resuming",
      "retrying", "completed", "failed", "canceled", "cancel_requested", "pause_requested", "wedged_somehow"]) {
      spoken.push(runStateLabel(state));
    }
    for (const kind of ["approval", "source_conflict", "source_confirmation", "budget_release"]) {
      spoken.push(decisionKindLabel(kind));
    }
    for (const sentence of spoken) {
      expect(bannedWord(sentence), sentence).toBeNull();
      expect(sentence).not.toMatch(RAW_ID);
    }
  });
});

describe("the guards the audit runs on", () => {
  it("bans the vocabulary spec section 7 names, in the forms it is written in", () => {
    for (const word of [
      "rev", "Rev", "revision", "Revisions", "CAS", "compare-and-swap", "DTO", "replay", "replayed",
      "replaying", "cursor", "cursors", "deduplicated", "deduplication", "dedupe", "idempotency",
      "idempotent", "Control API", "durable", "durability",
    ]) {
      expect(bannedWord(`Cortex says ${word} here.`), word).not.toBeNull();
    }
  });

  it("leaves the ordinary words that merely start the same alone", () => {
    for (const word of [
      "review", "Review", "reviewed", "reverse", "revert", "revised", "broadcast", "cast", "casting",
      "recursor", "endurable", "duration", "replaying".slice(2), "curse",
    ]) {
      expect(bannedWord(`Cortex says ${word} here.`), word).toBeNull();
    }
  });

  it("sees an identifier however short its body is, and no ordinary phrase", () => {
    for (const id of [
      "ws_1", "thread_1", "run_abc", "run_deadbeef01", "thr_9f8e", "cap_2", "src_11aabbcc",
      "dec_00ff00ff", "msg_1", "evt_7", "workspace_1", "attempt_abcdef01",
      "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "a".repeat(24),
    ]) {
      expect(`Cortex says ${id} here.`.match(RAW_ID)?.[0] ?? null, id).not.toBeNull();
    }
    for (const phrase of [
      "Load older", "runs and threads", "a run started", "the workspace", "Capture", "message input",
      "source kind", "decision", "Cancel reopen", "Working…",
    ]) {
      expect(phrase.match(RAW_ID)?.[0] ?? null, phrase).toBeNull();
    }
  });
});
