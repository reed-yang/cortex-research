import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Shell } from "../app/shell/shell";
import { copy } from "../app/shell/copy";
import { FakeControl, now } from "./shell/fake-control";

// Acceptance for the links the product hands out: a `?project=…&thread=…` query
// has to open exactly the conversation it names, or say plainly that it cannot.
// The shell owns the selection code; this exercises it from the research lane
// because the research Outputs panel is what a research link points at, and it
// runs against the same in-process fake Control every shell test uses -- local
// fixture evidence, not acceptance of the deployed origin.
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function seeded(): FakeControl {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.workspace("ws_2", "Second project");
  control.thread("thread_open", "ws_1", "Named conversation", { active_run_id: "run_named" });
  control.thread("thread_archived", "ws_1", "Retired conversation", { archived_at: "2026-09-01T00:00:00Z" });
  control.thread("thread_elsewhere", "ws_2", "Conversation in the other project");
  control.run("run_named", "thread_open", "completed");
  control.research.run_named = {
    schema_version: 1,
    run: { id: "run_named", thread_id: "thread_open", state: "completed", stage: null, active_attempt_id: null, latest_sequence: 0, engine_owned: false, revision: 0, created_at: now, updated_at: now },
    workflow: null,
    source_gates: [],
    sources: [],
    lineage: { nodes: [], links: [], successor_node_id: null },
    decisions: [],
    artifacts: [],
    snapshots: [],
  };
  return control;
}

function open(control: FakeControl, query: string) {
  window.history.replaceState(null, "", query);
  return render(<Shell client={control.client()} />);
}

// The open conversation is the one the thread pane is headed by, which is true
// for an archived thread as well -- the sidebar files that one under its own
// collapsed group, so the rail is not the place to ask.
const threadPane = () => screen.getByRole("region", { name: copy.thread.region });
// The pane also carries the runtime's own welcome heading, so the title is
// read from the header rather than from "the first h1 in here".
const openThread = () => threadPane().querySelector("header h1")?.textContent;
const notice = () => screen.getByRole("status", { name: copy.notice.region });

describe("research deep links", () => {
  it("opens exactly the project and thread the query names", async () => {
    const control = seeded();
    open(control, "/?project=ws_1&thread=thread_open");
    await waitFor(() => expect(openThread()).toBe("Named conversation"));
    // The query the link carried survives the shell writing its own location
    // back, so a refresh of this tab reopens the same conversation.
    expect(window.location.search).toContain("project=ws_1");
    expect(window.location.search).toContain("thread=thread_open");
  });

  it("still lands on the named thread when Control answers slowly", async () => {
    const control = seeded();
    // The thread list is held past mount, so the shell has to make its choice
    // from data that arrives late rather than from what it had at first paint
    // -- the refresh case, where an eager fallback selection would be wrong.
    let release = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    let first = true;
    control.beforeResponse = async (path) => {
      if (first && path.startsWith("threads?")) { first = false; await held; }
    };
    open(control, "/?project=ws_1&thread=thread_open");
    expect(openThread()).toBe(copy.thread.untitled);
    release();
    await waitFor(() => expect(openThread()).toBe("Named conversation"));
  });

  it("opens an archived thread the query names instead of hiding it", async () => {
    const control = seeded();
    open(control, "/?project=ws_1&thread=thread_archived");
    await waitFor(() => expect(openThread()).toBe("Retired conversation"));
    // It is opened as what it is, and the archived read is what makes it
    // reachable by a link at all.
    expect(screen.getAllByText(copy.thread.archived).length).toBeGreaterThan(0);
    expect(control.gets.some((path) => path.includes("include_archived=true"))).toBe(true);
  });

  it("opens nothing and says so when the thread is not in that project", async () => {
    const control = seeded();
    open(control, "/?project=ws_1&thread=thread_elsewhere");
    await waitFor(() => expect(notice().textContent).toContain(copy.notice.unknownThread));
    // Silently opening a different conversation than the link asked for would
    // be the failure this notice exists to prevent.
    expect(screen.getByText(copy.thread.noThread)).not.toBeNull();
  });

  it("falls back and drops the thread hint when the project is gone", async () => {
    const control = seeded();
    open(control, "/?project=ws_missing&thread=thread_open");
    await waitFor(() => expect(notice().textContent).toContain(copy.notice.unknownProject));
    // The thread belonged to the project that is gone, so the hint goes with
    // it rather than being applied to whatever opened instead.
    await waitFor(() => expect(window.location.search).not.toContain("ws_missing"));
    expect(window.location.search).toContain("project=ws_1");
  });

  it("refuses an identifier that is not one, without reaching Control", async () => {
    const control = seeded();
    open(control, "/?project=ws_1&thread=not%20a%20valid%20id%21");
    await waitFor(() => expect(openThread()).toBe("Named conversation"));
    // `readShellLocation` drops a malformed id, so it never becomes a request
    // and never becomes an unknown-thread notice either.
    expect(control.gets.some((path) => path.includes("not%20a"))).toBe(false);
    expect(screen.queryByRole("status", { name: copy.notice.region })).toBeNull();
  });

  it("reaches the research Outputs panel for the thread the link names", async () => {
    const control = seeded();
    open(control, "/?project=ws_1&thread=thread_open");
    // The panel exists only when the named thread's run carries a research
    // projection, so finding it proves the link resolved all the way through
    // run selection to the research read.
    await waitFor(() => expect(screen.getByLabelText(copy.thread.outputs)).not.toBeNull());
    expect(control.gets).toContain("runs/run_named/research-workflow");
  });

  it("does not let a URL hint stand in for authorization", async () => {
    const control = seeded();
    // Control refuses the read the shell cannot do without. A link naming a
    // project and a thread must not paint them anyway.
    control.failNext = { path: /^workspaces$/, status: 403, category: "authentication_required" };
    open(control, "/?project=ws_1&thread=thread_open");
    await waitFor(() => expect(screen.queryByLabelText(copy.thread.outputs)).toBeNull());
    expect(screen.queryByText("Named conversation")).toBeNull();
  });
});
