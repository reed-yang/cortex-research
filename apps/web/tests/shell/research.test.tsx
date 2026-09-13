import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { CortexControlClient } from "../../app/control/client";
import { copy, label } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl, now } from "./fake-control";

afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

const MATH = String.raw`$c_\text{act}\in\mathbb{R}^{T\times12}$`;
const DOSSIER = `# Memory decay\n\n${MATH}\n\nA retained finding, in the operator's own words.\n`;

function seeded(): FakeControl {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  const idea = control.researchItem("aaa1", "idea", "Memory decay in long-horizon agents", {
    status: "awaiting_human",
    pause_reason: "Waiting on your answer about scope.",
    round_count: 3,
    history: [
      { kind: "round", label: "Round 1", text: "Framed the question.", created_at: now },
      { kind: "pause", label: "Stopped for you", text: "", created_at: null },
    ],
  });
  control.researchDocument(idea, "rd_1", "Dossier", DOSSIER);
  control.researchItem("aaa2", "idea", "Retrieval grounding");
  control.researchItem("bbb1", "exploration", "Sleep-time compute");
  control.researchItem("ccc1", "project", "Echo-Infinity");
  return control;
}

function research(control: FakeControl, query = "", client = control.client()) {
  window.history.replaceState(null, "", `/?project=ws_1&view=research${query}`);
  return render(<Shell client={client} />);
}

function catalog() {
  return screen.findByRole("navigation", { name: copy.research.list });
}

async function openDossier(user: ReturnType<typeof userEvent.setup>, title: string) {
  const list = await catalog();
  await user.click(await within(list).findByRole("button", { name: new RegExp(`^${title}`) }));
  return screen.findByRole("heading", { level: 3, name: title });
}

describe("research catalog", () => {
  it("lists one kind at a time and asks Cortex for the kind the operator picked", async () => {
    const user = userEvent.setup();
    const control = seeded();
    research(control);

    const list = await catalog();
    await waitFor(() => expect(within(list).getAllByRole("button")).toHaveLength(2));
    expect(control.gets).toContain("research-items?kind=idea&limit=100&offset=0");
    expect(screen.queryByText("Sleep-time compute")).toBeNull();

    await user.click(screen.getByRole("button", { name: copy.research.explorations }));
    await screen.findByText("Sleep-time compute");
    expect(control.gets).toContain("research-items?kind=exploration&limit=100&offset=0");
    expect(screen.queryByText("Retrieval grounding")).toBeNull();
  });

  it("searches inside the page it has, without asking for another one", async () => {
    const user = userEvent.setup();
    const control = seeded();
    research(control);
    await catalog();
    await screen.findByText("Retrieval grounding");
    const reads = control.gets.length;

    await user.type(screen.getByRole("searchbox", { name: copy.research.searchLabel }), "grounding");
    expect(screen.getByText("Retrieval grounding")).toBeTruthy();
    expect(screen.queryByText("Memory decay in long-horizon agents")).toBeNull();
    expect(control.gets.length).toBe(reads);

    await user.clear(screen.getByRole("searchbox", { name: copy.research.searchLabel }));
    await user.type(screen.getByRole("searchbox", { name: copy.research.searchLabel }), "nothing here");
    expect(screen.getByText(copy.research.noMatches)).toBeTruthy();
    expect(control.gets.length).toBe(reads);
  });

  it("browses the rest of the catalog only when asked", async () => {
    const user = userEvent.setup();
    const control = seeded();
    control.researchItem("aaa3", "idea", "Sleep consolidation");
    control.researchPageLimit = 2;
    research(control);

    await catalog();
    await screen.findByText(label.researchPage(1, 2, 3));
    expect((screen.getByRole("button", { name: copy.research.previous }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole("button", { name: copy.research.next }));
    await screen.findByText(label.researchPage(3, 3, 3));
    expect(control.gets).toContain("research-items?kind=idea&limit=100&offset=2");
    expect((screen.getByRole("button", { name: copy.research.next }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Sleep consolidation")).toBeTruthy();
  });

  it("says an empty catalog is empty, and offers nothing to continue", async () => {
    const user = userEvent.setup();
    const control = new FakeControl();
    control.workspace("ws_1", "Echo memory");
    research(control);

    await screen.findByText(copy.research.empty);
    expect(screen.queryByRole("button", { name: copy.research.open })).toBeNull();
    expect(screen.getByText(copy.research.pick)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: copy.research.projects }));
    await screen.findByText(copy.research.empty);
  });

  it("reports a catalog it could not read and reads it again on retry", async () => {
    const user = userEvent.setup();
    const control = seeded();
    control.failNext = { path: /^research-items/, status: 503, category: "worker_unavailable" };
    research(control);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(copy.research.unreadable)).toBeTruthy();
    expect(screen.queryByText("Memory decay in long-horizon agents")).toBeNull();

    await user.click(within(alert).getByRole("button", { name: copy.research.retry }));
    await screen.findByText("Memory decay in long-horizon agents");
  });
});

describe("research dossier", () => {
  it("shows the state it was left in, its history and its rendered document", async () => {
    const user = userEvent.setup();
    const control = seeded();
    const { container } = research(control);

    await openDossier(user, "Memory decay in long-horizon agents");
    expect(screen.getAllByText("Waiting for you").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Waiting on your answer about scope\./).length).toBeGreaterThan(0);
    expect(screen.getByText("Round 1")).toBeTruthy();
    expect(screen.getByText("Stopped for you")).toBeTruthy();

    await waitFor(() => expect(container.querySelector(".katex")).not.toBeNull());
    expect(screen.queryByText(copy.research.documentRedacted)).toBeNull();

    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(container.querySelector("pre")?.textContent).toContain(MATH);
    expect(container.querySelector(".katex")).toBeNull();
  });

  it("says what a redacted document is, and shows only what may be copied", async () => {
    const user = userEvent.setup();
    const control = seeded();
    const item = control.researchItem("dddd", "exploration", "Private notes");
    control.researchDocument(
      item, "rd_2", "Notes", "Full retained text, including a private detail.\n", {},
      { content: "Full retained text.\n" },
    );
    research(control, "");
    await userEvent.setup().click(await screen.findByRole("button", { name: copy.research.explorations }));

    await openDossier(user, "Private notes");
    await screen.findByText(copy.research.documentRedacted);
    await user.click(screen.getByRole("button", { name: "Source" }));
    const source = document.querySelector("pre")?.textContent ?? "";
    expect(source).toContain("Full retained text.");
    expect(source).not.toContain("private detail");
  });

  it("reads a redaction that came back longer than what it was projected from", async () => {
    const user = userEvent.setup();
    const control = seeded();
    const item = control.researchItem("dddd", "exploration", "Private notes");
    // Replacing a short private line with a marker makes the authorized
    // projection longer than the retained bytes; that is an ordinary answer.
    control.researchDocument(
      item, "rd_2", "Notes", "Kept at /tmp/x\n", {},
      { content: "Kept at [redacted]\n" },
    );
    research(control, "");
    await user.click(await screen.findByRole("button", { name: copy.research.explorations }));

    await openDossier(user, "Private notes");
    await screen.findByText(copy.research.documentRedacted);
    await user.click(screen.getByRole("button", { name: "Source" }));
    const source = document.querySelector("pre")?.textContent ?? "";
    expect(source).toContain("Kept at [redacted]");
    expect(source).not.toContain("/tmp/x");

    // 19 bytes delivered, projected from 15 retained ones: the counts are
    // reported as they are, not forced into an order.
    const details = [...document.querySelectorAll("[data-details]")].map((node) => node.textContent).join("");
    expect(details).toContain(`${copy.details.documentShownBytes}19`);
    expect(details).toContain(`${copy.details.documentRetainedBytes}15`);
  });

  it("says when a document's bytes cannot be read", async () => {
    const user = userEvent.setup();
    const control = seeded();
    delete control.researchContents.rd_1;
    research(control);

    await openDossier(user, "Memory decay in long-horizon agents");
    await screen.findByText(copy.research.documentUnavailable);
    expect(screen.getByRole("heading", { level: 3, name: "Memory decay in long-horizon agents" })).toBeTruthy();
  });

  it("says when an item carries no document at all", async () => {
    const user = userEvent.setup();
    const control = seeded();
    research(control);

    await openDossier(user, "Retrieval grounding");
    expect(await screen.findByText(copy.research.noDocuments)).toBeTruthy();
  });

  it("reports a dossier it could not read", async () => {
    const user = userEvent.setup();
    const control = seeded();
    research(control);
    const list = await catalog();
    control.failNext = { path: /^research-items\/ri_/, status: 404, category: "not_found" };

    await user.click(within(list).getByRole("button", { name: /^Retrieval grounding/ }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(copy.research.dossierUnreadable)).toBeTruthy();

    await user.click(within(alert).getByRole("button", { name: copy.research.retry }));
    await screen.findByRole("heading", { level: 3, name: "Retrieval grounding" });
  });

  it("never shows one item's document under another when a read arrives late", async () => {
    const user = userEvent.setup();
    const control = seeded();
    const slow = control.researchItems[0]!;
    let release = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    control.beforeResponse = async (path) => { if (path === `research-items/${String(slow.id)}`) await held; };
    research(control);
    const list = await catalog();

    await user.click(within(list).getByRole("button", { name: /^Memory decay/ }));
    await user.click(within(list).getByRole("button", { name: /^Retrieval grounding/ }));
    await screen.findByRole("heading", { level: 3, name: "Retrieval grounding" });

    release();
    await waitFor(() => expect(screen.queryByText(copy.research.dossierLoading)).toBeNull());
    expect(screen.getByRole("heading", { level: 3, name: "Retrieval grounding" })).toBeTruthy();
    expect(screen.queryByText("Round 1")).toBeNull();
    expect(screen.queryByText(copy.research.documentRedacted)).toBeNull();
    expect(document.querySelector(".katex")).toBeNull();
  });
});

describe("opening a research conversation", () => {
  it("opens the item's conversation in the project the operator has open, and starts nothing", async () => {
    const user = userEvent.setup();
    const control = seeded();
    research(control);
    const item = await openDossier(user, "Memory decay in long-horizon agents");
    expect(item).toBeTruthy();

    await user.click(screen.getByRole("button", { name: copy.research.open }));
    await screen.findByText(copy.notice.researchOpened);

    expect(control.posts).toHaveLength(1);
    expect(control.posts[0]).toMatchObject({
      path: `research-items/${String(control.researchItems[0]!.id)}/thread`,
      body: { workspace_id: "ws_1", expected_revision: 0 },
    });
    // No project was invented, and no run was started on the way in.
    expect(control.posts.some((post) => post.path === "workspaces")).toBe(false);
    expect(control.posts.some((post) => post.path.endsWith("/runs"))).toBe(false);
    // The shell moved to the conversation itself.
    await waitFor(() => expect(screen.queryByRole("navigation", { name: copy.research.list })).toBeNull());
  });

  it("asks for a project instead of making one", async () => {
    const user = userEvent.setup();
    const control = new FakeControl();
    const idea = control.researchItem("aaa1", "idea", "Memory decay in long-horizon agents");
    control.researchDocument(idea, "rd_1", "Dossier", DOSSIER);
    window.history.replaceState(null, "", "/?view=research");
    render(<Shell client={control.client()} />);

    await openDossier(user, "Memory decay in long-horizon agents");
    expect(screen.getByText(copy.research.chooseProject)).toBeTruthy();
    expect((screen.getByRole("button", { name: copy.research.open }) as HTMLButtonElement).disabled).toBe(true);
    expect(control.posts).toHaveLength(0);
  });

  it("will not offer to continue an item Cortex says cannot be continued", async () => {
    const user = userEvent.setup();
    const control = seeded();
    control.researchItem("eeee", "idea", "Killed line of work", {
      status: "killed", continuation_ready: false,
      unavailable_reason: "The stored round for this item is not available here.",
    });
    research(control);

    await openDossier(user, "Killed line of work");
    expect((screen.getByRole("button", { name: copy.research.open }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(copy.research.blocked)).toBeTruthy();
    expect(screen.getByText("The stored round for this item is not available here.")).toBeTruthy();
  });

  it("retries an unconfirmed open under the key the first attempt carried", async () => {
    const user = userEvent.setup();
    const control = seeded();
    const keys: string[] = [];
    const carried = control.fetch;
    let issued = 0;
    const fetcher = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const key = new Headers(init?.headers).get("Idempotency-Key");
      if (key !== null) {
        keys.push(key);
        // The first delivery is never confirmed: the answer is lost, not refused.
        if (keys.length === 1) throw new TypeError("Failed to fetch");
      }
      return carried(input, init);
    };
    const client = new CortexControlClient({
      fetcher: fetcher as typeof fetch,
      idempotencyKeyFactory: () => `web-test-${++issued}`,
    });
    research(control, "", client);

    await openDossier(user, "Memory decay in long-horizon agents");
    await user.click(screen.getByRole("button", { name: copy.research.open }));
    await screen.findByText(copy.errors.unconfirmed);

    await user.click(screen.getByRole("button", { name: copy.research.open }));
    await screen.findByText(copy.notice.researchOpened);
    expect(keys).toEqual(["web-test-1", "web-test-1"]);
    expect(issued).toBe(1);
  });
});

describe("research navigation", () => {
  it("keeps the open item in the query and opens one a link names", async () => {
    const user = userEvent.setup();
    const control = seeded();
    const id = String(control.researchItems[0]!.id);
    research(control);

    await openDossier(user, "Memory decay in long-horizon agents");
    await waitFor(() => expect(window.location.search).toContain(`item=${id}`));
    expect(window.location.search).toContain("view=research");

    cleanup();
    const reopened = seeded();
    research(reopened, `&item=${id}`);
    await screen.findByRole("heading", { level: 3, name: "Memory decay in long-horizon agents" });
    expect(reopened.gets).toContain(`research-items/${id}`);
  });

  it("reaches Research from the sidebar without disturbing the open thread", async () => {
    const user = userEvent.setup();
    const control = seeded();
    window.history.replaceState(null, "", "/?project=ws_1&thread=thread_1");
    render(<Shell client={control.client()} />);
    await screen.findAllByText("First question");

    await user.click(screen.getAllByRole("button", { name: new RegExp(`^${copy.sidebar.research}`) })[0]!);
    await catalog();
    expect(window.location.search).toContain("thread=thread_1");
    expect(window.location.search).toContain("view=research");
  });
});
