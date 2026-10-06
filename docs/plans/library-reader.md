# Library reader: rendered Markdown and local figures

Status: implemented. Scope is adopted
`paper` sources; the planned `blog` and `xhs_note` kinds reuse the same reader
and routes later.

## Problem

Library shows Notes, Full text and Grounding as line-numbered literal Markdown.
Headings, tables and math are unrendered and figures are invisible: no route
serves corpus assets, and a relative `assets/fig1.png` rendered in the browser
would resolve against the Web origin. Some adopted papers also reference their
own figures through a stale `papers/<dir>/assets/` prefix, and some referenced
files are missing.

## Decisions

- Every content tab gets Preview / Source / Copy source. Preview is the default
  and shows the whole document rendered full width; Source is today's paged,
  line-numbered view and remains the citation view (line range and hash). There
  is no side-by-side mode. The choice is remembered for Library only.
- Rendering reuses `MarkdownDocument` (the chat renderer), so GFM, KaTeX and
  code styling match the thread. Chat and Outputs behaviour does not change.
- Preview reads the whole file once, up to 2 MiB. Larger files are Source-only.
  The paged endpoint is unchanged: it splits by bytes and can cut a fence,
  table or formula, and paging 2 MiB would take about 105 requests that each
  rehash the file.
- Figures load through a source-bound asset route that can only read the
  selected source's own `assets/` directory. Missing files render a placeholder
  naming the file. Remote, `data:`, `file:` and protocol-relative images are not
  loaded; remote ones show as text naming their host. Stored Markdown is never
  rewritten.

## Control API

### `GET /api/v1/sources/{source_id}/document?kind=notes|full_text|grounding`

Same authorization, adoption binding and safe read as the paged content route,
read once. `kind` defaults to `notes`; no other query keys.

200, `Cache-Control: no-store`:

```json
{
  "source_id": "…", "canonical_id": "arxiv:…", "kind": "full_text",
  "text": "…", "content_sha256": "<sha256 of retained bytes>",
  "retained_bytes": 123456, "redacted": false
}
```

`text` is the same public projection the paged route produces (per-line
redaction keeps line structure). `content_sha256` names the retained file, as it
does on pages; it is not a checksum of `text`. `redacted` is true when any line
was redacted.

Errors: 400 `source_query_invalid`; 404 `not_found`; 409
`source_content_unavailable` as on pages, including a source not adopted in the
root and a file over the root's `max_bytes`; 413 `source_document_too_large`
when the retained file exceeds 2,097,152 bytes but not the root limit (checked
before decoding, reported after the authorization recheck).

### `GET /api/v1/sources/{source_id}/asset?path=<relative path>`

Exactly one `path` query value, at most 512 bytes after decoding.

Accepted forms: `assets/<segments>`, `./assets/<segments>`, and
`papers/<dir>/assets/<segments>` only when `<dir>` equals the directory of the
source's own authorized adoption binding (read-time repair of the legacy
prefix). Everything else is rejected: absolute paths, schemes, backslashes, NUL
or control characters, empty, `.` or `..` segments, percent-encoded separators,
another source's directory.

Reading: the binding comes from Control, never from the request. Traverse with
the existing descriptor-relative, no-follow utilities (see
`artifacts/materializer.py`); the target must be a regular file with one link,
at most `min(root max_bytes, 8 MiB)`. Recheck root and source authorization in
a fresh snapshot after the read, as the content reader does.

Type is decided by signature, not extension: PNG, JPEG, GIF, WebP only. SVG,
HTML, XML, PDF and anything else are refused.

200 is raw bytes with the exact `Content-Type`, `Content-Length`,
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and
`Cross-Origin-Resource-Policy: same-origin`. Errors stay JSON problems with
path-free messages: 400 `source_asset_invalid`, 404 `source_asset_unavailable`
(unknown source, missing or non-regular file, wrong directory), 413
`source_asset_too_large`, 415 `source_asset_unsupported`.

The daemon's API response type gains a binary body branch; JSON responses are
unchanged.

## Web gateway

Add exactly these GET routes to the allowlist, with the same access checks as
other source GETs:

- `sources/{id}/document` with the single query key `kind`.
- `sources/{id}/asset` with the single query key `path` (bounded length). Send
  `Accept: image/*` upstream; on 200 require one of the four image media types
  and at most 8 MiB, and forward `Content-Type`, `Content-Length`,
  `Cache-Control`, `X-Content-Type-Options` and
  `Cross-Origin-Resource-Policy`. Problems pass through as JSON.

The service worker must not cache source images.

## Web reader

- `client.ts`: `readSourceDocument(sourceId, kind, signal)` and
  `sourceAssetUrl(sourceId, path)`. A decoder in `research-contracts.ts`
  validates the document DTO and checks the UTF-8 byte length of `text` against
  2 MiB (not JavaScript string length).
- `ArtifactDocument`'s Preview / Source / Copy controls become reusable with an
  optional controlled `mode`/`onModeChange` and optional Markdown component
  overrides. Existing callers keep their current behaviour.
- `MarkdownDocument` accepts optional `components`, merged over the defaults.
  Only Library passes an `img` override: local references map to
  `sourceAssetUrl`; the server decides whether a `papers/<dir>/assets/` prefix is
  the source's own. A failed load shows the placeholder "Figure not in the
  stored copy" with the referenced path. Remote images render as plain text
  naming their host, not as a link, so an image already wrapped in a Markdown
  link keeps that link's target. No `srcset`; images load lazily and are capped at the
  content width.
- `SourceContentReader`: content-kind tabs, mode switch and Copy source. Preview
  fetches the document once per (source, kind) with an abort on change; 413
  switches to Source with a one-line notice; other errors keep Retry. Source is
  the existing paged component. Copy source copies the whole document's public
  projection (redacted lines stay redacted) and is disabled until it has loaded
  or when the document is empty. In Source, a failed whole-document read shows
  a status line with Retry, since page errors already raise their own alert.
- The mode is stored in `localStorage` under `cortex.library.reader-mode.v1`
  (`preview` or `source`), read after hydration, tolerant of blocked storage.
- Chrome copy lives in `copy.ts` and passes the copy audit.

## Verification

- Python: document route (kinds, redaction flag, 2 MiB boundary, unauthorized
  and non-paper sources) and asset route (each accepted form, the own-directory
  prefix and a foreign one, traversal and encoding attacks, symlink and
  hardlink, non-regular file, each signature, SVG refusal, size cap,
  authorization recheck, headers).
- Web unit: decoder bounds, gateway allowlist and binary pass-through, reader
  mode switching and memory, abort on source/tab change, 413 fallback, Copy
  source, `img` override scoped to Library (chat unchanged), remote and missing
  images.
- `npm --prefix apps/web run test:fast`; the Markdown browser gate also covers
  Library Preview; Library screenshots are recaptured in both schemes.
