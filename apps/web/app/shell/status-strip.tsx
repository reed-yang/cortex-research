"use client";

import { ENGINE_THREAD_NO_NEW_RUN_SENTENCE, ENGINE_THREAD_SENTENCE } from "../control/assistant-adapter";
import type { JsonValue, Run, RunEvent, Thread } from "../control/contracts";
import type { ReplayState } from "../control/event-replay";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { BANNED_WORDS, copy, refusalText } from "./copy";
import type { RunActionName, ViewProps } from "./types";

// The strip offers run commands only: an archived thread is unarchived from
// the thread header, which owns that button.
export type StripActionName = RunActionName;

export type StripState = { text: string | null; tone: "muted" | "info" | "warning" | "danger"; actions: StripActionName[]; details: string | null };

// The shape of an identifier product chrome may not carry (spec section 7);
// it and the copy table's `BANNED_WORDS` are both consulted before any wire
// sentence reaches the screen.
const RAW_ID = /\b(?:run|thread|attempt|msg|decision|capture|ws)_[0-9a-zA-Z-]+/;

function record(value: JsonValue): { [key: string]: JsonValue } | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : null;
}

// Why a run failed, as its own events reported it. The run projection carries
// only the stage it stopped at, so the category -- when the daemon sent one --
// is read from the newest failure event of that run; a run whose events are
// not loaded simply has no category to show.
export function runFailureCategory(events: RunEvent[], runId: string | null): string | null {
  if (!runId) return null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    if (event.run_id !== runId) continue;
    const payload = record(event.payload);
    if (!payload) continue;
    const failure = payload.failure_category ?? (/fail|error/i.test(event.type) ? payload.category ?? record(payload.error ?? null)?.category : undefined);
    if (typeof failure === "string" && failure.length > 0) return failure;
  }
  return null;
}

// The turn's own answer is written for the operator, not copied from the wire:
// the id and the refusal code belong under Details, the sentence above it says
// what happened.
const RUN_STARTED = /^Run \S+ started\.$/;
const NOT_SAVED = /^Message was not saved: ([a-z_]+)\.$/;
const RUN_REFUSED = /^The run could not be started: ([a-z_]+)\./;
const CARD_PHRASES: Array<[RegExp, string]> = [
  [/ Your message was saved; use Start research run on the run card\./, " Your message was saved."],
  [/ on the run card/, ""],
];
export function humanOutcome(outcome: string): { text: string; details: string | null } {
  if (RUN_STARTED.test(outcome)) return { text: copy.strip.turnStarted, details: outcome };
  const notSaved = NOT_SAVED.exec(outcome);
  if (notSaved) return { text: refusalText(copy.leads.messageNotSent, notSaved[1]!), details: outcome };
  const refused = RUN_REFUSED.exec(outcome);
  if (refused) return { text: refusalText(copy.leads.messageSavedNoRun, refused[1]!), details: outcome };
  let text = outcome;
  for (const [phrase, replacement] of CARD_PHRASES) text = text.replace(phrase, replacement);
  if (RAW_ID.test(text) || BANNED_WORDS.test(text)) return { text: copy.strip.unspokenOutcome, details: outcome };
  return { text, details: text === outcome ? null : outcome };
}

function lastTurn(outcome: string | null): StripState {
  if (!outcome) return { text: null, tone: "muted", actions: [], details: null };
  const spoken = humanOutcome(outcome);
  return { text: spoken.text, tone: "muted", actions: [], details: spoken.details };
}

// What the one line above the composer says, and which buttons stand beside
// it. A pure function of the row state, so the sentence for every combination
// is decided in one place and can be read as a table.
export function deriveStrip(input: { thread: Thread | null; run: Run | null; dispatchGate: boolean | null; replayState: ReplayState; offline: boolean; failureCategory: string | null; lastTurnOutcome: string | null }): StripState {
  const { thread, run, dispatchGate, replayState } = input;
  // Both signals mean the same thing to the operator, and the send gate uses
  // the connectivity one, so the sentence follows whichever says it first.
  if (input.offline || replayState === "offline" || replayState === "reconnecting") return { text: copy.strip.offline, tone: "warning", actions: [], details: null };
  if (thread?.archived_at) return { text: copy.strip.archivedThread, tone: "warning", actions: [], details: null };
  if (!thread || !run || run.thread_id !== thread.id) return lastTurn(input.lastTurnOutcome);
  const engines = run.engine_owned;
  const act = (...names: RunActionName[]) => (engines ? [] : names);
  switch (run.state) {
    case "queued":
    case "starting":
      if (dispatchGate === false) return { text: copy.strip.gateClosed, tone: "warning", actions: act("cancel"), details: null };
      if (dispatchGate === null) return { text: copy.strip.gateUnknown, tone: "warning", actions: act("cancel"), details: null };
      return { text: copy.strip.starting, tone: "info", actions: act("cancel"), details: null };
    case "running": return { text: copy.strip.working, tone: "info", actions: act("pause", "cancel"), details: null };
    case "paused": return { text: copy.strip.paused, tone: "muted", actions: act("resume", "cancel"), details: null };
    case "waiting_for_decision": return { text: copy.strip.waiting, tone: "warning", actions: act("cancel"), details: null };
    case "resuming":
    case "retrying": return { text: copy.strip.resuming, tone: "info", actions: act("cancel"), details: null };
    case "failed": {
      const category = input.failureCategory;
      const details = [run.stage ? copy.details.stage(run.stage) : null, category ? copy.details.category(category) : null].filter((part) => part !== null).join(" ");
      return {
        text: category ? refusalText(copy.leads.turnFailed, category) : copy.strip.failed,
        tone: "danger",
        actions: act("retry"),
        details: details.length > 0 ? details : null,
      };
    }
    default: return lastTurn(input.lastTurnOutcome);
  }
}

const ACTION_LABEL: Record<StripActionName, string> = { pause: copy.strip.pause, resume: copy.strip.resume, cancel: copy.strip.cancel, retry: copy.strip.retry };

const TONE_CLASS: Record<StripState["tone"], string> = {
  muted: "text-muted-foreground",
  info: "text-foreground",
  warning: "text-foreground",
  danger: "text-destructive",
};

// ⟦ADJ-G-3⟧ The two halves of one fact about a thread the research engine
// owns: whose runs it takes, and that no new run may be started on it. The
// second is said for the operator's own run on that thread, which stays
// theirs to pause or cancel -- so the sentence replaces the state text and
// leaves the buttons the run itself allows.
function engineText(state: ViewProps["state"]): string | null {
  const thread = state.thread;
  if (!thread?.engine_owned) return null;
  const run = state.run && state.run.thread_id === thread.id ? state.run : null;
  return run && !run.engine_owned ? ENGINE_THREAD_NO_NEW_RUN_SENTENCE : ENGINE_THREAD_SENTENCE;
}

export function StatusStrip({ state, actions }: ViewProps) {
  const strip = deriveStrip({
    thread: state.thread,
    run: state.run,
    dispatchGate: state.dispatchGate,
    replayState: state.replayState,
    offline: state.offline,
    failureCategory: runFailureCategory(state.events, state.run?.id ?? null),
    lastTurnOutcome: state.lastTurnOutcome,
  });
  const quiet = state.offline || state.replayState === "offline" || state.replayState === "reconnecting" || state.thread?.archived_at !== null;
  const engine = quiet ? null : engineText(state);
  const text = engine ?? strip.text;
  // The line is a live region, so it is mounted before it has anything to say:
  // a region that arrives together with its first sentence is not announced.
  return (
    <div aria-live="polite" className="flex flex-wrap items-center gap-2 px-2 text-sm" role="status">
      {text === null ? null : <span className={cn("min-w-0 flex-1 truncate", TONE_CLASS[strip.tone])}>{text}</span>}
      {text !== null && strip.details ? (
        <details data-details>
          <summary className="cursor-pointer text-xs text-muted-foreground">{copy.strip.details}</summary>
          <p className="text-xs text-muted-foreground">{strip.details}</p>
        </details>
      ) : null}
      {strip.actions.map((action) => (
        <Button
          className="min-h-11 min-w-11 lg:min-h-6 lg:min-w-0"
          disabled={state.commandPending || state.offline}
          key={action}
          onClick={() => { void actions.runAction(action); }}
          size="xs"
          variant={action === "cancel" ? "destructive" : "outline"}
        >
          {ACTION_LABEL[action]}
        </Button>
      ))}
    </div>
  );
}
