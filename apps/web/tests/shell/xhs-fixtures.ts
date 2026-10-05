// Synthetic XHS projections shaped like Control's routes answer them
// (cortex_platform/product/api/app.py). Every identifier, caption and quote is
// invented; no real note, blogger or image is part of any fixture.

type Row = Record<string, unknown>;

export const xhsNow = "2026-10-05T08:00:00Z";
export const XHS_NOTE_ID = "0000000000000000000000a1";
export const XHS_USER_ID = "00000000000000000000b0b1";

export function xhsImage(ordinal: number, extra: Row = {}): Row {
  return {
    ordinal, asset_path: `assets/${ordinal}-${"0".repeat(11)}${ordinal % 10}.png`, media_type: "image/png",
    width: 1080, height: 1440, download_state: "ok", download_error: null, ocr_state: "ok", ocr_error: null,
    ocr_engine: "deepseek-ocr-2", ocr_flags: [], ...extra,
  };
}

export function xhsRecommendation(id: string, extra: Row = {}): Row {
  return {
    id, image_ordinal: 1, kind: "paper", title: "Synthetic Memory Networks", quote: "Synthetic Memory Networks (2401.00001)",
    arxiv_id: "2401.00001", url: null, url_state: "none", url_checked_title: null, origin: "rule+model",
    identify_run: "xhs_task_identify_1", capture_id: null, capture_state: null, capture_revision: null,
    import_state: "none", imported_source_id: null, imported_source_kind: null, revision: 0,
    created_at: xhsNow, updated_at: xhsNow, ...extra,
  };
}

export function xhsNoteHeader(sourceId: string, extra: Row = {}): Row {
  return {
    source_id: sourceId, note_id: XHS_NOTE_ID, title: "本周论文 Weekly reading list", state: "saved",
    last_error: null, content_version: 1, revision: 3, ...extra,
  };
}

export function xhsNoteProjection(sourceId: string, extra: Row = {}): Row {
  const noteId = String(extra.note_id ?? XHS_NOTE_ID);
  return {
    ...xhsNoteHeader(sourceId, { note_id: noteId }),
    blogger: { user_id: XHS_USER_ID, name: "Synthetic Curator 合成", role: "curator" },
    permalink: `https://www.xiaohongshu.com/explore/${noteId}`,
    published_at: "2026-10-01T09:00:00Z",
    caption: "Three papers and one blog post this week.\n本周推荐三篇论文和一篇博客。",
    caption_complete: true,
    images: [xhsImage(1), xhsImage(2, { asset_path: null, media_type: null, width: null, height: null, download_state: "failed", download_error: "url_expired", ocr_state: "pending", ocr_engine: null })],
    recommendations: [
      xhsRecommendation("xhs_rec_paper"),
      xhsRecommendation("xhs_rec_blog", {
        kind: "blog", title: "Notes on synthetic retrieval", quote: "Notes on synthetic retrieval", arxiv_id: null,
        url: "https://blog.example.org/synthetic-retrieval", url_state: "auto_matched",
        url_checked_title: "Notes on synthetic retrieval | Example blog", origin: "model",
      }),
    ],
    ...extra,
  };
}

export function sourceLinkEntry(sourceId: string, kind: string, title: string, extra: Row = {}): Row {
  return {
    source_id: sourceId, source_kind: kind, title, image_ordinal: 1,
    recommendation_id: `xhs_rec_${sourceId}`, created_at: xhsNow, ...extra,
  };
}

export function xhsBloggerStatus(userId: string, name: string | null, extra: Row = {}): Row {
  return {
    user_id: userId, display_name: name, role: "curator", followed: true, last_scan_at: null,
    last_scan_outcome: null, last_scan_error: null, last_new_note_at: null, ...extra,
  };
}

// The status a daemon answers with the plugin left off, as it ships.
export function xhsStatusProjection(extra: Row = {}): Row {
  return {
    enabled: false, enabled_in_config: false, roots_ready: true, refusal: "disabled_in_config",
    roots: { "xhs-notes": "ready", blogs: "ready" },
    schedules: {
      "xhs-pull": { enabled: false, revision: 0, interval_seconds: 86_400, next_due_at: xhsNow, last_outcome: null },
      "xhs-drain": { enabled: false, revision: 0, interval_seconds: 300, next_due_at: xhsNow, last_outcome: null },
    },
    bloggers: [],
    tasks: { canceled: 0, done: 0, failed: 0, pending: 0, running: 0 },
    usage: { gpt: { calls: 0, cap: 300 }, ocr: { calls: 0, cap: 1_000 }, tikhub: { calls: 0, cap: 100 } },
    last_failures: { tikhub: null, cdn: null, ocr: null, gpt: null, blog: null },
    ...extra,
  };
}
