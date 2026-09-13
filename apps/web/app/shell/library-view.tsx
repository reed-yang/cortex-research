"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { SourceContentReader, SourceKnowledgeSearch } from "../control/source-knowledge";
import { copy } from "./copy";
import { SourceRecord } from "./source-record";
import type { ViewProps } from "./types";

const SKELETON_ROWS = [0, 1, 2];

export function LibraryView({ state, actions, client }: ViewProps) {
  const { sources, sourcesError, sourcesLoading, sourceDetail, sourceDetailError, selectedSourceId } = state;
  const listed = !sourcesLoading && !sourcesError;
  // The reader is keyed on the record it belongs to, so it never shows one
  // source's text under another source's record.
  const reader = sourceDetail && sourceDetail.id === selectedSourceId ? sourceDetail : null;
  return (
    <section aria-label={copy.library.title} className="grid min-h-0 flex-1 grid-cols-1 gap-6 overflow-y-auto bg-background p-6 text-foreground lg:grid-cols-[minmax(280px,1fr)_2fr] lg:grid-rows-[minmax(0,1fr)] lg:overflow-hidden">
      <div className="flex min-w-0 flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
        <h2 className="text-lg font-semibold">{copy.library.title}</h2>
        <SourceKnowledgeSearch client={client} onSelect={actions.selectSource} />
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
        {listed && !sources.length ? <p className="text-sm text-muted-foreground">{copy.library.empty}</p> : null}
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
                  <span className="min-w-0 truncate text-xs text-muted-foreground">{source.canonical_id}</span>
                  <Badge variant="outline">{source.source_kind}</Badge>
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
            <SourceRecord detail={sourceDetail} detailError={sourceDetailError} selectedId={selectedSourceId} />
            {reader ? <SourceContentReader client={client} key={reader.id} sourceId={reader.id} /> : null}
          </>
        )}
      </div>
    </section>
  );
}
