"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { SourceProjection } from "../control/research-contracts";
import { copy } from "./copy";

type BadgeTone = "default" | "secondary" | "destructive" | "outline";

// The four states the Control store can report for an adopted source. Anything
// else is still shown verbatim, but never styled as if it were one of them.
const IMPORT_STATE_TONES: Record<string, BadgeTone> = {
  existing: "secondary",
  pending: "outline",
  imported: "default",
  failed: "destructive",
};

export function importStateTone(state: string): BadgeTone {
  return IMPORT_STATE_TONES[state] ?? "outline";
}

// The shell is English-only and the record carries instants, so the date is
// formatted in one fixed locale and UTC rather than the reader's machine.
const DATE = new Intl.DateTimeFormat("en", { day: "numeric", month: "short", timeZone: "UTC", year: "numeric" });

function readableDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DATE.format(parsed);
}

// Identifiers, the record's own counter and the exact instants are technical
// detail, so they live behind a closed disclosure; the record itself shows the
// title, what kind of source it is, where its import stands and readable dates.
export function SourceRecord({ detail, detailError, selectedId }: { detail: SourceProjection | null; detailError: string | null; selectedId: string | null }) {
  if (detailError) {
    return (
      <div className="flex flex-col items-start gap-2" role="alert">
        <p className="text-sm text-destructive">{copy.source.unreadable}</p>
        <details className="text-xs text-muted-foreground" data-details>
          <summary>{copy.source.details}</summary>
          <p>{detailError}</p>
        </details>
      </div>
    );
  }
  if (!detail) {
    return <p className="text-sm text-muted-foreground">{selectedId ? copy.source.loading : copy.source.pick}</p>;
  }
  return (
    <div className="flex flex-col items-start gap-2">
      <h3 className="text-base font-semibold">{detail.official_title}</h3>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="outline">{detail.source_kind}</Badge>
        <Badge variant={importStateTone(detail.import_state)}>{detail.import_state}</Badge>
        <span className="text-muted-foreground">{copy.source.added} {readableDate(detail.created_at)} · {copy.source.updated} {readableDate(detail.updated_at)}</span>
      </div>
      <Collapsible className="flex w-full flex-col items-start gap-2">
        <CollapsibleTrigger asChild>
          <Button size="sm" type="button" variant="outline">{copy.source.details}</Button>
        </CollapsibleTrigger>
        <CollapsibleContent aria-label={copy.source.details} className="flex w-full flex-col gap-3" data-details="source record" role="group">
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">{copy.details.identifier}</dt>
            <dd className="truncate font-mono text-xs">{detail.id}</dd>
            <dt className="text-muted-foreground">{copy.details.authority}</dt>
            <dd>{detail.authority}</dd>
            <dt className="text-muted-foreground">{copy.details.authorityId}</dt>
            <dd className="truncate">{detail.authority_id}</dd>
            <dt className="text-muted-foreground">{copy.details.canonicalId}</dt>
            <dd className="truncate">{detail.canonical_id}</dd>
            <dt className="text-muted-foreground">{copy.details.sourceKind}</dt>
            <dd>{detail.source_kind}</dd>
            <dt className="text-muted-foreground">{copy.details.importState}</dt>
            <dd>{detail.import_state}</dd>
            <dt className="text-muted-foreground">{copy.details.sourceRevision}</dt>
            <dd>{detail.revision}</dd>
            <dt className="text-muted-foreground">{copy.details.sourceCreated}</dt>
            <dd>{detail.created_at}</dd>
            <dt className="text-muted-foreground">{copy.details.sourceUpdated}</dt>
            <dd>{detail.updated_at}</dd>
          </dl>
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{copy.source.aliases}</span>
            {detail.aliases.length ? (
              <ul className="flex flex-col gap-1 text-sm">
                {detail.aliases.map((alias) => (
                  <li className="flex items-center gap-2" key={alias.id}>
                    <span className="text-muted-foreground">{alias.authority}</span>
                    <code className="truncate font-mono text-xs">{alias.value}</code>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-muted-foreground">{copy.source.noAliases}</p>}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
