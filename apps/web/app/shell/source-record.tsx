"use client";

import { useEffect, useState, type ComponentType, type FormEvent, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { ControlNetworkError, ControlProblemError, type CortexControlClient } from "../control/client";
import {
  decodeXhsRecommendation,
  type SourceLinks,
  type SourceProjection,
  type XhsImportItem,
  type XhsNote,
  type XhsRecommendation,
} from "../control/research-contracts";
import type { ReaderTab } from "../control/source-knowledge";
import { copy, label, refusalText } from "./copy";
import { failurePhrase } from "./xhs-status";

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

export function readableDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DATE.format(parsed);
}

// The reader tabs each kind maps onto the stored content kinds (Control's
// reader serves a blog's `article.md` and an XHS note's `transcription.md` as
// `full_text`). A paper keeps the reader's own three tabs.
const READER_TABS: Record<string, ReaderTab[] | undefined> = {
  blog: [["full_text", copy.reader.article], ["notes", copy.reader.notes]],
  xhs_note: [["notes", copy.reader.note], ["full_text", copy.reader.transcription]],
};

export function readerTabs(kind: string): ReaderTab[] | undefined {
  return READER_TABS[kind];
}

export type SourceRecordProps = {
  client: CortexControlClient;
  detail: SourceProjection;
  // Opens another source in the Library, such as the note that recommends this one.
  onOpenSource: (id: string) => void;
};

// The notes that recommend this source, each naming the image (or the
// caption) the recommendation came from. Nothing is shown when no note does;
// a read that fails says so, without hiding the record around it.
export function RecommendedIn({ client, sourceId, onOpenSource }: { client: CortexControlClient; sourceId: string; onOpenSource: (id: string) => void }) {
  const [links, setLinks] = useState<{ sourceId: string; value: SourceLinks | null } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    client.getSourceLinks(sourceId, controller.signal).then(
      (value) => { if (!controller.signal.aborted) setLinks({ sourceId, value }); },
      () => { if (!controller.signal.aborted) setLinks({ sourceId, value: null }); },
    );
    return () => controller.abort();
  }, [client, sourceId]);
  // An answer for a source the record has moved past is never shown.
  const current = links?.sourceId === sourceId ? links : null;
  if (!current) return null;
  if (!current.value) return <p className="text-sm text-muted-foreground" role="status">{copy.links.unreadable}</p>;
  if (!current.value.recommended_in.length) return null;
  return (
    <section aria-label={copy.links.title} className="flex flex-col gap-1 text-sm">
      <ul className="flex flex-col gap-1">
        {current.value.recommended_in.map((entry) => (
          <li className="flex flex-wrap items-baseline gap-1" data-recommendation-link="" key={entry.recommendation_id}>
            <span className="text-muted-foreground">{copy.links.title}</span>
            <button className="font-medium underline-offset-2 hover:underline" onClick={() => onOpenSource(entry.source_id)} type="button">{entry.title}</button>
            <span className="text-muted-foreground">· {label.noteEvidence(entry.image_ordinal)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RecordHeader({ detail, badges }: { detail: SourceProjection; badges?: ReactNode }) {
  return (
    <>
      <h3 className="text-base font-semibold">{detail.official_title}</h3>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="outline">{label.sourceKind(detail.source_kind)}</Badge>
        {badges}
        <Badge variant={importStateTone(detail.import_state)}>{detail.import_state}</Badge>
        <span className="text-muted-foreground">{copy.source.added} {readableDate(detail.created_at)} · {copy.source.updated} {readableDate(detail.updated_at)}</span>
      </div>
    </>
  );
}

// Identifiers, the record's own counter and the exact instants are technical
// detail, so they live behind a closed disclosure; the record itself shows the
// title, what kind of source it is, where its import stands and readable dates.
function RecordDetails({ detail }: { detail: SourceProjection }) {
  return (
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
  );
}

function PaperRecord({ client, detail, onOpenSource }: SourceRecordProps) {
  return (
    <div className="flex flex-col items-start gap-2">
      <RecordHeader detail={detail} />
      <RecommendedIn client={client} onOpenSource={onOpenSource} sourceId={detail.id} />
      <RecordDetails detail={detail} />
    </div>
  );
}

// A blog is web writing, not a reviewed paper, and the record says so before
// anything else; the notes that led to it follow.
function BlogRecord({ client, detail, onOpenSource }: SourceRecordProps) {
  return (
    <div className="flex flex-col items-start gap-2">
      <RecordHeader badges={<Badge variant="secondary">{copy.source.notPeerReviewed}</Badge>} detail={detail} />
      <RecommendedIn client={client} onOpenSource={onOpenSource} sourceId={detail.id} />
      <RecordDetails detail={detail} />
    </div>
  );
}

type Outcome = { tone: "success" | "error"; text: string };

// What a refused, unconfirmed or unreadable command says, led by what did not
// happen; the category itself is never spoken unless the copy table names it.
function commandFailure(lead: string, error: unknown): string {
  if (error instanceof ControlNetworkError) return copy.errors.unconfirmed;
  if (error instanceof ControlProblemError) return refusalText(lead, error.problem.category);
  return `${lead}. ${copy.errors.unreadable}`;
}

// A row can be selected only when Control would import it: a paper with its
// arXiv id or a blog with a link, not yet staged or imported. A blog still
// without a link, and anything else, waits.
export function importable(recommendation: XhsRecommendation): boolean {
  if (recommendation.import_state !== "none" && recommendation.import_state !== "failed") return false;
  if (recommendation.kind === "paper") return recommendation.arxiv_id !== null;
  if (recommendation.kind === "blog") return recommendation.url !== null;
  return false;
}

const IMAGE_EMBED = /^!\[Image [0-9]+\]\(assets\/[^)]*\)$/;

// Each image's part of the stored `transcription.md`, as Control writes it
// (cortex_platform/product/xhs/layout.py render_transcription): a `## Image N`
// heading in ordinal order, the embedded image, then the verbatim text or the
// failure. The headings are matched in order, and Control escapes a line of
// OCR text that reads like one, so an image's text never starts another part.
export function transcriptionSections(text: string, ordinals: number[]): Map<number, string> {
  const lines = text.split("\n");
  const starts: Array<[number, number]> = [];
  let from = 0;
  for (const ordinal of ordinals) {
    const at = lines.indexOf(`## Image ${ordinal}`, from);
    if (at < 0) continue;
    starts.push([ordinal, at]);
    from = at + 1;
  }
  return new Map(starts.map(([ordinal, at], index) => {
    const end = index + 1 < starts.length ? starts[index + 1]![1] : lines.length;
    const body = lines.slice(at + 1, end).filter((line) => !IMAGE_EMBED.test(line)).join("\n").trim();
    return [ordinal, body];
  }));
}

function webLink(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password;
  } catch {
    return false;
  }
}

type Transcription = { state: "loading" } | { state: "failed" } | { state: "ready"; sections: Map<number, string> };

function ProvenanceLabel({ children }: { children: ReactNode }) {
  return <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{children}</span>;
}

// A blog recommendation's link, which the operator may replace with their own
// until the blog is imported. Control normalizes the link and records it as set
// by the operator; a refusal that carries the newer row shows that row.
function LinkEditor({ client, noteSourceId, recommendation, onChanged }: {
  client: CortexControlClient;
  noteSourceId: string;
  recommendation: XhsRecommendation;
  onChanged: (recommendation: XhsRecommendation | null, saved?: boolean) => void;
}) {
  const [value, setValue] = useState(recommendation.url ?? "");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  async function save(event: FormEvent) {
    event.preventDefault();
    const url = value.trim();
    if (!webLink(url)) {
      setOutcome({ tone: "error", text: copy.xhs.linkInvalid });
      return;
    }
    setBusy(true);
    setOutcome(null);
    try {
      const { value: answer } = await client.prepareSetRecommendationLink(noteSourceId, recommendation, url).execute();
      setValue(answer.recommendation.url ?? url);
      onChanged(answer.recommendation, true);
      setOutcome({ tone: "success", text: copy.xhs.linkSaved });
    } catch (error) {
      let current: XhsRecommendation | null = null;
      if (error instanceof ControlProblemError && error.problem.category === "revision_conflict") {
        try {
          current = decodeXhsRecommendation(error.problem.current, "problem.current");
        } catch {
          current = null;
        }
      }
      onChanged(current?.id === recommendation.id ? current : null);
      setOutcome({ tone: "error", text: commandFailure(copy.xhs.linkNotSaved, error) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="flex w-full flex-col gap-1" onSubmit={(event) => void save(event)}>
      <div className="flex w-full items-center gap-2">
        <Input aria-label={copy.xhs.linkLabel} disabled={busy} onChange={(event) => setValue(event.target.value)} type="url" value={value} />
        <Button disabled={busy || !value.trim()} size="sm" type="submit" variant="outline">{copy.xhs.saveLink}</Button>
      </div>
      {outcome ? <p className={cn("text-xs", outcome.tone === "error" ? "text-destructive" : "text-muted-foreground")} data-link-outcome={outcome.tone} role="status">{outcome.text}</p> : null}
    </form>
  );
}

// One recommendation: its title, what it is, where in the note it came from
// and how far its import has gone. Expanded, it shows that evidence -- the
// image and its verbatim transcription, or the caption -- and then what was
// identified from it, which is the model's and the rules' reading, not the
// blogger's words.
function RecommendationRow({ client, note, recommendation, selected, disabled, outcome, transcription, onSelect, onExpand, onChanged, onOpenSource }: {
  client: CortexControlClient;
  note: XhsNote;
  recommendation: XhsRecommendation;
  selected: boolean;
  disabled: boolean;
  outcome: Outcome | undefined;
  transcription: Transcription | null;
  onSelect: (selected: boolean) => void;
  onExpand: () => void;
  onChanged: (recommendation: XhsRecommendation | null, saved?: boolean) => void;
  onOpenSource: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [imageMissing, setImageMissing] = useState(false);
  const ordinal = recommendation.image_ordinal;
  const image = ordinal === null ? null : note.images.find((item) => item.ordinal === ordinal) ?? null;
  const importState = copy.xhs.importStates[recommendation.import_state];
  const linkEditable = recommendation.kind === "blog" && recommendation.import_state !== "importing" && recommendation.import_state !== "imported";
  const section = ordinal !== null && transcription?.state === "ready" ? transcription.sections.get(ordinal) : undefined;
  return (
    <li className="flex flex-col gap-2 rounded-md border p-3" data-recommendation-id={recommendation.id}>
      <div className="flex items-start gap-3">
        {importable(recommendation) ? (
          <input
            aria-label={label.selectRecommendation(recommendation.title)}
            checked={selected}
            className="mt-1 size-4 accent-primary scheme-light-dark"
            disabled={disabled}
            onChange={(event) => onSelect(event.target.checked)}
            type="checkbox"
          />
        ) : <span aria-hidden className="mt-1 size-4 shrink-0" />}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-sm font-medium">{recommendation.title}</span>
          <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">{copy.xhs.kinds[recommendation.kind]}</Badge>
            <span>{label.noteEvidence(ordinal)}</span>
            {recommendation.kind === "blog" ? <span data-url-state={recommendation.url_state}>{copy.xhs.urlStates[recommendation.url_state]}</span> : null}
            {importState ? <Badge variant={recommendation.import_state === "failed" ? "destructive" : "secondary"}>{importState}</Badge> : null}
            {recommendation.import_state === "staged" && recommendation.capture_state ? <span>{copy.captureStates[recommendation.capture_state]}</span> : null}
            {recommendation.imported_source_id ? (
              <button className="font-medium text-foreground underline-offset-2 hover:underline" onClick={() => onOpenSource(recommendation.imported_source_id!)} type="button">{copy.xhs.open}</button>
            ) : null}
          </span>
          {outcome ? <p className={cn("text-sm", outcome.tone === "error" ? "text-destructive" : "text-foreground")} data-import-outcome={outcome.tone} role="status">{outcome.text}</p> : null}
        </div>
      </div>
      <Collapsible className="flex w-full flex-col items-start gap-2" onOpenChange={(next) => { setOpen(next); if (next) onExpand(); }} open={open}>
        <CollapsibleTrigger asChild>
          <Button size="sm" type="button" variant="ghost">{copy.xhs.evidence}</Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="flex w-full flex-col gap-3" data-evidence="">
          {ordinal === null ? (
            <div className="flex flex-col gap-1">
              <ProvenanceLabel>{copy.xhs.caption}</ProvenanceLabel>
              <p className="whitespace-pre-wrap text-sm" data-verbatim>{note.caption}</p>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <ProvenanceLabel>{copy.xhs.transcription} · {label.xhsImage(ordinal)}</ProvenanceLabel>
              {image?.asset_path && !imageMissing ? (
                // eslint-disable-next-line @next/next/no-img-element -- the asset route serves the stored file as is.
                <img
                  alt={label.xhsImage(ordinal)}
                  className="h-auto max-h-64 w-auto max-w-full self-start rounded-md border object-contain"
                  onError={() => setImageMissing(true)}
                  src={client.sourceAssetUrl(note.source_id, image.asset_path)}
                />
              ) : <p className="text-sm text-muted-foreground">{copy.xhs.imageMissing}</p>}
              {transcription === null || transcription.state === "loading" ? <p className="text-sm text-muted-foreground">{copy.xhs.transcriptionLoading}</p> : null}
              {transcription?.state === "failed" ? <p className="text-sm text-muted-foreground" role="status">{copy.xhs.transcriptionUnreadable}</p> : null}
              {transcription?.state === "ready" ? (
                section ? <p className="whitespace-pre-wrap text-sm" data-transcription={ordinal} data-verbatim>{section}</p> : <p className="text-sm text-muted-foreground">{copy.xhs.transcriptionMissing}</p>
              ) : null}
            </div>
          )}
          <div className="flex w-full flex-col gap-1">
            <ProvenanceLabel>{copy.xhs.identified}</ProvenanceLabel>
            <dl className="grid grid-cols-[max-content_minmax(0,1fr)] items-baseline gap-x-4 gap-y-1 text-sm" data-identified="">
              <dt className="text-muted-foreground">{copy.xhs.fields.kind}</dt>
              <dd>{copy.xhs.kinds[recommendation.kind]}</dd>
              <dt className="text-muted-foreground">{copy.xhs.fields.title}</dt>
              <dd>{recommendation.title}</dd>
              <dt className="text-muted-foreground">{copy.xhs.fields.quote}</dt>
              <dd className="whitespace-pre-wrap" data-verbatim>{recommendation.quote}</dd>
              {recommendation.arxiv_id ? (
                <>
                  <dt className="text-muted-foreground">{copy.xhs.fields.arxiv}</dt>
                  <dd>{recommendation.arxiv_id}</dd>
                </>
              ) : null}
              {recommendation.kind === "blog" || recommendation.url ? (
                <>
                  <dt className="text-muted-foreground">{copy.xhs.fields.link}</dt>
                  <dd className="flex min-w-0 flex-col items-start gap-1">
                    {recommendation.url ? <a className="break-all underline underline-offset-2" href={recommendation.url} rel="noreferrer" target="_blank">{recommendation.url}</a> : null}
                    <span className="text-xs text-muted-foreground">{copy.xhs.urlStates[recommendation.url_state]}</span>
                    {linkEditable ? <LinkEditor client={client} noteSourceId={note.source_id} onChanged={onChanged} recommendation={recommendation} /> : null}
                  </dd>
                </>
              ) : null}
            </dl>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </li>
  );
}

// A note's header line: who posted it, the role they are followed in, when it
// was published and where it lives on Xiaohongshu.
function NoteByline({ note }: { note: XhsNote }) {
  const role = note.blogger.role;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span>{copy.xhs.by} {note.blogger.name ?? copy.status.xhs.unnamed}</span>
      {role ? (
        <>
          <Badge variant="secondary">{copy.xhs.roles[role]}</Badge>
          <span className="text-muted-foreground">{copy.xhs.roleScope[role]}</span>
        </>
      ) : null}
      {note.published_at ? <span className="text-muted-foreground">· {copy.xhs.published} {readableDate(note.published_at)}</span> : null}
      <a className="underline underline-offset-2" href={note.permalink} rel="noreferrer" target="_blank">{copy.xhs.permalink}</a>
    </div>
  );
}

function noteProgress(note: XhsNote): string | null {
  if (note.state === "saved" || note.state === "unsupported") return null;
  if (note.state === "failed") return `${copy.xhs.noteFailed} (${failurePhrase(note.last_error) ?? copy.status.unknown}).`;
  return copy.xhs.processing;
}

// One note read from Control: the recommendations first, each importable row
// with a checkbox, then the images that failed, each with Retry. Importing
// stages the selection in one command and then approves each staged paper as
// its own decision, so every row reports its own outcome, including a paper
// that was staged but could not be approved.
function XhsNoteBody({ client, detail, onOpenSource }: SourceRecordProps) {
  const sourceId = detail.id;
  const [note, setNote] = useState<XhsNote | null>(null);
  const [failed, setFailed] = useState(false);
  const [reads, setReads] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const [retries, setRetries] = useState<Record<number, Outcome>>({});
  const [transcription, setTranscription] = useState<Transcription | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    client.getXhsNote(sourceId, controller.signal).then(
      (value) => { if (!controller.signal.aborted) { setNote(value); setFailed(false); } },
      () => { if (!controller.signal.aborted) setFailed(true); },
    );
    return () => controller.abort();
  }, [client, sourceId, reads]);

  const reread = () => setReads((count) => count + 1);

  // The stored transcription is read once, when the first image evidence is
  // opened; the record never reads it for a note nobody expands.
  function loadTranscription() {
    if (!note || (transcription && transcription.state !== "failed")) return;
    const ordinals = note.images.map((image) => image.ordinal);
    setTranscription({ state: "loading" });
    client.readSourceDocument(sourceId, "full_text").then(
      (document) => setTranscription({ state: "ready", sections: transcriptionSections(document.text, ordinals) }),
      () => setTranscription({ state: "failed" }),
    );
  }

  function replaceRecommendation(recommendation: XhsRecommendation | null, saved = false) {
    if (!recommendation) { reread(); return; }
    setNote((current) => current && {
      ...current,
      recommendations: current.recommendations.map((item) => item.id === recommendation.id ? recommendation : item),
    });
    // A saved link moves the note's revision, which an import names.
    if (saved) reread();
  }

  // A staged or reused paper is approved here unless its Capture is already
  // past waiting for the operator, in which case the open one is kept.
  async function settle(item: XhsImportItem): Promise<Outcome> {
    if (item.disposition === "refused") return { tone: "error", text: copy.xhs.outcomes.refused[item.reason!] };
    if (item.disposition === "blog_import_queued") return { tone: "success", text: copy.xhs.outcomes.blogQueued };
    if (item.capture_state !== "pending") {
      return { tone: "success", text: label.alreadyCaptured(copy.captureStates[item.capture_state!]) };
    }
    try {
      await client.prepareApproveCapture({ id: item.capture_id!, revision: item.capture_revision! }).execute();
      return { tone: "success", text: copy.xhs.outcomes.approved };
    } catch (error) {
      return { tone: "error", text: commandFailure(copy.xhs.outcomes.notApproved, error) };
    }
  }

  async function importSelected() {
    if (!note) return;
    const ids = note.recommendations.filter((item) => selected.has(item.id) && importable(item)).map((item) => item.id);
    if (!ids.length) return;
    setBusy("import");
    setImportError(null);
    try {
      const { value } = await client.prepareImportRecommendations(note, ids).execute();
      const settled: Record<string, Outcome> = {};
      for (const item of value.items) settled[item.recommendation_id] = await settle(item);
      setOutcomes((current) => ({ ...current, ...settled }));
      setSelected(new Set());
    } catch (error) {
      setImportError(commandFailure(copy.xhs.importNotDone, error));
    } finally {
      setBusy(null);
      reread();
    }
  }

  async function retry(ordinal: number) {
    if (!note) return;
    setBusy(`retry:${ordinal}`);
    try {
      await client.prepareRetryXhsImage(note, ordinal).execute();
      setRetries((current) => ({ ...current, [ordinal]: { tone: "success", text: copy.xhs.retryQueued } }));
    } catch (error) {
      setRetries((current) => ({ ...current, [ordinal]: { tone: "error", text: commandFailure(copy.xhs.retryNotDone, error) } }));
    } finally {
      setBusy(null);
      reread();
    }
  }

  if (!note) {
    return <p className="text-sm text-muted-foreground" role={failed ? "alert" : undefined}>{failed ? copy.xhs.unreadable : copy.xhs.loading}</p>;
  }
  const progress = noteProgress(note);
  // A retried image keeps its row, now saying the retry is queued, until the
  // record is opened again.
  const imageRows = note.images.filter((image) => image.download_state === "failed" || image.ocr_state === "failed" || retries[image.ordinal]);
  const chosen = note.recommendations.filter((item) => selected.has(item.id) && importable(item)).length;
  return (
    <div className="flex w-full flex-col gap-4">
      <NoteByline note={note} />
      {progress ? <p className="text-sm text-muted-foreground" role="status">{progress}</p> : null}
      {failed ? <p className="text-sm text-destructive" role="alert">{copy.xhs.unreadable}</p> : null}
      <section aria-label={copy.xhs.recommendations} className="flex w-full flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <h4 className="text-sm font-semibold">{copy.xhs.recommendations}</h4>
          <Button disabled={busy !== null || chosen === 0} onClick={() => void importSelected()} size="sm" type="button">
            {busy === "import" ? copy.xhs.importing : copy.xhs.importSelected}
          </Button>
        </div>
        {importError ? <p className="text-sm text-destructive" role="alert">{importError}</p> : null}
        {note.recommendations.length ? (
          <ul className="flex flex-col gap-2">
            {note.recommendations.map((recommendation) => (
              <RecommendationRow
                client={client}
                disabled={busy !== null}
                key={recommendation.id}
                note={note}
                onChanged={replaceRecommendation}
                onExpand={() => { if (recommendation.image_ordinal !== null) loadTranscription(); }}
                onOpenSource={onOpenSource}
                onSelect={(on) => setSelected((current) => {
                  const next = new Set(current);
                  if (on) next.add(recommendation.id); else next.delete(recommendation.id);
                  return next;
                })}
                outcome={outcomes[recommendation.id]}
                recommendation={recommendation}
                selected={selected.has(recommendation.id)}
                transcription={transcription}
              />
            ))}
          </ul>
        ) : <p className="text-sm text-muted-foreground">{copy.xhs.noRecommendations}</p>}
      </section>
      {imageRows.length ? (
        <section aria-label={copy.xhs.failedImages} className="flex w-full flex-col gap-2">
          <h4 className="text-sm font-semibold">{copy.xhs.failedImages}</h4>
          <ul className="flex flex-col gap-2">
            {imageRows.map((image) => {
              const downloadFailed = image.download_state === "failed";
              const failedStep = downloadFailed || image.ocr_state === "failed";
              const reason = failurePhrase(downloadFailed ? image.download_error : image.ocr_error) ?? copy.status.unknown;
              const outcome = retries[image.ordinal];
              return (
                <li className="flex flex-col gap-1 rounded-md border p-3" data-failed-image={image.ordinal} key={image.ordinal}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm">
                      {failedStep ? label.xhsImageFailed(image.ordinal, downloadFailed ? copy.xhs.download : copy.xhs.ocr, reason) : label.xhsImage(image.ordinal)}
                    </span>
                    {failedStep ? (
                      <Button aria-label={label.retryImage(image.ordinal)} disabled={busy !== null} onClick={() => void retry(image.ordinal)} size="sm" type="button" variant="outline">
                        {copy.xhs.retry}
                      </Button>
                    ) : null}
                  </div>
                  {outcome ? <p className={cn("text-sm", outcome.tone === "error" ? "text-destructive" : "text-muted-foreground")} data-retry-outcome={outcome.tone} role="status">{outcome.text}</p> : null}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

// The XHS note record. It is keyed on the note, so a selection, an outcome or
// a loaded transcription never carries over to another note.
function XhsNoteRecord(props: SourceRecordProps) {
  return (
    <div className="flex flex-col items-start gap-2">
      <RecordHeader detail={props.detail} />
      <XhsNoteBody key={props.detail.id} {...props} />
      <RecordDetails detail={props.detail} />
    </div>
  );
}

// The record each kind is read through. A kind with no record of its own is
// shown as a paper is: title, badges, dates and the details disclosure.
export const SOURCE_RECORDS: Record<string, ComponentType<SourceRecordProps> | undefined> = {
  paper: PaperRecord,
  blog: BlogRecord,
  xhs_note: XhsNoteRecord,
};

export function SourceRecord({ client, detail, detailError, selectedId, onOpenSource }: {
  client: CortexControlClient;
  detail: SourceProjection | null;
  detailError: string | null;
  selectedId: string | null;
  onOpenSource: (id: string) => void;
}) {
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
  const Record = SOURCE_RECORDS[detail.source_kind] ?? PaperRecord;
  return <Record client={client} detail={detail} onOpenSource={onOpenSource} />;
}
