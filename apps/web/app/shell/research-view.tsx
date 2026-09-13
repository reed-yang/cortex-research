"use client";

import { CopyIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { cn } from "@/lib/utils";
import { ArtifactDocument } from "../control/artifact-document";
import { ControlProblemError, type CortexControlClient } from "../control/client";
import {
  RESEARCH_ITEM_KINDS,
  RESEARCH_ITEM_STATUSES,
  type ResearchDocumentContent,
  type ResearchDocumentRef,
  type ResearchItem,
  type ResearchItemDetail,
  type ResearchItemKind,
} from "../control/research-items-contracts";
import { copy, label, researchKindLabel, researchStatusLabel } from "./copy";
import type { ControlActions, ControlState, ViewProps } from "./types";

const SKELETON_ROWS = [0, 1, 2];

const KIND_TITLES: Record<ResearchItemKind, string> = {
  idea: copy.research.ideas,
  exploration: copy.research.explorations,
  project: copy.research.projects,
};

// The shell is English-only and the catalog carries instants, so a date is
// formatted in one fixed locale and UTC rather than the reader's machine.
const DATE = new Intl.DateTimeFormat("en", { day: "numeric", month: "short", timeZone: "UTC", year: "numeric" });

function readableDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DATE.format(parsed);
}

// Search is deliberately local to the page the operator is looking at: the
// catalog pages explicitly, so a search box that silently queried the whole
// database would be answering a different question than the one on screen.
function matches(item: ResearchItem, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [item.title, item.summary, item.pause_reason ?? "", researchStatusLabel(item.status)]
    .some((field) => field.toLowerCase().includes(needle));
}

function ResearchRow({ item, selected, onSelect }: {
  item: ResearchItem;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      aria-current={selected ? "true" : undefined}
      className={cn(
        "flex min-h-11 flex-col items-start gap-1 rounded-md border border-transparent px-3 py-2 text-left hover:bg-muted",
        selected && "border-border bg-muted",
      )}
      onClick={onSelect}
      type="button"
    >
      <span className="w-full truncate text-sm font-medium">{item.title}</span>
      <span className="flex w-full flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{researchStatusLabel(item.status)}</Badge>
        <span>{copy.research.rounds} {item.round_count}</span>
        <span>{item.updated_at ? `${copy.research.updated} ${readableDate(item.updated_at)}` : copy.research.noActivity}</span>
      </span>
      {item.pause_reason ? (
        <span className="line-clamp-2 w-full text-xs text-muted-foreground">{copy.research.paused} {item.pause_reason}</span>
      ) : null}
    </button>
  );
}

function ResearchCatalog({ state, actions }: { state: ControlState; actions: ControlActions }) {
  const [query, setQuery] = useState("");
  const {
    researchItems, researchKind, researchLimit, researchListError, researchListLoading,
    researchOffset, researchStatus, researchTotal, selectedResearchItemId,
  } = state;
  const listed = !researchListLoading && !researchListError;
  const visible = useMemo(() => researchItems.filter((item) => matches(item, query)), [query, researchItems]);
  // The six persisted statuses, plus any other status this page actually
  // carries: a state the backend truthfully holds stays filterable even when
  // this build has never heard of it.
  const statuses = useMemo(() => {
    const known = new Set<string>(RESEARCH_ITEM_STATUSES);
    for (const item of researchItems) known.add(item.status);
    return [...known];
  }, [researchItems]);
  const first = researchTotal === 0 ? 0 : researchOffset + 1;
  const last = Math.min(researchOffset + researchItems.length, researchTotal);

  return (
    <div className="flex min-w-0 flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold">{copy.research.title}</h2>
        <p className="text-sm text-muted-foreground">{copy.research.subtitle}</p>
      </header>

      <nav aria-label={copy.research.kinds} className="flex flex-wrap gap-1">
        {RESEARCH_ITEM_KINDS.map((kind) => (
          <Button
            aria-current={kind === researchKind ? "true" : undefined}
            className="min-h-11 lg:min-h-8"
            key={kind}
            onClick={() => actions.selectResearchKind(kind)}
            size="sm"
            variant={kind === researchKind ? "secondary" : "ghost"}
          >
            {KIND_TITLES[kind]}
          </Button>
        ))}
      </nav>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          aria-label={copy.research.searchLabel}
          className="min-h-11 min-w-0 flex-1 lg:min-h-9"
          onChange={(event) => setQuery(event.target.value)}
          placeholder={copy.research.search}
          type="search"
          value={query}
        />
        <select
          aria-label={copy.research.status}
          className="min-h-11 rounded-md border bg-background px-2 text-sm lg:min-h-9"
          onChange={(event) => actions.selectResearchStatus(event.target.value || null)}
          value={researchStatus ?? ""}
        >
          <option value="">{copy.research.anyStatus}</option>
          {statuses.map((status) => (
            <option key={status} value={status}>{researchStatusLabel(status)}</option>
          ))}
        </select>
      </div>

      {researchListLoading ? (
        <div className="flex flex-col gap-2">
          {SKELETON_ROWS.map((row) => <Skeleton className="h-12 w-full" key={row} />)}
          <p className="text-sm text-muted-foreground">{copy.research.loading}</p>
        </div>
      ) : null}

      {!researchListLoading && researchListError ? (
        <div className="flex flex-col items-start gap-2" role="alert">
          <p className="text-sm">{copy.research.unreadable}</p>
          <details className="text-xs text-muted-foreground" data-details>
            <summary className="cursor-pointer">{copy.research.details}</summary>
            <p>{researchListError}</p>
          </details>
          <Button onClick={actions.refreshResearchItems} size="sm" type="button" variant="outline">
            {copy.research.retry}
          </Button>
        </div>
      ) : null}

      {listed && researchItems.length === 0 ? <p className="text-sm text-muted-foreground">{copy.research.empty}</p> : null}
      {listed && researchItems.length > 0 && visible.length === 0 ? (
        <p className="text-sm text-muted-foreground">{copy.research.noMatches}</p>
      ) : null}

      {listed && visible.length > 0 ? (
        <nav aria-label={copy.research.list} className="flex flex-col gap-1">
          {visible.map((item) => (
            <ResearchRow
              item={item}
              key={item.id}
              onSelect={() => actions.selectResearchItem(item.id)}
              selected={item.id === selectedResearchItemId}
            />
          ))}
        </nav>
      ) : null}

      {listed && researchTotal > 0 ? (
        <div className="flex flex-wrap items-center gap-2 border-t pt-2 text-sm text-muted-foreground">
          <span>{label.researchPage(first, last, researchTotal)}</span>
          <Button
            className="ms-auto min-h-11 lg:min-h-8"
            disabled={researchOffset === 0}
            onClick={() => actions.browseResearchItems(researchOffset - researchLimit)}
            size="sm"
            type="button"
            variant="outline"
          >
            {copy.research.previous}
          </Button>
          <Button
            className="min-h-11 lg:min-h-8"
            disabled={researchOffset + researchItems.length >= researchTotal}
            onClick={() => actions.browseResearchItems(researchOffset + researchLimit)}
            size="sm"
            type="button"
            variant="outline"
          >
            {copy.research.next}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

// One document version's stored bytes, read by document-version identity. The
// reader is mounted under a key that carries both the item and the version, so
// a document is never shown under an item that does not own it: switching
// document remounts this with empty state, and the abort in the cleanup cancels
// the read the operator has already moved past.
function DossierDocument({ client, document }: { client: CortexControlClient; document: ResearchDocumentRef }) {
  const [content, setContent] = useState<ResearchDocumentContent | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void client.getResearchDocumentContent(document.id, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setContent(value);
        setSettled(true);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setFailure(
          error instanceof ControlProblemError
            ? error.problem.category
            : error instanceof Error ? error.message : copy.errors.unreadable,
        );
        setSettled(true);
      });
    return () => controller.abort();
  }, [client, document.id]);

  if (!settled) return <p className="text-sm text-muted-foreground">{copy.research.documentLoading}</p>;
  if (failure !== null || !content) {
    return (
      <div className="flex flex-col items-start gap-2" role="alert">
        <p className="text-sm">{copy.research.documentUnavailable}</p>
        <details className="text-xs text-muted-foreground" data-details>
          <summary className="cursor-pointer">{copy.research.details}</summary>
          <p>{failure ?? copy.errors.unreadable}</p>
          <DocumentDetails document={document} />
        </details>
      </div>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {/* What was withheld is said before the document, not after it: the
          reader is looking at an authorized projection, and Copy source copies
          that same projection rather than the retained bytes behind it. */}
      {content.redacted ? (
        <p className="text-sm text-muted-foreground">{copy.research.documentRedacted}</p>
      ) : null}
      <div className="rounded-md border bg-card p-4 text-card-foreground">
        <ArtifactDocument content={content} />
      </div>
      <details className="text-xs text-muted-foreground" data-details>
        <summary className="cursor-pointer py-2">{copy.research.details}</summary>
        <DocumentDetails content={content} document={document} />
      </details>
    </div>
  );
}

function DocumentDetails({ document, content }: {
  document: ResearchDocumentRef;
  content?: ResearchDocumentContent;
}) {
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
      <dt className="text-muted-foreground">{copy.details.documentId}</dt>
      <dd className="truncate font-mono text-xs">{document.document_id}</dd>
      <dt className="text-muted-foreground">{copy.details.documentVersionId}</dt>
      <dd className="truncate font-mono text-xs">{document.id}</dd>
      <dt className="text-muted-foreground">{copy.details.documentVersion}</dt>
      <dd>{document.version}</dd>
      <dt className="text-muted-foreground">{copy.details.documentBytes}</dt>
      <dd>{document.byte_length}</dd>
      {content ? (
        <>
          <dt className="text-muted-foreground">{copy.details.documentShownBytes}</dt>
          <dd>{content.byte_length}</dd>
        </>
      ) : null}
      {content?.retained_byte_length !== null && content?.retained_byte_length !== undefined ? (
        <>
          <dt className="text-muted-foreground">{copy.details.documentRetainedBytes}</dt>
          <dd>{content.retained_byte_length}</dd>
        </>
      ) : null}
      <dt className="text-muted-foreground">{copy.details.documentDigest}</dt>
      <dd className="truncate font-mono text-xs">{document.sha256}</dd>
    </dl>
  );
}

// The bot command that selects the SAME item in the operator's own bound
// conversation. It lives inside a disclosure because it spells an identity, and
// an identity is not authorization: whoever sends it still has to be the person
// the bot is bound to.
function TelegramCommand({ item }: { item: ResearchItemDetail }) {
  const { isCopied, copyToClipboard } = useCopyToClipboard();
  const command = label.researchItemCommand(item.id);
  return (
    <details className="text-sm text-muted-foreground" data-details>
      <summary className="cursor-pointer py-2">{copy.research.telegram}</summary>
      <p className="text-xs">{copy.research.telegramHint}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <code className="min-w-0 truncate rounded bg-muted px-2 py-1 font-mono text-xs">{command}</code>
        <Button
          aria-label={isCopied ? copy.research.copied : copy.research.copyCommand}
          className="min-h-11 lg:min-h-8"
          onClick={() => copyToClipboard(command)}
          size="sm"
          type="button"
          variant="outline"
        >
          <CopyIcon aria-hidden="true" className="size-3.5" />
          {isCopied ? copy.research.copied : copy.research.copyCommand}
        </Button>
      </div>
    </details>
  );
}

function Dossier({ client, item, state, actions }: {
  client: CortexControlClient;
  item: ResearchItemDetail;
  state: ControlState;
  actions: ControlActions;
}) {
  const [documentId, setDocumentId] = useState<string | null>(null);
  const selected = item.documents.find((entry) => entry.id === documentId) ?? item.documents[0] ?? null;
  const hasProject = state.workspace !== null;
  const blocked = !item.continuation_ready;

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <header className="flex flex-col gap-2">
        <h3 className="text-base font-semibold">{item.title}</h3>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant="outline">{researchKindLabel(item.kind)}</Badge>
          <Badge variant="secondary">{researchStatusLabel(item.status)}</Badge>
          <span className="text-muted-foreground">{copy.research.rounds} {item.round_count}</span>
          <span className="text-muted-foreground">
            {item.updated_at ? `${copy.research.updated} ${readableDate(item.updated_at)}` : copy.research.noActivity}
          </span>
        </div>
        {item.pause_reason ? (
          <p className="text-sm text-muted-foreground">{copy.research.paused} {item.pause_reason}</p>
        ) : null}
        {item.unavailable_reason ? (
          <p className="text-sm text-destructive">{item.unavailable_reason}</p>
        ) : null}
      </header>

      <section aria-label={copy.research.open} className="flex flex-col items-start gap-2 rounded-md border p-3">
        <Button
          className="min-h-11"
          disabled={blocked || !hasProject || state.commandPending}
          onClick={() => void actions.openResearchThread()}
          type="button"
        >
          {state.commandPending ? copy.research.opening : copy.research.open}
        </Button>
        <p className="text-sm text-muted-foreground">{copy.research.openHint}</p>
        <p className="text-sm text-muted-foreground">{copy.research.askHint}</p>
        {blocked ? <p className="text-sm text-muted-foreground">{copy.research.blocked}</p> : null}
        {!hasProject ? <p className="text-sm text-muted-foreground">{copy.research.chooseProject}</p> : null}
        <TelegramCommand item={item} />
      </section>

      <section aria-label={copy.research.summary} className="flex flex-col gap-1">
        <h4 className="text-sm font-medium">{copy.research.summary}</h4>
        <p className="text-sm text-muted-foreground">{item.summary || copy.research.noSummary}</p>
      </section>

      <section aria-label={copy.research.documents} className="flex min-w-0 flex-col gap-2">
        <h4 className="text-sm font-medium">{copy.research.documents}</h4>
        {item.documents.length === 0 ? (
          <p className="text-sm text-muted-foreground">{copy.research.noDocuments}</p>
        ) : (
          <>
            {item.documents.length > 1 ? (
              <nav aria-label={copy.research.documentList} className="flex flex-wrap gap-1">
                {item.documents.map((entry) => (
                  <Button
                    aria-current={entry.id === selected?.id ? "true" : undefined}
                    className="min-h-11 lg:min-h-8"
                    key={entry.id}
                    onClick={() => setDocumentId(entry.id)}
                    size="sm"
                    variant={entry.id === selected?.id ? "secondary" : "ghost"}
                  >
                    {label.researchDocument(entry.title, entry.version)}
                  </Button>
                ))}
              </nav>
            ) : null}
            {selected ? (
              <>
                <p className="text-sm text-muted-foreground">{label.researchDocument(selected.title, selected.version)}</p>
                <DossierDocument client={client} document={selected} key={`${item.id}:${selected.id}`} />
              </>
            ) : null}
          </>
        )}
      </section>

      <section aria-label={copy.research.history} className="flex flex-col gap-2">
        <h4 className="text-sm font-medium">{copy.research.history}</h4>
        {item.history.length === 0 ? (
          <p className="text-sm text-muted-foreground">{copy.research.noHistory}</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {item.history.map((entry, index) => (
              <li className="flex flex-col gap-0.5 border-s ps-3 text-sm" key={`${entry.kind}:${index}`}>
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{entry.label}</span>
                  <span className="text-xs text-muted-foreground">
                    {entry.created_at ? readableDate(entry.created_at) : copy.research.noActivity}
                  </span>
                </span>
                {entry.text ? <span className="text-muted-foreground">{entry.text}</span> : null}
              </li>
            ))}
          </ol>
        )}
      </section>

      <Collapsible className="flex w-full flex-col items-start gap-2">
        <CollapsibleTrigger asChild>
          <Button className="min-h-11 lg:min-h-8" size="sm" type="button" variant="outline">{copy.research.details}</Button>
        </CollapsibleTrigger>
        <CollapsibleContent aria-label={copy.research.details} className="flex w-full flex-col gap-3" data-details="research item" role="group">
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">{copy.details.itemId}</dt>
            <dd className="truncate font-mono text-xs">{item.id}</dd>
            <dt className="text-muted-foreground">{copy.details.originId}</dt>
            <dd className="truncate">{item.origin_id}</dd>
            <dt className="text-muted-foreground">{copy.details.itemKind}</dt>
            <dd>{item.kind}</dd>
            <dt className="text-muted-foreground">{copy.details.itemStatus}</dt>
            <dd>{item.status}</dd>
            <dt className="text-muted-foreground">{copy.details.threadId}</dt>
            <dd className="truncate font-mono text-xs">{item.thread_id ?? "—"}</dd>
          </dl>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

export function ResearchView({ state, actions, client }: ViewProps) {
  const { researchItem, researchItemError, researchItemLoading, selectedResearchItemId } = state;
  // The dossier on screen is only ever the selected item's: a detail that
  // arrived for an item the operator has already left is not rendered under the
  // one they are looking at now.
  const open = researchItem && researchItem.id === selectedResearchItemId ? researchItem : null;
  return (
    <section
      aria-label={copy.research.title}
      className="grid min-h-0 flex-1 grid-cols-1 gap-6 overflow-y-auto bg-background p-6 text-foreground lg:grid-cols-[minmax(300px,1fr)_2fr] lg:grid-rows-[minmax(0,1fr)] lg:overflow-hidden"
    >
      <ResearchCatalog actions={actions} state={state} />
      <div className="flex min-w-0 flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
        {!selectedResearchItemId ? <p className="text-sm text-muted-foreground">{copy.research.pick}</p> : null}
        {selectedResearchItemId && researchItemLoading && !open ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-2/3" />
            <p className="text-sm text-muted-foreground">{copy.research.dossierLoading}</p>
          </div>
        ) : null}
        {selectedResearchItemId && !researchItemLoading && researchItemError ? (
          <div className="flex flex-col items-start gap-2" role="alert">
            <p className="text-sm">{copy.research.dossierUnreadable}</p>
            <details className="text-xs text-muted-foreground" data-details>
              <summary className="cursor-pointer">{copy.research.details}</summary>
              <p>{researchItemError}</p>
            </details>
            <Button
              onClick={() => actions.selectResearchItem(selectedResearchItemId)}
              size="sm"
              type="button"
              variant="outline"
            >
              {copy.research.retry}
            </Button>
          </div>
        ) : null}
        {open ? <Dossier actions={actions} client={client} item={open} key={open.id} state={state} /> : null}
      </div>
    </section>
  );
}
