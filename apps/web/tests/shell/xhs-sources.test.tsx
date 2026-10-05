import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { copy } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl } from "./fake-control";
import { xhsBloggerStatus, xhsStatusProjection, XHS_USER_ID } from "./xhs-fixtures";

afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

// One paper, one blog and one note; the note recommends both, and the three
// were added on different days so newest-first is visible in the order.
function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  control.source("source_paper", "arxiv:2401.00001", "Synthetic Memory Networks", { created_at: "2026-09-01T00:00:00Z" });
  control.blog("source_blog", "Notes on synthetic retrieval", { created_at: "2026-09-03T00:00:00Z" });
  control.xhsNote("source_note", "本周论文 Weekly reading list");
  control.sources.find((s) => s.id === "source_note")!.created_at = "2026-09-05T00:00:00Z";
  control.link("source_note", "source_blog", 2);
  control.link("source_note", "source_paper", null);
  control.sourceContent("source_blog", "full_text", "# Notes on synthetic retrieval\n\nThe article body.\n");
  control.sourceContent("source_blog", "notes", "# Recommended in\n\nOne note.\n");
  control.sourceContent("source_paper", "notes", "Paper notes\n");
  return control;
}

function library(control: FakeControl, query = "") {
  window.history.replaceState(null, "", `/?project=ws_1&thread=thread_1&view=library${query}`);
  return render(<Shell client={control.client()} />);
}

async function rows() {
  const list = await screen.findByRole("navigation", { name: copy.library.sources });
  return within(list).getAllByRole("button");
}

describe("Library kind filter", () => {
  it("lists every kind newest first and names each kind without its hash", async () => {
    library(seeded());
    const listed = await rows();
    expect(listed.map((row) => row.querySelector("span")?.textContent)).toEqual([
      "本周论文 Weekly reading list", "Notes on synthetic retrieval", "Synthetic Memory Networks",
    ]);
    // A paper is cited by its arXiv id; a note or a blog shows when it was added.
    expect(listed[0]!.textContent).toBe("本周论文 Weekly reading listSep 5, 2026XHS note");
    expect(listed[1]!.textContent).toBe("Notes on synthetic retrievalSep 3, 2026blog");
    expect(listed[2]!.textContent).toBe("Synthetic Memory Networksarxiv:2401.00001paper");
    const kinds = screen.getByRole("navigation", { name: copy.library.kinds });
    expect(within(kinds).getAllByRole("button").map((button) => button.textContent)).toEqual(["All", "Papers", "Blogs", "XHS notes"]);
    expect(within(kinds).getByRole("button", { name: "All" }).getAttribute("aria-current")).toBe("true");
  });

  it("asks Control for one kind, keeps it in the query and keeps the open record", async () => {
    const control = seeded();
    library(control);
    await userEvent.click((await rows())[2]!);
    await screen.findByRole("heading", { level: 3, name: "Synthetic Memory Networks" });

    await userEvent.click(screen.getByRole("button", { name: "Blogs" }));
    await waitFor(async () => expect((await rows()).map((row) => row.querySelector("span")?.textContent)).toEqual(["Notes on synthetic retrieval"]));
    expect(control.gets).toContain("sources?kind=blog");
    expect(new URLSearchParams(window.location.search).get("kind")).toBe("blog");
    expect(screen.getByRole("heading", { level: 3, name: "Synthetic Memory Networks" })).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "All" }));
    await waitFor(async () => expect(await rows()).toHaveLength(3));
    expect(new URLSearchParams(window.location.search).has("kind")).toBe(false);
  });

  it("opens straight onto the kind the query names", async () => {
    const control = seeded();
    library(control, "&kind=xhs_note");
    await waitFor(async () => expect((await rows()).map((row) => row.querySelector("span")?.textContent)).toEqual(["本周论文 Weekly reading list"]));
    expect(control.gets.filter((path) => path.startsWith("sources?") || path === "sources")).toEqual(["sources?kind=xhs_note"]);
    expect(screen.getByRole("button", { name: "XHS notes" }).getAttribute("aria-current")).toBe("true");
  });

  it("offers search, labelled as papers only, for All and Papers and nowhere else", async () => {
    library(seeded());
    await rows();
    expect(screen.getByLabelText("Search stored papers")).toBeTruthy();
    expect(screen.getByText(copy.library.searchScope)).toBeTruthy();
    for (const [filter, shown] of [["Papers", true], ["Blogs", false], ["XHS notes", false], ["All", true]] as const) {
      await userEvent.click(screen.getByRole("button", { name: filter }));
      await waitFor(() => expect(Boolean(screen.queryByLabelText("Search stored papers")), filter).toBe(shown));
      expect(Boolean(screen.queryByText(copy.library.searchScope)), filter).toBe(shown);
    }
  });

  it("says when a kind has nothing yet", async () => {
    const control = new FakeControl();
    control.workspace("ws_1", "Echo memory");
    control.thread("thread_1", "ws_1", "First question");
    control.source("source_paper", "arxiv:2401.00001", "Synthetic Memory Networks");
    library(control, "&kind=blog");
    expect(await screen.findByText(copy.library.emptyKind)).toBeTruthy();
  });
});

describe("blog and paper records", () => {
  it("reads a blog as Article and Notes, marks it not peer-reviewed and names the note that recommended it", async () => {
    const control = seeded();
    library(control);
    await userEvent.click((await rows())[1]!);
    await screen.findByRole("heading", { level: 3, name: "Notes on synthetic retrieval" });
    expect(screen.getByText(copy.source.notPeerReviewed)).toBeTruthy();
    expect(screen.getAllByText("blog").length).toBe(2);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Article", "Notes"]);
    expect(screen.getByRole("tab", { name: "Article" }).getAttribute("aria-selected")).toBe("true");
    expect(await screen.findByRole("heading", { name: "Notes on synthetic retrieval", level: 1 })).toBeTruthy();
    expect(control.gets).toContain("sources/source_blog/document?kind=full_text");
    await userEvent.click(screen.getByRole("tab", { name: "Notes" }));
    await screen.findByText("One note.");
    expect(control.gets).toContain("sources/source_blog/document?kind=notes");
    expect(control.gets.some((path) => path.includes("kind=grounding"))).toBe(false);

    const recommended = screen.getByRole("region", { name: copy.links.title });
    expect(recommended.textContent).toBe("Recommended in本周论文 Weekly reading list· image 2");
    await userEvent.click(within(recommended).getByRole("button", { name: "本周论文 Weekly reading list" }));
    expect(await screen.findByRole("heading", { level: 3, name: "本周论文 Weekly reading list" })).toBeTruthy();
    expect(control.gets).toContain("sources/source_note");
  });

  it("shows Recommended in on a paper only when a note recommends it, citing the caption when no image does", async () => {
    const control = seeded();
    control.source("source_alone", "arxiv:2401.00002", "Unrecommended paper", { created_at: "2026-08-01T00:00:00Z" });
    library(control);
    await userEvent.click((await rows())[2]!);
    await screen.findByRole("heading", { level: 3, name: "Synthetic Memory Networks" });
    const recommended = await screen.findByRole("region", { name: copy.links.title });
    expect(recommended.textContent).toBe("Recommended in本周论文 Weekly reading list· caption");
    expect(screen.queryByText(copy.source.notPeerReviewed)).toBeNull();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Notes", "Full text", "Grounding"]);

    await userEvent.click((await rows())[3]!);
    await screen.findByRole("heading", { level: 3, name: "Unrecommended paper" });
    await waitFor(() => expect(control.gets).toContain("sources/source_alone/links"));
    expect(screen.queryByRole("region", { name: copy.links.title })).toBeNull();
  });

  it("says so when the notes behind a source cannot be read", async () => {
    const control = seeded();
    library(control);
    const listed = await rows();
    control.failNext = { path: /^sources\/source_blog\/links$/, status: 503, category: "unavailable" };
    await userEvent.click(listed[1]!);
    expect(await screen.findByText(copy.links.unreadable)).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Notes on synthetic retrieval" })).toBeTruthy();
  });

  it("maps an XHS note's tabs onto Note and Transcription until its own record lands", async () => {
    const control = seeded();
    control.sourceContent("source_note", "notes", "# Weekly reading list\n");
    library(control);
    await userEvent.click((await rows())[0]!);
    await screen.findByRole("heading", { level: 3, name: "本周论文 Weekly reading list" });
    expect(screen.getAllByText("XHS note").length).toBe(2);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Note", "Transcription"]);
  });
});

describe("Status XHS line", () => {
  async function status(control: FakeControl) {
    window.history.replaceState(null, "", "/?project=ws_1&view=status");
    render(<Shell client={control.client()} />);
    return screen.findByRole("region", { name: copy.status.xhs.title });
  }

  it("keeps success, no new notes and a provider failure distinct for each blogger", async () => {
    const control = seeded();
    control.xhsStatus = xhsStatusProjection({
      enabled: true, enabled_in_config: true, refusal: null,
      bloggers: [
        xhsBloggerStatus(XHS_USER_ID, "Synthetic Curator", { last_scan_at: "2026-10-05T08:00:00Z", last_scan_outcome: "ok", last_new_note_at: "2026-10-05T08:00:00Z" }),
        xhsBloggerStatus("00000000000000000000b0b2", "合成作者", { role: "author", last_scan_at: "2026-10-05T08:01:00Z", last_scan_outcome: "no_new_notes" }),
        xhsBloggerStatus("00000000000000000000b0b3", "Rate-limited blogger", { last_scan_at: "2026-10-05T08:02:00Z", last_scan_outcome: "failed", last_scan_error: "rate_limited" }),
        xhsBloggerStatus("00000000000000000000b0b4", null),
      ],
    });
    const section = await status(control);
    await within(section).findByText(copy.status.xhs.on);
    const lines = within(within(section).getByRole("list", { name: copy.status.xhs.bloggers })).getAllByRole("listitem");
    expect(lines.map((line) => line.getAttribute("data-scan-outcome"))).toEqual(["ok", "no_new_notes", "failed", "none"]);
    expect(lines[0]!.textContent).toBe("Synthetic Curator: last scan succeeded · Oct 5, 2026, 08:00 UTC");
    expect(lines[1]!.textContent).toBe("合成作者: last scan found no new notes · Oct 5, 2026, 08:01 UTC");
    expect(lines[2]!.textContent).toBe("Rate-limited blogger: last scan failed (rate limited) · Oct 5, 2026, 08:02 UTC");
    // An unnamed blogger is told apart only inside the details disclosure.
    expect(lines[3]!.textContent).toContain("Unnamed blogger: not scanned yet");
    expect(lines[3]!.querySelector("[data-details]")?.textContent).toContain("00000000000000000000b0b4");
  });

  it.each([
    [{}, copy.status.xhs.offConfig],
    [{ enabled_in_config: true, roots_ready: false, refusal: "roots_not_ready", roots: { "xhs-notes": "missing", blogs: "ready" } }, copy.status.xhs.offRoots],
    [{ enabled_in_config: true, refusal: null }, copy.status.xhs.offSchedule],
  ])("says why scanning is off: %j", async (extra, sentence) => {
    const control = seeded();
    control.xhsStatus = xhsStatusProjection(extra);
    const section = await status(control);
    expect(await within(section).findByText(sentence)).toBeTruthy();
    expect(within(section).getByText(copy.status.xhs.noBloggers)).toBeTruthy();
  });

  it("says the status could not be read rather than guessing", async () => {
    const control = seeded();
    control.xhsStatus = { ...xhsStatusProjection(), lease_until: "2026-10-05T08:00:00Z" };
    const section = await status(control);
    expect(await within(section).findByText(copy.status.xhs.unavailable)).toBeTruthy();
    expect(within(section).queryByText(copy.status.xhs.offConfig)).toBeNull();
  });
});
