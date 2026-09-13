"use client";

import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type ExternalStoreThreadListAdapter,
  type ThreadMessage,
} from "@assistant-ui/react";
import {
  ActivityIcon,
  ArchiveRestoreIcon,
  BookOpenIcon,
  ChevronRightIcon,
  FlaskConicalIcon,
  InboxIcon,
  MenuIcon,
} from "lucide-react";
import { useMemo, useState } from "react";
import { ThreadList } from "@/components/assistant-ui/elements/thread-list.aui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { copy, label } from "./copy";
import { ProjectSwitcher } from "./project-switcher";
import { buildThreadListAdapter } from "./thread-list-adapter";
import type { ShellView, ViewProps } from "./types";

const ENTRIES: Array<[ShellView, string, typeof BookOpenIcon]> = [
  ["research", copy.sidebar.research, FlaskConicalIcon],
  ["library", copy.sidebar.library, BookOpenIcon],
  ["inbox", copy.sidebar.inbox, InboxIcon],
  ["status", copy.sidebar.status, ActivityIcon],
];

// The sidebar runtime carries no conversation, so its message list is a fixed
// empty array: a fresh one every render would restate the thread on each pass.
const NO_MESSAGES: ThreadMessage[] = [];
const NO_TURN = async () => {};

// The archived group renders from the adapter's own items, so it stays in the
// same order as the live list and reuses the same commands.
function ArchivedThreads({ adapter }: { adapter: ExternalStoreThreadListAdapter }) {
  const archived = adapter.archivedThreads ?? [];
  if (archived.length === 0) return null;
  return (
    <Collapsible className="mt-2 border-t pt-2">
      <CollapsibleTrigger asChild>
        <Button className="group h-8 w-full justify-start gap-2 px-2.5 font-normal min-h-11 lg:min-h-8" variant="ghost">
          <ChevronRightIcon className="size-4 transition-transform group-data-[state=open]:rotate-90" />
          {copy.sidebar.archived}
          <span className="ms-auto text-xs text-muted-foreground">{archived.length}</span>
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-0.5 pt-0.5">
        {archived.map((entry) => (
          <div className="flex h-8 min-h-11 items-center rounded-md hover:bg-muted lg:min-h-8" key={entry.id}>
            <button
              className="min-w-0 flex-1 self-stretch truncate rounded-md px-2.5 text-start text-sm outline-none focus-visible:ring-1 focus-visible:ring-ring/50"
              onClick={() => void adapter.onSwitchToThread?.(entry.id)}
              type="button"
            >
              {entry.title}
            </button>
            <Button
              aria-label={label.unarchiveThread(entry.title ?? copy.sidebar.untitledThread)}
              className="me-1.5 size-6 min-h-11 min-w-11 lg:min-h-6 lg:min-w-6"
              onClick={() => void adapter.onUnarchive?.(entry.id)}
              size="icon-sm"
              variant="ghost"
            >
              <ArchiveRestoreIcon className="size-3.5" />
            </Button>
          </div>
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}

// `onClose` is supplied only by the drawer instance: it both dismisses the
// drawer after a navigation and reserves room for the drawer's close control.
function SidebarBody({ state, actions, client, onClose }: ViewProps & { onClose?: () => void }) {
  const adapter = useMemo(() => {
    const base = buildThreadListAdapter(state, actions);
    if (!onClose) return base;
    return {
      ...base,
      onSwitchToThread: async (threadId: string) => {
        await base.onSwitchToThread?.(threadId);
        onClose();
      },
      onSwitchToNewThread: async () => {
        await base.onSwitchToNewThread?.();
        onClose();
      },
    };
    // The hook hands back one memoised state object, so this rebuilds exactly
    // when Cortex reports something new: a render that only opens or closes the
    // drawer leaves the thread-list store alone.
  }, [state, actions, onClose]);

  // The registry ThreadList reads the thread list from the nearest runtime; the
  // sidebar owns a runtime whose only job is the list (messages stay empty here).
  const runtime = useExternalStoreRuntime({
    messages: NO_MESSAGES,
    onNew: NO_TURN,
    adapters: { threadList: adapter },
  });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex h-full flex-col gap-2 p-2">
        {/* The switcher's own trigger is sized from here rather than inside it,
            because on a phone it has to clear the drawer's close control. */}
        <div className={cn("[&_[data-slot=dropdown-menu-trigger]]:min-h-11 lg:[&_[data-slot=dropdown-menu-trigger]]:min-h-8", onClose && "pe-14")}>
          <ProjectSwitcher actions={actions} client={client} onProjectSwitched={onClose} state={state} />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <ThreadList />
          <ArchivedThreads adapter={adapter} />
        </div>
        <nav aria-label={copy.sidebar.global} className="flex flex-col gap-0.5 border-t pt-2">
          {ENTRIES.map(([view, label, Icon]) => (
            <Button
              aria-current={state.view === view ? "page" : undefined}
              className="min-h-11 justify-start gap-2 lg:min-h-8"
              key={view}
              onClick={() => { actions.setView(view); onClose?.(); }}
              variant={state.view === view ? "secondary" : "ghost"}
            >
              <Icon className="size-4" />
              {label}
              {view === "inbox" && state.pendingDecisions.length > 0 ? (
                <Badge className="ms-auto" variant="secondary">{state.pendingDecisions.length}</Badge>
              ) : null}
            </Button>
          ))}
        </nav>
      </div>
    </AssistantRuntimeProvider>
  );
}

export function Sidebar(props: ViewProps) {
  const [open, setOpen] = useState(false);
  const close = useMemo(() => () => setOpen(false), []);
  return (
    <>
      {/* Legacy page styling still paints the body, so the panel states its own
          surface rather than inheriting one. */}
      <aside
        aria-label={copy.sidebar.navigation}
        className="hidden w-[260px] shrink-0 border-r bg-sidebar text-sidebar-foreground lg:block"
      >
        <SidebarBody {...props} />
      </aside>
      <Sheet onOpenChange={setOpen} open={open}>
        <SheetTrigger asChild>
          <Button
            aria-label={copy.sidebar.openNavigation}
            className="fixed start-2 top-2 z-20 min-h-11 min-w-11 lg:hidden"
            size="icon"
            variant="ghost"
          >
            <MenuIcon className="size-5" />
          </Button>
        </SheetTrigger>
        {/* The variant's own width is written with the `data-[side=left]` prefix, so
            only a width written the same way replaces it. */}
        <SheetContent
          className="bg-sidebar text-sidebar-foreground data-[side=left]:w-[300px] data-[side=left]:sm:max-w-[300px] [&_[data-slot=sheet-close]]:min-h-11 [&_[data-slot=sheet-close]]:min-w-11"
          side="left"
        >
          <SheetTitle className="sr-only">{copy.sidebar.navigation}</SheetTitle>
          <SidebarBody {...props} onClose={close} />
        </SheetContent>
      </Sheet>
    </>
  );
}
