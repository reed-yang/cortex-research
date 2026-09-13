import type { ExternalStoreThreadData, ExternalStoreThreadListAdapter } from "@assistant-ui/react";
import type { Thread } from "../control/contracts";
import type { ControlActions, ControlState } from "./types";

// 0.15.18 declares no `lastMessageAt` on the item, but the runtime spreads every
// key it is given onto the thread-list state, and that is the date the list groups
// by (Today / Yesterday / Earlier). Widening the item type is what carries it.
type ListItem<S extends "regular" | "archived"> = ExternalStoreThreadData<S> & { lastMessageAt: Date };

function item<S extends "regular" | "archived">(thread: Thread, status: S): ListItem<S> {
  return { id: thread.id, status, title: thread.title, lastMessageAt: new Date(thread.updated_at) };
}

// One adapter serves both the sidebar list and the thread runtime, so a thread
// selected, renamed or archived in either place travels the same commands.
export function buildThreadListAdapter(state: ControlState, actions: ControlActions): ExternalStoreThreadListAdapter {
  const byId = new Map<string, Thread>(
    [...state.threads, ...state.archivedThreads].map((thread) => [thread.id, thread]),
  );
  // Last activity leads; the list itself carries no dates, so this order is
  // what the reader sees.
  const newest = (threads: Thread[]) => [...threads].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return {
    threadId: state.thread?.id,
    isLoading: state.loading,
    threads: newest(state.threads).map((thread) => item(thread, "regular")),
    archivedThreads: newest(state.archivedThreads).map((thread) => item(thread, "archived")),
    onSwitchToThread: (threadId) => actions.selectThread(threadId),
    onSwitchToNewThread: async () => {
      await actions.createThread("New thread");
    },
    onRename: async (threadId, title) => {
      const thread = byId.get(threadId);
      if (thread) await actions.renameThread(thread, title);
    },
    onArchive: async (threadId) => {
      const thread = byId.get(threadId);
      if (thread) await actions.archiveThread(thread);
    },
    onUnarchive: async (threadId) => {
      const thread = byId.get(threadId);
      if (thread) await actions.unarchiveThread(thread);
    },
  };
}
