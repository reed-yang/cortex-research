"use client";

import { memo, useId, useMemo, useState } from "react";
import { CodeIcon, CopyIcon, EyeIcon } from "lucide-react";
import { MarkdownDocument, type MarkdownComponents } from "@/components/assistant-ui/elements/markdown-text";
import { Button } from "@/components/ui/button";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { cn } from "@/lib/utils";

// The reader needs the bytes and what they are, and nothing else. Narrowed to
// those two fields so a verified research document -- which carries a document
// version identity, not an artifact one -- shares the same Preview/Source
// reader without being given a borrowed artifact id.
export type ReadableDocument = {
  media_type: "text/markdown" | "text/plain";
  content: string;
};

const PROVENANCE_MARKER = "\n\n## Retained source provenance\n\n```json\n";

// Only recognize the exact appendix shape emitted by ResearchService. Unknown
// or malformed text remains part of the document; stored bytes are never edited.
export function splitRetainedProvenance(text: string, generated: boolean) {
  const unchanged = { body: text, appendix: "", sources: 0, documents: 0 };
  if (!generated || !text.endsWith("\n```\n")) return unchanged;
  const start = text.lastIndexOf(PROVENANCE_MARKER);
  if (start < 0) return unchanged;
  try {
    const data: unknown = JSON.parse(text.slice(start + PROVENANCE_MARKER.length, -5));
    if (!data || typeof data !== "object" || Array.isArray(data)) return unchanged;
    const record = data as Record<string, unknown>;
    if (!Array.isArray(record.sources) || !/^[a-f0-9]{64}$/.test(String(record.context_sha256))) return unchanged;
    if (!["run_id", "attempt_id", "runtime_release_id", "runtime_worker_protocol", "retrieval_mode", "citation_status"]
      .every((key) => typeof record[key] === "string" && record[key] !== "")) return unchanged;
    return { body: text.slice(0, start), appendix: text.slice(start), sources: record.sources.length, documents: Array.isArray(record.documents) ? record.documents.length : 0 };
  } catch {
    return unchanged;
  }
}

export type DocumentMode = "preview" | "source";

// The Preview / Source / Copy source bar, shared by every reader of a stored
// document. `copyText` is what Copy source writes, or `null` while it is not
// yet available, which disables the button rather than copying a part.
export function DocumentViewControls({ markdown, mode, onModeChange, copyText, controls, previewDisabled = false, className }: {
  markdown: boolean;
  mode: DocumentMode;
  onModeChange: (mode: DocumentMode) => void;
  copyText: string | null;
  controls: string;
  previewDisabled?: boolean;
  className?: string;
}) {
  const { isCopied, copyToClipboard } = useCopyToClipboard();
  return (
    <div className={cn("sticky top-0 z-10 mb-3 flex flex-wrap items-center gap-2 bg-card py-1", className)} role="group" aria-label="Document view">
      {markdown ? <>
        <Button type="button" className="min-h-11" variant={mode === "preview" ? "secondary" : "ghost"} aria-pressed={mode === "preview"} aria-controls={controls} disabled={previewDisabled} onClick={() => onModeChange("preview")}><EyeIcon aria-hidden="true" />Preview</Button>
        <Button type="button" className="min-h-11" variant={mode === "source" ? "secondary" : "ghost"} aria-pressed={mode === "source"} aria-controls={controls} onClick={() => onModeChange("source")}><CodeIcon aria-hidden="true" />Source</Button>
      </> : <span className="text-sm text-muted-foreground">Plain text</span>}
      <Button type="button" className="ms-auto min-h-11" variant="ghost" title={isCopied ? "Copied" : "Copy source"} aria-label={isCopied ? "Copied" : "Copy source"} disabled={copyText === null} onClick={() => { if (copyText !== null) copyToClipboard(copyText); }}><CopyIcon aria-hidden="true" /><span className="hidden sm:inline">{isCopied ? "Copied" : "Copy source"}</span></Button>
      <span className="sr-only" role="status">{isCopied ? "Source copied to clipboard" : ""}</span>
    </div>
  );
}

// `mode` and `onModeChange` make the view controlled by a caller that keeps
// the choice itself; without them the document starts in Preview and keeps
// its own. `components` reaches only this document's Markdown renderer.
export const ArtifactDocument = memo(function ArtifactDocument({ content, hasRetainedProvenance = false, mode: controlledMode, onModeChange, components }: {
  content: ReadableDocument;
  hasRetainedProvenance?: boolean;
  mode?: DocumentMode;
  onModeChange?: (mode: DocumentMode) => void;
  components?: MarkdownComponents;
}) {
  const [ownMode, setOwnMode] = useState<DocumentMode>("preview");
  const mode = controlledMode ?? ownMode;
  const changeMode = (next: DocumentMode) => {
    if (controlledMode === undefined) setOwnMode(next);
    onModeChange?.(next);
  };
  const regionId = useId();
  const markdown = content.media_type === "text/markdown";
  const { body, appendix, sources, documents } = useMemo(
    () => splitRetainedProvenance(content.content, markdown && hasRetainedProvenance),
    [content.content, markdown, hasRetainedProvenance],
  );
  return (
    <div className="artifact-content min-w-0">
      <DocumentViewControls markdown={markdown} mode={mode} onModeChange={changeMode} copyText={content.content} controls={regionId} />
      <div id={regionId} role="region" aria-label={markdown ? (mode === "preview" ? "Rendered Markdown" : "Markdown source") : "Plain text document"}>
        {mode === "preview" && markdown ? <MarkdownDocument text={body} components={components} /> : <pre className="artifact-source">{body}</pre>}
      </div>
      {appendix ? <details className="artifact-provenance mt-5 border-t border-border pt-2" data-details>
        <summary className="cursor-pointer py-3 text-sm text-muted-foreground">Source provenance · {sources} {sources === 1 ? "source" : "sources"}{documents > 0 ? ` · ${documents} ${documents === 1 ? "document" : "documents"}` : ""}</summary>
        <p className="text-sm text-muted-foreground">Retained source records. Included in Copy source.</p>
        <pre className="artifact-source">{appendix}</pre>
      </details> : null}
    </div>
  );
});
