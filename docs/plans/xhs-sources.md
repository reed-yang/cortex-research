# XHS notes, blogs and recommendation import

Status: in progress on `feat/xhs-sources` (stacked on the Library reader,
`docs/plans/library-reader.md`).

## Problem

The legacy XHS pull (TikHub list, local OCR, a model gist, an importance gate)
lives outside this product and is broken in ways that lose or corrupt data:

| Legacy defect | What this design does instead |
| --- | --- |
| Image IDs collide across images; one failed image shifts every later index | One row per carousel image keyed by full note ID and original ordinal, with the upstream `fileid` and the downloaded SHA-256; nothing is derived from a compacted list position |
| A note with failed images is marked seen and never retried | Per-note state chain and per-image task state; a note is saved with its failed images visible and retryable |
| The model's gist is stored as OCR | OCR text is stored verbatim from the OCR provider; model output is stored separately and labelled as identified |
| A provider outage returns an empty list, recorded as "no new notes" | Typed provider failures; a scan records success, success with no new notes, or failure, never an empty success after an error |
| 112 blogs stuck under the importance gate, 38 with `https:///` URLs | No importance gate: every recommendation is kept and the operator picks; URLs are validated and normalized, and an unresolved link is `null`, not an empty-host URL |
| Only `id[:8]` treated as stable | The TikHub probe on 2026-10-05 showed the full 24-hex note ID is stable across list, repeat and detail calls; identity is `xhs:<full id>` |

Missing figure files and the external cache count stay with the corpus asset
audit (#34); the Library reader already shows a placeholder for a missing figure.

## Scope

In this change:

- Two new source kinds, `xhs_note` and `blog`, with their own identities,
  asset roots and content bindings. The Library reader, the whole-document
  route and the asset route read them.
- A first-party XHS acquisition plugin. It is disabled by default. cortexd
  runs it from its schedule table: a daily scan plus a bounded drain. It covers
  followed bloggers, TikHub list and detail, image download, image OCR,
  recommendation identification, blog link resolution and note save.
- Importing recommendations, one or many at a time. Papers go through the
  existing Capture flow. Blogs go through a blog import task that fetches the
  page, extracts the article and saves a `blog` source. Every import links back
  to the note and image that recommended it.
- Web:
  - a Library kind filter;
  - an XHS note detail page that lists recommendations first;
  - the blog record;
  - "Recommended in" backlinks;
  - an XHS line on the Status page.
- CLI commands to follow bloggers and set their role, enable and disable the
  plugin, start a scan, and see status.

Out of scope, each a follow-up:

- Search and `/research` evidence for notes and blogs. The blogger role and the
  per-note override are stored now but have no effect until research context
  packet schema 3 exists. Library search stays papers-only and says so.
- An unread marker for new notes. New notes sort first.
- Reclassifying the three legacy web items adopted as papers.
- Downloading images inside blog pages. Remote images keep the Library reader's
  host-text rendering.
- Release composition. `distribution/release.toml` still targets schema 19
  while main is at 20; this change adds 21.

## Placement

- `cortex_platform/` keeps its standard-library-only rule (root
  `pyproject.toml`, closure proof). Control state, the task queue, scheduling,
  file layout, adoption, the API and the CLI live under
  `cortex_platform/product/`. XHS Control commands go in a new
  `control/xhs_store.py`, following `fragment_store.py`.
- Provider clients run only inside engine children. They live in the research
  profile beside the existing httpx clients, as new modules:
  - `xhs_client.py`: TikHub API and image CDN;
  - `image_ocr.py`: Novita DeepSeek-OCR-2 with GLM-OCR as fallback;
  - `responses_client.py`: sub2api Responses calls for identification and link
    search;
  - `blog_fetch.py`: safe fetch, trafilatura extraction, Jina fallback.

  The nine bridge modules must not import them, so the supported-surface probe
  keeps `trafilatura` forbidden for those nine. Add `trafilatura` to
  `profiles/research/pyproject.toml` and regenerate `uv.lock` with uv. Update
  `CLAUDE.md` and `product-boundaries.md`: the profile now also hosts these
  clients, and first-party image OCR is in-product, while PDF OCR stays an
  operator skill.
- New child operations, named in `engine/protocol.py` and handled in
  `engine/child.py`, with imports inside the handler:
  - `xhs_list_page`
  - `xhs_note_detail`
  - `xhs_download_image`
  - `xhs_ocr_image`
  - `xhs_identify`
  - `xhs_resolve_link`
  - `blog_fetch`

  Each operation gets only its own credentials:

  | Operation | Credentials |
  | --- | --- |
  | `xhs_list_page`, `xhs_note_detail` | `tikhub` |
  | `xhs_ocr_image` | `novita`, `glm`, `glm-app-id` |
  | `xhs_identify`, `xhs_resolve_link` | `sub2api-gpt` |
  | `blog_fetch` | `jina`, optional |

  This needs operation-scoped secret selection in the engine service. Today
  every configured alias is resolved for every effect. A missing XHS credential
  must never block arXiv ingestion. Child write roots are the XHS or blog asset
  root for that operation only.

## Control schema 21

Allocate migration 21 (`XHS_SOURCES_MIGRATION`); never edit migration 20 or
earlier. Add backup-roster entries gated on schema 21. Narrow #38's "fragments
migration is newest" test to check fragment creation at 20.

Tables (columns indicative; add revision, `created_at` and `updated_at` as the
existing tables do):

- `xhs_bloggers`: `user_id` (24 hex, primary key), `display_name`, `role` in
  (`curator`, `author`), `followed` (0/1), `last_scan_at`, `last_scan_outcome`
  in (`ok`, `no_new_notes`, `failed`), `last_scan_error`, `last_new_note_at`.
- `xhs_notes`: `note_id` (24 hex, primary key), `user_id`, `note_type`,
  `state`, `title`, `caption`, `caption_complete` (list captions are cut at
  100 characters; only the detail caption is complete), `published_at`,
  `evidence_override` in (null, `include`, `exclude`), `source_id` (null until
  the first save), `content_version`.

  `state` takes these values: `discovered`, `detail_ok`, `assets_done`,
  `ocr_done`, `identified`, `saved`, `unsupported`, `failed`. Video and other
  non-image notes become `unsupported` and are not processed.
- `xhs_note_images`: (`note_id`, `ordinal`) primary key, `fileid`, upstream
  width and height, `sha256`, `byte_size`, `media_type`, decoded width and
  height, `asset_name`. Also:
  - `download_state` and `ocr_state`, each in (`pending`, `ok`, `failed`);
  - `ocr_engine`, `ocr_flags` (`truncated`, `empty`), `ocr_text_sha256`.
- `xhs_recommendations`:
  - `id`, `note_id`, `image_ordinal` (null when the caption is the evidence),
    `kind` in (`paper`, `blog`, `other`);
  - `title`, `quote` (verbatim, required), `arxiv_id`, `url`;
  - `url_state` in (`none`, `from_text`, `auto_matched`, `unverified`,
    `not_found`, `operator_set`, `failed`), `url_checked_title`;
  - `origin` in (`rule`, `model`, `rule+model`), `identify_run`;
  - `capture_id`, `import_state` in (`none`, `staged`, `importing`,
    `imported`, `failed`), `imported_source_id`, `revision`.
- `source_content_bindings`: append-only content versions for non-paper
  sources: `source_id`, `version`, `root_id`, `directory` (relative,
  versioned), `tree_sha256`, `metadata_json`. Primary key (`source_id`,
  `version`). The reader always uses the highest version.
- `source_links`: `id`, `from_source_id`, `to_source_id`, `relation`
  (`recommends`), `recommendation_id` (unique), `created_at`. Both ends are
  foreign keys, self-links are refused, and the from end must be an `xhs_note`.
- `xhs_tasks`: `id`, `kind`, `subject_key` (unique, so creation is
  idempotent), `payload_json`, `state` in (`pending`, `running`, `done`,
  `failed`, `canceled`), `attempts`, `next_attempt_at`, `lease_until`,
  `last_error`, `result_json`.

  `payload_json` is private: a signed CDN URL may live there. No DTO, log or
  public file may expose it.
- `xhs_usage`: (`day`, `provider`) primary key, `calls`. Used for the daily
  caps.

Widen `research_schedules.operation` to admit `xhs_pull` and `xhs_drain` with
an explicit table rebuild that keeps every row, index and guard trigger. Seed:

| Job | Operation | Interval | Enabled |
| --- | --- | --- | --- |
| `xhs-pull` | `xhs_pull` | 86400 s | no |
| `xhs-drain` | `xhs_drain` | 300 s | no |

The legacy `xhs-pull-scan` rows stay inert.

## Identity, storage and reading

- `xhs_note`: authority `xhs`, canonical `xhs:<24-hex note id>`.
- `blog`: authority `url`, canonical `url:<sha256 of normalized URL>`. URL
  normalization:
  - lowercase the scheme and host, and drop the default port and the fragment;
  - keep path case, the query and the trailing slash;
  - refuse URLs that contain credentials, and refuse any scheme other than
    http and https.

  The normalized URL and the final URL are kept in the binding metadata.
- Extend identity validation, alias validation and the public projection for
  both authorities together. Do not change the paper resolver or the arXiv
  capture parser.
- Two new asset roots are configured like `research-corpus`, and both stay
  outside the corpus, so the paper indexer can never scan them:
  - `xhs-notes`: one directory per note;
  - `blogs`: one directory per blog.

  The plugin refuses to run, and says why, without both roots enabled.
- Note layout: `<note_id>/v<N>/` contains:
  - `note.md`: caption, blogger, date, permalink and the recommendation list;
  - `transcription.md`: one section per image, in order, with each image's
    verbatim OCR or its failure;
  - `assets/<ordinal>-<sha12>.<ext>`.

  Private files are not served by any route:
  - `raw/list.json` and `raw/detail.json`, with signed URLs removed;
  - `ocr/<ordinal>.json`, the raw provider responses.

  A version is written once. Later changes write `v<N+1>`: a retried image,
  an edited link or new recommendations.
- Blog layout: `<url-hash-16>/v<N>/` contains:
  - `article.md`: the extracted body, with title, author and date when known;
  - `notes.md`: source link, "Recommended in", the screenshot and its
    transcription excerpt;
  - `assets/` with the screenshot copied from the note.

  Private: `raw/page.html` when the origin fetch succeeded, and `raw/jina.md`
  when Jina was used. Never claim raw HTML that was not fetched.
- The reader, the document route and the asset route resolve a paper through
  the existing adoption join, unchanged. A `blog` or `xhs_note` resolves through
  its latest `source_content_bindings` row and an enabled root, using the same
  no-follow reads and post-read authorization recheck. Content kinds stay
  `notes`, `full_text` and `grounding`, mapped per kind:

  | Kind | `notes` | `full_text` | `grounding` |
  | --- | --- | --- | --- |
  | paper | as today | as today | as today |
  | `xhs_note` | `note.md` | `transcription.md` | none (409) |
  | `blog` | `notes.md` | `article.md` | none (409) |
- Readings publication: add an explicit paper-kind requirement in
  `readings/service.py`, covering discovery, retry and queued tasks.

## Pipeline

`xhs_pull` (daily) creates one scan per followed blogger as a `list_page`
task, and does nothing else. `xhs_drain` (every 300 s) runs at most
`drain_units_per_tick` tasks (default 10). It picks due tasks in order, oldest
first, and runs each through the engine supervisor as one child operation. It
then records the result in one Control transaction, fenced by the task
revision. All network I/O happens outside SQLite transactions, and both jobs
honour the existing dispatch gate.

| Task | Does | Next |
| --- | --- | --- |
| `list_page` (`scan:<user>:<scan>:<page>`) | TikHub `app_v2/get_user_posted_notes?user_id=&cursor=` (data at `data.data.notes`, `has_more`, cursor is the last note's `cursor`) | Upsert unseen notes as `discovered` and create `detail` tasks. Create the next page only if `has_more`, the page is under `max_list_pages`, and the page had at least one unseen non-`sticky` note. Record the blogger's scan outcome. |
| `detail` (`detail:<note>`) | `app_v2/get_image_note_detail?note_id=` (note at `data.data[0].note_list[0]`); require `data.success` and returned id == requested id | Store the full caption, `time`, `user` and an ordered `images_list` (variant order `original`, `url_size_large`, `url`); create `download` tasks |
| `download` (`download:<note>:<ordinal>`) | CDN GET, no cookies, ≤ 20 MiB, type by signature (JPEG, PNG, WebP, GIF) | Write the file into the note's staging directory under its hash. On HTTP 403 or 404, re-run `detail` once and match the image by `fileid`. |
| `ocr` (`ocr:<note>:<ordinal>:<sha256>`) | Novita `deepseek/deepseek-ocr-2`, prompt `<\|grounding\|>Convert the document to markdown.`, temperature 0, max_tokens 8000, image from local bytes; on failure GLM `layout_parsing`. Aggregate deadline 300 s, no internal retry loops longer than that. | Keep raw and markdown. `finish_reason == "length"` sets `truncated`; empty text sets `empty`. |
| `identify` (`identify:<note>:<input sha256>`) | Rules, then `gpt-6-luna`, effort `xhigh`, no tools (see below) | Upsert recommendations; create `resolve` tasks for blog items without a usable URL |
| `resolve` (`resolve:<rec>:<n>`) | Luna with `web_search`: exact title in, `{url \| null, page_title}` out; then fetch-and-verify | Set `url_state` |
| `save` (`save:<note>:<version>`) | Write `v<N>` atomically (stage, then rename), compute the tree digest | Register or update the `xhs_note` source and binding in one transaction. The note becomes `saved`. |
| `blog_import` (`blog:<rec>:<n>`) | `blog_fetch` | Register or reuse the `blog` source by identity; write the binding, the `source_links` row and `imported_source_id` together |
| `capture_link` (`capture:<capture_id>`) | Read the Capture | When the Capture is consumed, link its source; when it fails, set `import_state=failed` |

A note's state advances only when all its images have reached a final download
state, and then a final OCR state. `save` runs after `identify`. A note with a
failed image is still saved, and the failure is shown. Retrying an image resets
that image's task, then its OCR, then `identify` and `save`. The retry
produces a new version.

Failures carry a category:

| Category | Handling |
| --- | --- |
| `auth`, `payment` | The task fails and the tick stops; Status shows it. These calls are not billed. |
| `rate_limited`, `transient`, `outcome_unknown` | Retry after 10 min, then 1 h, then 6 h; at most 3 attempts, then `failed` |
| `upstream_error` (TikHub `data.success` false or a mismatched id; such calls are still billed), `not_found`, `invalid_response`, `url_expired` | Not retried automatically |

A child timeout is `outcome_unknown`. A paid call may have been charged twice;
that is accepted and documented.

Usage caps per day (config, defaults): `tikhub` 100, `ocr` 1000, `gpt` 300.
When a provider reaches its cap, its tasks wait until the next day. A full
backfill is an explicit `cortex xhs scan --full --max-pages N`. The CLI prints
the estimated call count before it enqueues anything and needs `--yes`.

## Identification

- Rules:
  - an arXiv-ID regex over each transcription and the caption, giving `paper`
    items with `origin=rule`;
  - a URL regex, which sets `url_state=from_text` for a matching blog item.

  arXiv titles come from the existing arXiv metadata client when the Capture
  imports the paper. Identification does not call arXiv.
- Model: one Responses call per note (`POST <base>/responses`, `stream:
  false`). The input is the caption plus every transcription, each labelled
  with its image index. The prompt version is recorded in the run.
  - The model must return JSON:
    `{"items":[{"kind":"paper|blog|other","title":str,"image":int|null,"quote":str,"arxiv_id":str|null,"url":str|null}]}`.
  - Take the text from `output[*].content[*]` where `type == "output_text"`.
  - Keep an item only if both `quote` and `title` occur, after whitespace and
    case normalization, in the cited image's transcription (or in the caption
    when `image` is null). Count dropped items in the run.
  - Merge items with rule items by arXiv ID, or by normalized title and image.
  - A model failure fails the task and never yields an empty recommendation
    list.
- Link verification: fetch at most 2 MiB through the `blog_fetch` safety rules
  and compare the page `<title>` or `og:title` with the item title.
  - Containment, or token overlap ≥ 0.6 after normalization, gives
    `auto_matched`.
  - Any other result gives `unverified`. The link is still shown, labelled.
  - The operator can replace any link (`operator_set`).

## Blog fetch safety

- http and https only, and default ports only.
- Resolve the host and refuse private, loopback, link-local, multicast and
  reserved addresses. Connect to the resolved address and check it again on
  every redirect, at most 5.
- No cookies, no credentials, and never forward a header across origins.
- 20 s per request, and at most 5 MiB for an HTML body.
- Extraction: trafilatura to Markdown with metadata. When it fails or produces
  under 200 characters, fall back to Jina Reader (`https://r.jina.ai/<url>`,
  key optional) and record `content_source=jina`.

## Import

- `POST /api/v1/sources/{note_source_id}/recommendations/import` with
  `{recommendation_ids: [..≤100], expected_revision}`. Each item gets its own
  disposition:
  - A paper is staged as a pending Capture (`https://arxiv.org/abs/<id>`, note
    "Recommended in XHS note <title> · image N"). An already-open Capture with
    the same payload is reused and reported. The response lists Capture IDs
    and revisions. Approval stays separate: the Web approves each one through
    the existing approve endpoint.
  - A blog with a URL gets a `blog_import` task.
  - `other` items, and blogs without a URL, are refused.
- `POST /api/v1/sources/{note_source_id}/recommendations/{id}/link` with
  `{url, expected_revision}` validates the URL and sets `operator_set`.
- `POST /api/v1/sources/{note_source_id}/images/{ordinal}/retry` with
  `{expected_revision}` resets a failed image.
- Every command uses the existing idempotency-key receipt and revision checks.
  The Capture DTO stays unchanged; links are separate rows. One paper or blog
  recommended by several notes has one source and several links.

## Read API

All read routes use the same authorization as the other source GETs and send
`no-store`.

- `GET /api/v1/sources?kind=paper|blog|xhs_note`: an optional filter on the
  existing list, applied before any limit.
- `GET /api/v1/sources/{id}/note` returns the note projection for an
  `xhs_note`:
  - blogger name and role, permalink, `published_at`, caption and
    `caption_complete`, state, revision;
  - images, each with ordinal, asset path, download and OCR state, flags and
    error category;
  - recommendations, each with every field above except private provider data,
    its Capture state, and its imported source ID and kind.

  Never include signed URLs, raw responses, private paths or task leases.
- `GET /api/v1/sources/{id}/links` returns `recommended_in` (note source,
  title, image ordinal) for any source, and `recommends` for a note.
- `GET /api/v1/xhs/status` returns enabled, roots ready, followed bloggers
  with their last scan outcome and time, task counts by state, today's usage
  against the caps, and the last failure category per provider.
- The Web gateway allowlists these routes with exact query keys and the three
  POSTs with exact bodies. Add decoders in `research-contracts.ts`.

## Web

- Library:
  - a kind filter (All, Papers, Blogs, XHS notes) kept in URL state;
  - newest first;
  - search is shown for All and Papers only, labelled "Searches stored papers".
- XHS note record (`source-record.tsx` by kind):
  - a header with blogger, role badge (Curator: Library only; Author: research
    evidence later), date and permalink;
  - the recommendations list first. Each row expands to show the image (asset
    route), the verbatim transcription, and the identified fields labelled
    "Identified (auto)", with an editable link for blogs and its `url_state`
    label;
  - a checkbox per importable row and "Import selected". That button stages,
    then approves each paper Capture and shows the outcome per row;
  - failed images as their own rows, each with Retry;
  - then the shared reader, with tabs Note and Transcription.

  Provenance labels: Transcription, Identified (auto), Caption.
- Blog record: reader tabs Article and Notes, a "Not peer-reviewed" badge, and
  "Recommended in <note> · image N".
- Paper record: "Recommended in" when links exist.
- Status: one XHS line with the last scan time and outcome per blogger. Keep
  success, no new notes and provider failure distinct.
- All copy goes in `copy.ts` and passes the copy audit. FakeControl and the
  mock server gain synthetic notes and blogs. Recapture the Library scenes in
  both schemes, plus one XHS note detail scene. Commit no real XHS images or
  captions; use generated test images.

## CLI and configuration

`cortex xhs` subcommands:

- `follow <user_id> --role curator|author [--name]`
- `unfollow <user_id>`
- `set-role <user_id> curator|author`
- `list`
- `status`
- `enable` and `disable`: both schedule rows, by expected revision.
- `scan [--user ID] [--full --max-pages N --yes]`
- `retry --failed [--kind K]`

Configuration:

```toml
[xhs]
enabled = false            # also requires `cortex xhs enable`
tikhub_base = "https://api.tikhub.io"
gpt_base = ""              # HTTPS base for the Responses API; blank fails closed
gpt_model = "gpt-6-luna"
gpt_effort = "xhigh"
max_list_pages = 3
drain_units_per_tick = 10
daily_calls = { tikhub = 100, ocr = 1000, gpt = 300 }

[secret_refs]
tikhub = "age://cortex/TIKHUB_API_KEY"
sub2api-gpt = "keychain://cortex-research/sub2api-gpt"
# novita, glm, glm-app-id already exist; jina is optional
```

## Docs

Update these:

- `product-boundaries.md`: the first-party XHS plugin, image OCR, no
  publication, and the scope of the research profile.
- `architecture.md`, `roadmap.md` (the bounded XHS milestone), `README.md`
  (supported paths and privacy: images and captions leave the machine for OCR,
  the model and Jina), and `CLAUDE.md` (the research profile bullet).
- `runtime-credentials.md`, the Web user guide and `web-verification.md`.

Add `docs/runbooks/xhs.md` covering setup, follow, cadence, caps, backfill,
retries, disable and provider data disclosure, and index it in
`docs/README.md`.

## Verification

All tests are provider-free and use synthetic fixtures shaped like the
recorded responses.

- Migration, from schema 20 and fresh:
  - schedule rows kept and the rebuilt CHECK enforced;
  - backup gates;
  - new constraints: links, task key uniqueness, binding versions.
- Clients:
  - inner-success validation, id mismatch, the variant order;
  - 401, 402, 429, 5xx and timeout mapped to their categories;
  - expired URL followed by a detail refresh;
  - OCR truncation and empty results, and the GLM fallback;
  - Responses parsing, verbatim filtering, a malformed answer and `url:null`;
  - blog fetch refusing redirects to private addresses, size and scheme limits,
    and the Jina fallback recorded.
- Pipeline:
  - pagination stops on a fully seen page while ignoring sticky notes;
  - a provider failure is not "no new notes";
  - a failed image keeps its ordinal while later images proceed;
  - retry creates a new version;
  - lease expiry, a crash between file write and registration, idempotent
    re-run, caps;
  - disabled by default;
  - a missing credential does not affect `ingest_arxiv`.
- Reader, document and asset routes for both kinds, including cross-source
  refusal; readings exclusion of non-paper kinds.
- Import:
  - batch dispositions, Capture reuse, approval through the existing endpoints;
  - the `capture_link` completion;
  - blog dedupe by identity with two notes linking to one blog.
- Web: `npm --prefix apps/web run test:fast`, the screenshot verifier and the
  control workflow process gate. Python: the focused suites for control,
  sources, api, engine, readings, profiles and tooling, plus
  `tools/check_citations.py` and the supported-surface and closure-proof tests.

Live provider acceptance follows this PR, after operator approval:

1. Store the TikHub and GPT keys as references.
2. Run a 27-note test set and measure the identification match rate.
3. Approve the backfill budget, then run the backfill.
