import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { copy } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl } from "./fake-control";
import { xhsBloggerStatus, xhsImage, xhsRecommendation, xhsStatusProjection, XHS_NOTE_ID, XHS_USER_ID } from "./xhs-fixtures";

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

  it("maps an XHS note's tabs onto Note and Transcription", async () => {
    const control = seeded();
    control.sourceContent("source_note", "notes", "# Weekly reading list\n");
    library(control);
    await userEvent.click((await rows())[0]!);
    await screen.findByRole("heading", { level: 3, name: "本周论文 Weekly reading list" });
    expect(screen.getAllByText("XHS note").length).toBe(2);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Note", "Transcription"]);
  });
});

describe("XHS note record", () => {
  const TRANSCRIPTION = [
    "# Transcription", "",
    "## Image 1", "", "![Image 1](assets/1-000000000001.png)", "",
    "Synthetic Memory Networks (2401.00001)", "## Results", "第一张图：合成记忆网络", "",
    "## Image 2", "", "Download failed (url_expired).", "",
  ].join("\n");

  // Every kind of row the record has to tell apart: papers to stage, one of
  // them already open in the inbox, a blog with a link, a blog without one,
  // something that is neither, and a paper already imported.
  function choices(control: FakeControl) {
    control.capture("capture_seen", "https://arxiv.org/abs/2404.00004", { state: "approved", revision: 1 });
    Object.assign(control.xhsNotes.source_note!, {
      recommendations: [
        xhsRecommendation("xhs_rec_paper"),
        xhsRecommendation("xhs_rec_second", { title: "Synthetic Attention Maps", quote: "合成注意力 Synthetic Attention Maps", arxiv_id: "2402.00002", image_ordinal: null }),
        xhsRecommendation("xhs_rec_blog", {
          kind: "blog", title: "Notes on synthetic retrieval", quote: "Notes on synthetic retrieval", arxiv_id: null, image_ordinal: 2,
          url: "https://blog.example.org/synthetic-retrieval", url_state: "auto_matched", origin: "model",
        }),
        xhsRecommendation("xhs_rec_reused", { title: "Reused paper", quote: "Reused paper 2404.00004", arxiv_id: "2404.00004" }),
        xhsRecommendation("xhs_rec_unlinked", { kind: "blog", title: "An unlinked blog", quote: "一篇没有链接的博客", arxiv_id: null, url_state: "not_found", origin: "model" }),
        xhsRecommendation("xhs_rec_other", { kind: "other", title: "A weekly reading group", quote: "每周读书会", arxiv_id: null, origin: "model" }),
        xhsRecommendation("xhs_rec_done", {
          title: "Already imported paper", quote: "Already imported paper 2403.00003", arxiv_id: "2403.00003",
          import_state: "imported", imported_source_id: "source_paper", imported_source_kind: "paper",
        }),
      ],
    });
  }

  async function openNote(control: FakeControl) {
    library(control, "&kind=xhs_note");
    await userEvent.click((await rows())[0]!);
    return screen.findByRole("region", { name: copy.xhs.recommendations });
  }

  function row(region: HTMLElement, id: string): HTMLElement {
    return region.querySelector<HTMLElement>(`[data-recommendation-id="${id}"]`)!;
  }

  it("heads the note with its blogger, role, date and permalink and lists recommendations before failed images and the reader", async () => {
    const control = seeded();
    control.sourceContent("source_note", "notes", "# Weekly reading list\n");
    const region = await openNote(control);
    expect(screen.getByText("By Synthetic Curator 合成")).toBeTruthy();
    expect(screen.getByText(copy.xhs.roles.curator)).toBeTruthy();
    expect(screen.getByText(copy.xhs.roleScope.curator)).toBeTruthy();
    expect(screen.getByText("· Published Oct 1, 2026")).toBeTruthy();
    // The note id lives in the link target only, never in the words.
    const permalink = screen.getByRole("link", { name: copy.xhs.permalink });
    expect(permalink.getAttribute("href")).toBe(`https://www.xiaohongshu.com/explore/${XHS_NOTE_ID}`);
    expect(within(region).getAllByRole("listitem").map((item) => item.getAttribute("data-recommendation-id"))).toEqual(["xhs_rec_paper", "xhs_rec_blog"]);

    const failedImages = screen.getByRole("region", { name: copy.xhs.failedImages });
    expect(within(failedImages).getByText("Image 2: download failed (image link expired)")).toBeTruthy();
    const tabs = screen.getByRole("tablist");
    expect(region.compareDocumentPosition(failedImages) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(failedImages.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Note", "Transcription"]);
  });

  it("expands a row into its image and verbatim transcription, or the caption, then what was identified", async () => {
    const control = seeded();
    choices(control);
    control.sourceContent("source_note", "full_text", TRANSCRIPTION);
    const region = await openNote(control);
    expect(control.gets.some((path) => path.startsWith("sources/source_note/document?kind=full_text"))).toBe(false);

    const paper = row(region, "xhs_rec_paper");
    await userEvent.click(within(paper).getByRole("button", { name: copy.xhs.evidence }));
    const image = within(paper).getByRole("img", { name: "Image 1" });
    expect(image.getAttribute("src")).toBe(control.client().sourceAssetUrl("source_note", "assets/1-000000000001.png"));
    expect(within(paper).getByText("Transcription · Image 1")).toBeTruthy();
    // The image's own part only, heading-like lines in its text included.
    await waitFor(() => expect(paper.querySelector("[data-transcription='1']")?.textContent).toBe("Synthetic Memory Networks (2401.00001)\n## Results\n第一张图：合成记忆网络"));
    expect(within(paper).getByText(copy.xhs.identified)).toBeTruthy();
    const identified = paper.querySelector<HTMLElement>("[data-identified]")!;
    expect(Array.from(identified.querySelectorAll("dt"), (term) => term.textContent)).toEqual(["Kind", "Title", "Quote", "arXiv"]);
    expect(within(identified).getByText("2401.00001")).toBeTruthy();

    const caption = row(region, "xhs_rec_second");
    await userEvent.click(within(caption).getByRole("button", { name: copy.xhs.evidence }));
    expect(within(caption).getByText(copy.xhs.caption)).toBeTruthy();
    expect(caption.querySelector("[data-verbatim]")?.textContent).toBe("Three papers and one blog post this week.\n本周推荐三篇论文和一篇博客。");
    expect(within(caption).queryByRole("img")).toBeNull();

    // The second image failed to download: its row says so, with its failure text.
    const blog = row(region, "xhs_rec_blog");
    await userEvent.click(within(blog).getByRole("button", { name: copy.xhs.evidence }));
    expect(within(blog).getByText(copy.xhs.imageMissing)).toBeTruthy();
    expect(blog.querySelector("[data-transcription='2']")?.textContent).toBe("Download failed (url_expired).");
    expect(within(blog).getAllByText(copy.xhs.urlStates.auto_matched).length).toBe(2);
    expect(control.gets.filter((path) => path === "sources/source_note/document?kind=full_text")).toHaveLength(1);
  });

  it("offers a checkbox only for rows Control would import", async () => {
    const control = seeded();
    choices(control);
    const region = await openNote(control);
    const selectable = within(region).getAllByRole("checkbox").map((box) => box.closest("[data-recommendation-id]")?.getAttribute("data-recommendation-id"));
    expect(selectable).toEqual(["xhs_rec_paper", "xhs_rec_second", "xhs_rec_blog", "xhs_rec_reused"]);
    expect(within(row(region, "xhs_rec_unlinked")).getByText(copy.xhs.urlStates.not_found)).toBeTruthy();
    expect(within(row(region, "xhs_rec_done")).getByText(copy.xhs.importStates.imported!)).toBeTruthy();
    const importButton = within(region).getByRole("button", { name: copy.xhs.importSelected });
    expect((importButton as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(within(region).getByRole("checkbox", { name: "Select Synthetic Memory Networks" }));
    expect((importButton as HTMLButtonElement).disabled).toBe(false);

    await userEvent.click(within(row(region, "xhs_rec_done")).getByRole("button", { name: copy.xhs.open }));
    expect(await screen.findByRole("heading", { level: 3, name: "Synthetic Memory Networks" })).toBeTruthy();
  });

  it("never sends more than 100 recommendations in one import", async () => {
    const control = seeded();
    Object.assign(control.xhsNotes.source_note!, {
      recommendations: Array.from({ length: 101 }, (_, index) => xhsRecommendation(`xhs_rec_many_${index}`, {
        title: `Synthetic paper ${index}`, quote: `Synthetic paper ${index}`, arxiv_id: `2401.${String(index + 1).padStart(5, "0")}`,
      })),
    });
    const region = await openNote(control);
    for (const box of within(region).getAllByRole("checkbox")) await userEvent.click(box);
    const importButton = within(region).getByRole("button", { name: copy.xhs.importSelected });
    expect((importButton as HTMLButtonElement).disabled).toBe(true);
    expect(within(region).getByText(copy.xhs.importTooMany)).toBeTruthy();
    await userEvent.click(within(region).getAllByRole("checkbox")[0]!);
    expect((importButton as HTMLButtonElement).disabled).toBe(false);
    expect(within(region).queryByText(copy.xhs.importTooMany)).toBeNull();
    expect(control.posts).toHaveLength(0);
  });

  it("stages the selection, approves each staged paper and reports every row, a failed approval included", async () => {
    const control = seeded();
    choices(control);
    const region = await openNote(control);
    for (const title of ["Synthetic Memory Networks", "Synthetic Attention Maps", "Notes on synthetic retrieval", "Reused paper"]) {
      await userEvent.click(within(region).getByRole("checkbox", { name: `Select ${title}` }));
    }
    // The second staged paper (capture_3) cannot be approved.
    control.failNext = { path: /^captures\/capture_3\/approve$/, status: 503, category: "unavailable" };
    await userEvent.click(within(region).getByRole("button", { name: copy.xhs.importSelected }));

    await waitFor(() => expect(row(region, "xhs_rec_paper").querySelector("[data-import-outcome]")?.textContent).toBe(copy.xhs.outcomes.approved));
    expect(control.posts.map(({ path, body }) => [path, body])).toEqual([
      ["sources/source_note/recommendations/import", { recommendation_ids: ["xhs_rec_paper", "xhs_rec_second", "xhs_rec_blog", "xhs_rec_reused"], expected_revision: 3 }],
      ["captures/capture_2/approve", { expected_revision: 0 }],
    ]);
    const outcome = (id: string) => row(region, id).querySelector("[data-import-outcome]");
    expect(outcome("xhs_rec_second")?.textContent).toBe("In the inbox but not approved: Unavailable.");
    expect(outcome("xhs_rec_second")?.getAttribute("data-import-outcome")).toBe("error");
    expect(outcome("xhs_rec_blog")?.textContent).toBe(copy.xhs.outcomes.blogQueued);
    // An open Capture of the same paper is kept, and not approved a second time.
    expect(outcome("xhs_rec_reused")?.textContent).toBe("This is already in the inbox as Approved. Cortex kept the one already there.");
    expect(control.captures.map((capture) => [capture.id, capture.state])).toEqual([["capture_seen", "approved"], ["capture_2", "approved"], ["capture_3", "pending"]]);

    // The note is read again, so each row shows where its import stands.
    await waitFor(() => expect(within(row(region, "xhs_rec_second")).queryByText(copy.captureStates.pending)).toBeTruthy());
    expect(within(row(region, "xhs_rec_paper")).getByText(copy.captureStates.approved)).toBeTruthy();
    expect(within(row(region, "xhs_rec_blog")).getByText(copy.xhs.importStates.importing!)).toBeTruthy();
    expect(within(region).queryAllByRole("checkbox")).toHaveLength(0);
    expect(control.gets.filter((path) => path === "sources/source_note/note").length).toBeGreaterThanOrEqual(2);
  });

  it("says nothing was imported when Control refuses the whole selection, and reads the note again", async () => {
    const control = seeded();
    choices(control);
    const region = await openNote(control);
    await userEvent.click(within(region).getByRole("checkbox", { name: "Select Synthetic Memory Networks" }));
    control.failNext = { path: /recommendations\/import$/, status: 409, category: "revision_conflict" };
    await userEvent.click(within(region).getByRole("button", { name: copy.xhs.importSelected }));
    expect((await within(region).findByRole("alert")).textContent).toBe("Nothing was imported. Someone else changed this first. It has been refreshed; try again.");
    expect(control.captures).toHaveLength(1);
    await waitFor(() => expect(control.gets.filter((path) => path === "sources/source_note/note")).toHaveLength(2));
  });

  it("lets the operator set a blog's link, refusing anything but a web link, and then offers it for import", async () => {
    const control = seeded();
    choices(control);
    const region = await openNote(control);
    const unlinked = row(region, "xhs_rec_unlinked");
    expect(within(unlinked).queryByRole("checkbox")).toBeNull();
    await userEvent.click(within(unlinked).getByRole("button", { name: copy.xhs.evidence }));
    const input = within(unlinked).getByRole("textbox", { name: copy.xhs.linkLabel });

    await userEvent.type(input, "ftp://files.example.org/post");
    await userEvent.click(within(unlinked).getByRole("button", { name: copy.xhs.saveLink }));
    expect(within(unlinked).getByText(copy.xhs.linkInvalid)).toBeTruthy();
    expect(control.posts).toHaveLength(0);

    await userEvent.clear(input);
    await userEvent.type(input, " https://blog.example.org/unlinked ");
    await userEvent.click(within(unlinked).getByRole("button", { name: copy.xhs.saveLink }));
    expect(await within(unlinked).findByText(copy.xhs.linkSaved)).toBeTruthy();
    expect(control.posts.map(({ path, body }) => [path, body])).toEqual([
      ["sources/source_note/recommendations/xhs_rec_unlinked/link", { url: "https://blog.example.org/unlinked", expected_revision: 0 }],
    ]);
    expect(within(unlinked).getAllByText(copy.xhs.urlStates.operator_set).length).toBe(2);
    expect(within(unlinked).getByRole("link", { name: "https://blog.example.org/unlinked" })).toBeTruthy();
    expect(within(unlinked).getByRole("checkbox", { name: "Select An unlinked blog" })).toBeTruthy();
    // The note is read again, so an import names the revision the link moved.
    await userEvent.click(await within(row(region, "xhs_rec_unlinked")).findByRole("checkbox", { name: "Select An unlinked blog" }));
    await userEvent.click(within(region).getByRole("button", { name: copy.xhs.importSelected }));
    await waitFor(() => expect(control.posts.at(-1)?.path).toBe("sources/source_note/recommendations/import"));
    expect(control.posts.at(-1)?.body).toEqual({ recommendation_ids: ["xhs_rec_unlinked"], expected_revision: 4 });
  });

  it("shows the newer row when someone else changed a link first", async () => {
    const control = seeded();
    choices(control);
    const region = await openNote(control);
    const unlinked = row(region, "xhs_rec_unlinked");
    await userEvent.click(within(unlinked).getByRole("button", { name: copy.xhs.evidence }));
    const stored = (control.xhsNotes.source_note!.recommendations as Array<Record<string, unknown>>).find((item) => item.id === "xhs_rec_unlinked")!;
    const newer = { ...stored, url: "https://blog.example.org/elsewhere", url_state: "operator_set", revision: 1 };
    control.failNext = { path: /\/link$/, status: 409, category: "revision_conflict", current: newer };
    await userEvent.type(within(unlinked).getByRole("textbox", { name: copy.xhs.linkLabel }), "https://blog.example.org/mine");
    await userEvent.click(within(unlinked).getByRole("button", { name: copy.xhs.saveLink }));
    expect(await within(unlinked).findByText(/^Link not saved\. Someone else changed this first/)).toBeTruthy();
    expect(within(unlinked).getByRole("link", { name: "https://blog.example.org/elsewhere" })).toBeTruthy();
  });

  it("retries a failed image and keeps its row saying so, or says why it was not retried", async () => {
    const control = seeded();
    Object.assign(control.xhsNotes.source_note!, {
      images: [xhsImage(1), xhsImage(2, { asset_path: null, media_type: null, width: null, height: null, download_state: "failed", download_error: "url_expired", ocr_state: "pending", ocr_engine: null }), xhsImage(3, { ocr_state: "failed", ocr_error: "rate_limited", ocr_engine: null })],
    });
    library(control, "&kind=xhs_note");
    await userEvent.click((await rows())[0]!);
    const failedImages = await screen.findByRole("region", { name: copy.xhs.failedImages });
    expect(Array.from(failedImages.querySelectorAll("[data-failed-image]"), (item) => item.querySelector("span")?.textContent)).toEqual([
      "Image 2: download failed (image link expired)", "Image 3: transcription failed (rate limited)",
    ]);

    await userEvent.click(within(failedImages).getByRole("button", { name: "Retry image 2" }));
    await waitFor(() => expect(failedImages.querySelector("[data-failed-image='2'] [data-retry-outcome]")?.textContent).toBe(copy.xhs.retryQueued));
    expect(control.posts.map(({ path, body }) => [path, body])).toEqual([["sources/source_note/images/2/retry", { expected_revision: 3 }]]);
    // Read again, the image is pending: its row stays, without Retry.
    await waitFor(() => expect(within(failedImages).queryByRole("button", { name: "Retry image 2" })).toBeNull());
    expect(failedImages.querySelector("[data-failed-image='2'] span")?.textContent).toBe("Image 2");
    expect(await screen.findByText(copy.xhs.processing)).toBeTruthy();

    // The note moved on, so a retry against what was on screen is refused.
    control.xhsNotes.source_note!.revision = 9;
    await userEvent.click(within(failedImages).getByRole("button", { name: "Retry image 3" }));
    await waitFor(() => expect(failedImages.querySelector("[data-failed-image='3'] [data-retry-outcome]")?.getAttribute("data-retry-outcome")).toBe("error"));
    expect(failedImages.querySelector("[data-failed-image='3'] [data-retry-outcome]")?.textContent).toBe("Not retried. Someone else changed this first. It has been refreshed; try again.");
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
