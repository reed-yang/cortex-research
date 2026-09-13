import { useExternalStoreRuntime, type AppendMessage, type AssistantRuntime, type ThreadMessageLike } from "@assistant-ui/react";
import { useEffect, useMemo, useRef } from "react";
import { persistedAssistantMessages, submitDurableTurn } from "../control/assistant-adapter";
import { ControlNetworkError } from "../control/client";
import { conversationContent, type ConversationMode } from "../control/research-mode";
import { buildThreadListAdapter } from "./thread-list-adapter";
import type { ViewProps } from "./types";

// The run states in which Cortex is still working on the turn, so the composer
// shows the stop affordance instead of the send one.
export const IN_FLIGHT_RUN_STATES = new Set(["queued", "retrying", "starting", "running", "resuming"]);

// The store's converter is compared by identity: a fresh arrow per render
// resets the runtime's conversion cache and rebuilds every message object, so
// the identity has to be stable for the life of the module.
const asThreadMessage = (message: ThreadMessageLike): ThreadMessageLike => message;

export function draftText(message: AppendMessage): string {
  return message.content.filter((part) => part.type === "text").map((part) => (part.type === "text" ? part.text : "")).join(" ").trim();
}

export function useCortexThreadRuntime({ state, actions, client, mode, persistedMode, onSent }: ViewProps & { mode: ConversationMode; persistedMode: ConversationMode; onSent: () => void }): AssistantRuntime {
  // `onNew` outlives the render that built it, so it reads the open thread and
  // the selected mode from a ref rather than from the closure it was made in.
  // The ref is refreshed after the commit, never during render: a submission
  // can only follow one.
  const latest = useRef({ state, actions, mode, persistedMode });
  useEffect(() => { latest.current = { state, actions, mode, persistedMode }; });
  const messages = useMemo<ThreadMessageLike[]>(() => persistedAssistantMessages(state.messages), [state.messages]);
  const thread = state.thread;
  const isRunning = state.run !== null && state.run.thread_id === thread?.id && IN_FLIGHT_RUN_STATES.has(state.run.state);
  // Two different refusals: a thread nothing may be added to at all (none open,
  // the engine's, or archived) disables the input; a passing condition
  // (disconnected, another command in flight) only blocks the send.
  const isDisabled = !thread || thread.engine_owned || thread.archived_at !== null;
  const isSendDisabled = state.offline || state.commandPending || isDisabled;
  return useExternalStoreRuntime({
    messages,
    isDisabled,
    isRunning,
    isSendDisabled,
    // 0.15 fills in message status and metadata only for stores that convert.
    convertMessage: asThreadMessage,
    adapters: { threadList: buildThreadListAdapter(state, actions) },
    // Stop is the composer's face of the run command the strip's Cancel
    // issues; without it the registry renders the button permanently disabled.
    onCancel: async () => { await latest.current.actions.runAction("cancel"); },
    onNew: async (message) => {
      const { state: current, actions: act, mode: m, persistedMode: pm } = latest.current;
      const openThread = current.thread;
      if (!openThread) return;
      const text = draftText(message);
      if (!text) return;
      // An uncertain send retains its exact durable command even if the mode
      // selector resets before the operator retries the same draft; a retry is
      // the same thread and the same text, and anything else is a new message.
      // The retained body wins on purpose: the mode prefix the first attempt
      // carried is part of the message Control may already hold.
      // The slot lives in the state hook, not here: this view is unmounted and
      // rebuilt by an ordinary switch to Library or Status, while the sentence
      // telling the operator to retry survives it.
      const retry = act.takeRetainedTurn(openThread.id, text);
      const command = retry ?? client.prepareAppendMessage(openThread, conversationContent(text, m, pm));
      const outcome = await submitDurableTurn(client, openThread, text, {
        onCommitted: (committed, replayed) => {
          act.retainTurn(openThread.id, null, text);
          act.messageCommitted(committed, replayed);
        },
        onRunCreated: act.runCreated,
        // Only an unconfirmed delivery leaves something to replay: a refusal
        // is an answer, and the command that met it is spent.
        onAppendFailed: (error) => {
          act.retainTurn(openThread.id, error instanceof ControlNetworkError ? command : null, text);
        },
      }, command);
      act.turnOutcome(outcome);
      onSent();
    },
  });
}
