import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CortexControlClient } from "../app/control/client";
import { Shell } from "../app/shell/shell";
import { decodeSourceContent, decodeSourceSearch } from "../app/control/research-contracts";
import { SourceContentReader, SourceKnowledgeSearch } from "../app/control/source-knowledge";

const page = { source_id: "source_1", canonical_id: "arxiv:2401.12345", kind: "notes", text: "First line\nSecond line\n", content_sha256: "a".repeat(64), start_line: 1, end_line: 2, next_cursor: "next" };
const result = { query: "memory", retrieval_mode: "fts5_or", results: [{ source_id: "source_1", canonical_id: page.canonical_id, title: "Memory paper", evidence_id: "source:source_1:chunk:1", section: "Method", excerpt: "A grounded passage.", content_sha256: "b".repeat(64) }] };
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const problem = (category: string) => new Response(JSON.stringify({ type: `urn:cortex:problem:${category}`, category, status: 409, title: "/Users/private/file", owner: "cortexd", retryable: false }), { status: 409, headers: { "Content-Type": "application/problem+json" } });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }

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
});

describe("source reader", () => {
  it("shows loading, citations, all kinds, pagination and end of document", async () => {
    const user = userEvent.setup();
    const first = deferred<Response>();
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://test");
      if (fetcher.mock.calls.length === 1) return first.promise;
      return json({ ...page, kind: url.searchParams.get("kind"), text: "Last line", start_line: 3, end_line: 3, next_cursor: null });
    });
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    expect(screen.getByRole("status").textContent).toContain("Loading source content");
    await act(async () => first.resolve(json(page)));
    expect(await screen.findByText("First line")).toBeTruthy();
    expect(screen.getByLabelText("Line 2")).toBeTruthy();
    expect(screen.getByText(/L1–L2/).textContent).toContain(page.canonical_id);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Last line")).toBeTruthy();
    expect(screen.getByText("First line")).toBeTruthy();
    expect(screen.getByText("End of document")).toBeTruthy();
    expect(String(fetcher.mock.calls[1][0])).toContain("cursor=next");
    for (const name of ["Full text", "Grounding"]) {
      await user.click(screen.getByRole("tab", { name }));
      await screen.findByText("Last line");
      expect(screen.queryByText("First line")).toBeNull();
    }
    expect(String(fetcher.mock.calls.at(-1)?.[0])).toContain("kind=grounding");
  });
  it("keeps loaded pages on pagination failure and retries the same cursor", async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockResolvedValueOnce(json(page)).mockRejectedValueOnce(new Error("/tmp/private")).mockResolvedValueOnce(json({ ...page, text: "Recovered", start_line: 3, end_line: 3, next_cursor: null }));
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("First line")).toBeTruthy();
    expect(document.body.textContent).not.toContain("/tmp/");
    await user.click(screen.getByRole("button", { name: "Retry content" }));
    await screen.findByText("Recovered");
    expect(fetcher.mock.calls[1][0]).toBe(fetcher.mock.calls[2][0]);
  });
  it.each(["source_content_unavailable", "source_query_invalid"])("shows a safe recoverable %s state", async (category) => {
    render(<SourceContentReader client={new CortexControlClient({ fetcher: async () => problem(category) })} sourceId="source_1" />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(document.body.textContent).not.toContain("/Users/");
    expect(screen.getByRole("button", { name: "Reopen document" })).toBeTruthy();
  });
  it("rejects changed content during pagination", async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockResolvedValueOnce(json(page)).mockResolvedValueOnce(json({ ...page, content_sha256: "b".repeat(64), text: "Changed", start_line: 3, end_line: 3, next_cursor: null }));
    render(<SourceContentReader client={new CortexControlClient({ fetcher })} sourceId="source_1" />);
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    expect((await screen.findByRole("alert")).textContent).toContain("document changed");
    expect(screen.queryByText("Changed")).toBeNull();
  });
  it("ignores a stale tab response and supports keyboard tab navigation", async () => {
    const user = userEvent.setup();
    const stale = deferred<Response>();
    const client = new CortexControlClient({ fetcher: async (input) => String(input).endsWith("kind=notes") ? stale.promise : json({ ...page, kind: "full_text", text: "Current full text", next_cursor: null }) });
    render(<SourceContentReader client={client} sourceId="source_1" />);
    screen.getByRole("tab", { name: "Notes" }).focus();
    await user.keyboard("{ArrowRight}");
    await screen.findByText("Current full text");
    await act(async () => stale.resolve(json(page)));
    expect(screen.queryByText("First line")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Full text" }));
  });
  it("shows an explicitly empty document", async () => {
    render(<SourceContentReader client={new CortexControlClient({ fetcher: async () => json({ ...page, text: "", start_line: 0, end_line: 0, next_cursor: null }) })} sourceId="source_1" />);
    await screen.findByText("This document is empty.");
    expect(screen.queryByLabelText("Line 0")).toBeNull();
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
  await screen.findByText("First line");
  await act(async () => listing.resolve(json({ items: [source], next_cursor: null })));
  expect(await screen.findByRole("navigation", { name: "Adopted sources" })).toBeTruthy();
  expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBe(0);
  expect(screen.getByText("First line")).toBeTruthy();
  window.history.replaceState(null, "", "/");
});
