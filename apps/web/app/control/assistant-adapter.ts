import type { ThreadMessageLike } from "@assistant-ui/react";
import { copy } from "../shell/copy";
import { ControlNetworkError, ControlProblemError, type CortexControlClient, type PreparedMutation } from "./client";
import type { Message, Run, Thread } from "./contracts";

export function persistedAssistantMessages(messages: Message[]): ThreadMessageLike[] {
  return messages
    .filter((message) => message.role !== "system")
    .map((message) => ({
      id: message.id,
      role: message.role as "user" | "assistant",
      content: [{ type: "text" as const, text: message.content }],
      createdAt: new Date(message.created_at),
    }));
}

// P8: what a message on a thread with an active run becomes depends on that
// run's state, which the API decides the same way (FOLLOWUP_RUN_STATES): in
// flight, the message joins it; waiting on the operator, the message is kept
// and the run is left standing for the decision. V-5: and on the dispatch
// gate -- under a closed gate the API submits nothing, so a queued run is not
// "answered after that turn"; the message is kept for when dispatch is enabled.
// N-2: a gate the daemon does not report (`null`: no bridge or an unbound
// worker, V6-4; or health could not be read) is neither, and gets its own
// sentence rather than the promise.
const FOLLOWUP_RUN_STATES = new Set(["queued", "retrying", "starting", "running"]);

async function activeRunNotice(client: CortexControlClient, runId: string): Promise<string> {
  let state: string;
  try {
    state = (await client.getRun(runId)).state;
  } catch {
    return "The thread already has an active run; the message is kept with it.";
  }
  if (FOLLOWUP_RUN_STATES.has(state)) {
    let gate: boolean | null;
    try {
      gate = await client.getRuntimeDispatchGate();
    } catch {
      gate = null;
    }
    if (gate === null) return "The thread already has an active run; this daemon cannot say whether runtime dispatch is enabled, so the message is saved and may wait.";
    if (gate === false) return "The thread already has an active run and runtime dispatch is disabled; the message is saved and the run will run when dispatch is enabled.";
    return "The thread already has an active run; this message joins it and is answered after that turn.";
  }
  if (state === "waiting_for_decision") return "The thread's run is waiting for your decision on the run card; the message is kept and no turn was started.";
  return `The thread's run is ${state}; the message is kept and no turn was started.`;
}

// P8 V-R2 / N-3: `machine_thread` is permanent -- the thread belongs to the
// research engine and no button clears it -- so the copy names the owner,
// promises nothing, and points at no button.
// ⟦A-4⟧ The sentence is exported so the run card says the same thing in the
// same words; the composer adds what happened to the message it just sent,
// which is true there and false on a button.
// The sentence itself lives in the copy table (spec section 7: one file); the
// name stays here because the run card and the strip already import it.
export const ENGINE_THREAD_SENTENCE = copy.engine.thread;
const ENGINE_THREAD_NOTICE = `${ENGINE_THREAD_SENTENCE} Your message was saved.`;

// ⟦ADJ-G-3 / ADJ-H-3⟧ The other half of the same fact, for a run on a carrier
// thread that is the OPERATOR's. It is THREAD-scoped, not run-state-scoped:
// no new run may be started on an engine thread whatever that run is doing,
// so the card says it for a running run as much as for a terminal one -- and
// a terminal one, whose buttons are rightly gone, would otherwise explain
// nothing at all.
export const ENGINE_THREAD_NO_NEW_RUN_SENTENCE = copy.engine.noNewRun;

// V-2: one sentence for the one refusal a fresh thread can meet, shared by the
// run card's notice and the composer's own answer.
export const NO_TURN_TO_DRIVE_NOTICE =
  "This thread has nothing to answer yet. Send a message first, then the run has something to work on.";

// P8: a composer turn is two durable commands, never one combined endpoint --
// the message is appended first (it is captured whether or not anything will
// answer it), then a run is created from the thread revision that append
// produced, so the run's expected_revision is the durable one and not a guess.
// A thread that already has an active run gets no second run: the message
// joins the run in flight, which is the same rule the turn bridge applies.
// ADJ-1: engine ownership is decided BEFORE that short-circuit. A carrier
// thread's run is the engine's, and the API stores the message without ever
// driving it there, so "joins it and is answered after that turn" would be
// false; the thread projection says whose the thread is, and no run is asked
// for on the engine's.
async function startRun(
  client: CortexControlClient,
  threadId: string,
  onRunCreated: ((run: Run) => void) | undefined,
): Promise<string> {
  let fresh: Thread;
  try {
    fresh = await client.getThread(threadId);
  } catch {
    return "No run was started: the thread could not be re-read after the message committed.";
  }
  if (fresh.engine_owned) return ENGINE_THREAD_NOTICE;
  if (fresh.active_run_id) return activeRunNotice(client, fresh.active_run_id);
  try {
    const result = await client.prepareCreateRun(fresh).execute();
    onRunCreated?.(result.value);
    return `Run ${result.value.id} started.`;
  } catch (error) {
    if (error instanceof ControlProblemError) {
      // The API's own answer, for a thread that became the engine's between
      // the two reads.
      if (error.problem.category === "machine_thread") return ENGINE_THREAD_NOTICE;
      // ⟦ADJ-E⟧ Its own sentence, not the run card's. The card's copy tells the
      // operator to send a message; here they just did, and saying both in one
      // breath contradicts itself. Reachable only if the message the composer
      // appended two commands ago vanished between the two reads.
      if (error.problem.category === "thread_has_no_user_message") {
        return "The run was refused: the thread had nothing to answer when it was asked. Your message was saved; use Start research run on the run card.";
      }
      return `The run could not be started: ${error.problem.category}. The message is saved; use Create run on the run card once that is cleared.`;
    }
    if (error instanceof ControlNetworkError) return "The run start has an unknown delivery outcome; refresh the thread before retrying.";
    throw error;
  }
}

export type TurnHooks = {
  onCommitted: (message: Message, replayed: boolean) => void;
  onRunCreated?: (run: Run) => void;
  prepareContent?: (content: string) => string;
  // The class of the failure, not only that there was one: a caller that keeps
  // its own retry bookkeeping keeps a network refusal and drops a problem
  // refusal, and only the caller owns that distinction.
  onAppendFailed?: (error: unknown) => void;
};

// The two-command durable turn (append, then run) as one call whose result is
// the sentence to show. Throws only on programming errors; Control refusals
// and network ambiguity come back as text.
// A caller that retains its own durable command -- a retry path whose point is
// to replay the exact command an uncertain send already carried -- passes it as
// `prepared`; everyone else lets the turn prepare one from the content the
// hooks shape.
export async function submitDurableTurn(
  client: CortexControlClient,
  thread: Thread,
  draft: string,
  hooks: TurnHooks,
  prepared?: PreparedMutation<Message>,
): Promise<string> {
  const command = prepared ?? client.prepareAppendMessage(thread, hooks.prepareContent?.(draft) ?? draft);
  let result;
  try {
    result = await command.execute();
  } catch (error) {
    hooks.onAppendFailed?.(error);
    if (error instanceof ControlProblemError) return `Message was not saved: ${error.problem.category}.`;
    if (error instanceof ControlNetworkError) return "Message delivery is unconfirmed. Retry the same message to recover its receipt.";
    throw error;
  }
  hooks.onCommitted(result.value, result.replayed);
  return startRun(client, thread.id, hooks.onRunCreated);
}
