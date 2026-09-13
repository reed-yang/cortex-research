import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { CortexControlClient } from "../app/control/client";
import { Shell } from "../app/shell/shell";
import { FakeControl } from "./shell/fake-control";

// The composer moved into the shell, so the conversation-mode contract is now
// exercised through the surface the product ships.
afterEach(() => { window.history.replaceState(null, "", "/"); });

function seeded(history: string[] = []) {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "Thread one", { active_run_id: "run_one" });
  control.thread("thread_2", "ws_1", "Thread two", { active_run_id: "run_two" });
  // A run waiting on the operator leaves the composer open: the next message
  // joins the standing turn instead of starting a second run, which is what
  // lets one thread take several messages in a row.
  control.run("run_one", "thread_1", "waiting_for_decision");
  control.run("run_two", "thread_2", "waiting_for_decision");
  history.forEach((content, index) =>
    control.message(`msg_seed_${index}`, "thread_1", "user", content, index + 1));
  return control;
}

// The content of every append command, including the ones Control refuses --
// the refusal is recorded before the fake daemon can answer, so a rejected
// message is still observable as the prefixed text it carried.
function spied(control: FakeControl) {
  const writes: string[] = [];
  const client = new CortexControlClient({
    fetcher: (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST" && /\/messages$/.test(new URL(String(input), "http://127.0.0.1").pathname)) {
        writes.push(String((JSON.parse(String(init.body)) as { content: string }).content));
      }
      return control.fetch(input, init);
    }) as typeof fetch,
    idempotencyKeyFactory: () => `composer-test-command-${writes.length + 1}`,
  });
  return { client, writes };
}

async function mounted(control: FakeControl, thread = "thread_1") {
  window.history.replaceState(null, "", `/?project=ws_1&thread=${thread}`);
  const { client, writes } = spied(control);
  const view = render(<Shell client={client} />);
  await screen.findByRole("textbox", { name: "Message input" });
  return { user: userEvent.setup(), view, writes };
}

const composer = () => screen.getByRole("textbox", { name: "Message input" });
const checked = (name: string) => screen.getByRole("radio", { name }).getAttribute("aria-checked");

async function send(user: ReturnType<typeof userEvent.setup>, text: string, count: number, writes: string[]) {
  await user.type(composer(), text);
  await user.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(writes).toHaveLength(count));
}

describe("research composer", () => {
  it("enters research once, inherits follow-ups and explicitly exits without a second submission", async () => {
    const { user, writes } = await mounted(seeded());
    await user.click(screen.getByRole("radio", { name: "Research" }));
    await send(user, "Compare memory mechanisms", 1, writes);
    await waitFor(() => expect(checked("Research")).toBe("true"));
    await send(user, "Which experiment next?", 2, writes);
    await user.click(screen.getByRole("radio", { name: "Chat" }));
    await send(user, "Hello", 3, writes);
    expect(writes).toEqual(["/research Compare memory mechanisms", "Which experiment next?", "/chat Hello"]);
  });

  it("honors manually typed commands without duplicating their prefixes", async () => {
    const { user, writes } = await mounted(seeded());
    await user.type(composer(), "/Research Evidence question");
    expect(checked("Research")).toBe("true");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    await send(user, "/CHAT Short reply", 2, writes);
    expect(writes).toEqual(["/Research Evidence question", "/CHAT Short reply"]);
    await waitFor(() => expect(checked("Chat")).toBe("true"));
  });

  it("keeps ordinary chat unchanged", async () => {
    const { user, writes } = await mounted(seeded());
    await send(user, "Hello", 1, writes);
    expect(writes).toEqual(["Hello"]);
  });

  it("derives mode from durable user history on reopen and thread switch", async () => {
    const control = seeded(["/Research First question", "A follow-up"]);
    const { user, view } = await mounted(control);
    await waitFor(() => expect(checked("Research")).toBe("true"));
    await user.click(screen.getByRole("button", { name: "Thread two" }));
    await waitFor(() => expect(checked("Chat")).toBe("true"));
    await user.click(screen.getByRole("button", { name: "Thread one" }));
    await waitFor(() => expect(checked("Research")).toBe("true"));
    view.unmount();
    await mounted(control);
    await waitFor(() => expect(checked("Research")).toBe("true"));
  });

  it("does not persist a selected mode after a rejected message", async () => {
    const control = seeded();
    const { user, writes } = await mounted(control);
    // Armed only once the thread is open: the same path serves the message list
    // the shell reads on mount, and that read would consume the refusal.
    control.failNext = { path: /^threads\/thread_1\/messages$/, status: 409, category: "revision_conflict" };
    await user.click(screen.getByRole("radio", { name: "Research" }));
    await send(user, "Uncommitted question", 1, writes);
    await waitFor(() => expect(checked("Chat")).toBe("true"));
    expect(control.messages).toHaveLength(0);
    await send(user, "Hello again", 2, writes);
    expect(writes).toEqual(["/research Uncommitted question", "Hello again"]);
  });
});
