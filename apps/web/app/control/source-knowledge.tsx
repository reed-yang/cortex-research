"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ControlProblemError, CortexControlClient } from "./client";
import type { SourceContent, SourceContentKind, SourceSearch } from "./research-contracts";

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
          {!page.text ? <p className="research-empty text-muted-foreground!">This document is empty.</p> : <div className="source-document">{page.text.replace(/\n$/, "").split("\n").map((line, offset) => (
            <div className="source-document-line" key={offset}>
              <span className="source-line-number" aria-label={`Line ${page.start_line + offset}`}>{page.start_line + offset}</span>
              <span>{line || "\u00a0"}</span>
            </div>
          ))}</div>}
        </section>
      ))}
      {loading ? <p role="status" className="research-empty text-muted-foreground!">Loading source content…</p> : null}
      {error ? <div role="alert"><p>{error}</p><button disabled={loading} onClick={() => void load()} type="button">Retry content</button></div> : null}
      {!loading && !error && pages.at(-1)?.next_cursor ? <button onClick={() => void load()} type="button">Load more</button> : null}
      {!loading && !error && pages.length > 0 && !pages.at(-1)?.next_cursor ? <p className="research-empty text-muted-foreground!">End of document</p> : null}
    </div>
  );
}

export function SourceContentReader({ client, sourceId }: { client: CortexControlClient; sourceId: string }) {
  const [kind, setKind] = useState<SourceContentKind>("notes");
  const [revision, setRevision] = useState(0);
  return (
    <div className="source-content-reader">
      <div aria-label="Source content" className="output-tabs" role="tablist">
        {KINDS.map(([value, label], index) => <button aria-selected={kind === value} aria-controls={`source-panel-${value}`} className={kind === value ? "text-foreground!" : "text-muted-foreground!"} id={`source-tab-${value}`} key={value} role="tab" tabIndex={kind === value ? 0 : -1} type="button" onClick={() => setKind(value)} onKeyDown={(event) => {
          const next = event.key === "ArrowRight" ? (index + 1) % KINDS.length : event.key === "ArrowLeft" ? (index + KINDS.length - 1) % KINDS.length : event.key === "Home" ? 0 : event.key === "End" ? KINDS.length - 1 : null;
          if (next !== null) { event.preventDefault(); setKind(KINDS[next][0]); document.getElementById(`source-tab-${KINDS[next][0]}`)?.focus(); }
        }}>{label}</button>)}
      </div>
      <ContentPages client={client} key={`${sourceId}:${kind}:${revision}`} kind={kind} sourceId={sourceId} />
      <button className="source-reopen" onClick={() => setRevision((value) => value + 1)} type="button">Reopen document</button>
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
