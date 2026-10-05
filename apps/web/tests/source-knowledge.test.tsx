import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MarkdownDocument } from "@/components/assistant-ui/elements/markdown-text";
import { ArtifactDocument } from "../app/control/artifact-document";
import { CortexControlClient } from "../app/control/client";
import { Shell } from "../app/shell/shell";
import { decodeSourceContent, decodeSourceDocument, decodeSourceSearch, MAX_SOURCE_DOCUMENT_BYTES } from "../app/control/research-contracts";
import { imageReference, READER_MODE_KEY, SourceContentReader, SourceKnowledgeSearch } from "../app/control/source-knowledge";
import { memoryStorage } from "./shell/fake-control";

const page = { source_id: "source_1", canonical_id: "arxiv:2401.12345", kind: "notes", text: "First line\nSecond line\n", content_sha256: "a".repeat(64), start_line: 1, end_line: 2, next_cursor: "next" };
const result = { query: "memory", retrieval_mode: "fts5_or", results: [{ source_id: "source_1", canonical_id: page.canonical_id, title: "Memory paper", evidence_id: "source:source_1:chunk:1", section: "Method", excerpt: "A grounded passage.", content_sha256: "b".repeat(64) }] };
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const problem = (category: string) => new Response(JSON.stringify({ type: `urn:cortex:problem:${category}`, category, status: 409, title: "/Users/private/file", owner: "cortexd", retryable: false }), { status: 409, headers: { "Content-Type": "application/problem+json" } });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
const documentOf = (kind = "notes", text = "First line\nSecond line\n") => ({ source_id: "source_1", canonical_id: page.canonical_id, kind, text, content_sha256: "a".repeat(64), retained_bytes: new TextEncoder().encode(text).length, redacted: false });
const isDocument = (input: RequestInfo | URL) => new URL(String(input), "http://test").pathname.endsWith("/document");
const kindOf = (input: RequestInfo | URL) => new URL(String(input), "http://test").searchParams.get("kind") ?? "notes";
// Answers every whole-document read itself and hands each paged read to
// `pages`, so a test about paging counts only the reads it is about.
function pagedFetcher(pages: (input: RequestInfo | URL) => Promise<Response>) {
  return vi.fn(async (input: RequestInfo | URL) => isDocument(input) ? json(documentOf(kindOf(input))) : pages(input));
}
const tooLarge = () => new Response(JSON.stringify({ type: "urn:cortex:problem:source_document_too_large", category: "source_document_too_large", status: 413, title: "Too large", owner: "cortexd", retryable: false }), { status: 413, headers: { "Content-Type": "application/problem+json" } });

beforeEach(() => { vi.stubGlobal("localStorage", memoryStorage()); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("source content contracts and client", () => {
  it("sends only the frozen content/search queries and checks requested identity", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => String(input).includes("/search?") ? json(result) : json(page));
    const client = new CortexControlClient({ fetcher });
    await client.getSourceContent("source_1", "notes", "next");
    await client.searchSources(" memory ");
    expect(String(fetcher.mock.calls[0][0])).toBe("/api/cortex/sources/source_1/content?kind=notes&cursor=next");
    expect(String(fetcher.mock.calls[1][0])).toBe("/api/cortex/sources/search?q=memory&limit=10");
    await expect(client.getSourceContent("other")).rejects.toThrow("identity");
    await expect(client.getSourceContent("source_1", "grounding")).rejects.toThrow("identity");
  });
  it.each(["text", "canonical_id"])("rejects a private path in content %s", (key) => {
    expect(() => decodeSourceContent({ ...page, [key]: "/Users/private/file" })).toThrow("private location");
  });
  it.each(["title", "excerpt", "evidence_id", "section"])("rejects a private path in search %s", (key) => {
    expect(() => decodeSourceSearch({ ...result, results: [{ ...result.results[0], [key]: "/home/private/file" }] })).toThrow("private location");
  });
  it("validates empty document, line range, hash and opaque cursor", () => {
    expect(decodeSourceContent({ ...page, text: "", start_line: 0, end_line: 0, next_cursor: null }).text).toBe("");
    for (const mutation of [{ start_line: 3 }, { content_sha256: "bad" }, { next_cursor: "/tmp/file" }, { kind: "pdf" }]) {
      expect(() => decodeSourceContent({ ...page, ...mutation })).toThrow();
    }
  });
  it("reads one whole document with the requested identity and an abort signal", async () => {
    const fetcher = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async (input) => json(documentOf(kindOf(input))));
    const client = new CortexControlClient({ fetcher });
    const controller = new AbortController();
    expect((await client.readSourceDocument("source_1", "full_text", controller.signal)).text).toBe("First line\nSecond line\n");
    expect(String(fetcher.mock.calls[0][0])).toBe("/api/cortex/sources/source_1/document?kind=full_text");
    expect(fetcher.mock.calls[0][1]?.signal).toBe(controller.signal);
    await expect(client.readSourceDocument("other", "notes")).rejects.toThrow("identity");
    expect(client.sourceAssetUrl("source 1", "assets/fig 1.png")).toBe("/api/cortex/sources/source%201/asset?path=assets%2Ffig+1.png");
  });
  it("bounds a document's text by its UTF-8 bytes, not its string length", () => {
    const ascii = "a".repeat(MAX_SOURCE_DOCUMENT_BYTES);
    expect(decodeSourceDocument(documentOf("notes", ascii)).text.length).toBe(MAX_SOURCE_DOCUMENT_BYTES);
    expect(() => decodeSourceDocument({ ...documentOf("notes", ascii + "a"), retained_bytes: 1 })).toThrow("2 MiB");
    // 699,050 three-byte characters are 2,097,150 bytes; one more is over the
    // bound although the string is a third of its length.
    const wide = "中".repeat(699_050);
    expect(decodeSourceDocument(documentOf("notes", wide)).text).toBe(wide);
    expect(() => decodeSourceDocument({ ...documentOf("notes", wide + "中"), retained_bytes: 1 })).toThrow("2 MiB");
    expect(() => decodeSourceDocument({ ...documentOf(), retained_bytes: MAX_SOURCE_DOCUMENT_BYTES + 1 })).toThrow("2 MiB");
  });
  it("validates the rest of the document projection", () => {
    expect(decodeSourceDocument({ ...documentOf("grounding", ""), redacted: true })).toMatchObject({ kind: "grounding", text: "", redacted: true });
    for (const mutation of [{ kind: "pdf" }, { content_sha256: "bad" }, { redacted: "no" }, { retained_bytes: -1 }, { text: "/Users/private/file" }, { canonical_id: "/home/private" }, { source_id: "a/b" }]) {
      expect(() => decodeSourceDocument({ ...documentOf(), ...mutation })).toThrow();
    }
  });
});

// The paged view is Source; these tests open in it the way a returning reader
// does, from the remembered choice.
describe("source reader", () => {
  beforeEach(() => { window.localStorage.setItem(READER_MODE_KEY, "source"); });
  it("shows loading, citations, all kinds, pagination and end of document", async () => {
    const user = userEvent.setup();
    const first = deferred<Response>();
    const pages = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://test");
      if (pages.mock.calls.length === 1) return first.promise;
      return json({ ...page, kind: url.searchParams.get("kind"), text: "Last line", start_line: 3, end_line: 3, next_cursor: null });
    });
    render(<SourceContentReader client={new CortexControlClient({ fetcher: pagedFetcher(pages) })} sourceId="source_1" />);
    expect(screen.getByText("Loading source content…")).toBeTruthy();
    await act(async () => first.resolve(json(page)));
    expect(await screen.findByText("First line")).toBeTruthy();
    expect(screen.getByLabelText("Line 2")).toBeTruthy();
    expect(screen.getByText(/L1–L2/).textContent).toContain(page.canonical_id);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Last line")).toBeTruthy();
    expect(screen.getByText("First line")).toBeTruthy();
    expect(screen.getByText("End of document")).toBeTruthy();
    expect(String(pages.mock.calls[1][0])).toContain("cursor=next");
    for (const name of ["Full text", "Grounding"]) {
      await user.click(screen.getByRole("tab", { name }));
      await screen.findByText("Last line");
      expect(screen.queryByText("First line")).toBeNull();
    }
    expect(String(pages.mock.calls.at(-1)?.[0])).toContain("kind=grounding");
  });
  it("keeps loaded pages on pagination failure and retries the same cursor", async () => {
    const user = userEvent.setup();
    const pages = vi.fn().mockResolvedValueOnce(json(page)).mockRejectedValueOnce(new Error("/tmp/private")).mockResolvedValueOnce(json({ ...page, text: "Recovered", start_line: 3, end_line: 3, next_cursor: null }));
    render(<SourceContentReader client={new CortexControlClient({ fetcher: pagedFetcher(pages) })} sourceId="source_1" />);
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("First line")).toBeTruthy();
    expect(document.body.textContent).not.toContain("/tmp/");
    await user.click(screen.getByRole("button", { name: "Retry content" }));
    await screen.findByText("Recovered");
    expect(pages.mock.calls[1][0]).toBe(pages.mock.calls[2][0]);
  });
  it.each(["source_content_unavailable", "source_query_invalid"])("shows a safe recoverable %s state", async (category) => {
    render(<SourceContentReader client={new CortexControlClient({ fetcher: async () => problem(category) })} sourceId="source_1" />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(document.body.textContent).not.toContain("/Users/");
    expect(screen.getByRole("button", { name: "Reopen document" })).toBeTruthy();
  });
  it("rejects changed content during pagination", async () => {
    const user = userEvent.setup();
    const pages = vi.fn().mockResolvedValueOnce(json(page)).mockResolvedValueOnce(json({ ...page, content_sha256: "b".repeat(64), text: "Changed", start_line: 3, end_line: 3, next_cursor: null }));
    render(<SourceContentReader client={new CortexControlClient({ fetcher: pagedFetcher(pages) })} sourceId="source_1" />);
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    expect((await screen.findByRole("alert")).textContent).toContain("document changed");
    expect(screen.queryByText("Changed")).toBeNull();
  });
  it("ignores a stale tab response and supports keyboard tab navigation", async () => {
    const user = userEvent.setup();
    const stale = deferred<Response>();
    const client = new CortexControlClient({ fetcher: pagedFetcher(async (input) => String(input).endsWith("kind=notes") ? stale.promise : json({ ...page, kind: "full_text", text: "Current full text", next_cursor: null })) });
    render(<SourceContentReader client={client} sourceId="source_1" />);
    screen.getByRole("tab", { name: "Notes" }).focus();
    await user.keyboard("{ArrowRight}");
    await screen.findByText("Current full text");
    await act(async () => stale.resolve(json(page)));
    expect(screen.queryByText("First line")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Full text" }));
  });
  it("shows an explicitly empty document", async () => {
    render(<SourceContentReader client={new CortexControlClient({ fetcher: pagedFetcher(async () => json({ ...page, text: "", start_line: 0, end_line: 0, next_cursor: null })) })} sourceId="source_1" />);
    await screen.findByText("This document is empty.");
    expect(screen.queryByLabelText("Line 0")).toBeNull();
  });
});

// The Library reader around the paged view: Preview by default, Source on
// request, the choice remembered, and each whole document read once.
describe("library reader views", () => {
  const preview = /^First line\s+Second line/;
  const pressed = (name: string) => screen.getByRole("button", { name }).getAttribute("aria-pressed");
  const disabled = (name: string) => (screen.getByRole("button", { name }) as HTMLButtonElement).disabled;

  it("opens in Preview, switches to the paged Source and remembers the choice", async () => {
    const user = userEvent.setup();
    const fetcher = pagedFetcher(async () => json(page));
    const client = new CortexControlClient({ fetcher });
    const first = render(<SourceContentReader client={client} sourceId="source_1" />);
    expect(await screen.findByText(preview)).toBeTruthy();
    expect(pressed("Preview")).toBe("true");
    expect(screen.queryByLabelText("Line 1")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(await screen.findByLabelText("Line 2")).toBeTruthy();
    expect(screen.getByText(/L1–L2/)).toBeTruthy();
    expect(window.localStorage.getItem(READER_MODE_KEY)).toBe("source");
    // The document is read once, not again for each view.
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText(preview)).toBeTruthy();
    expect(fetcher.mock.calls.filter(([input]) => isDocument(input))).toHaveLength(1);
    expect(window.localStorage.getItem(READER_MODE_KEY)).toBe("preview");
    await user.click(screen.getByRole("button", { name: "Source" }));
    first.unmount();
    render(<SourceContentReader client={client} sourceId="source_1" />);
    expect(pressed("Source")).toBe("true");
    expect(await screen.findByText(/L1–L2/)).toBeTruthy();
  });

  it("still reads and switches views when the browser refuses its storage", async () => {
    const refuse = () => { throw new DOMException("denied", "SecurityError"); };
    vi.stubGlobal("localStorage", { ...memoryStorage(), getItem: refuse, setItem: refuse });
    const user = userEvent.setup();
    render(<SourceContentReader client={new CortexControlClient({ fetcher: pagedFetcher(async () => json(page)) })} sourceId="source_1" />);
    expect(await screen.findByText(preview)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(await screen.findByLabelText("Line 2")).toBeTruthy();
    expect(pressed("Source")).toBe("true");
  });

  it("aborts the read of a tab or source the reader has left, and keeps a finished one", async () => {
    const user = userEvent.setup();
    const reads: Array<{ url: string; signal: AbortSignal }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!isDocument(input)) return json(page);
      reads.push({ url: String(input), signal: init!.signal! });
      // The first and the fourth read never answer on their own.
      if (reads.length === 1 || reads.length === 4) return new Promise<Response>(() => {});
      const source = new URL(String(input), "http://test").pathname.split("/")[4]!;
      const kind = kindOf(input);
      return json({ ...documentOf(kind, `${kind === "notes" ? "Notes" : "Full text"} body of ${source}\n`), source_id: source });
    });
    const client = new CortexControlClient({ fetcher });
    const view = render(<SourceContentReader client={client} sourceId="source_1" />);
    await waitFor(() => expect(reads).toHaveLength(1));
    await user.click(screen.getByRole("tab", { name: "Full text" }));
    expect(reads[0]!.signal.aborted).toBe(true);
    expect(await screen.findByText("Full text body of source_1")).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "Notes" }));
    expect(await screen.findByText("Notes body of source_1")).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "Full text" }));
    expect(await screen.findByText("Full text body of source_1")).toBeTruthy();
    expect(reads).toHaveLength(3);
    view.rerender(<SourceContentReader client={client} sourceId="source_2" />);
    await waitFor(() => expect(reads).toHaveLength(4));
    expect(screen.queryByText("Full text body of source_1")).toBeNull();
    view.rerender(<SourceContentReader client={client} sourceId="source_1" />);
    expect(reads[3]!.signal.aborted).toBe(true);
    expect(await screen.findByText("Full text body of source_1")).toBeTruthy();
    expect(reads).toHaveLength(4);
  });

  it("opens a tab over the preview bound as paged source with a one-line notice", async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      if (!isDocument(input)) return json({ ...page, kind: kindOf(input) });
      return kindOf(input) === "full_text" ? tooLarge() : json(documentOf(kindOf(input)));
    });
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    expect(await screen.findByText(preview)).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "Full text" }));
    expect(await screen.findByText("This document is too large to preview, so it opens as paged source.")).toBeTruthy();
    expect(await screen.findByLabelText("Line 2")).toBeTruthy();
    expect(pressed("Source")).toBe("true");
    expect(disabled("Preview")).toBe(true);
    expect(disabled("Copy source")).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    // The remembered choice is the operator's, so the next tab previews again.
    expect(window.localStorage.getItem(READER_MODE_KEY)).toBeNull();
    await user.click(screen.getByRole("tab", { name: "Notes" }));
    expect(await screen.findByText(preview)).toBeTruthy();
    expect(screen.queryByText(/too large to preview/)).toBeNull();
  });

  it("keeps any other preview failure retryable without leaking the problem", async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockResolvedValueOnce(problem("source_content_unavailable")).mockResolvedValueOnce(json(documentOf()));
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("missing or could not be verified");
    expect(document.body.textContent).not.toContain("/Users/");
    await user.click(within(alert).getByRole("button", { name: "Retry content" }));
    expect(await screen.findByText(preview)).toBeTruthy();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("copies the whole document, in either view, only once it has loaded", async () => {
    const user = userEvent.setup();
    const answer = deferred<Response>();
    const whole = "# Heading\n\nFirst passage.\n\nSecond passage.\n";
    const fetcher = vi.fn(async (input: RequestInfo | URL) => isDocument(input) ? answer.promise : json(page));
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    expect(disabled("Copy source")).toBe(true);
    await act(async () => answer.resolve(json(documentOf("notes", whole))));
    expect(await screen.findByRole("heading", { name: "Heading" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(await screen.findByLabelText("Line 2")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Copy source" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy());
    expect(await navigator.clipboard.readText()).toBe(whole);
  });

  it("shows a failed document read and its Retry in Source too", async () => {
    window.localStorage.setItem(READER_MODE_KEY, "source");
    const user = userEvent.setup();
    let failures = 1;
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      if (!isDocument(input)) return json(page);
      return failures-- > 0 ? problem("source_content_unavailable") : json(documentOf());
    });
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    expect(await screen.findByLabelText("Line 2")).toBeTruthy();
    const notice = (await screen.findByText("The whole document could not be read, so Copy source is unavailable.")).parentElement!;
    expect(disabled("Copy source")).toBe(true);
    await user.click(within(notice).getByRole("button", { name: "Retry content" }));
    await waitFor(() => expect(disabled("Copy source")).toBe(false));
    expect(screen.queryByText(/Copy source is unavailable/)).toBeNull();
    expect(screen.getByLabelText("Line 2")).toBeTruthy();
  });

  it("leaves Copy source disabled for an empty document", async () => {
    render(<SourceContentReader client={new CortexControlClient({ fetcher: async (input) => isDocument(input) ? json(documentOf("notes", "")) : json(page) })} sourceId="source_1" />);
    expect(await screen.findByText(/empty/i)).toBeTruthy();
    expect(disabled("Copy source")).toBe(true);
  });
});

describe("library figures", () => {
  const MARKDOWN = [
    "# Method", "Inline $x^2$.",
    "![Figure 1](assets/fig1.png)", "![Old prefix](papers/2401.12345/assets/fig2.png)", "![Spaced](<./assets/fig 3.png>)",
    "![Missing](assets/missing.png)", "![Remote](https://images.example.org/plot.png)",
    "![Elsewhere](//cdn.example.org/plot.png)", "![Inline](data:image/png;base64,AAAA)",
  ].join("\n\n");
  const reader = () => render(<SourceContentReader client={new CortexControlClient({ fetcher: async (input) => isDocument(input) ? json(documentOf("notes", MARKDOWN)) : json(page) })} sourceId="source_1" />);

  it("loads a local figure only through the source's asset route, lazily and width-capped", async () => {
    reader();
    const figure = await screen.findByRole("img", { name: "Figure 1" });
    expect(figure.getAttribute("src")).toBe("/api/cortex/sources/source_1/asset?path=assets%2Ffig1.png");
    expect(figure.getAttribute("loading")).toBe("lazy");
    expect(figure.hasAttribute("srcset")).toBe(false);
    expect(figure.className).toContain("max-w-full");
    // The route, not the page, decides whether this prefix is the source's own.
    expect(screen.getByRole("img", { name: "Old prefix" }).getAttribute("src")).toBe("/api/cortex/sources/source_1/asset?path=papers%2F2401.12345%2Fassets%2Ffig2.png");
    expect(screen.getByRole("img", { name: "Spaced" }).getAttribute("src")).toBe("/api/cortex/sources/source_1/asset?path=.%2Fassets%2Ffig+3.png");
    expect(document.querySelector(".katex")).not.toBeNull();
    const loaded = [...document.querySelectorAll("img")].map((image) => image.getAttribute("src"));
    expect(loaded).toHaveLength(4);
    expect(loaded.every((src) => src?.startsWith("/api/cortex/sources/source_1/asset?path="))).toBe(true);
  });

  it("names a remote image by its host and loads no other reference", async () => {
    reader();
    const remote = await screen.findByText("Image on images.example.org");
    expect(remote.closest("a")).toBeNull();
    expect(remote.getAttribute("title")).toBe("https://images.example.org/plot.png");
    expect(screen.getAllByText("Image not loaded from this reference")).toHaveLength(2);
    expect(screen.getByText("//cdn.example.org/plot.png")).toBeTruthy();
    expect(screen.getByText("Inline")).toBeTruthy();
    expect(document.querySelector('img[src*="example.org"], img[src^="data:"]')).toBeNull();
  });

  it("keeps the target of a linked remote image without nesting links", async () => {
    render(<SourceContentReader client={new CortexControlClient({ fetcher: async (input) => isDocument(input) ? json(documentOf("notes", "[![badge](https://images.example.org/plot.png)](https://paper.example.org/results)")) : json(page) })} sourceId="source_1" />);
    const label = await screen.findByText("Image on images.example.org");
    expect(label.closest("a")?.getAttribute("href")).toBe("https://paper.example.org/results");
    expect(document.querySelector("a a")).toBeNull();
  });

  it("names a figure the stored copy does not hold", async () => {
    reader();
    fireEvent.error(await screen.findByRole("img", { name: "Missing" }));
    expect(await screen.findByText("Figure not in the stored copy")).toBeTruthy();
    expect(screen.getByText("assets/missing.png")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "Missing" })).toBeNull();
    expect(screen.getByRole("img", { name: "Figure 1" })).toBeTruthy();
  });

  it("leaves chat and Outputs image rendering unchanged", () => {
    for (const element of [<MarkdownDocument key="chat" text={MARKDOWN} />, <ArtifactDocument key="outputs" content={{ media_type: "text/markdown", content: MARKDOWN }} />]) {
      const { container, unmount } = render(element);
      const loaded = [...container.querySelectorAll("img")].map((image) => image.getAttribute("src"));
      expect(loaded).toContain("assets/fig1.png");
      expect(loaded).toContain("https://images.example.org/plot.png");
      expect(loaded.some((src) => src?.includes("/asset?"))).toBe(false);
      expect(container.textContent).not.toContain("Image on");
      unmount();
    }
  });

  it("classifies a reference before anything is requested", () => {
    expect(imageReference("assets/fig%201.png")).toEqual({ kind: "local", path: "assets/fig 1.png" });
    expect(imageReference("./assets/a.png")).toEqual({ kind: "local", path: "./assets/a.png" });
    expect(imageReference("assets/%E0%A4.png")).toEqual({ kind: "local", path: "assets/%E0%A4.png" });
    expect(imageReference("HTTPS://Images.Example.org/a.png")).toMatchObject({ kind: "remote" });
    for (const refused of ["", "  ", "//cdn.example.org/a.png", "\\\\host\\a.png", "data:image/png;base64,AA", "file:///etc/hosts", "javascript:alert(1)", "blob:https://example.org/x", undefined, 3]) {
      expect(imageReference(refused).kind, String(refused)).toBe("refused");
    }
  });
});

describe("source search", () => {
  it("shows loading, evidence and opens the existing source detail", async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    const select = vi.fn();
    render(<SourceKnowledgeSearch client={new CortexControlClient({ fetcher: async () => pending.promise })} onSelect={select} />);
    await user.type(screen.getByLabelText("Search stored papers"), "memory");
    await user.click(screen.getByRole("button", { name: "Search sources" }));
    expect(screen.getByRole("status").textContent).toContain("Searching");
    await act(async () => pending.resolve(json(result)));
    await user.click(await screen.findByRole("button", { name: "Memory paper" }));
    expect(select).toHaveBeenCalledWith("source_1");
    expect(screen.getByText(/source:source_1:chunk:1/)).toBeTruthy();
  });
  it("reports empty results without claiming absent research, and retries errors", async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockRejectedValueOnce(new Error("/tmp/private")).mockResolvedValueOnce(json({ ...result, results: [] }));
    render(<SourceKnowledgeSearch client={new CortexControlClient({ fetcher })} onSelect={() => {}} />);
    await user.type(screen.getByLabelText("Search stored papers"), "memory");
    await user.click(screen.getByRole("button", { name: "Search sources" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(document.body.textContent).not.toContain("/tmp/");
    await user.click(screen.getByRole("button", { name: "Search sources" }));
    await screen.findByText(/does not establish/);
  });
  it("clears search while a response is pending", async () => {
    const user = userEvent.setup();
    const pending = deferred<Response>();
    render(<SourceKnowledgeSearch client={new CortexControlClient({ fetcher: async () => pending.promise })} onSelect={() => {}} />);
    await user.type(screen.getByLabelText("Search stored papers"), "memory");
    await user.click(screen.getByRole("button", { name: "Search sources" }));
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    await act(async () => pending.resolve(json(result)));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Source search results" })).toBeNull());
  });
});


it("can open a search hit while the source index is still loading", async () => {
  const user = userEvent.setup();
  const listing = deferred<Response>();
  const now = "2026-09-06T12:00:00Z";
  const source = { id: "source_1", authority: "arxiv", authority_id: "2401.12345", canonical_id: page.canonical_id, official_title: "Memory paper", source_kind: "paper", import_state: "existing", revision: 0, aliases: [], created_at: now, updated_at: now };
  const workspace = { id: "ws_1", title: "Echo memory", engine_owned: false, revision: 0, created_at: now, updated_at: now };
  const client = new CortexControlClient({ fetcher: async (input) => {
    const url = new URL(String(input), "http://test");
    if (url.pathname.endsWith("/workspaces")) return json({ items: [workspace], next_cursor: null });
    // The shell reads the project named in the URL as a row before it lists
    // its threads; a list-shaped fallback would fail to decode and the shell
    // would show its "unavailable" state instead of the Library.
    if (url.pathname.endsWith("/workspaces/ws_1")) return json(workspace);
    if (url.pathname.endsWith("/sources")) return listing.promise;
    if (url.pathname.endsWith("/sources/search")) return json(result);
    if (url.pathname.endsWith("/sources/source_1/content")) return json(page);
    if (url.pathname.endsWith("/sources/source_1/document")) return json(documentOf());
    if (url.pathname.endsWith("/sources/source_1")) return json(source);
    return json({ items: [], next_cursor: null });
  } });
  // The Library is the shell's own view of the corpus, and it is opened the way
  // a reload opens it: from the query the shell reads once on mount.
  window.history.replaceState(null, "", "/?project=ws_1&view=library");
  const { container } = render(<Shell client={client} />);
  await user.type(await screen.findByLabelText("Search stored papers"), "memory");
  await user.click(screen.getByRole("button", { name: "Search sources" }));
  await user.click(await screen.findByRole("button", { name: "Memory paper" }));
  await screen.findByText(/^First line\s+Second line/);
  await act(async () => listing.resolve(json({ items: [source], next_cursor: null })));
  expect(await screen.findByRole("navigation", { name: "Adopted sources" })).toBeTruthy();
  expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBe(0);
  expect(screen.getByText(/^First line\s+Second line/)).toBeTruthy();
  window.history.replaceState(null, "", "/");
});
