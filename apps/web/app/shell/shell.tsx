"use client";

import { XIcon } from "lucide-react";
import { useMemo } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { CortexControlClient } from "../control/client";
import { copy } from "./copy";
import { Sidebar } from "./sidebar";
import { ThreadView } from "./thread-view";
import { LibraryView } from "./library-view";
import { ResearchView } from "./research-view";
import { InboxView } from "./inbox-view";
import { StatusView } from "./status-view";
import type { Notice } from "./types";
import { useControlState } from "./use-control-state";

const NOTICE_TONE: Record<Notice["tone"], string> = {
  success: "text-foreground",
  warning: "text-foreground",
  error: "text-destructive",
};

// What the last command did, above whichever view the operator is in. A
// refusal is an alert because it interrupts what they were doing; the rest is
// a status line. The code a refusal was filed under stays closed under
// Details, as every other error surface in the shell does.
function NoticeBar({ notice, onDismiss }: { notice: Notice; onDismiss: () => void }) {
  return (
    <div
      aria-label={copy.notice.region}
      className="flex flex-wrap items-center gap-2 border-b px-4 py-2 text-sm"
      role={notice.tone === "error" ? "alert" : "status"}
    >
      <span className={cn("min-w-0 flex-1", NOTICE_TONE[notice.tone])}>{notice.text}</span>
      {notice.details ? (
        <details className="text-xs text-muted-foreground" data-details>
          <summary className="cursor-pointer">{copy.strip.details}</summary>
          <p>{notice.details}</p>
        </details>
      ) : null}
      <Button aria-label={copy.notice.dismiss} className="min-h-11 min-w-11 lg:min-h-7 lg:min-w-7" onClick={onDismiss} size="icon-sm" variant="ghost">
        <XIcon className="size-3.5" />
      </Button>
    </div>
  );
}

// Spec section 11: Cortex could not be read at all. The shell has no state to
// show behind this, so it replaces the view rather than sitting above it, and
// the retry re-reads everything from the projects down.
function Unavailable({ detail, onRetry, reloading }: { detail: string; onRetry: () => void; reloading: boolean }) {
  return (
    <section
      aria-label={copy.errors.unavailable}
      className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 p-6 text-sm"
      role="alert"
    >
      <p>{copy.errors.unavailable}</p>
      <details className="text-xs text-muted-foreground" data-details>
        <summary className="cursor-pointer">{copy.strip.details}</summary>
        <p>{detail}</p>
      </details>
      <Button disabled={reloading} onClick={onRetry}>{copy.strip.retry}</Button>
    </section>
  );
}

export function Shell({ client: supplied }: { client?: CortexControlClient }) {
  const client = useMemo(() => supplied ?? new CortexControlClient(), [supplied]);
  const [state, actions] = useControlState(client);
  const props = { state, actions, client };
  return (
    <div className="flex h-dvh w-full bg-background text-foreground">
      <Sidebar {...props} />
      {/* Not a live region itself: the status strip and the notice bar mark
          the two lines that actually change, and a live wrapper around the
          whole view would re-announce all of it for either of them. */}
      <main className="flex min-w-0 flex-1 flex-col">
        {state.fatalError !== null ? (
          <Unavailable detail={state.fatalError} onRetry={actions.retry} reloading={state.loading} />
        ) : (
          <>
            {state.notice ? <NoticeBar notice={state.notice} onDismiss={actions.dismissNotice} /> : null}
            {state.view === "thread" ? <ThreadView {...props} /> : null}
            {state.view === "research" ? <ResearchView {...props} /> : null}
            {state.view === "library" ? <LibraryView {...props} /> : null}
            {state.view === "inbox" ? <InboxView {...props} /> : null}
            {state.view === "status" ? <StatusView {...props} /> : null}
          </>
        )}
      </main>
    </div>
  );
}
