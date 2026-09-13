"use client";

import { AssistantRuntimeProvider, useAuiState } from "@assistant-ui/react";
import { createContext, useCallback, useContext, useMemo, useState, type FC } from "react";
import { Thread, type ThreadComponents } from "@/components/assistant-ui/elements/thread.aui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  explicitConversationMode,
  persistedConversationMode,
  type ConversationMode,
} from "../control/research-mode";
import { ComposerMode } from "./composer-mode";
import { copy } from "./copy";
import { DecisionCards } from "./decision-card";
import { OutputsPanel } from "./outputs-panel";
import { RunHistory } from "./run-history";
import { StatusStrip } from "./status-strip";
import { useCortexThreadRuntime } from "./thread-runtime";
import type { ViewProps } from "./types";

// The slots the registry Thread renders live under its own provider, so they
// read the open thread through a context instead of through props: a component
// identity that changed every render would remount the strip on every keystroke.
type ThreadSlots = { view: ViewProps; mode: ConversationMode; onSelectMode: (mode: ConversationMode) => void; disabled: boolean };

const SlotContext = createContext<ThreadSlots | null>(null);

function useSlots(): ThreadSlots {
  const slots = useContext(SlotContext);
  if (!slots) throw new Error("The thread slots are only rendered inside the thread view");
  return slots;
}

const ComposerHeaderSlot: FC = () => {
  const { view } = useSlots();
  return (
    <div className="flex flex-col gap-2">
      <DecisionCards {...view} />
      <StatusStrip {...view} />
    </div>
  );
};

const ComposerLeadingSlot: FC = () => {
  const { disabled, mode, onSelectMode } = useSlots();
  // A draft that opens with `/research` or `/chat` decides its own mode, and
  // the segment says so before the message is sent.
  const draft = useAuiState((state) => state.composer.text);
  return <ComposerMode disabled={disabled} mode={explicitConversationMode(draft) ?? mode} onChange={onSelectMode} />;
};

const THREAD_COMPONENTS: ThreadComponents = { ComposerHeader: ComposerHeaderSlot, ComposerLeading: ComposerLeadingSlot };

export function ThreadView(props: ViewProps) {
  const { state, actions } = props;
  const thread = state.thread;
  const persistedMode = useMemo(() => persistedConversationMode(state.messages), [state.messages]);
  // The selection belongs to the thread it was made on; opening another thread
  // falls back to what that thread's own messages last said.
  const [selection, setSelection] = useState<{ threadId: string; mode: ConversationMode } | null>(null);
  const mode = selection && selection.threadId === thread?.id ? selection.mode : persistedMode;
  const onSelectMode = useCallback((next: ConversationMode) => {
    setSelection(thread ? { threadId: thread.id, mode: next } : null);
  }, [thread]);
  const onSent = useCallback(() => setSelection(null), []);
  const runtime = useCortexThreadRuntime({ ...props, mode, persistedMode, onSent });
  const disabled = state.offline || !thread || thread.engine_owned || thread.archived_at !== null;
  const slots = useMemo<ThreadSlots>(() => ({ view: props, mode, onSelectMode, disabled }), [disabled, mode, onSelectMode, props]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <SlotContext.Provider value={slots}>
        <section aria-label={copy.thread.region} className="flex min-h-0 flex-1 flex-col bg-background text-foreground">
          <header className="flex h-12 items-center gap-2 border-b px-4 ps-14 lg:ps-4">
            <h1 className="truncate text-sm font-medium">{thread?.title ?? copy.thread.untitled}</h1>
            {thread?.archived_at ? <Badge variant="outline">{copy.thread.archived}</Badge> : null}
            {thread?.archived_at
              ? <Button className="ms-auto" disabled={state.commandPending} onClick={() => { void actions.unarchiveThread(thread); }} size="sm" variant="outline">{copy.thread.unarchive}</Button>
              : null}
          </header>
          {thread ? (
            <>
              <RunHistory {...props} />
              <OutputsPanel {...props} />
              <div className="min-h-0 flex-1">
                <Thread
                  components={THREAD_COMPONENTS}
                  composerPlaceholder={mode === "research" ? copy.thread.placeholderResearch : copy.thread.placeholderChat}
                />
              </div>
            </>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 p-6 text-sm">
              <p>{copy.thread.noThread}</p>
              <Button onClick={() => { void actions.createThread(copy.sidebar.newThread); }}>{copy.sidebar.newThread}</Button>
            </div>
          )}
        </section>
      </SlotContext.Provider>
    </AssistantRuntimeProvider>
  );
}
