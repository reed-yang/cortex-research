import { ControlProblemError, type CortexControlClient } from "./client";
import type { RunEvent } from "./contracts";

export type ReplayState = "idle" | "polling" | "offline" | "reconnecting" | "error";

type CursorStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

type ReplayOptions = {
  client: Pick<CortexControlClient, "listEvents">;
  storage?: CursorStorage;
  online?: () => boolean;
  visible?: () => boolean;
  schedule?: (callback: () => void, delay: number) => ReturnType<typeof setTimeout>;
  cancel?: (timer: ReturnType<typeof setTimeout>) => void;
  onEvents: (events: RunEvent[]) => void;
  onState?: (state: ReplayState) => void;
  intervals?: readonly number[];
};

export const EVENT_CURSOR_STORAGE_KEY = "cortex.control.event-cursor.v1";

export function invalidatedRunId(event: RunEvent): string {
  return event.run_id;
}

export class EventReplayController {
  private readonly client: ReplayOptions["client"];
  private readonly storage?: CursorStorage;
  private readonly online: () => boolean;
  private readonly visible: () => boolean;
  private readonly schedule: NonNullable<ReplayOptions["schedule"]>;
  private readonly cancel: NonNullable<ReplayOptions["cancel"]>;
  private readonly onEvents: ReplayOptions["onEvents"];
  private readonly onState: NonNullable<ReplayOptions["onState"]>;
  private readonly intervals: readonly number[];
  private readonly seen = new Set<string>();
  private cursor: string | null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private inFlight = false;
  private failures = 0;

  constructor(options: ReplayOptions) {
    this.client = options.client;
    this.storage = options.storage;
    this.online = options.online ?? (() => navigator.onLine);
    this.visible = options.visible ?? (() => document.visibilityState !== "hidden");
    this.schedule = options.schedule ?? ((callback, delay) => setTimeout(callback, delay));
    this.cancel = options.cancel ?? ((timer) => clearTimeout(timer));
    this.onEvents = options.onEvents;
    this.onState = options.onState ?? (() => {});
    this.intervals = options.intervals ?? [2_500, 5_000, 10_000, 30_000];
    this.cursor = this.readCursor();
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.queue(0);
  }

  stop() {
    this.stopped = true;
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
  }

  wake() {
    if (this.stopped || this.inFlight) return;
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
    this.queue(0);
  }

  private queue(delay: number) {
    if (this.stopped || this.timer !== null) return;
    this.timer = this.schedule(() => {
      this.timer = null;
      void this.poll();
    }, delay);
  }

  private async poll() {
    if (this.stopped || this.inFlight) return;
    if (!this.online()) {
      this.onState("offline");
      this.queue(this.intervals[0]);
      return;
    }
    if (!this.visible()) {
      this.onState("idle");
      this.queue(this.intervals[1] ?? this.intervals[0]);
      return;
    }
    this.inFlight = true;
    this.onState(this.failures ? "reconnecting" : "polling");
    try {
      const envelope = await this.client.listEvents(this.cursor);
      const fresh = envelope.items.filter((event) => {
        if (this.seen.has(event.id)) return false;
        this.seen.add(event.id);
        return true;
      });
      while (this.seen.size > 2_000) this.seen.delete(this.seen.values().next().value!);
      if (fresh.length) this.onEvents(fresh);
      this.cursor = envelope.next_cursor;
      this.writeCursor(envelope.next_cursor);
      this.failures = 0;
      this.onState("idle");
    } catch (error) {
      this.failures += 1;
      if (
        error instanceof ControlProblemError &&
        error.problem.category === "event_cursor_expired"
      ) {
        const current = error.problem.current?.cursor;
        if (typeof current === "string") {
          this.cursor = current;
          this.writeCursor(current);
        } else {
          this.cursor = null;
          this.removeCursor();
        }
      }
      this.onState("error");
    } finally {
      this.inFlight = false;
      const index = Math.min(this.failures, this.intervals.length - 1);
      this.queue(this.intervals[index]);
    }
  }

  private readCursor(): string | null {
    try {
      return this.storage?.getItem(EVENT_CURSOR_STORAGE_KEY) ?? null;
    } catch {
      return null;
    }
  }

  private writeCursor(cursor: string | null) {
    if (!cursor) return;
    try {
      this.storage?.setItem(EVENT_CURSOR_STORAGE_KEY, cursor);
    } catch {
      // Cursor persistence is an optimization; replay remains correct in memory.
    }
  }

  private removeCursor() {
    try {
      this.storage?.removeItem(EVENT_CURSOR_STORAGE_KEY);
    } catch {
      // Cursor persistence is an optimization; replay remains correct in memory.
    }
  }
}
