import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { copy } from "../../app/shell/copy";
import { useControlState } from "../../app/shell/use-control-state";
import { FakeControl } from "./fake-control";

afterEach(() => { window.history.replaceState(null, "", "/"); });

function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.workspace("ws_engine", "Capture consumer", { engine_owned: true });
  control.thread("thread_1", "ws_1", "First question");
  control.thread("thread_old", "ws_1", "Archived one", { archived_at: "2026-09-01T00:00:00Z" });
  control.message("msg_1", "thread_1", "user", "hello", 1);
  return control;
}

describe("useControlState", () => {
  it("loads the first non-engine project, splits archived threads, and writes the URL", async () => {
    const control = seeded();
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    expect(result.current[0].workspace?.id).toBe("ws_1");
    expect(result.current[0].threads.map((t) => t.id)).toEqual(["thread_1"]);
    expect(result.current[0].archivedThreads.map((t) => t.id)).toEqual(["thread_old"]);
    expect(window.location.search).toBe("?project=ws_1&thread=thread_1");
  });

  it("honours a URL that names a thread and view", async () => {
    const control = seeded();
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_old&view=inbox");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    expect(result.current[0].thread?.id).toBe("thread_old");
    expect(result.current[0].view).toBe("inbox");
  });

  it("archives a thread through Control and drops it from the list", async () => {
    const control = seeded();
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    const thread = result.current[0].threads[0]!;
    await act(async () => { expect(await result.current[1].archiveThread(thread)).toBe(true); });
    expect(control.posts.at(-1)).toMatchObject({ path: "threads/thread_1/archive", body: { expected_revision: 0 } });
    expect(result.current[0].threads).toEqual([]);
    expect(result.current[0].archivedThreads.map((t) => t.id)).toEqual(["thread_old", "thread_1"]);
    expect(result.current[0].thread).toBeNull();
  });

  it("says why an archived thread starts no run, in the words the operator already read", async () => {
    // Control refuses the write, not just the screen: the shell offers only
    // Unarchive, and the sentence for the refusal is the one the strip
    // already shows on an archived thread.
    const control = seeded();
    control.message("msg_old", "thread_old", "user", "still there?", 1);
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_old");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    await act(async () => { await result.current[1].createRun(); });
    expect(result.current[0].notice?.tone).toBe("error");
    expect(result.current[0].notice?.text).toBe(copy.strip.archivedThread);
    expect(result.current[0].notice?.details).toBe("thread_archived");
    expect(control.runs).toEqual([]);
  });

  it("surfaces a revision conflict on rename as a notice and reloads the row", async () => {
    const control = seeded();
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    control.failNext = { path: /^threads\/thread_1\/rename$/, status: 409, category: "revision_conflict", current: { ...control.threads[0]!, revision: 3 } };
    await act(async () => { expect(await result.current[1].renameThread(result.current[0].threads[0]!, "Renamed")).toBe(false); });
    expect(result.current[0].notice?.tone).toBe("error");
    expect(result.current[0].notice?.details).toBe("revision_conflict");
  });

  it("loads the inbox when the URL opens straight onto it", async () => {
    const control = seeded();
    control.capture("capture_1", "https://example.com/echo");
    window.history.replaceState(null, "", "/?project=ws_1&view=inbox");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].captures.map((c) => c.id)).toEqual(["capture_1"]));
    expect(control.gets).toContain("captures");
  });

  it("opens a thread from another project by switching the project with it", async () => {
    const control = seeded();
    control.workspace("ws_2", "Second project");
    control.thread("thread_2b", "ws_2", "Other project thread");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));

    await act(async () => { expect(await result.current[1].openThread("thread_2b")).toBe(true); });

    expect(result.current[0].workspace?.id).toBe("ws_2");
    expect(result.current[0].thread?.id).toBe("thread_2b");
    expect(result.current[0].view).toBe("thread");
    await waitFor(() => expect(window.location.search).toBe("?project=ws_2&thread=thread_2b"));
  });

  it("refuses to open a thread in an engine project while engine projects are hidden", async () => {
    const control = seeded();
    control.thread("thread_engine", "ws_engine", "Carrier", { engine_owned: true });
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));

    await act(async () => { expect(await result.current[1].openThread("thread_engine")).toBe(false); });

    expect(result.current[0].workspace?.id).toBe("ws_1");
    expect(result.current[0].notice?.tone).toBe("warning");
  });

  it("says so when the thread a decision names is gone", async () => {
    const control = seeded();
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));

    await act(async () => { expect(await result.current[1].openThread("thread_missing")).toBe(false); });

    expect(result.current[0].notice?.tone).toBe("warning");
    expect(result.current[0].thread?.id).toBe("thread_1");
  });

  it("carries the health capabilities the daemon reports into the shell state", async () => {
    const control = seeded();
    control.health = { api_version: "v1", capabilities: { event_replay: true }, runtime_dispatch_enabled: false };
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].capabilities).toEqual({ event_replay: true }));
  });

  it("falls back to the first project with a notice when the URL names an unknown one", async () => {
    const control = seeded();
    window.history.replaceState(null, "", "/?project=ws_missing&thread=thread_1");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    expect(result.current[0].workspace?.id).toBe("ws_1");
    expect(result.current[0].notice?.tone).toBe("warning");
  });

  it("opens no thread and says so when the URL names an unknown one", async () => {
    const control = seeded();
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_missing");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    expect(result.current[0].thread).toBeNull();
    expect(result.current[0].loadedThreadId).toBeNull();
    expect(result.current[0].notice?.tone).toBe("warning");
    await waitFor(() => expect(window.location.search).toBe("?project=ws_1"));
  });

  it("advances the replay cursor, so seeded events reload the thread once", async () => {
    const control = seeded();
    const run = control.run("run_1", "thread_1", "running", { active_attempt_id: "attempt_1" });
    control.researchWorkflow(run);
    control.event("event_1", "run_1");
    control.event("event_2", "run_1");
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    await waitFor(() => expect(result.current[0].events.map((e) => e.id)).toEqual(["event_1", "event_2"]));
    await waitFor(() => expect(control.gets.filter((p) => p === "threads/thread_1/messages")).toHaveLength(2));
    act(() => { window.dispatchEvent(new Event("online")); });
    await waitFor(() => expect(control.gets).toContain("events?after_cursor=event_cursor_2"));
    expect(control.gets.filter((p) => p === "threads/thread_1/messages")).toHaveLength(2);
  });

  it("resolves a source gate through the source-intent route", async () => {
    const control = seeded();
    const run = control.run("run_1", "thread_1", "waiting_for_decision", { active_attempt_id: "attempt_1" });
    const decision = control.decision("decision_1", "run_1", "Keep both sources?", [{ id: "keep_both", label: "Keep both" }], { kind: "source_conflict" });
    control.researchWorkflow(run, {
      source_gates: [control.sourceGate("source_intent_1", run, decision)],
      decisions: [decision],
    });
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].research?.source_gates).toHaveLength(1));
    await act(async () => { await result.current[1].resolveDecision(result.current[0].decisions[0]!, "keep_both"); });
    expect(control.posts.at(-1)).toMatchObject({
      path: "source-intents/source_intent_1/resolve",
      body: { choice: "keep_both", expected_revision: 0 },
    });
  });

  it("answers capture with whether Cortex took it", async () => {
    const control = seeded();
    const { result } = renderHook(() => useControlState(control.client()));
    await waitFor(() => expect(result.current[0].loading).toBe(false));
    await act(async () => { expect(await result.current[1].capture("https://example.com/one", "", false)).toBe(true); });
    control.failNext = { path: /^captures$/, status: 400, category: "invalid_request" };
    await act(async () => { expect(await result.current[1].capture("https://example.com/two", "", false)).toBe(false); });
    expect(result.current[0].notice?.tone).toBe("error");
  });
});
