"use client";

import { ChevronDownIcon } from "lucide-react";
import type { Run } from "../control/contracts";
import { copy, humanCategory, runStateLabel } from "./copy";
import { runFailureCategory } from "./status-strip";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { ViewProps } from "./types";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

// A run's age in the words a person uses for it; an unreadable timestamp is
// shown as nothing rather than as a wrong age.
export function relativeTime(iso: string, from: number = Date.now()): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const elapsed = Math.max(0, from - at);
  if (elapsed < MINUTE) return "just now";
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)} min ago`;
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)} h ago`;
  return `${Math.floor(elapsed / DAY)} d ago`;
}

// State, when it started, and for a failed run why -- when its own events said
// so. Started time is both the sort key and the one shown, so a resumed old run
// cannot read as the newest.
export function rowLabel(run: Run, failureCategory: string | null): string {
  const parts = [runStateLabel(run.state), relativeTime(run.created_at)];
  const phrase = run.state === "failed" && failureCategory ? humanCategory(failureCategory) : null;
  if (phrase) parts.push(phrase);
  return parts.join(" · ");
}

function newestFirst(runs: Run[]): Run[] {
  return [...runs].sort((left, right) =>
    right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id));
}

export function RunHistory({ state, actions }: ViewProps) {
  if (state.runs.length === 0) return null;
  return (
    <Collapsible aria-label={copy.thread.runHistory} className="border-b px-4 py-2">
      <CollapsibleTrigger className="flex w-full items-center gap-1 text-sm font-medium">
        <ChevronDownIcon className="size-4" />{copy.thread.runs}
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-0.5 pt-2">
        {newestFirst(state.runs).map((run) => (
          <Button
            aria-pressed={run.id === state.selectedRunId}
            className={cn("justify-start", run.id === state.selectedRunId && "bg-muted")}
            key={run.id}
            onClick={() => actions.selectRun(run.id)}
            size="sm"
            variant="ghost"
          >
            {rowLabel(run, runFailureCategory(state.events, run.id))}
          </Button>
        ))}
        {state.nextRunCursor ? (
          <Button className="justify-start" onClick={() => actions.loadOlderRuns()} size="sm" variant="ghost">{copy.thread.loadOlder}</Button>
        ) : null}
      </CollapsibleContent>
    </Collapsible>
  );
}
