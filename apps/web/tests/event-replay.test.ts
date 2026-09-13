import { describe, expect, it, vi } from "vitest";
import {
  EVENT_CURSOR_STORAGE_KEY,
  EventReplayController,
  invalidatedRunId,
} from "../app/control/event-replay";

const event = {
  cursor: "opaque-cursor-1",
  schema_version: 1,
  id: "event_1",
  run_id: "run_1",
  attempt_id: "attempt_1",
  sequence: 1,
  type: "run.queued",
  occurred_at: "2026-07-23T12:00:00Z",
  causation_id: null,
  durability: "durable" as const,
  payload: { state: "queued" },
};

describe("opaque cursor replay", () => {
  it.each([
    "source.conflict_detected",
    "artifact.version_committed",
    "run.completed",
    "decision.resolved",
    "event.redacted",
    "future.workflow.event",
  ])("invalidates the envelope run for %s without inspecting payload", (type) => {
    expect(invalidatedRunId({
      ...event,
      type,
      payload: { run_id: "payload_run_must_not_route" },
    })).toBe("run_1");
  });

  it("persists only the cursor and deduplicates at-least-once events", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    };
    const callbacks: Array<() => void> = [];
    const client = { listEvents: vi.fn(async () => ({ items: [event], next_cursor: event.cursor })) };
    const delivered: string[] = [];
    const controller = new EventReplayController({
      client,
      storage,
      online: () => true,
      visible: () => true,
      intervals: [1, 2],
      schedule: (callback) => { callbacks.push(callback); return callbacks.length as unknown as ReturnType<typeof setTimeout>; },
      cancel: () => {},
      onEvents: (events) => delivered.push(...events.map((item) => item.id)),
    });

    controller.start();
    callbacks.shift()?.();
    await vi.waitFor(() => expect(client.listEvents).toHaveBeenCalledTimes(1));
    expect(delivered).toEqual(["event_1"]);
    expect(values).toEqual(new Map([[EVENT_CURSOR_STORAGE_KEY, "opaque-cursor-1"]]));

    callbacks.shift()?.();
    await vi.waitFor(() => expect(client.listEvents).toHaveBeenCalledTimes(2));
    expect(client.listEvents).toHaveBeenLastCalledWith("opaque-cursor-1");
    expect(delivered).toEqual(["event_1"]);
    controller.stop();
  });

  it("never overlaps a slow poll", async () => {
    let resolve!: (value: { items: []; next_cursor: string }) => void;
    const client = { listEvents: vi.fn(() => new Promise<{ items: []; next_cursor: string }>((done) => { resolve = done; })) };
    const callbacks: Array<() => void> = [];
    const controller = new EventReplayController({
      client,
      online: () => true,
      visible: () => true,
      schedule: (callback) => { callbacks.push(callback); return callbacks.length as unknown as ReturnType<typeof setTimeout>; },
      cancel: () => {},
      onEvents: () => {},
    });
    controller.start();
    callbacks.shift()?.();
    controller.wake();
    expect(client.listEvents).toHaveBeenCalledTimes(1);
    resolve({ items: [], next_cursor: "opaque-cursor-2" });
    await vi.waitFor(() => expect(callbacks).toHaveLength(1));
    controller.stop();
  });
});
