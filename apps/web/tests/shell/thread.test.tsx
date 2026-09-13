import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AppendMessage } from "@assistant-ui/react";
import { useMemo } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { copy } from "../../app/shell/copy";
import { draftText } from "../../app/shell/thread-runtime";
import { ThreadView } from "../../app/shell/thread-view";
import { useControlState } from "../../app/shell/use-control-state";
import { FakeControl } from "./fake-control";

// `tests/jsdom-setup.ts` owns the Testing Library cleanup for every suite; this
// hook also resets the query the shell reads once on mount, and unmounts first
// so the reset never lands under a mounted tree.
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function Harness({ control }: { control: FakeControl }) {
  const client = useMemo(() => control.client(), [control]);
  const [state, actions] = useControlState(client);
  if (state.loading) return <p>Loading</p>;
  return <ThreadView actions={actions} client={client} state={state} />;
}

function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  control.message("msg_1", "thread_1", "user", "hello", 1);
  return control;
}

async function mounted(control: FakeControl) {
  render(<Harness control={control} />);
  await waitFor(() => expect(screen.queryByText("Loading")).toBeNull());
  return userEvent.setup();
}

function composer(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Message input" }) as HTMLTextAreaElement;
}

describe("thread view", () => {
  it("joins the text parts of an appended message and trims them", () => {
    const message = { content: [{ type: "text", text: " What does" }, { type: "image", image: "x" }, { type: "text", text: "Echo claim? " }] } as unknown as AppendMessage;
    expect(draftText(message)).toBe("What does Echo claim?");
  });

  it("sends a durable message and then starts a run", async () => {
    const control = seeded();
    const user = await mounted(control);
    await user.type(composer(), "What does Echo claim?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(control.posts.map((post) => post.path)).toEqual(["threads/thread_1/messages", "threads/thread_1/runs"]));
    expect(control.posts[0]!.body).toMatchObject({ content: "What does Echo claim?", role: "user", expected_revision: 0 });
  });

  it("sends the research segment's message as a research turn", async () => {
    const control = seeded();
    const user = await mounted(control);
    await user.click(screen.getByRole("radio", { name: "Research" }));
    await user.type(composer(), "What does Echo claim?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(control.posts).toHaveLength(2));
    expect(control.posts[0]!.body).toMatchObject({ content: "/research What does Echo claim?" });
  });

  it("asks for the kind of message the segment is set to", async () => {
    const control = seeded();
    const user = await mounted(control);
    expect(composer().placeholder).toBe(copy.thread.placeholderChat);
    await user.click(screen.getByRole("radio", { name: "Research" }));
    await waitFor(() => expect(composer().placeholder).toBe(copy.thread.placeholderResearch));
    await user.click(screen.getByRole("radio", { name: "Chat" }));
    await waitFor(() => expect(composer().placeholder).toBe(copy.thread.placeholderChat));
  });

  it("follows a draft that names its own mode", async () => {
    const control = seeded();
    const user = await mounted(control);
    await user.type(composer(), "/research What does Echo claim?");
    expect(screen.getByRole("radio", { name: "Research" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("radio", { name: "Chat" }).getAttribute("aria-checked")).toBe("false");
  });

  it("offers to unarchive an archived thread and takes no message for it", async () => {
    const control = seeded();
    control.thread("thread_old", "ws_1", "Archived one", { archived_at: "2026-09-01T00:00:00Z" });
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_old");
    const user = await mounted(control);
    expect(screen.getByText("Archived")).toBeTruthy();
    expect(composer().disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("This thread is archived. Unarchive it to continue.");
    await user.click(screen.getAllByRole("button", { name: "Unarchive" })[0]!);
    await waitFor(() => expect(control.posts.map((post) => post.path)).toEqual(["threads/thread_old/unarchive"]));
  });

  it("takes no message for a thread the research engine owns", async () => {
    const control = seeded();
    control.thread("thread_engine", "ws_1", "Capture carrier", { engine_owned: true });
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_engine");
    await mounted(control);
    expect(composer().disabled).toBe(true);
  });

  it("shows the working affordance while the thread's run is in flight", async () => {
    const control = seeded();
    control.run("run_1", "thread_1", "running");
    control.threads[0]!.active_run_id = "run_1";
    await mounted(control);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeNull());
    expect(screen.queryByRole("button", { name: "Send message" })).toBeNull();
    // The four slots are the thread view's own: the strip sits above the
    // composer and the run history above the conversation.
    expect(screen.getByRole("status").textContent).toContain("Working…");
    expect(screen.getByLabelText("Run history")).toBeTruthy();
    const stop = screen.getByRole("button", { name: "Stop generating" }) as HTMLButtonElement;
    expect(stop.disabled).toBe(false);
    await userEvent.setup().click(stop);
    await waitFor(() => expect(control.posts.map((post) => post.path)).toEqual(["runs/run_1/cancel"]));
  });

  it("offers no regenerate on an answer Control cannot regenerate", async () => {
    const control = seeded();
    control.message("msg_2", "thread_1", "assistant", "Echo claims memory transfers.", 2);
    await mounted(control);
    expect(screen.getByText("Echo claims memory transfers.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
  });

  it("offers no edit on a message Control cannot edit", async () => {
    const control = seeded();
    await mounted(control);
    expect(screen.getByText("hello")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("keeps the live region mounted before it has a sentence", async () => {
    const control = seeded();
    const user = await mounted(control);
    const region = screen.getByRole("status");
    expect(region.textContent).toBe("");
    // Mounted is not enough: a container hidden while empty is out of the
    // accessibility tree, so its first sentence is never announced.
    expect(region.hidden).toBe(false);
    expect(region.getAttribute("aria-hidden")).toBeNull();
    expect(region.className.split(/\s+/).filter((token) => token.endsWith("hidden"))).toEqual([]);
    await user.type(composer(), "What does Echo claim?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    // The fake daemon reports the gate closed, so the run's own sentence is what
    // the already-mounted region gains.
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Saved. Runtime dispatch is off"));
    expect(screen.getByRole("status")).toBe(region);
  });
});
