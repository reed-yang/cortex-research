import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Decision, Thread, Workspace } from "../../app/control/contracts";
import { Sidebar } from "../../app/shell/sidebar";
import { buildThreadListAdapter } from "../../app/shell/thread-list-adapter";
import type { ControlActions, ControlState } from "../../app/shell/types";
import { FakeControl, now } from "./fake-control";


function workspace(id: string, title: string): Workspace {
  return { id, title, engine_owned: false, revision: 0, created_at: now, updated_at: now };
}

function thread(id: string, title: string, updated_at: string, archived_at: string | null = null): Thread {
  return {
    id,
    workspace_id: "ws_1",
    title,
    status: "idle",
    active_run_id: null,
    archived_at,
    engine_owned: false,
    revision: 0,
    created_at: now,
    updated_at,
  };
}

function baseState(overrides: Partial<ControlState> = {}): ControlState {
  return {
    loading: false,
    fatalError: null,
    offline: false,
    commandPending: false,
    replayState: "idle",
    dispatchGate: null,
    apiVersion: "v1",
    view: "thread",
    showEngine: false,
    notice: null,
    workspaces: [],
    workspace: null,
    threads: [],
    archivedThreads: [],
    thread: null,
    loadedThreadId: null,
    messages: [],
    runs: [],
    nextRunCursor: null,
    run: null,
    selectedRunId: null,
    events: [],
    decisions: [],
    pendingDecisions: [],
    research: null,
    lastTurnOutcome: null,
    captures: [],
    capturesLoading: false,
    capturesError: null,
    sources: [],
    sourcesLoading: false,
    sourcesError: null,
    sourceDetail: null,
    sourceDetailError: null,
    selectedSourceId: null,
    capabilities: null,
    selectedCaptureId: null,
    researchKind: "idea",
    researchStatus: null,
    researchItems: [],
    researchTotal: 0,
    researchLimit: 100,
    researchOffset: 0,
    researchListLoading: false,
    researchListError: null,
    selectedResearchItemId: null,
    researchItem: null,
    researchItemLoading: false,
    researchItemError: null,
    ...overrides,
  };
}

function fakeActions(): ControlActions {
  return {
    setView: vi.fn(),
    setShowEngine: vi.fn(),
    dismissNotice: vi.fn(),
    selectWorkspace: vi.fn(),
    createWorkspace: vi.fn(async () => null),
    renameWorkspace: vi.fn(async () => true),
    selectThread: vi.fn(),
    createThread: vi.fn(async () => null),
    renameThread: vi.fn(async () => true),
    archiveThread: vi.fn(async () => true),
    unarchiveThread: vi.fn(async () => true),
    selectRun: vi.fn(),
    loadOlderRuns: vi.fn(),
    createRun: vi.fn(async () => {}),
    runAction: vi.fn(async () => {}),
    resolveDecision: vi.fn(async () => {}),
    retainTurn: vi.fn(),
    takeRetainedTurn: vi.fn(() => null),
    messageCommitted: vi.fn(),
    runCreated: vi.fn(),
    turnOutcome: vi.fn(),
    capture: vi.fn(async () => true),
    decideCapture: vi.fn(async () => {}),
    refreshCaptures: vi.fn(),
    selectSource: vi.fn(),
    refreshSources: vi.fn(),
    refreshThread: vi.fn(),
    retry: vi.fn(),
    openThread: vi.fn(async () => true),
    selectResearchKind: vi.fn(),
    selectResearchStatus: vi.fn(),
    selectResearchItem: vi.fn(),
    browseResearchItems: vi.fn(),
    refreshResearchItems: vi.fn(),
    openResearchThread: vi.fn(async () => true),
  };
}

const DAY = 86_400_000;
const ws = workspace("ws_1", "Echo");
const alpha = thread("thread_1", "Alpha", "2026-09-06T10:00:00Z");
const beta = thread("thread_2", "Beta", "2026-09-06T11:00:00Z");
const gamma = thread("thread_3", "Gamma", "2026-09-05T09:00:00Z", now);
const fresh = thread("thread_4", "Fresh", new Date().toISOString());
const stale = thread("thread_5", "Stale", new Date(Date.now() - 3 * DAY).toISOString());

function renderSidebar(state: ControlState, actions: ControlActions) {
  const control = new FakeControl();
  render(<Sidebar actions={actions} client={control.client()} state={state} />);
  return screen.getByRole("complementary", { name: "Projects and threads" });
}

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Open navigation" }));
  return screen.findByRole("dialog", { name: "Projects and threads" });
}

describe("thread list adapter", () => {
  it("orders live threads newest first and keeps archived ones apart", () => {
    const actions = fakeActions();
    const adapter = buildThreadListAdapter(
      baseState({ workspace: ws, workspaces: [ws], threads: [alpha, beta], archivedThreads: [gamma] }),
      actions,
    );

    expect(adapter.threads?.map((entry) => entry.id)).toEqual(["thread_2", "thread_1"]);
    expect(adapter.threads?.map((entry) => entry.title)).toEqual(["Beta", "Alpha"]);
    expect(adapter.archivedThreads?.map((entry) => entry.id)).toEqual(["thread_3"]);
    expect(adapter.threads?.every((entry) => entry.status === "regular")).toBe(true);
    expect(adapter.archivedThreads?.every((entry) => entry.status === "archived")).toBe(true);
  });

  it("dates every item so the list can group them by day", () => {
    const adapter = buildThreadListAdapter(
      baseState({ threads: [alpha], archivedThreads: [gamma] }),
      fakeActions(),
    );

    const dated = (entry: unknown) => (entry as { lastMessageAt?: Date }).lastMessageAt;
    expect(dated(adapter.threads?.[0])).toBeInstanceOf(Date);
    expect(dated(adapter.threads?.[0])?.getTime()).toBe(new Date(alpha.updated_at).getTime());
    expect(dated(adapter.archivedThreads?.[0])?.getTime()).toBe(new Date(gamma.updated_at).getTime());
  });

  it("routes archive, unarchive and rename back to the matching thread", async () => {
    const actions = fakeActions();
    const adapter = buildThreadListAdapter(
      baseState({ workspace: ws, workspaces: [ws], threads: [alpha, beta], archivedThreads: [gamma] }),
      actions,
    );

    await adapter.onArchive?.("thread_1");
    await adapter.onUnarchive?.("thread_3");
    await adapter.onRename?.("thread_2", "Beta renamed");

    expect(actions.archiveThread).toHaveBeenCalledWith(alpha);
    expect(actions.unarchiveThread).toHaveBeenCalledWith(gamma);
    expect(actions.renameThread).toHaveBeenCalledWith(beta, "Beta renamed");
  });
});

describe("sidebar", () => {
  it("lists the project's threads and selects the one that is clicked", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const aside = renderSidebar(
      baseState({ workspace: ws, workspaces: [ws], threads: [alpha, beta] }),
      actions,
    );

    expect(within(aside).getByText("Alpha")).toBeTruthy();
    expect(within(aside).getByText("Beta")).toBeTruthy();

    await user.click(within(aside).getByRole("button", { name: "Beta" }));
    expect(actions.selectThread).toHaveBeenCalledWith("thread_2");
  });

  it("starts a new thread from the list header", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const aside = renderSidebar(baseState({ workspace: ws, workspaces: [ws], threads: [alpha] }), actions);

    await user.click(within(aside).getByRole("button", { name: /new thread/i }));
    expect(actions.createThread).toHaveBeenCalledWith("New thread");
  });

  it("offers rename and archive on a thread but never a delete", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const aside = renderSidebar(baseState({ workspace: ws, workspaces: [ws], threads: [alpha] }), actions);

    await user.click(within(aside).getAllByRole("button", { name: "More options" })[0]);
    expect(await screen.findByRole("menuitem", { name: "Rename" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Archive" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).toBeNull();
  });

  it("switches views from the global entries and counts what is waiting", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const control = new FakeControl();
    const pending = [
      control.decision("decision_1", "run_1", "Pick one", ["a", "b"]) as unknown as Decision,
      control.decision("decision_2", "run_1", "Pick one", ["a", "b"]) as unknown as Decision,
    ];
    const aside = renderSidebar(
      baseState({ workspace: ws, workspaces: [ws], pendingDecisions: pending }),
      actions,
    );

    const inbox = within(aside).getByRole("button", { name: /inbox/i });
    expect(within(inbox).getByText("2")).toBeTruthy();

    await user.click(inbox);
    expect(actions.setView).toHaveBeenCalledWith("inbox");
  });

  it("opens the same panel in a drawer on a narrow screen", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    renderSidebar(baseState({ workspace: ws, workspaces: [ws], threads: [alpha] }), actions);

    expect(screen.getAllByText("Alpha")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Open navigation" }));

    const drawer = await screen.findByRole("dialog", { name: "Projects and threads" });
    expect(within(drawer).getByText("Alpha")).toBeTruthy();
  });

  it("heads the list with the day each thread was last active", () => {
    const actions = fakeActions();
    const aside = renderSidebar(
      baseState({ workspace: ws, workspaces: [ws], threads: [fresh, stale] }),
      actions,
    );

    expect(within(aside).getByText("Today")).toBeTruthy();
    expect(within(aside).getByText("Earlier")).toBeTruthy();
  });

  it("closes the drawer once a thread is chosen in it", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    renderSidebar(baseState({ workspace: ws, workspaces: [ws], threads: [alpha, beta] }), actions);

    const drawer = await openDrawer(user);
    await user.click(within(drawer).getByRole("button", { name: "Beta" }));

    expect(actions.selectThread).toHaveBeenCalledWith("thread_2");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Projects and threads" })).toBeNull());
  });

  it("closes the drawer when a global entry or a project is chosen in it", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const other = workspace("ws_2", "Foxtrot");
    renderSidebar(baseState({ workspace: ws, workspaces: [ws, other], threads: [alpha] }), actions);

    let drawer = await openDrawer(user);
    await user.click(within(drawer).getByRole("button", { name: /inbox/i }));
    expect(actions.setView).toHaveBeenCalledWith("inbox");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Projects and threads" })).toBeNull());

    drawer = await openDrawer(user);
    await user.click(within(drawer).getByRole("button", { name: /Echo/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Foxtrot" }));
    expect(actions.selectWorkspace).toHaveBeenCalledWith("ws_2");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Projects and threads" })).toBeNull());
  });

  it("gives the drawer its own width and keeps its close control clear of the switcher", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    renderSidebar(baseState({ workspace: ws, workspaces: [ws], threads: [alpha] }), actions);

    const drawer = await openDrawer(user);
    // The variant's own `data-[side=left]:w-3/4` only gives way to a width written
    // with the same variant, so the drawer states its width that way.
    expect(drawer.className).toContain("data-[side=left]:w-[300px]");
    expect(drawer.className).not.toContain("data-[side=left]:w-3/4");
    expect(drawer.className).not.toContain("data-[side=left]:sm:max-w-sm");
    const switcher = within(drawer).getByRole("button", { name: /Echo/ });
    // The close control is a 44px touch target inset by 12px, so the switcher
    // reserves 56px beside it rather than the 36px a 28px control needed.
    expect(switcher.parentElement?.className).toContain("pe-14");
    expect(drawer.className).toContain("[&_[data-slot=sheet-close]]:min-w-11");
  });

  it("keeps archived threads in a section that reopens them", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const aside = renderSidebar(
      baseState({ workspace: ws, workspaces: [ws], threads: [alpha], archivedThreads: [gamma] }),
      actions,
    );

    expect(within(aside).queryByText("Gamma")).toBeNull();
    await user.click(within(aside).getByRole("button", { name: /Archived/ }));

    expect(await within(aside).findByRole("button", { name: "Gamma" })).toBeTruthy();
    await user.click(within(aside).getByRole("button", { name: "Unarchive Gamma" }));
    expect(actions.unarchiveThread).toHaveBeenCalledWith(gamma);

    await user.click(within(aside).getByRole("button", { name: "Gamma" }));
    expect(actions.selectThread).toHaveBeenCalledWith("thread_3");
  });

  it("leaves the archived section out when nothing is archived", () => {
    const aside = renderSidebar(baseState({ workspace: ws, workspaces: [ws], threads: [alpha] }), fakeActions());
    expect(within(aside).queryByRole("button", { name: /Archived/ })).toBeNull();
  });

  it("renames the current project from the switcher", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const aside = renderSidebar(baseState({ workspace: ws, workspaces: [ws] }), actions);

    await user.click(within(aside).getByRole("button", { name: /Echo/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename project…" }));

    const input = await screen.findByLabelText("Project name");
    await user.clear(input);
    await user.type(input, "Echo v2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(actions.renameWorkspace).toHaveBeenCalledWith(ws, "Echo v2"));
  });

  it("creates a project from the switcher and switches between them", async () => {
    const user = userEvent.setup();
    const actions = fakeActions();
    const other = workspace("ws_2", "Foxtrot");
    const aside = renderSidebar(baseState({ workspace: ws, workspaces: [ws, other] }), actions);

    await user.click(within(aside).getByRole("button", { name: /Echo/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Foxtrot" }));
    expect(actions.selectWorkspace).toHaveBeenCalledWith("ws_2");

    await user.click(within(aside).getByRole("button", { name: /Echo/ }));
    await user.click(await screen.findByRole("menuitem", { name: "New project…" }));

    const input = await screen.findByLabelText("Project name");
    expect(screen.getByRole("button", { name: "Create" }).hasAttribute("disabled")).toBe(true);
    await user.type(input, "Golf");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(actions.createWorkspace).toHaveBeenCalledWith("Golf"));
  });
});
