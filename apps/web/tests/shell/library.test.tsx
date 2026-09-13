import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SourceProjection } from "../../app/control/research-contracts";
import { LibraryView } from "../../app/shell/library-view";
import { Shell } from "../../app/shell/shell";
import type { ControlActions, ControlState } from "../../app/shell/types";
import { FakeControl } from "./fake-control";

// `tests/jsdom-setup.ts` owns the Testing Library cleanup for every suite; this
// hook also resets the query the shell reads once on mount, and unmounts first
// so the reset never lands under a mounted tree.
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  control.source("source_1", "arxiv:2401.12345", "Memory in long-horizon agents");
  control.source("source_2", "arxiv:2502.00099", "Grounded retrieval for research");
  control.sourceContent("source_1", "notes", "First line\nSecond line");
  return control;
}

function library(control: FakeControl) {
  window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1&view=library");
  return render(<Shell client={control.client()} />);
}

const PROJECTION: SourceProjection = {
  id: "source_1", authority: "arxiv", authority_id: "2401.12345", canonical_id: "arxiv:2401.12345",
  source_kind: "paper", official_title: "Memory in long-horizon agents", import_state: "imported", revision: 0,
  aliases: [], created_at: "2026-09-06T12:00:00Z", updated_at: "2026-09-07T09:30:00Z",
};

const BASE_STATE: ControlState = {
  loading: false, fatalError: null, offline: false, commandPending: false, replayState: "idle", dispatchGate: null,
  apiVersion: "v1", view: "library", showEngine: false, notice: null, workspaces: [], workspace: null, threads: [],
  archivedThreads: [], thread: null, loadedThreadId: null, messages: [], runs: [], nextRunCursor: null, run: null,
  selectedRunId: null, events: [], decisions: [], pendingDecisions: [], research: null, lastTurnOutcome: null,
  captures: [], capturesLoading: false, capturesError: null, sources: [], sourcesLoading: false, sourcesError: null,
  sourceDetail: null, sourceDetailError: null, selectedSourceId: null, capabilities: null, selectedCaptureId: null,
  researchKind: "idea", researchStatus: null, researchItems: [], researchTotal: 0, researchLimit: 100,
  researchOffset: 0, researchListLoading: false, researchListError: null, selectedResearchItemId: null,
  researchItem: null, researchItemLoading: false, researchItemError: null,
};

function stubActions(overrides: Partial<ControlActions> = {}): ControlActions {
  const noop = () => {};
  const asyncNoop = async () => {};
  return {
    setView: noop, setShowEngine: noop, dismissNotice: noop, selectWorkspace: noop,
    createWorkspace: async () => null, renameWorkspace: async () => false, selectThread: noop,
    createThread: async () => null, renameThread: async () => false, archiveThread: async () => false,
    unarchiveThread: async () => false, selectRun: noop, loadOlderRuns: noop, createRun: asyncNoop,
    runAction: asyncNoop, resolveDecision: asyncNoop, retainTurn: noop, takeRetainedTurn: () => null,
    messageCommitted: noop, runCreated: noop,
    turnOutcome: noop, capture: async () => false, decideCapture: asyncNoop, refreshCaptures: noop,
    selectSource: noop, refreshSources: noop, refreshThread: noop, retry: noop,
    openThread: async () => true,
    selectResearchKind: noop, selectResearchStatus: noop, selectResearchItem: noop,
    browseResearchItems: noop, refreshResearchItems: noop, openResearchThread: async () => true,
    ...overrides,
  };
}

describe("LibraryView", () => {
  it("lists the adopted sources and prompts for a selection", async () => {
    const control = seeded();
    library(control);
    const list = await screen.findByRole("navigation", { name: "Adopted sources" });
    const rows = within(list).getAllByRole("button");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Memory in long-horizon agentsarxiv:2401.12345paper",
      "Grounded retrieval for researcharxiv:2502.00099paper",
    ]);
    expect(screen.getByText("Pick a source to read it")).toBeTruthy();
  });

  it("asks the state hook for the clicked source", async () => {
    const selectSource = vi.fn();
    const control = seeded();
    render(<LibraryView actions={stubActions({ selectSource })} client={control.client()} state={{ ...BASE_STATE, sources: [PROJECTION] }} />);
    await userEvent.click(screen.getByRole("button", { name: /Memory in long-horizon agents/ }));
    expect(selectSource).toHaveBeenCalledWith("source_1");
  });

  it("opens the record and the reader for the selected source", async () => {
    const control = seeded();
    library(control);
    const list = await screen.findByRole("navigation", { name: "Adopted sources" });
    const row = within(list).getAllByRole("button")[0];
    await userEvent.click(row);
    expect(await screen.findByRole("heading", { name: "Memory in long-horizon agents" })).toBeTruthy();
    expect(row.getAttribute("aria-current")).toBe("true");
    expect(screen.getByText("imported")).toBeTruthy();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Notes", "Full text", "Grounding"]);
    expect(await screen.findByText("First line")).toBeTruthy();
    expect(control.gets).toContain("sources/source_1");
  });

  it("selects a source that the search returned", async () => {
    const selectSource = vi.fn();
    const control = seeded();
    render(<LibraryView actions={stubActions({ selectSource })} client={control.client()} state={{ ...BASE_STATE, sources: [PROJECTION] }} />);
    await userEvent.type(screen.getByLabelText("Search stored papers"), "memory");
    await userEvent.click(screen.getByRole("button", { name: "Search sources" }));
    const hits = await screen.findByRole("region", { name: "Source search results" });
    await userEvent.click(within(hits).getByRole("button", { name: "Memory in long-horizon agents" }));
    expect(selectSource).toHaveBeenCalledWith("source_1");
  });

  it("keeps the identifiers out of the record until the disclosure is opened", async () => {
    const control = seeded();
    render(<LibraryView actions={stubActions()} client={control.client()} state={{ ...BASE_STATE, sources: [PROJECTION], selectedSourceId: PROJECTION.id, sourceDetail: PROJECTION }} />);
    expect(screen.getByText(/Added Sep 6, 2026/)).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Details" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    const details = screen.getByRole("group", { name: "Details" });
    expect(within(details).getByText(PROJECTION.id)).toBeTruthy();
    expect(within(details).getByText(PROJECTION.authority_id)).toBeTruthy();
    expect(within(details).getByText(PROJECTION.created_at)).toBeTruthy();
  });

  it("scrolls the source list and the record pane on their own", () => {
    const control = seeded();
    const { container } = render(<LibraryView actions={stubActions()} client={control.client()} state={{ ...BASE_STATE, sources: [PROJECTION], selectedSourceId: PROJECTION.id, sourceDetail: PROJECTION }} />);
    const section = screen.getByRole("region", { name: "Library" });
    expect(section.className).toContain("lg:grid-rows-[minmax(0,1fr)]");
    expect(section.className).toContain("lg:overflow-hidden");
    const panes = section.querySelectorAll(":scope > div");
    expect(panes.length).toBe(2);
    for (const pane of panes) expect(pane.className).toContain("lg:min-h-0 lg:overflow-y-auto");
  });

  it("shows three placeholder rows while the corpus loads", () => {
    const control = seeded();
    const { container } = render(<LibraryView actions={stubActions()} client={control.client()} state={{ ...BASE_STATE, sourcesLoading: true }} />);
    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBe(3);
  });

  it("offers a retry that reloads a failed listing", async () => {
    const control = seeded();
    control.failNext = { path: /^sources$/, status: 503, category: "unavailable" };
    library(control);
    const retry = await screen.findByRole("button", { name: "Retry" });
    await userEvent.click(retry);
    const list = await screen.findByRole("navigation", { name: "Adopted sources" });
    expect(within(list).getAllByRole("button").length).toBe(2);
    // The refused listing is never recorded as a read, so the one recorded
    // read is the reload the retry asked for.
    expect(control.gets.filter((path) => path === "sources").length).toBe(1);
  });
});
