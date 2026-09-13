import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { copy, problemSentence } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl } from "./fake-control";

afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function seeded(): FakeControl {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  return control;
}

function deferred(): { held: Promise<void>; release: () => void } {
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  return { held, release };
}

// `arm` runs after the Inbox's own read of the captures list, so a refusal
// staged there meets the submission and not the listing.
async function capture(control: FakeControl, arm?: () => void): Promise<void> {
  const user = userEvent.setup();
  window.history.replaceState(null, "", "/?project=ws_1&view=inbox");
  render(<Shell client={control.client()} />);
  await screen.findByLabelText(copy.inbox.payloadLabel);
  await waitFor(() => expect(control.gets).toContain("captures"));
  await user.type(screen.getByLabelText(copy.inbox.payloadLabel), "https://example.com/echo");
  arm?.();
  await user.click(screen.getByRole("button", { name: copy.inbox.capture }));
}

describe("Shell", () => {
  it("says what a command did and lets the operator dismiss it", async () => {
    const user = userEvent.setup();
    const control = seeded();
    await capture(control);

    const notice = await screen.findByRole("status", { name: copy.notice.region });
    expect(within(notice).getByText(copy.notice.savedToInbox)).toBeTruthy();

    await user.click(within(notice).getByRole("button", { name: copy.notice.dismiss }));
    await waitFor(() => expect(screen.queryByRole("status", { name: copy.notice.region })).toBeNull());
  });

  it("raises a refusal as an alert, with the code closed under details", async () => {
    const control = seeded();
    await capture(control, () => {
      control.failNext = { path: /^captures$/, status: 400, category: "invalid_request" };
    });

    const alert = await screen.findByRole("alert", { name: copy.notice.region });
    expect(within(alert).getByText(copy.errors.refused)).toBeTruthy();
    const details = alert.querySelector("[data-details]") as HTMLDetailsElement | null;
    expect(details).not.toBeNull();
    expect(details!.open).toBe(false);
    expect(details!.textContent).toContain("invalid_request");
    // The line itself never spells the code the refusal was filed under.
    details!.remove();
    expect(alert.textContent ?? "").not.toContain("invalid_request");
  });

  it("shows the unavailable state when Cortex cannot be read, and retries it", async () => {
    const user = userEvent.setup();
    const control = seeded();
    control.failNext = { path: /^workspaces$/, status: 503, category: "unavailable" };
    render(<Shell client={control.client()} />);

    const fatal = await screen.findByRole("alert", { name: copy.errors.unavailable });
    expect(within(fatal).getByText(copy.errors.unavailable)).toBeTruthy();
    // No view is mounted behind it: the shell has no state to show.
    expect(screen.queryByRole("region", { name: copy.thread.region })).toBeNull();

    await user.click(within(fatal).getByRole("button", { name: copy.strip.retry }));
    expect(await screen.findByRole("region", { name: copy.thread.region })).toBeTruthy();
    expect(screen.queryByRole("alert", { name: copy.errors.unavailable })).toBeNull();
  });
});

// Opens a second thread whose read is made to fail in `arm`, and hands back
// the notice that came of it.
// `preArm` runs before the client is built, for a failure that has to be
// staged on the fetcher itself: the client captures `control.fetch` once.
async function readSecondThread(control: FakeControl, arm: () => void, preArm?: () => void): Promise<HTMLElement> {
  const user = userEvent.setup();
  control.thread("thread_2", "ws_1", "Second question");
  window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1");
  preArm?.();
  render(<Shell client={control.client()} />);
  await screen.findByRole("region", { name: copy.thread.region });
  arm();
  await user.click((await screen.findAllByRole("button", { name: "Second question" }))[0]!);
  return screen.findByRole("alert", { name: copy.notice.region });
}

describe("Shell failures that are not fatal", () => {
  it("keeps the shell mounted when one thread cannot be read", async () => {
    const control = seeded();
    const alert = await readSecondThread(control, () => {
      control.failNext = { path: /^threads\/thread_2$/, status: 500, category: "control_store_error" };
    });

    expect(within(alert).getByText(copy.errors.refused)).toBeTruthy();
    // The whole shell is not replaced for one row that could not be read.
    expect(screen.queryByRole("alert", { name: copy.errors.unavailable })).toBeNull();
    expect(screen.getByRole("region", { name: copy.thread.region })).toBeTruthy();
  });

  it("says which refusal it was when Cortex named one", async () => {
    const control = seeded();
    const alert = await readSecondThread(control, () => {
      control.failNext = { path: /^threads\/thread_2$/, status: 409, category: "machine_thread" };
    });

    expect(within(alert).getByText(problemSentence("machine_thread"))).toBeTruthy();
    expect(alert.querySelector("[data-details]")!.textContent).toContain("machine_thread");
  });

  it("says the connection is gone, not that Cortex refused, when nothing answered", async () => {
    const control = seeded();
    const alert = await readSecondThread(control, () => { control.offline = true; });

    expect(within(alert).getByText(copy.strip.offline)).toBeTruthy();
    expect(within(alert).queryByText(copy.errors.refused)).toBeNull();
    // The wire's own words stay where every other error surface keeps them.
    expect(alert.querySelector("[data-details]")!.textContent).toContain("unreachable");
  });

  it("says the answer could not be read when the row did not decode", async () => {
    const control = seeded();
    const alert = await readSecondThread(control, () => { control.threads[1]!.revision = "one"; });

    expect(within(alert).getByText(copy.errors.unreadable)).toBeTruthy();
    expect(alert.querySelector("[data-details]")!.textContent).toContain("revision");
  });

  it("falls back to the plain refusal for a failure of no known kind", async () => {
    const control = seeded();
    const alert = await readSecondThread(control, () => {}, () => {
      const answered = control.fetch;
      control.fetch = async (input, init) => (
        String(input).includes("threads/thread_2")
          ? new Response("{ this is not json", { status: 200, headers: { "Content-Type": "application/json" } })
          : answered(input, init)
      );
    });

    expect(within(alert).getByText(copy.errors.refused)).toBeTruthy();
  });
});

describe("Shell recovery", () => {
  it("retries where the operator is now, not where they came in", async () => {
    const user = userEvent.setup();
    const control = seeded();
    control.workspace("ws_2", "Second project");
    control.thread("thread_2b", "ws_2", "Other project thread");
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1");
    control.failNext = { path: /^workspaces$/, status: 503, category: "unavailable" };
    render(<Shell client={control.client()} />);
    await screen.findByRole("alert", { name: copy.errors.unavailable });

    // Wherever the shell stands when Retry is pressed -- the query is the
    // shell's own record of that -- is what it reads again.
    window.history.replaceState(null, "", "/?project=ws_2&thread=thread_2b");
    await user.click(screen.getByRole("button", { name: copy.strip.retry }));

    expect(await screen.findByRole("heading", { name: "Other project thread" })).toBeTruthy();
    expect(screen.queryByRole("alert", { name: copy.errors.unavailable })).toBeNull();
  });

  it("offers the retry once while it is running", async () => {
    const user = userEvent.setup();
    const control = seeded();
    control.failNext = { path: /^workspaces$/, status: 503, category: "unavailable" };
    render(<Shell client={control.client()} />);
    await screen.findByRole("alert", { name: copy.errors.unavailable });

    const gate = deferred();
    control.beforeResponse = (path) => (path === "workspaces" ? gate.held : undefined);
    await user.click(screen.getByRole("button", { name: copy.strip.retry }));

    // The state it answers is still on screen while the read is in flight, so
    // the button has to say it is already working.
    await waitFor(() => expect((screen.getByRole("button", { name: copy.strip.retry }) as HTMLButtonElement).disabled).toBe(true));
    gate.release();
    expect(await screen.findByRole("region", { name: copy.thread.region })).toBeTruthy();
  });
});

describe("Shell live regions", () => {
  it("marks one region per thing that changes, and never nests them", async () => {
    const control = seeded();
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1");
    const { container } = render(<Shell client={control.client()} />);
    // Settled: while the thread list is still loading the registry mounts its
    // own `role="status"` skeleton rows, which are exactly the announcements
    // this assertion is not about.
    await screen.findByRole("heading", { name: "First question" });

    const regions = Array.from(container.querySelectorAll('[aria-live], [role="status"], [role="alert"]'));
    // The status strip, and nothing above it: a live <main> would re-announce
    // the whole view every time one line inside it changed.
    expect(regions).toHaveLength(1);
    expect(regions[0]!.getAttribute("role")).toBe("status");
    expect(container.querySelector("main")!.hasAttribute("aria-live")).toBe(false);
    for (const region of regions) {
      expect(region.querySelector('[aria-live], [role="status"], [role="alert"]')).toBeNull();
    }
  });
});
