"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type { SourceKind } from "../control/research-contracts";
import { SourceContentReader, SourceKnowledgeSearch } from "../control/source-knowledge";
import { copy, label } from "./copy";
import { ReadingsPublication } from "./readings-publication";
import { readableDate, readerTabs, SourceRecord } from "./source-record";
import type { ViewProps } from "./types";

const SKELETON_ROWS = [0, 1, 2];

// The kind filter, in the order the operator reads it; null is every kind.
const KIND_FILTERS: Array<[SourceKind | null, string]> = [
  [null, copy.library.kindAll],
  ["paper", copy.library.kindPapers],
  ["blog", copy.library.kindBlogs],
  ["xhs_note", copy.library.kindNotes],
];

export function LibraryView({ state, actions, client }: ViewProps) {
  const { sources, sourcesError, sourcesLoading, sourceDetail, sourceDetailError, selectedSourceId, sourceKind } = state;
  const listed = !sourcesLoading && !sourcesError;
  // The reader is keyed on the record it belongs to, so it never shows one
  // source's text under another source's record.
  const reader = sourceDetail && sourceDetail.id === selectedSourceId ? sourceDetail : null;
  // Search reads the paper index only, so it is offered where papers are listed.
  const searchable = sourceKind === null || sourceKind === "paper";
  return (
    <section aria-label={copy.library.title} className="grid min-h-0 flex-1 grid-cols-1 gap-6 overflow-y-auto bg-background p-6 text-foreground lg:grid-cols-[minmax(280px,1fr)_2fr] lg:grid-rows-[minmax(0,1fr)] lg:overflow-hidden">
      <div className="flex min-w-0 flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
        <h2 className="text-lg font-semibold">{copy.library.title}</h2>
        <ReadingsPublication client={client} sourceId={selectedSourceId} />
        <nav aria-label={copy.library.kinds} className="flex flex-wrap gap-1">
          {KIND_FILTERS.map(([kind, name]) => (
            <Button
              aria-current={kind === sourceKind ? "true" : undefined}
              className="min-h-11 lg:min-h-8"
              key={name}
              onClick={() => actions.selectSourceKind(kind)}
              size="sm"
              type="button"
              variant={kind === sourceKind ? "secondary" : "ghost"}
            >
              {name}
            </Button>
          ))}
        </nav>
        {searchable ? <SourceKnowledgeSearch client={client} onSelect={actions.selectSource} scope={copy.library.searchScope} /> : null}
        {sourcesLoading ? (
          <div className="flex flex-col gap-2">
            {SKELETON_ROWS.map((row) => <Skeleton className="h-12 w-full" key={row} />)}
          </div>
        ) : null}
        {!sourcesLoading && sourcesError ? (
          <div className="flex flex-col items-start gap-2" role="alert">
            <p className="text-sm">{copy.errors.unreadable}</p>
            <details className="text-xs text-muted-foreground" data-details>
              <summary>{copy.library.details}</summary>
              <p>{sourcesError}</p>
            </details>
            <Button onClick={actions.refreshSources} size="sm" type="button" variant="outline">{copy.library.retry}</Button>
          </div>
        ) : null}
        {listed && !sources.length ? <p className="text-sm text-muted-foreground">{sourceKind ? copy.library.emptyKind : copy.library.empty}</p> : null}
        {listed && sources.length ? (
          <nav aria-label={copy.library.sources} className="flex flex-col gap-1">
            {sources.map((source) => (
              <button
                aria-current={source.id === selectedSourceId ? "true" : undefined}
                className={cn(
                  "flex flex-col items-start gap-1 rounded-md border border-transparent px-3 py-2 text-left hover:bg-muted",
                  source.id === selectedSourceId && "border-border bg-muted",
                )}
                key={source.id}
                onClick={() => actions.selectSource(source.id)}
                type="button"
              >
                <span className="w-full truncate text-sm font-medium">{source.official_title}</span>
                <span className="flex w-full items-center gap-2">
                  {/* A paper's canonical id is how it is cited; a note's or a
                      blog's is a hash, so the row names when it was added. */}
                  <span className="min-w-0 truncate text-xs text-muted-foreground">{source.source_kind === "paper" ? source.canonical_id : readableDate(source.created_at)}</span>
                  <Badge variant="outline">{label.sourceKind(source.source_kind)}</Badge>
                </span>
              </button>
            ))}
          </nav>
        ) : null}
      </div>
      <div className="flex min-w-0 flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
        {!selectedSourceId && !sourceDetail && !sourceDetailError ? (
          <p className="text-sm text-muted-foreground">{copy.library.pick}</p>
        ) : (
          <>
            <SourceRecord client={client} detail={sourceDetail} detailError={sourceDetailError} onOpenSource={actions.selectSource} selectedId={selectedSourceId} />
            {reader ? <SourceContentReader client={client} key={reader.id} sourceId={reader.id} tabs={readerTabs(reader.source_kind)} /> : null}
          </>
        )}
      </div>
    </section>
  );
}
