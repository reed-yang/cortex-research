import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { CortexControlClient } from "../../app/control/client";
import type { Message } from "../../app/control/contracts";
import { ThreadView } from "../../app/shell/thread-view";
import { useCortexThreadRuntime } from "../../app/shell/thread-runtime";
import { useControlState } from "../../app/shell/use-control-state";
import type { ControlActions } from "../../app/shell/types";
import { actionsBag, baseState } from "./state-fixture";
import { FakeControl } from "./fake-control";

const now = "2026-09-06T12:00:00Z";

const MESSAGES: Message[] = [
  { id: "msg_1", thread_id: "thread_1", role: "user", content: "hello", position: 1, created_at: now },
  { id: "msg_2", thread_id: "thread_1", role: "assistant", content: "hi", position: 2, created_at: now },
];

const THREAD = {
  id: "thread_1", workspace_id: "ws_1", title: "First question", status: "idle", active_run_id: null,
  archived_at: null, engine_owned: false, revision: 0, created_at: now, updated_at: now,
};

const DRAFT = "What does Echo claim?";

afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

// A daemon that received the append and then lost the connection before the
// receipt got back: the message may or may not have landed, which is exactly
// the state the retained command exists for. Every append attempt is logged
// with the key it carried, including the ones the fake refuses before it
// records them.
function testClient(control: FakeControl, options: { dropFirstAppend?: boolean } = {}) {
  const attempts: Array<string | null> = [];
  let keys = 0;
  let dropped = false;
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const target = new URL(String(input), "http://127.0.0.1");
    const relative = target.pathname.replace(/^\/api\/cortex\//, "");
    const append = (init?.method ?? "GET") === "POST" && /^threads\/[^/]+\/messages$/.test(relative);
    if (append) attempts.push(new Headers(init?.headers).get("Idempotency-Key"));
    if (append && options.dropFirstAppend && !dropped) {
      dropped = true;
      await control.fetch(input, init);
      throw new TypeError("Failed to fetch");
    }
    return control.fetch(input, init);
  }) as typeof fetch;
  return { attempts, client: new CortexControlClient({ fetcher, idempotencyKeyFactory: () => `web-test-${++keys}` }) };
}

function seededThread(): FakeControl {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  return control;
}

function appendBodies(control: FakeControl): unknown[] {
  return control.posts.filter((post) => /messages$/.test(post.path)).map((post) => post.body.content);
}

// The shell's own mount rule: switching to another view unmounts the thread
// view, which is what made a slot kept inside it disappear.
const captured: { actions: ControlActions | null } = { actions: null };

function Harness({ client }: { client: CortexControlClient }) {
  const [state, actions] = useControlState(client);
  useEffect(() => { captured.actions = actions; }, [actions]);
  if (state.loading) return <p>Loading</p>;
  if (state.view !== "thread") return <p>Elsewhere</p>;
  return <ThreadView actions={actions} client={client} state={state} />;
}

async function mounted(client: CortexControlClient) {
  captured.actions = null;
  render(<Harness client={client} />);
  await waitFor(() => expect(screen.queryByText("Loading")).toBeNull());
  return userEvent.setup();
}

function composer(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Message input" }) as HTMLTextAreaElement;
}

async function send(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(composer(), text);
  await user.click(screen.getByRole("button", { name: "Send message" }));
}

function setView(view: "thread" | "library") {
  act(() => { captured.actions!.setView(view); });
}

describe("useCortexThreadRuntime", () => {
  it("keeps the converted messages across a re-render that changed nothing about them", () => {
    const client = new FakeControl().client();
    const actions = actionsBag();
    const state = baseState({ thread: THREAD, messages: MESSAGES });
    const { result, rerender } = renderHook(
      ({ pending }: { pending: boolean }) => useCortexThreadRuntime({
        state: { ...state, commandPending: pending },
        actions,
        client,
        mode: "chat",
        persistedMode: "chat",
        onSent: () => {},
      }),
      { initialProps: { pending: false } },
    );
    const before = result.current.thread.getState().messages;
    expect(before).toHaveLength(2);
    // An unrelated control-state tick: a converter rebuilt per render would
    // reset the runtime's cache and hand back new message objects.
    rerender({ pending: true });
    const after = result.current.thread.getState().messages;
    expect(after[0]).toBe(before[0]);
    expect(after[1]).toBe(before[1]);
  });
});

describe("an unconfirmed send", () => {
  it("is retried under the command it was sent with, body and all", async () => {
    const control = seededThread();
    const { attempts, client } = testClient(control, { dropFirstAppend: true });
    const user = await mounted(client);
    await user.click(screen.getByRole("radio", { name: "Research" }));
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(1));
    // The operator does what the strip told them: send the same text again.
    // The mode segment has reset in between, and the retained body wins.
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(2));
    expect(attempts[1]).toBe(attempts[0]);
    expect(appendBodies(control)).toEqual([`/research ${DRAFT}`, `/research ${DRAFT}`]);
  });

  it("survives a visit to another view and back", async () => {
    const control = seededThread();
    const { attempts, client } = testClient(control, { dropFirstAppend: true });
    const user = await mounted(client);
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(1));
    setView("library");
    expect(screen.getByText("Elsewhere")).toBeTruthy();
    setView("thread");
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(2));
    expect(attempts[1]).toBe(attempts[0]);
  });

  it("is not what a refused send leaves behind", async () => {
    const control = seededThread();
    const { attempts, client } = testClient(control);
    const user = await mounted(client);
    control.failNext = { path: /^threads\/thread_1\/messages$/, status: 409, category: "revision_conflict" };
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(1));
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(2));
    expect(attempts[1]).not.toBe(attempts[0]);
  });

  it("is forgotten once a message of its own commits", async () => {
    const control = seededThread();
    const { attempts, client } = testClient(control);
    const user = await mounted(client);
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(1));
    // The send landed, so a run is in flight and the composer offers Stop;
    // ending it is what gives the operator the composer back.
    await act(async () => { await captured.actions!.runAction("cancel"); });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Send message" })).not.toBeNull());
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(2));
    expect(attempts[1]).not.toBe(attempts[0]);
  });

  it("is not reused for different text", async () => {
    const control = seededThread();
    const { attempts, client } = testClient(control, { dropFirstAppend: true });
    const user = await mounted(client);
    await send(user, DRAFT);
    await waitFor(() => expect(attempts).toHaveLength(1));
    await send(user, "What does Echo measure?");
    await waitFor(() => expect(attempts).toHaveLength(2));
    expect(attempts[1]).not.toBe(attempts[0]);
  });
});
