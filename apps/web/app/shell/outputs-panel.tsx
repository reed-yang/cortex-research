"use client";

import { ChevronDownIcon } from "lucide-react";
import { ResearchWorkflowView } from "../control/research-workflow";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { copy } from "./copy";
import type { ViewProps } from "./types";

// The verified output of the selected run. The panel exists only when the run
// has a research projection; its count and chevron expose content and state.
export function OutputsPanel({ state, client }: ViewProps) {
  const research = state.research;
  if (!research) return null;
  return (
    <Collapsible aria-label={copy.thread.outputs} key={research.run.id} className="shrink-0 border-b px-4 py-2">
      <CollapsibleTrigger aria-description={`${research.artifacts.length} saved outputs`} className="group flex min-h-11 w-full items-center gap-2 rounded-md px-2 text-sm font-medium hover:bg-muted data-[state=open]:bg-muted focus-visible:outline-2 focus-visible:outline-ring">
        <ChevronDownIcon aria-hidden="true" className="size-4 -rotate-90 transition-transform group-data-[state=open]:rotate-0 motion-reduce:transition-none" />{copy.thread.outputs}
        {research.artifacts.length > 0 ? <span aria-hidden="true" className="rounded bg-secondary px-1.5 text-xs text-secondary-foreground" data-outputs-dot>{research.artifacts.length}</span> : null}
      </CollapsibleTrigger>
      <CollapsibleContent className="max-h-[45dvh] sm:max-h-[55dvh] overflow-y-auto overscroll-contain pt-2">
        <ResearchWorkflowView client={client} offline={state.offline} research={research} />
      </CollapsibleContent>
    </Collapsible>
  );
}
