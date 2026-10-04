"use client";

import { Badge } from "@/components/ui/badge";
import type { Fragment } from "../control/contracts";
import { copy } from "./copy";

// The shell is English-only and Control stores instants, so the time is shown
// in one fixed locale and UTC, like the research catalog's dates.
const TIME = new Intl.DateTimeFormat("en", {
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  month: "short",
  timeZone: "UTC",
  timeZoneName: "short",
  year: "numeric",
});

function readableTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : TIME.format(parsed);
}

// A saved idea is read-only: it has no state and offers no command. The text
// and note are the operator's own words, shown exactly as saved -- whitespace
// included -- and marked `data-verbatim` so the copy audit checks the chrome
// around them and not what the operator wrote. Ids stay under Details.
export function FragmentCard({ fragment }: { fragment: Fragment }) {
  return (
    <article
      aria-label={copy.fragment.label}
      className="flex flex-col gap-2 rounded-lg border p-3"
      data-fragment-id={fragment.id}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">
          {fragment.origin === "telegram" ? copy.fragment.fromTelegram : copy.fragment.savedHere}
        </Badge>
        <span className="text-xs text-muted-foreground">{readableTime(fragment.created_at)}</span>
      </div>
      <p className="text-sm break-words whitespace-pre-wrap" data-verbatim>{fragment.text}</p>
      {fragment.note ? (
        <p className="text-sm text-muted-foreground">
          <span>{copy.fragment.note}: </span>
          <span className="whitespace-pre-wrap" data-verbatim>{fragment.note}</span>
        </p>
      ) : null}
      <details className="text-xs text-muted-foreground" data-details>
        <summary>{copy.fragment.details}</summary>
        <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3">
          <dt>{copy.details.ideaId}</dt><dd>{fragment.id}</dd>
          {fragment.thread_id ? <><dt>{copy.details.threadId}</dt><dd>{fragment.thread_id}</dd></> : null}
          {fragment.context_item_id ? (
            <><dt>{copy.details.contextItemId}</dt><dd>{fragment.context_item_id}</dd></>
          ) : null}
          <dt>{copy.details.created}</dt><dd>{fragment.created_at}</dd>
        </dl>
      </details>
    </article>
  );
}
