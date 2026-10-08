import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copy } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl } from "./fake-control";
import { xhsNeedsDecisionItem } from "./xhs-fixtures";

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

  it("lists the captures newest first with their state and decides one of them", async () => {
    const control = seeded();
    control.capture("capture_failed", "https://example.com/failed", { state: "failed", created_at: "2026-09-06T09:00:00Z" });
    control.capture("capture_pending", "https://example.com/pending", { created_at: "2026-09-06T10:00:00Z" });
    control.capture("capture_consumed", "https://example.com/consumed", { state: "consumed", created_at: "2026-09-06T11:00:00Z" });
    control.capture("capture_approved", "https://example.com/approved", { state: "approved", created_at: "2026-09-06T12:00:00Z" });
    const user = userEvent.setup();
    const { container } = await openInbox(control);

    await screen.findByText("https://example.com/pending");
    // Submission time orders the list whatever the state: a failure never
    // sinks below older imports.
    await waitFor(() => expect(
      [...container.querySelectorAll<HTMLElement>("[data-capture-id]")].map((node) => node.dataset.captureId),
    ).toEqual(["capture_approved", "capture_consumed", "capture_pending", "capture_failed"]));
    const card = (id: string) => container.querySelector<HTMLElement>(`[data-capture-id="${id}"]`)!;
    expect(within(card("capture_failed")).getByText(copy.captureStates.failed)).toBeTruthy();
    expect(within(card("capture_consumed")).getByText(copy.captureStates.consumed)).toBeTruthy();
    expect(within(card("capture_approved")).getByText(copy.captureStates.approved)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(control.posts.at(-1)).toMatchObject({
      path: "captures/capture_pending/approve",
      body: { expected_revision: 0 },
    }));
  });

  it("says when a capture failed only because OCR is not ready", async () => {
    const control = seeded();
    control.capture("capture_pdf", "https://arxiv.org/abs/2609.07398", {
      state: "failed", failure_category: "capability_unavailable", created_at: "2026-09-06T09:00:00Z",
    });
    control.capture("capture_broken", "https://arxiv.org/abs/2609.00001", {
      state: "failed", failure_category: "materialization_failed", created_at: "2026-09-06T10:00:00Z",
    });
    await openInbox(control);

    await screen.findByText(copy.capture.ocrUnavailable);
    expect(screen.getAllByText(copy.capture.failed)).toHaveLength(1);
  });

  it("shows the note from the submitted text apart from the explicit note", async () => {
    const control = seeded();
    control.capture("capture_noted", "https://arxiv.org/abs/2601.00042 请总结方法部分", {
      state: "approved", note: "Compare with the memory idea", payload_note: "请总结方法部分",
    });
    await openInbox(control);

    const card = (await screen.findByText("https://arxiv.org/abs/2601.00042 请总结方法部分")).closest("article")!;
    expect(within(card).getByText("Compare with the memory idea")).toBeTruthy();
    const derived = within(card).getByText(copy.capture.payloadNote).parentElement!;
    expect(within(derived).getByText("请总结方法部分")).toBeTruthy();
    expect(within(derived).queryByText("Compare with the memory idea")).toBeNull();
  });

  it("opens the Library source a failed capture's paper is now adopted as", async () => {
    const control = seeded();
    control.source("source_paper", "arxiv:2601.00042", "Synthetic paper");
    control.capture("capture_failed", "https://arxiv.org/abs/2601.00042", {
      state: "failed", failure_category: "materialization_failed", available_source_id: "source_paper",
      revision: 3, created_at: "2026-09-06T10:00:00Z",
    });
    control.capture("capture_lost", "https://arxiv.org/abs/2601.00099", {
      state: "failed", failure_category: "materialization_failed", created_at: "2026-09-06T09:00:00Z",
    });
    const user = userEvent.setup();
    await openInbox(control);

    const card = (await screen.findByText("https://arxiv.org/abs/2601.00042")).closest("article")!;
    expect(within(card).getByText(copy.capture.availableInLibrary)).toBeTruthy();
    // A failed capture stays terminal: there is still nothing to reopen or retry.
    expect(within(card).queryByRole("button", { name: "Reopen" })).toBeNull();
    expect(within(card).queryByRole("button", { name: /retry/i })).toBeNull();
    const lost = screen.getByText("https://arxiv.org/abs/2601.00099").closest("article")!;
    expect(within(lost).queryByRole("button", { name: copy.capture.openSource })).toBeNull();
    expect(within(lost).queryByText(copy.capture.availableInLibrary)).toBeNull();

    await user.click(within(card).getByRole("button", { name: copy.capture.openSource }));

    await waitFor(() => expect(control.gets).toContain("sources/source_paper"));
    await screen.findByLabelText("Library");
    expect(control.posts).toEqual([]);
    expect(control.captures.find((row) => row.id === "capture_failed")).toMatchObject({ state: "failed", revision: 3 });
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

  describe("background rereads", () => {
    afterEach(() => { vi.useRealTimers(); });

    function captureReads(control: FakeControl) {
      return control.gets.filter((path) => path === "captures").length;
    }

    it("shows a failure that lands after approval without a manual refresh", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const control = seeded();
      const live = control.capture("capture_live", "https://arxiv.org/pdf/2609.20744", { state: "approved" });
      const { container } = await openInbox(control);
      await screen.findByText("https://arxiv.org/pdf/2609.20744");
      const reads = captureReads(control);

      Object.assign(live, { state: "failed", failure_category: "adapter_unavailable", revision: 3 });
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });

      await waitFor(() => expect(
        container.querySelector<HTMLElement>('[data-capture-id="capture_live"]')?.dataset.captureState,
      ).toBe("failed"));
      expect(captureReads(control)).toBe(reads + 1);
      expect(screen.getByText(copy.capture.failed)).toBeTruthy();
      // A background reread is not an operator refresh: no progress line.
      expect(screen.queryByText(copy.inbox.refreshingCaptures)).toBeNull();
    });

    it("keeps the rows on screen when a background reread fails", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const control = seeded();
      control.capture("capture_idle", "https://example.com/idle");
      await openInbox(control);
      await screen.findByText("https://example.com/idle");
      const reads = captureReads(control);

      control.failNext = { path: /^captures$/, status: 503, category: "store_unavailable" };
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });

      // The refused read answers before it is logged; the consumed failure is
      // what shows the reread happened.
      await waitFor(() => expect(control.failNext).toBeNull());
      expect(screen.getByText("https://example.com/idle")).toBeTruthy();
      expect(screen.queryByText(copy.inbox.capturesUnreadable)).toBeNull();

      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      await waitFor(() => expect(captureReads(control)).toBe(reads + 1));
    });

    it("waits for an operator refresh that is still in flight", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const control = seeded();
      const live = control.capture("capture_live", "https://example.com/live", { state: "approved" });
      const { container } = await openInbox(control);
      await screen.findByText("https://example.com/live");
      const reads = captureReads(control);

      // Hold the operator's read so a timer tick lands while it is in flight.
      let release: (() => void) | null = null;
      const held = new Promise<void>((resolve) => { release = resolve; });
      let armed = true;
      control.beforeResponse = async (path, method) => {
        if (!armed || method !== "GET" || path !== "captures") return;
        armed = false;
        await held;
      };
      await act(async () => { screen.getByRole("button", { name: copy.inbox.refresh }).click(); });
      await screen.findByText(copy.inbox.refreshingCaptures);

      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      // A held read is logged only when it answers, so no read means the tick yielded.
      expect(captureReads(control)).toBe(reads);

      Object.assign(live, { state: "failed", failure_category: "adapter_unavailable", revision: 3 });
      release!();
      await waitFor(() => expect(screen.queryByText(copy.inbox.refreshingCaptures)).toBeNull());
      expect(container.querySelector<HTMLElement>('[data-capture-id="capture_live"]')?.dataset.captureState).toBe("failed");
      expect(captureReads(control)).toBe(reads + 1);

      // Nothing is in flight any more, so rereads resume at the idle cadence.
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      await waitFor(() => expect(captureReads(control)).toBe(reads + 2));
    });

    it("pauses while the page is hidden and rereads when it is shown again", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const control = seeded();
      control.capture("capture_busy", "https://example.com/busy", { state: "claimed" });
      await openInbox(control);
      await screen.findByText("https://example.com/busy");
      const reads = captureReads(control);

      let visibility: DocumentVisibilityState = "hidden";
      const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
      try {
        await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
        expect(captureReads(control)).toBe(reads);

        visibility = "visible";
        await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
        await waitFor(() => expect(captureReads(control)).toBe(reads + 1));
      } finally {
        spy.mockRestore();
      }
    });
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

// jsdom lays nothing out, so these read the classes a browser sizes from. The
// workflow and mobile gates measure the result in a 390 px browser.
describe("Inbox on a phone", () => {
  // One world holding every control and every Details list the Inbox renders.
  async function everyInboxControl() {
    const control = seeded();
    const run = control.run("run_1", "thread_1", "waiting_for_decision");
    control.decision("decision_1", String(run.id), "Allow the import?", [{ id: "approve_once", label: "Approve once" }]);
    control.source("source_paper", "arxiv:2601.00042", "Synthetic paper");
    control.capture("capture_pending", "https://example.com/pending", { created_at: "2026-09-06T12:00:00Z" });
    control.capture("capture_uncertain", "https://example.com/lost", { state: "uncertain", created_at: "2026-09-06T11:00:00Z" });
    control.capture("capture_failed", "https://arxiv.org/abs/2601.00042", {
      state: "failed", failure_category: "materialization_failed", available_source_id: "source_paper",
      created_at: "2026-09-06T10:00:00Z",
    });
    control.fragment(`fragment_${"c".repeat(32)}`, "from the bot", {
      origin: "telegram", thread_id: "thread_1", context_item_id: `ri_${"a".repeat(32)}`,
    });
    control.xhsNeedsDecision = [xhsNeedsDecisionItem("xhs_rec_waiting")];
    const user = userEvent.setup();
    await openInbox(control);
    await screen.findByText("Allow the import?");
    await screen.findByText("from the bot");
    await screen.findByText("An unlinked blog");
    await user.click(await screen.findByRole("button", { name: copy.inbox.reopen }));
    await screen.findByRole("button", { name: copy.capture.confirmReopen });
    return screen.getByRole("region", { name: copy.inbox.title });
  }

  it("gives every Inbox button the shell's 44 px phone floor", async () => {
    const inbox = await everyInboxControl();

    const buttons = [...inbox.querySelectorAll<HTMLButtonElement>("button")];
    expect(buttons.map((button) => button.textContent).sort()).toEqual([
      copy.inbox.saveIdea, copy.inbox.capture, copy.inbox.open, copy.inbox.open, copy.inbox.refreshIdeas, copy.inbox.refresh,
      copy.inbox.approve, copy.inbox.dismiss, copy.inbox.dismiss, copy.inbox.reopen,
      copy.capture.confirmReopen, copy.capture.cancelReopen, copy.capture.openSource,
    ].sort());
    // Below `lg` the shell's controls are 44 px tall and return to the compact
    // size at `lg`; the Inbox sets that once for every button inside it.
    expect(inbox.className).toContain("[&_[data-slot=button]]:min-h-11");
    expect(inbox.className).toContain("lg:[&_[data-slot=button]]:min-h-7");
    for (const button of buttons) expect(button.dataset.slot, button.textContent ?? "").toBe("button");
  });

  it("lets a long id wrap inside every Inbox Details list", async () => {
    const inbox = await everyInboxControl();

    // One decision, three captures and one idea. A `1fr` track never shrinks
    // below its longest unbroken id, so the value column has to be allowed to
    // reach zero and the id to break anywhere.
    const lists = [...inbox.querySelectorAll("dl")];
    expect(lists).toHaveLength(5);
    for (const list of lists) {
      expect(list.className).toContain("grid-cols-[max-content_minmax(0,1fr)]");
      expect(list.className).toContain("wrap-anywhere");
    }
  });
});

describe("Inbox XHS recommendations", () => {
  const LIST_READ = "xhs/recommendations?review=needs_operator&limit=100";

  it("lists what the weekly review left to the operator above Ideas, and opens the note without changing anything", async () => {
    const control = seeded();
    control.xhsNote("source_note", "本周论文 Weekly reading list");
    control.xhsNeedsDecision = [
      xhsNeedsDecisionItem("xhs_rec_blog"),
      xhsNeedsDecisionItem("xhs_rec_paper", { kind: "paper", title: "Synthetic Memory Networks", reason_code: "title_mismatch", reason: null, note_title: "" }),
    ];
    const user = userEvent.setup();
    await openInbox(control);

    const section = await screen.findByRole("region", { name: copy.inbox.xhsTitle });
    const ideas = screen.getByRole("region", { name: copy.inbox.ideas });
    expect(section.compareDocumentPosition(ideas) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(section).getByText(copy.inbox.xhsIntro)).toBeTruthy();
    const [blog, paper] = within(section).getAllByRole("article", { name: copy.inbox.xhsItem });
    expect(within(blog!).getByText(copy.xhs.kinds.blog)).toBeTruthy();
    expect(within(blog!).getByText("An unlinked blog")).toBeTruthy();
    expect(within(blog!).getByText("The caption names the post but not where it lives.").hasAttribute("data-verbatim")).toBe(true);
    expect(within(blog!).getByText("In 本周论文 Weekly reading list")).toBeTruthy();
    expect(within(paper!).getByText(copy.xhs.reviewReasons.title_mismatch!)).toBeTruthy();
    expect(within(paper!).getByText(copy.inbox.xhsUntitledNote)).toBeTruthy();
    expect(within(section).queryByText(/^Showing /)).toBeNull();

    await user.click(within(blog!).getByRole("button", { name: copy.inbox.open }));
    await screen.findByLabelText("Library");
    expect(await screen.findByRole("heading", { level: 3, name: "本周论文 Weekly reading list" })).toBeTruthy();
    expect(control.posts).toEqual([]);
  });

  it("says how many wait beyond the page it shows", async () => {
    const control = seeded();
    control.xhsNeedsDecision = Array.from({ length: 101 }, (_, index) => xhsNeedsDecisionItem(`xhs_rec_${index}`, { title: `Synthetic blog ${index}` }));
    await openInbox(control);

    const section = await screen.findByRole("region", { name: copy.inbox.xhsTitle });
    expect(within(section).getAllByRole("article")).toHaveLength(100);
    expect(within(section).getByText("Showing 100 of 101.")).toBeTruthy();
  });

  it("shows nothing while nothing waits, and reads the list on entry and again after a command", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    await waitFor(() => expect(control.gets.filter((path) => path === LIST_READ)).toHaveLength(1));
    expect(screen.queryByRole("region", { name: copy.inbox.xhsTitle })).toBeNull();

    control.xhsNeedsDecision = [xhsNeedsDecisionItem("xhs_rec_blog")];
    await user.type(screen.getByLabelText(copy.inbox.ideaLabel), "An idea");
    await user.click(screen.getByRole("button", { name: copy.inbox.saveIdea }));

    const section = await screen.findByRole("region", { name: copy.inbox.xhsTitle });
    expect(within(section).getByText("An unlinked blog")).toBeTruthy();
    expect(control.gets.filter((path) => path === LIST_READ).length).toBeGreaterThanOrEqual(2);
  });

  it("says the list could not be read, keeping the wire message under Details", async () => {
    const control = seeded();
    control.xhsNeedsDecision = [xhsNeedsDecisionItem("xhs_rec_blog")];
    control.failNext = { path: /^xhs\/recommendations$/, status: 503, category: "unavailable" };
    await openInbox(control);

    const section = await screen.findByRole("region", { name: copy.inbox.xhsTitle });
    expect(within(section).getByRole("alert").textContent).toContain(copy.inbox.xhsUnreadable);
    expect(within(section).queryByRole("article")).toBeNull();
  });
});

describe("Inbox ideas", () => {
  const idea = "  第一行的想法 🎬\n\n  indented second line  ";

  function ideaCards() {
    return within(screen.getByRole("region", { name: copy.inbox.ideas })).queryAllByRole("article");
  }

  async function saveIdea(user: ReturnType<typeof userEvent.setup>, text: string, note = "") {
    const box = screen.getByLabelText(copy.inbox.ideaLabel) as HTMLTextAreaElement;
    if (!box.value) {
      await user.click(box);
      await user.paste(text);
    }
    if (note) await user.type(screen.getByLabelText(copy.inbox.ideaNoteLabel), note);
    await user.click(screen.getByRole("button", { name: copy.inbox.saveIdea }));
  }

  it("saves an idea verbatim through its own composer and starts nothing", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    await waitFor(() => expect(control.gets).toContain("fragments"));

    const form = screen.getByRole("form", { name: copy.inbox.ideaTitle });
    expect(within(form).queryByRole("checkbox")).toBeNull();
    await saveIdea(user, idea, "later");

    await waitFor(() => expect(control.posts).toEqual([
      { path: "fragments", body: { text: idea, note: "later" }, key: "web-test-1" },
    ]));
    await waitFor(() => expect((screen.getByLabelText(copy.inbox.ideaLabel) as HTMLTextAreaElement).value).toBe(""));
    const [card] = ideaCards();
    expect(card!.querySelector("[data-verbatim]")!.textContent).toBe(idea);
    expect(within(card!).getByText(copy.fragment.savedHere)).toBeTruthy();
    expect(control.captures).toEqual([]);
    expect(control.messages).toEqual([]);
  });

  it("keeps the typed idea when the save is refused", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    control.failNext = { path: /^fragments$/, status: 400, category: "invalid_request" };

    await saveIdea(user, idea);

    await screen.findByRole("alert", { name: copy.notice.region });
    expect((screen.getByLabelText(copy.inbox.ideaLabel) as HTMLTextAreaElement).value).toBe(idea);
    expect(control.fragments).toEqual([]);
  });

  it("retries an unconfirmed save under the same key and lists one card", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    control.loseNextResponse = /^fragments$/;

    await saveIdea(user, idea);
    await screen.findByText(copy.errors.unconfirmed);
    expect((screen.getByLabelText(copy.inbox.ideaLabel) as HTMLTextAreaElement).value).toBe(idea);
    await saveIdea(user, idea);

    await waitFor(() => expect(control.posts.map((post) => post.key)).toEqual(["web-test-1", "web-test-1"]));
    await waitFor(() => expect(ideaCards()).toHaveLength(1));
    expect(control.fragments).toHaveLength(1);

    // A confirmed save forgets its command: the same words again are a new idea.
    await saveIdea(user, idea);
    await waitFor(() => expect(ideaCards()).toHaveLength(2));
    expect(control.posts.at(-1)!.key).not.toBe("web-test-1");
  });

  it("keeps the command when the gateway's retryable 503 follows a commit", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    control.gatewayDropsNextResponse = /^fragments$/;

    await saveIdea(user, idea);
    await screen.findByText(copy.errors.unconfirmed);
    expect((screen.getByLabelText(copy.inbox.ideaLabel) as HTMLTextAreaElement).value).toBe(idea);
    await saveIdea(user, idea);

    await waitFor(() => expect(control.posts.map((post) => post.key)).toEqual(["web-test-1", "web-test-1"]));
    await waitFor(() => expect(ideaCards()).toHaveLength(1));
    expect(control.fragments).toHaveLength(1);
  });

  it("keeps a saved card on screen when the reread after it fails", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    await waitFor(() => expect(control.gets).toContain("fragments"));
    control.beforeResponse = (path, method) => {
      if (path === "fragments" && method === "GET") {
        control.beforeResponse = null;
        control.failNext = { path: /^fragments$/, status: 503, category: "control_store_unavailable" };
      }
    };

    await saveIdea(user, "kept after a failed reread");

    await screen.findByText(copy.inbox.ideasUnreadable);
    expect(ideaCards()).toHaveLength(1);
    expect((screen.getByLabelText(copy.inbox.ideaLabel) as HTMLTextAreaElement).value).toBe("");
  });

  it("shows a Telegram idea as such and keeps every id under Details", async () => {
    const control = seeded();
    control.fragment("fragment_1a2b", "from the bot", {
      origin: "telegram", thread_id: "thread_1", context_item_id: "ri_" + "a".repeat(32),
    });
    await openInbox(control);

    await waitFor(() => expect(ideaCards()).toHaveLength(1));
    const [card] = ideaCards();
    expect(within(card!).getByText(copy.fragment.fromTelegram)).toBeTruthy();
    const details = card!.querySelector("[data-details]")!;
    expect(details.textContent).toContain("fragment_1a2b");
    expect(details.textContent).toContain("thread_1");
    const outside = card!.cloneNode(true) as HTMLElement;
    outside.querySelectorAll("[data-details]").forEach((node) => node.remove());
    expect(outside.textContent).not.toMatch(/fragment_1a2b|thread_1|ri_a/);
  });

  it("picks up an idea saved elsewhere when its list is refreshed", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openInbox(control);
    await screen.findByText(copy.inbox.noIdeas);

    control.fragment("fragment_late", "saved from Telegram", { origin: "telegram", thread_id: "thread_1" });
    await user.click(screen.getByRole("button", { name: copy.inbox.refreshIdeas }));

    await waitFor(() => expect(ideaCards()).toHaveLength(1));
  });

  it("separates the idea composer from the arXiv source composer", async () => {
    const control = seeded();
    await openInbox(control);

    expect(screen.getByRole("heading", { name: copy.inbox.ideaTitle })).toBeTruthy();
    expect(screen.getByRole("heading", { name: copy.inbox.sourceTitle })).toBeTruthy();
    expect((screen.getByLabelText("Capture payload") as HTMLTextAreaElement).placeholder).toMatch(/arXiv/);
  });
});
