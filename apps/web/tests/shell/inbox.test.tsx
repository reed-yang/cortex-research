import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { Shell } from "../../app/shell/shell";
import { FakeControl } from "./fake-control";

// `tests/jsdom-setup.ts` owns the Testing Library cleanup for every suite; this
// hook also resets the query the shell reads once on mount, and unmounts first
// so the reset never lands under a mounted tree.
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  return control;
}

async function openInbox(control: FakeControl) {
  window.history.replaceState(null, "", "/?project=ws_1&view=inbox");
  const view = render(<Shell client={control.client()} />);
  await screen.findByLabelText("Inbox");
  await waitFor(() => expect(control.gets).toContain("captures"));
  return view;
}

describe("InboxView", () => {
  it("saves a pasted link with its note and clears the composer", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);

    await user.type(screen.getByLabelText("Capture payload"), "https://example.com/echo");
    await user.type(screen.getByLabelText("Capture note"), "Matches the memory idea");
    await user.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => expect(control.posts.at(-1)).toMatchObject({
      path: "captures",
      body: { payload: "https://example.com/echo", note: "Matches the memory idea" },
    }));
    // The real create takes exactly `payload` and `note`; approval is its own command.
    expect(Object.keys(control.posts.at(-1)!.body).sort()).toEqual(["note", "payload"]);
    await waitFor(() => expect((screen.getByLabelText("Capture payload") as HTMLTextAreaElement).value).toBe(""));
  });

  it("approves in the same gesture as a second recorded command", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);

    await user.type(screen.getByLabelText("Capture payload"), "https://example.com/second");
    await user.click(screen.getByRole("checkbox", { name: "Approve now" }));
    await user.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => expect(control.posts.map((post) => post.path)).toEqual([
      "captures",
      "captures/capture_1/approve",
    ]));
  });

  it("keeps the pasted text when the capture is refused", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    control.failNext = { path: /^captures$/, status: 409, category: "already_captured", current: control.capture("capture_9", "https://example.com/dup") };

    await user.type(screen.getByLabelText("Capture payload"), "https://example.com/dup");
    await user.click(screen.getByRole("button", { name: "Capture" }));

    // The refusal re-reads the inbox and stages nothing new; what the operator
    // pasted is still in the box.
    await waitFor(() => expect(control.gets.filter((path) => path === "captures").length).toBeGreaterThan(1));
    expect(control.captures).toHaveLength(1);
    expect((screen.getByLabelText("Capture payload") as HTMLTextAreaElement).value).toBe("https://example.com/dup");
  });

  it("opens the thread that owns a pending decision from another thread", async () => {
    const control = seeded();
    control.thread("thread_2", "ws_1", "Second question");
    const run = control.run("run_2", "thread_2", "waiting_for_decision");
    control.decision("decision_1", String(run.id), "Allow the import?\nSecond line", [{ id: "approve_once", label: "Approve once" }]);
    const user = userEvent.setup();
    await openInbox(control);

    await screen.findByText("Allow the import?");
    await user.click(screen.getByRole("button", { name: "Open" }));

    await screen.findByLabelText("Thread");
    await waitFor(() => expect(window.location.search).toBe("?project=ws_1&thread=thread_2"));
    expect(control.gets).toContain("runs/run_2");
  });

  it("groups the captures in state order and decides one of them", async () => {
    const control = seeded();
    control.capture("capture_failed", "https://example.com/failed", { state: "failed", created_at: "2026-09-06T09:00:00Z" });
    control.capture("capture_pending", "https://example.com/pending", { created_at: "2026-09-06T10:00:00Z" });
    control.capture("capture_consumed", "https://example.com/consumed", { state: "consumed", created_at: "2026-09-06T11:00:00Z" });
    control.capture("capture_approved", "https://example.com/approved", { state: "approved", created_at: "2026-09-06T12:00:00Z" });
    const user = userEvent.setup();
    const { container } = await openInbox(control);

    await screen.findByText("https://example.com/pending");
    await waitFor(() => expect(
      [...container.querySelectorAll<HTMLElement>("[data-capture-group]")].map((node) => node.dataset.captureGroup),
    ).toEqual(["pending", "approved", "consumed", "failed"]));

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(control.posts.at(-1)).toMatchObject({
      path: "captures/capture_pending/approve",
      body: { expected_revision: 0 },
    }));
  });

  it("asks for a second gesture before reopening a capture", async () => {
    const control = seeded();
    control.capture("capture_uncertain", "https://example.com/lost", { state: "uncertain" });
    const user = userEvent.setup();
    await openInbox(control);

    await screen.findByText("https://example.com/lost");
    await user.click(screen.getByRole("button", { name: "Reopen" }));
    expect(control.posts).toEqual([]);

    await user.click(screen.getByRole("button", { name: "Confirm reopen" }));
    await waitFor(() => expect(control.posts.at(-1)).toMatchObject({ path: "captures/capture_uncertain/reopen" }));
  });

  it("opens a decision owned by another project by switching to that project", async () => {
    const control = seeded();
    control.workspace("ws_2", "Second project");
    control.thread("thread_2b", "ws_2", "Other project thread");
    const run = control.run("run_far", "thread_2b", "waiting_for_decision");
    control.decision("decision_far", String(run.id), "Allow the import?", [{ id: "approve_once", label: "Approve once" }]);
    const user = userEvent.setup();
    await openInbox(control);

    await screen.findByText("Allow the import?");
    await user.click(screen.getByRole("button", { name: "Open" }));

    await screen.findByLabelText("Thread");
    // The project has to move with the thread, or the shell lands on a thread
    // its own project does not hold.
    await waitFor(() => expect(window.location.search).toBe("?project=ws_2&thread=thread_2b"));
    expect(control.gets).toContain("threads/thread_2b");
    expect(control.gets).toContain("threads?workspace_id=ws_2&include_archived=true");
  });

  it("points at the existing row when a capture is already in the inbox", async () => {
    const control = seeded();
    const existing = control.capture("capture_9", "https://example.com/dup");
    const user = userEvent.setup();
    await openInbox(control);
    control.failNext = { path: /^captures$/, status: 409, category: "already_captured", current: existing };

    await user.type(screen.getByLabelText("Capture payload"), "https://example.com/dup");
    await user.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => expect(
      document.querySelector('[data-capture-id="capture_9"]')?.getAttribute("aria-current"),
    ).toBe("true"));
  });

  it("names a decision kind in product words, never the raw token", async () => {
    const control = seeded();
    const run = control.run("run_kind", "thread_1", "waiting_for_decision");
    control.decision("decision_kind", String(run.id), "Keep both sources?", [], { kind: "source_conflict" });
    await openInbox(control);

    await screen.findByText("Keep both sources?");
    expect(screen.getByText("Source conflict")).toBeTruthy();
    expect(screen.queryByText("source_conflict")).toBeNull();
  });

  it("offers no gesture while the shell is offline", async () => {
    const control = seeded();
    control.capture("capture_offline", "https://example.com/offline");
    await openInbox(control);
    await screen.findByText("https://example.com/offline");

    await act(async () => { window.dispatchEvent(new Event("offline")); });

    expect((screen.getByRole("button", { name: "Capture" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("Capture payload") as HTMLTextAreaElement).disabled).toBe(true);
  });

  it("keeps the capture list on screen while a decision reloads it", async () => {
    const control = seeded();
    control.capture("capture_kept", "https://example.com/kept");
    const user = userEvent.setup();
    await openInbox(control);
    await screen.findByText("https://example.com/kept");

    // Hold the reread that follows the decision so the in-flight window is
    // observable rather than a microtask race.
    let release: (() => void) | null = null;
    const held = new Promise<void>((resolve) => { release = resolve; });
    let armed = true;
    control.beforeResponse = async (path, method) => {
      if (!armed || method !== "GET" || path !== "captures") return;
      armed = false;
      await held;
    };

    await user.click(screen.getByRole("button", { name: "Approve" }));

    await screen.findByText("Refreshing…");
    // The row -- and the focus of whoever just clicked it -- survives the reread.
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeTruthy();
    expect(document.querySelector('[data-capture-id="capture_kept"]')).toBeTruthy();

    release!();
    await waitFor(() => expect(screen.queryByText("Refreshing…")).toBeNull());
  });

  it("explains a capture another run still holds instead of offering a decision", async () => {
    const control = seeded();
    control.capture("capture_blocked", "https://example.com/held", {
      state: "claimed",
      failure_category: "foreign_carrier_run",
      blocked_by: "run_deadbeef01",
    });
    await openInbox(control);

    await screen.findByText("https://example.com/held");
    expect(screen.getByText(/still holds this capture/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });
});
