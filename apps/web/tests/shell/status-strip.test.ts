import { describe, expect, it } from "vitest";
import { BANNED_WORDS, humanCategory } from "../../app/shell/copy";
import { deriveStrip, humanOutcome, runFailureCategory } from "../../app/shell/status-strip";
import type { Run, RunEvent, Thread } from "../../app/control/contracts";
import type { ReplayState } from "../../app/control/event-replay";
import type { StripActionName } from "../../app/shell/status-strip";

const now = "2026-09-06T12:00:00Z";

const thread: Thread = {
  id: "thread_1", workspace_id: "ws_1", title: "First question", status: "idle",
  active_run_id: null, archived_at: null, engine_owned: false, revision: 0, created_at: now, updated_at: now,
};

function run(state: string, extra: Partial<Run> = {}): Run {
  return {
    id: "run_1", thread_id: "thread_1", state, active_attempt_id: null, stage: null,
    latest_sequence: 0, engine_owned: false, revision: 0, created_at: now, updated_at: now, ...extra,
  };
}

type Row = {
  name: string;
  run: Run | null;
  dispatchGate: boolean | null;
  replayState: ReplayState;
  thread: Thread | null;
  offline?: boolean;
  failureCategory?: string | null;
  text: string | null;
  actions: StripActionName[];
};

const archived: Thread = { ...thread, archived_at: "2026-09-01T00:00:00Z" };

const ROWS: Row[] = [
  { name: "a connectivity drop says so even before the poller notices", run: run("running"), dispatchGate: true, replayState: "idle", thread, offline: true, text: "Offline. Reconnecting…", actions: [] },
  // The thread header owns Unarchive, so the line explains and offers nothing.
  { name: "an archived thread says how to continue", run: null, dispatchGate: true, replayState: "idle", thread: archived, text: "This thread is archived. Unarchive it to continue.", actions: [] },
  { name: "offline wins over an archived thread", run: null, dispatchGate: true, replayState: "idle", thread: archived, offline: true, text: "Offline. Reconnecting…", actions: [] },
  { name: "a failed run names its category when one is known", run: run("failed"), dispatchGate: true, replayState: "idle", thread, failureCategory: "worker_unavailable", text: "The last turn failed: Worker unavailable.", actions: ["retry"] },
  { name: "a failed run whose category is Control's own vocabulary says what to do instead", run: run("failed"), dispatchGate: true, replayState: "idle", thread, failureCategory: "revision_conflict", text: "The last turn failed. Someone else changed this first. It has been refreshed; try again.", actions: ["retry"] },
  { name: "offline wins over a running run", run: run("running"), dispatchGate: true, replayState: "offline", thread, text: "Offline. Reconnecting…", actions: [] },
  { name: "reconnecting wins over a failed run", run: run("failed"), dispatchGate: true, replayState: "reconnecting", thread, text: "Offline. Reconnecting…", actions: [] },
  { name: "no thread open", run: run("running"), dispatchGate: true, replayState: "idle", thread: null, text: null, actions: [] },
  { name: "no run on the open thread", run: null, dispatchGate: true, replayState: "idle", thread, text: null, actions: [] },
  { name: "a run belonging to another thread is ignored", run: run("running", { thread_id: "thread_2" }), dispatchGate: true, replayState: "idle", thread, text: null, actions: [] },
  { name: "queued under an open gate", run: run("queued"), dispatchGate: true, replayState: "idle", thread, text: "Starting…", actions: ["cancel"] },
  { name: "starting under an open gate", run: run("starting"), dispatchGate: true, replayState: "idle", thread, text: "Starting…", actions: ["cancel"] },
  { name: "queued under a closed gate", run: run("queued"), dispatchGate: false, replayState: "idle", thread, text: "Saved. Runtime dispatch is off, so nothing will answer until it is enabled.", actions: ["cancel"] },
  { name: "starting under an unreported gate", run: run("starting"), dispatchGate: null, replayState: "idle", thread, text: "Saved. This daemon does not report whether dispatch is enabled.", actions: ["cancel"] },
  { name: "queued and the engine's", run: run("queued", { engine_owned: true }), dispatchGate: false, replayState: "idle", thread, text: "Saved. Runtime dispatch is off, so nothing will answer until it is enabled.", actions: [] },
  { name: "running", run: run("running"), dispatchGate: true, replayState: "idle", thread, text: "Working…", actions: ["pause", "cancel"] },
  { name: "running and the engine's", run: run("running", { engine_owned: true }), dispatchGate: true, replayState: "idle", thread, text: "Working…", actions: [] },
  { name: "paused", run: run("paused"), dispatchGate: true, replayState: "idle", thread, text: "Paused", actions: ["resume", "cancel"] },
  { name: "paused and the engine's", run: run("paused", { engine_owned: true }), dispatchGate: true, replayState: "idle", thread, text: "Paused", actions: [] },
  { name: "waiting for a decision", run: run("waiting_for_decision"), dispatchGate: true, replayState: "idle", thread, text: "Waiting for your decision", actions: ["cancel"] },
  { name: "resuming", run: run("resuming"), dispatchGate: true, replayState: "idle", thread, text: "Resuming…", actions: ["cancel"] },
  { name: "retrying", run: run("retrying"), dispatchGate: true, replayState: "idle", thread, text: "Resuming…", actions: ["cancel"] },
  { name: "failed", run: run("failed"), dispatchGate: true, replayState: "idle", thread, text: "The last turn failed.", actions: ["retry"] },
  { name: "failed and the engine's", run: run("failed", { engine_owned: true }), dispatchGate: true, replayState: "idle", thread, text: "The last turn failed.", actions: [] },
  { name: "completed", run: run("completed"), dispatchGate: true, replayState: "idle", thread, text: null, actions: [] },
  { name: "canceled", run: run("canceled"), dispatchGate: true, replayState: "idle", thread, text: null, actions: [] },
];

describe("deriveStrip", () => {
  it.each(ROWS)("$name", (row) => {
    const strip = deriveStrip({ thread: row.thread, run: row.run, dispatchGate: row.dispatchGate, replayState: row.replayState, offline: row.offline ?? false, failureCategory: row.failureCategory ?? null, lastTurnOutcome: null });
    expect(strip.text).toBe(row.text);
    expect(strip.actions).toEqual(row.actions);
  });

  it("falls back to the last turn's own answer when no run is driving", () => {
    const outcome = "Run run_1 started.";
    const spoken = { text: "Your message was sent and a run started.", tone: "muted", actions: [], details: outcome };
    expect(deriveStrip({ thread, run: null, dispatchGate: true, replayState: "idle", offline: false, failureCategory: null, lastTurnOutcome: outcome })).toEqual(spoken);
    expect(deriveStrip({ thread, run: run("completed"), dispatchGate: true, replayState: "idle", offline: false, failureCategory: null, lastTurnOutcome: outcome }).text).toBe(spoken.text);
  });

  it("keeps the failed run's stage and category as details and never as the line", () => {
    const strip = deriveStrip({ thread, run: run("failed", { stage: "compose" }), dispatchGate: true, replayState: "idle", offline: false, failureCategory: "worker_unavailable", lastTurnOutcome: null });
    expect(strip).toEqual({ text: "The last turn failed: Worker unavailable.", tone: "danger", actions: ["retry"], details: "Stage compose. Category worker_unavailable." });
  });
});

describe("humanOutcome", () => {
  const OUTCOMES = [
    "Run run_1 started.",
    "Message was not saved: revision_conflict.",
    "Message was not saved: worker_unavailable.",
    "The run could not be started: thread_active_run. The message is saved; use Create run on the run card once that is cleared.",
    "The run was refused: the thread had nothing to answer when it was asked. Your message was saved; use Start research run on the run card.",
    "The thread's run is waiting for your decision on the run card; the message is kept and no turn was started.",
    "This thread belongs to the research engine; its runs are created by the engine only. Your message was saved.",
    "Message delivery is unconfirmed. Retry the same message to recover its receipt.",
  ];

  it.each(OUTCOMES)("says %s without an id, a code or a button that is not there", (outcome) => {
    const spoken = humanOutcome(outcome);
    expect(spoken.text).not.toMatch(/\b(?:run|thread|attempt|msg|decision|capture|ws)_[0-9a-zA-Z-]+/);
    expect(spoken.text).not.toMatch(BANNED_WORDS);
    expect(spoken.text).not.toContain("run card");
    expect(spoken.text.length).toBeGreaterThan(0);
  });

  it("names what happened and keeps the wire wording under details", () => {
    expect(humanOutcome("Run run_1 started.")).toEqual({ text: "Your message was sent and a run started.", details: "Run run_1 started." });
    const conflict = humanOutcome("Message was not saved: revision_conflict.");
    expect(conflict.text).toContain("Someone else changed this first. It has been refreshed; try again.");
    expect(conflict.text).not.toMatch(/revision/i);
    expect(conflict.details).toBe("Message was not saved: revision_conflict.");
    expect(humanOutcome("Message was not saved: worker_unavailable.").text).toBe("Your message was not sent: Worker unavailable.");
    expect(humanOutcome("The run could not be started: thread_active_run. The message is saved; use Create run on the run card once that is cleared.").text)
      .toBe("Your message was saved, but no run started. Finish or cancel the running turn first.");
    // A code made of banned words is never spelled out; the code stays in details.
    expect(humanCategory("revision_conflict")).toBeNull();
    expect(humanCategory("worker_unavailable")).toBe("Worker unavailable");
    // A sentence that was already the operator's keeps its own words.
    expect(humanOutcome("Message delivery is unconfirmed. Retry the same message to recover its receipt."))
      .toEqual({ text: "Message delivery is unconfirmed. Retry the same message to recover its receipt.", details: null });
  });
});

describe("runFailureCategory", () => {
  const event = (overrides: Partial<RunEvent>): RunEvent => ({
    cursor: "cursor_1", schema_version: 1, id: "event_1", run_id: "run_1", attempt_id: null,
    sequence: 1, type: "run.failed", occurred_at: now, causation_id: null, durability: "durable",
    payload: { failure_category: "worker_unavailable" }, ...overrides,
  });

  it("reads the newest failure the run itself reported", () => {
    const events = [event({}), event({ id: "event_2", sequence: 2, payload: { failure_category: "provider_error" } })];
    expect(runFailureCategory(events, "run_1")).toBe("provider_error");
  });

  it("ignores another run's failure and a run with nothing to say", () => {
    expect(runFailureCategory([event({ run_id: "run_2" })], "run_1")).toBeNull();
    expect(runFailureCategory([event({ type: "run.started", payload: { category: "chat" } })], "run_1")).toBeNull();
    expect(runFailureCategory([], "run_1")).toBeNull();
    expect(runFailureCategory([event({})], null)).toBeNull();
  });
});
