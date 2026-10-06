"use client";

import { FormEvent, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { MarkdownDocument, type MarkdownComponents } from "@/components/assistant-ui/elements/markdown-text";
import { copy, label as phrase } from "../shell/copy";
import { DocumentViewControls, type DocumentMode } from "./artifact-document";
import { ControlProblemError, CortexControlClient } from "./client";
import type { SourceContent, SourceContentKind, SourceDocument, SourceSearch } from "./research-contracts";

const KINDS: Array<[SourceContentKind, string]> = [["notes", "Notes"], ["full_text", "Full text"], ["grounding", "Grounding"]];
const RETRIEVAL_LABELS: Record<string, string> = { fts5_or: "Keyword matches", unicode_title_fallback: "Title matches", "fts5_or+unicode_title_fallback": "Keyword and title matches" };

function contentError(error: unknown): string {
  if (error instanceof ControlProblemError) {
    if (error.problem.category === "source_content_unavailable") return "This content is missing or could not be verified. Try another tab or retry.";
    if (error.problem.category === "source_query_invalid") return "This reading position is no longer valid. Reopen the document to start again.";
  }
  return "Source content could not be loaded. Retry to read this document.";
}

function ContentPages({ client, sourceId, kind }: { client: CortexControlClient; sourceId: string; kind: SourceContentKind }) {
  const [pages, setPages] = useState<SourceContent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const active = useRef(true);
  const busy = useRef(false);
  useEffect(() => {
    active.current = true;
    let current = true;
    client.getSourceContent(sourceId, kind).then((page) => {
      if (current) setPages([page]);
    }).catch((error: unknown) => {
      if (current) setError(contentError(error));
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; active.current = false; };
  }, [client, sourceId, kind]);

  async function load() {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    setError(null);
    const last = pages.at(-1);
    try {
      const page = await client.getSourceContent(sourceId, kind, last?.next_cursor ?? undefined);
      if (!active.current) return;
      if (last && (page.content_sha256 !== last.content_sha256 || page.canonical_id !== last.canonical_id || page.start_line < last.end_line || page.next_cursor === last.next_cursor)) {
        setError("The document changed while reading. Reopen the document to start again.");
        return;
      }
      setPages((previous) => [...previous, page]);
    } catch (error) {
      if (active.current) setError(contentError(error));
    } finally {
      busy.current = false;
      if (active.current) setLoading(false);
    }
  }

  return (
    <div aria-label={`${KINDS.find(([value]) => value === kind)?.[1]} content`} aria-busy={loading} className="source-content-pages" role="tabpanel" id={`source-panel-${kind}`} aria-labelledby={`source-tab-${kind}`}>
      {pages.map((page, index) => (
        <section className="source-content-page" key={index}>
          <p className="source-citation">{page.canonical_id} / {page.kind} / L{page.start_line}–L{page.end_line} <span title={page.content_sha256}>sha256 {page.content_sha256.slice(0, 12)}</span></p>
          {!page.text ? <p className="research-empty text-muted-foreground!">{copy.reader.empty}</p> : <div className="source-document">{page.text.replace(/\n$/, "").split("\n").map((line, offset) => (
            <div className="source-document-line" key={offset}>
              <span className="source-line-number" aria-label={`Line ${page.start_line + offset}`}>{page.start_line + offset}</span>
              <span>{line || "\u00a0"}</span>
            </div>
          ))}</div>}
        </section>
      ))}
      {loading ? <p role="status" className="research-empty text-muted-foreground!">{copy.reader.loading}</p> : null}
      {error ? <div role="alert"><p>{error}</p><button disabled={loading} onClick={() => void load()} type="button">{copy.reader.retry}</button></div> : null}
      {!loading && !error && pages.at(-1)?.next_cursor ? <button onClick={() => void load()} type="button">Load more</button> : null}
      {!loading && !error && pages.length > 0 && !pages.at(-1)?.next_cursor ? <p className="research-empty text-muted-foreground!">End of document</p> : null}
    </div>
  );
}

// The Library's Preview/Source choice, remembered in this browser only.
export const READER_MODE_KEY = "cortex.library.reader-mode.v1";

// A store that refuses to be read is the default view, not an error.
function storedReaderMode(): DocumentMode {
  try {
    return window.localStorage.getItem(READER_MODE_KEY) === "source" ? "source" : "preview";
  } catch {
    return "preview";
  }
}

function rememberReaderMode(mode: DocumentMode) {
  try {
    window.localStorage.setItem(READER_MODE_KEY, mode);
  } catch {
    // A refused store still switches the view; only the memory is lost.
  }
}

function subscribeReaderMode(onChange: () => void) {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

type DocumentState =
  | { status: "ready"; document: SourceDocument }
  | { status: "too_large" }
  | { status: "error"; message: string };

function documentTooLarge(error: unknown): boolean {
  return error instanceof ControlProblemError &&
    (error.problem.status === 413 || error.problem.category === "source_document_too_large");
}

type ImageReference = { kind: "local"; path: string } | { kind: "remote"; url: URL } | { kind: "refused" };

// What one Markdown image reference is. Only a relative reference can be the
// source's own figure, and the asset route alone decides whether it names one
// -- including whether a `papers/<dir>/assets/` prefix is this source's. The
// renderer has already emptied `data:`, `file:` and other unsafe schemes; a
// protocol-relative reference would load from whatever host it names, so it is
// refused with them. Markdown keeps a reference percent-encoded, and the route
// takes the file name, so it is decoded once.
export function imageReference(src: unknown): ImageReference {
  const value = typeof src === "string" ? src.trim() : "";
  if (!value || value.startsWith("//") || value.startsWith("\\")) return { kind: "refused" };
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)) {
    try {
      const url = new URL(value);
      if (url.protocol === "https:" || url.protocol === "http:") return { kind: "remote", url };
    } catch {
      // An absolute reference that does not parse is refused below.
    }
    return { kind: "refused" };
  }
  try {
    return { kind: "local", path: decodeURIComponent(value) };
  } catch {
    return { kind: "local", path: value };
  }
}

// A figure from the stored copy, loaded only through the source-bound asset
// route. A remote image is never loaded: it becomes text naming its host, not
// a link, because Markdown often wraps an image in a link of its own and an
// anchor inside that one would replace its target. A figure the route cannot
// serve is named, not hidden.
function SourceImage({ client, sourceId, src, alt, title }: { client: CortexControlClient; sourceId: string; src: unknown; alt?: string; title?: string }) {
  const [failed, setFailed] = useState(false);
  const reference = imageReference(src);
  if (reference.kind === "remote") {
    return <span className="text-muted-foreground" data-remote-image="" title={reference.url.href}>{phrase.remoteImage(reference.url.host)}</span>;
  }
  if (reference.kind === "refused" || failed) {
    // The renderer empties an unsafe reference, so a refused image may have
    // only its description left to say which one it was.
    const named = reference.kind === "local" ? reference.path : typeof src === "string" ? src.trim() : "";
    return (
      <span className="my-2 inline-flex max-w-full flex-col gap-1 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground" data-figure-missing="">
        <span>{reference.kind === "refused" ? copy.reader.imageNotLoaded : copy.reader.figureMissing}</span>
        {named ? <code className="font-mono text-xs [overflow-wrap:anywhere]">{named}</code> : alt ? <span>{alt}</span> : null}
      </span>
    );
  }
  // A plain element on purpose: `next/image` would add a `srcset` and fetch the
  // figure through its own optimizer, outside the gateway's access checks.
  // eslint-disable-next-line @next/next/no-img-element
  return <img alt={alt ?? ""} className="aui-md-img h-auto max-w-full" decoding="async" loading="lazy" onError={() => setFailed(true)} src={client.sourceAssetUrl(sourceId, reference.path)} title={title} />;
}

function DocumentPreview({ kind, state, components, onRetry }: { kind: SourceContentKind; state: DocumentState | undefined; components: MarkdownComponents; onRetry: () => void }) {
  return (
    <div aria-busy={!state} aria-labelledby={`source-tab-${kind}`} className="min-w-0" id={`source-panel-${kind}`} role="tabpanel">
      {!state ? <p role="status" className="research-empty text-muted-foreground!">{copy.reader.loading}</p> : null}
      {state?.status === "error" ? <div role="alert"><p>{state.message}</p><button className="source-reopen" onClick={onRetry} type="button">{copy.reader.retry}</button></div> : null}
      {state?.status === "ready" && !state.document.text ? <p className="research-empty text-muted-foreground!">{copy.reader.empty}</p> : null}
      {state?.status === "ready" && state.document.text ? <MarkdownDocument components={components} text={state.document.text} /> : null}
    </div>
  );
}

export function SourceContentReader({ client, sourceId }: { client: CortexControlClient; sourceId: string }) {
  const [kind, setKind] = useState<SourceContentKind>("notes");
  const [revision, setRevision] = useState(0);
  // A choice made here wins. Until there is one, the remembered choice is read
  // from the browser -- never while server rendering or hydrating, where it is
  // `null` and neither view starts a read the stored choice would abandon.
  const [chosen, setChosen] = useState<DocumentMode | null>(null);
  const remembered = useSyncExternalStore(subscribeReaderMode, storedReaderMode, () => null);
  const preferred = chosen ?? remembered;
  // Each (source, kind) document, read at most once per opening and in either
  // view: Preview renders it and Copy source copies it. A read for a source or
  // tab the reader has left is aborted; Reopen document starts a new opening.
  const [documents, setDocuments] = useState<Record<string, DocumentState>>({});
  const documentKey = `${sourceId}:${kind}`;
  const current = documents[documentKey];
  const pending = preferred !== null && current === undefined;
  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    client.readSourceDocument(sourceId, kind, controller.signal).then((document) => {
      if (!controller.signal.aborted) setDocuments((previous) => ({ ...previous, [documentKey]: { status: "ready", document } }));
    }, (error: unknown) => {
      if (controller.signal.aborted) return;
      setDocuments((previous) => ({ ...previous, [documentKey]: documentTooLarge(error) ? { status: "too_large" } : { status: "error", message: contentError(error) } }));
    });
    return () => controller.abort();
  }, [client, sourceId, kind, documentKey, revision, pending]);
  const components = useMemo<MarkdownComponents>(() => ({
    img: ({ src, alt, title }) => <SourceImage alt={alt} client={client} key={String(src)} sourceId={sourceId} src={src} title={title} />,
  }), [client, sourceId]);
  // A document over the preview bound is read as paged source whatever the
  // remembered choice; the choice itself is left as the operator made it.
  const oversized = current?.status === "too_large";
  const mode = oversized ? "source" : preferred;
  const changeMode = (next: DocumentMode) => {
    setChosen(next);
    rememberReaderMode(next);
  };
  const forget = (target: string) => setDocuments((previous) => {
    const next = { ...previous };
    delete next[target];
    return next;
  });
  return (
    <div className="source-content-reader">
      <div aria-label="Source content" className="output-tabs" role="tablist">
        {KINDS.map(([value, label], index) => <button aria-selected={kind === value} aria-controls={`source-panel-${value}`} className={kind === value ? "text-foreground!" : "text-muted-foreground!"} id={`source-tab-${value}`} key={value} role="tab" tabIndex={kind === value ? 0 : -1} type="button" onClick={() => setKind(value)} onKeyDown={(event) => {
          const next = event.key === "ArrowRight" ? (index + 1) % KINDS.length : event.key === "ArrowLeft" ? (index + KINDS.length - 1) % KINDS.length : event.key === "Home" ? 0 : event.key === "End" ? KINDS.length - 1 : null;
          if (next !== null) { event.preventDefault(); setKind(KINDS[next][0]); document.getElementById(`source-tab-${KINDS[next][0]}`)?.focus(); }
        }}>{label}</button>)}
      </div>
      <DocumentViewControls className="mt-3 bg-background" controls={`source-panel-${kind}`} copyText={current?.status === "ready" && current.document.text ? current.document.text : null} markdown mode={mode ?? "preview"} onModeChange={changeMode} previewDisabled={oversized} />
      {oversized ? <p className="mb-3 text-sm text-muted-foreground" role="status">{copy.reader.tooLarge}</p> : null}
      {/* Source has its own page errors; this one names only what the failed
          whole-document read takes away, so a double failure is not two alerts. */}
      {mode === "source" && current?.status === "error" ? <div className="mb-3 text-sm text-muted-foreground" role="status"><p>{copy.reader.copyUnavailable}</p><button className="source-reopen" onClick={() => forget(documentKey)} type="button">{copy.reader.retry}</button></div> : null}
      {mode === "source" ? <ContentPages client={client} key={`${sourceId}:${kind}:${revision}`} kind={kind} sourceId={sourceId} /> : null}
      {mode === "preview" ? <DocumentPreview components={components} kind={kind} onRetry={() => forget(documentKey)} state={current} /> : null}
      <button className="source-reopen" onClick={() => { setDocuments({}); setRevision((value) => value + 1); }} type="button">Reopen document</button>
    </div>
  );
}

export function SourceKnowledgeSearch({ client, onSelect }: { client: CortexControlClient; onSelect: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SourceSearch | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  async function search(event: FormEvent) {
    event.preventDefault();
    const current = ++generation.current;
    setLoading(true); setError(null); setResult(null);
    try {
      const response = await client.searchSources(query);
      if (current === generation.current) setResult(response);
    } catch {
      if (current === generation.current) setError("Source search could not be completed. Check the query and try again.");
    } finally { if (current === generation.current) setLoading(false); }
  }
  return (
    <div className="source-knowledge-search">
      <form className="source-search-form" onSubmit={(event) => void search(event)}>
        <label htmlFor="source-query">Search stored papers</label>
        <div><input id="source-query" maxLength={1024} onChange={(event) => setQuery(event.target.value)} placeholder="Keywords in English or Chinese" value={query} /><button disabled={!query.trim() || loading} type="submit">Search sources</button><button onClick={() => { generation.current += 1; setQuery(""); setResult(null); setError(null); setLoading(false); }} type="button">Clear search</button></div>
      </form>
      {loading ? <p role="status">Searching stored papers…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {result ? <section aria-label="Source search results">
        <p className="source-search-summary">{result.results.length} {result.results.length === 1 ? "match" : "matches"} for “{result.query}” · {RETRIEVAL_LABELS[result.retrieval_mode] ?? "Search results"}</p>
        {!result.results.length ? <p>No matching passages were found. Try other keywords; this does not establish that the corpus lacks relevant research.</p> : null}
        {result.results.map((item, index) => <article className="source-search-hit" key={`${item.evidence_id}:${index}`}>
          <button onClick={() => onSelect(item.source_id)} type="button">{item.title}</button>
          <p className="source-citation">{item.canonical_id} / {item.section} / {item.evidence_id} <span title={item.content_sha256}>sha256 {item.content_sha256.slice(0, 12)}</span></p>
          <p className="source-search-excerpt">{item.excerpt}</p>
        </article>)}
      </section> : null}
    </div>
  );
}
