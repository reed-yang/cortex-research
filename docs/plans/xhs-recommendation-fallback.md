# Weekly XHS recommendation fallback

Status: implemented (pending release) in product 0.1.36; approved by the
operator on 2026-10-08. It extends `docs/plans/xhs-sources.md` and needs
Control schema 22. Operator procedures are in `docs/runbooks/xhs.md`. The
sections below were corrected to match the implementation where it differs
from the approved text.

## Problem

Identification and link resolution leave some recommendations unimportable. On
the operator host on 2026-10-08, of 328 recommendations, 21 blogs were not
imported: 6 have arXiv links (papers misread as blogs), 5 have links that were
not verified, 10 have no link. 68 of the 168 unimported papers have no arXiv ID,
and Cortex imports only arXiv papers. Today each case waits for the operator.

## Decisions

- Deterministic rules run first and need no model.
- Once a week, `gpt-6.1-sol` at reasoning effort `xhigh` reviews at most 100
  remaining items (`fallback_weekly_cap`). It may correct an item, or exclude it
  with a reason. The first run takes the existing backlog.
- A corrected blog is imported automatically, but only after Cortex itself
  fetches the page and checks it. A corrected paper is never imported: importing
  a paper makes research evidence, and the operator imports papers one by one.
- Exclusion is reversible, keeps the row and shows its reason. Restore hands the
  item to the operator; automatic review leaves it alone afterwards.
- Corrections, imports and exclusions notify no one. Items the model cannot
  decide are collected into one Telegram message per run, with a link to the
  Web Inbox, where the operator handles them.
- `other` recommendations (courses, tools, books) and papers that already have
  an arXiv ID are out of scope: the first cannot be imported, the second is
  waiting for the operator by choice.

## Control schema 22

Three new tables; no existing table is rebuilt or altered. Allocate
`XHS_FALLBACK_MIGRATION = 22`, set `SCHEMA_VERSION` to it, and gate the three
tables to 22 in `cortex_platform/backup.py`. Add 22 to `SUPPORTED_SCHEMAS` in
`deployment/research_activation/state_archive.py`; the descriptor targets 22 and
adds 21 to `upgrade_from_schemas`. `migration_probe.py` accepts three new empty
tables without change; confirm that in its test.

`xhs_recommendation_reviews`: the current review state of one recommendation.
No row means never reviewed.

| Column | Constraint |
| --- | --- |
| `recommendation_id` | TEXT PK, FK `xhs_recommendations(id)` |
| `state` | `resolved_blog`, `resolved_paper`, `excluded`, `needs_operator`, `operator_owned` |
| `method` | `rule`, `model`, `operator` |
| `reason_code` | nullable; one of the codes below |
| `reason` | nullable public text, at most 500 characters |
| `corrected_fields` | JSON array, a subset of `kind`, `arxiv_id`, `url` |
| `duplicate_of` | nullable FK `xhs_recommendations(id)`, same note, not itself |
| `run_id` | nullable FK `xhs_fallback_runs(id)` |
| `revision`, `created_at`, `updated_at` | as elsewhere |

`excluded` and `needs_operator` require `reason_code`. Reason codes:
`arxiv_link` (rule), `duplicate`, `not_on_arxiv`, `not_a_blog`,
`not_a_recommendation`, `insufficient_evidence`, `conflicting_evidence`,
`title_mismatch`, `fetch_failed`, `outcome_unknown`, `operator`.

`xhs_fallback_runs`: one weekly run.

| Column | Constraint |
| --- | --- |
| `id` | TEXT PK |
| `state` | `running`, `completed` (partial unique index: one `running`) |
| `trigger` | `schedule`, `operator` |
| `started_at`, `finished_at` | finish null while running |
| `item_cap` | 1..100 |
| `model`, `effort`, `prompt_version` | bounded text |
| `summary` | JSON counts by applied action |
| `digest_state` | `none`, `pending`, `sent`, `suppressed`, `blocked` |
| `digest_reason` | nullable bounded text (why blocked) |
| `revision`, `created_at`, `updated_at` | |

`xhs_fallback_items`: one recommendation in one run.

| Column | Constraint |
| --- | --- |
| `id` | TEXT PK |
| `run_id`, `recommendation_id` | FKs; UNIQUE together |
| `ordinal` | 1..100, UNIQUE with `run_id` |
| `expected_revision` | the recommendation revision when selected |
| `input_sha256` | 64 hex, digest of the model input's Control-state parts; a cited image transcription enters by its text hash |
| `state` | `pending`, `deciding`, `verifying`, `done`, `stale` |
| `call_state` | `not_started`, `may_have_started`, `finished` |
| `proposal` | nullable JSON, at most 16 KiB (validated model answer) |
| `verification` | nullable JSON, at most 16 KiB |
| `applied` | nullable: `blog_queued`, `paper_corrected`, `paper_kept`, `excluded`, `needs_operator` |
| `usage` | nullable JSON (tokens and search calls as returned; absent is unknown) |
| `attempts` | verification attempts, 0..3 |
| `next_attempt_at`, `lease_until`, `last_error` | as `xhs_tasks` |
| `revision`, `created_at`, `updated_at` | |

## Rules

`cortex_platform/product/xhs/fallback.py` holds pure functions; the store
applies their result in one transaction per batch, with an audit event.

- **arXiv link.** A `blog` that is not imported, staged or importing, has no
  review row (or one in `resolved_blog`), whose `url_state` is not
  `operator_set`, and whose URL is an arXiv abs, pdf or
  html URL with a canonical ID (`sources/identity.py`), becomes `kind=paper`
  with that `arxiv_id`. Its URL is kept. Review: `resolved_paper`, method
  `rule`, reason `arxiv_link`, `corrected_fields=["kind","arxiv_id"]`. Not
  imported.
- **Duplicate.** If another paper in the same note already has that arXiv ID,
  the row is excluded instead (`duplicate`, `duplicate_of` the other row)
  without being converted: it stays `kind=blog` with empty
  `corrected_fields`. Never across notes. A model `reclassify_paper` whose ID
  another paper of the same note already has is excluded the same way.

While `fallback_enabled` is true, rules run at the start of every `xhs-drain`
tick (bounded to 100 rows) and when a run starts, so an upgrade alone changes
no data. They make no network call and never import.

Correction keeps the recommendation's `id`, `item_key`, `title`, `quote`,
`image_ordinal`, `origin` and `identify_run`, bumps its revision and the note's
revision, and queues the note's next saved version as a link edit does.
Re-identification must not undo a correction: `_xhs_upsert_recommendation` skips
a `url` the review lists in `corrected_fields`, and skips `kind` and `arxiv_id`
together when it lists either, so a kept ID never pairs with a re-identified
kind. It never touches the review row.

## The weekly run

**Trigger.** No new schedule row. When `fallback_enabled` is true, the
`xhs-drain` tick starts a run if none is running, the last run started at
least 7 days ago (or there was none) and the selection is not empty; no empty
run is created, so an empty backlog does not start the 7-day wait (refusal
`nothing_selected`). `cortex xhs fallback run --yes` starts one under the same
rule; there is no force. It also refuses while the plugin refuses
(`disabled_in_config`, `roots_not_ready`) or `fallback_enabled` is false.
`cortex xhs disable` stops the drain and therefore the fallback.

**Selection** (in the transaction that creates the run), after the rules:

- `kind='blog'` with `import_state IN ('none','failed')` and no pending or
  running `blog_import` task; or `kind='paper'` with `arxiv_id IS NULL` and
  `import_state='none'`;
- the note is saved;
- `url_state` is not `operator_set`: a link the operator chose is theirs;
- no review row, or none in `excluded`, `operator_owned`, `resolved_blog`,
  `resolved_paper`, `needs_operator`;
- not already an item of an earlier run with the same `input_sha256`, unless
  that item went stale before its call (`call_state='not_started'`).

Order: blogs first, then papers; then `created_at`, `id`. Take at most
`min(fallback_weekly_cap, 100)`. The rest waits for the next run.

**Execution.** The drain processes at most 2 fallback stages per tick, counted
within `drain_units_per_tick`, after the rules and before ordinary tasks.

Each claim checks that the item is still current before the model call and
before each verification attempt; apply checks again.

1. `deciding`: reserve one `gpt` call in `xhs_usage` (the daily cap still
   applies; if it is spent the item waits for the next UTC day), set
   `call_state=may_have_started`, and run the child operation
   `xhs_fallback_decide`. A result is validated, stored in `proposal`, and the
   item moves to `verifying` (or straight to apply for `exclude`, `undecided`
   and `reclassify_paper` without an ID). A provider failure after dispatch,
   or a child result that fails its shape check or answers another input or
   prompt version, is never retried: the item gets `needs_operator`, reason
   `outcome_unknown`. A failure proven before dispatch releases the
   reservation, leaves the item pending and stops the tick: an unresolved
   credential, the runtime gate, or the child failures `auth` and `payment`
   (the Responses client raises `auth` before any request when the key or base
   is missing, and the parent cannot tell that from a 401). The item then waits
   1 hour, except when only dispatch was off. An item whose input cannot be
   read (a missing or changed staged transcription) is released before any
   reservation and waits 1 hour; the run stays running until it can be read.
2. `verifying`: the child operation `xhs_fallback_verify` (no credentials)
   checks the proposal (below). A transient fetch failure retries after 10
   minutes and 1 hour, at most 3 attempts, without calling the model again; then
   `needs_operator`, reason `fetch_failed`.
3. Apply, in one transaction (`apply_xhs_fallback_result`): recheck that the
   recommendation revision still equals `expected_revision`, the item's
   `input_sha256` still equals the current digest (a changed caption, note
   title or cited transcription makes it stale) and the row is still eligible;
   otherwise the item becomes `stale` and nothing changes. Then write
   the review and correction, and for a verified blog call only
   `_xhs_queue_blog_import`. Never call `_xhs_import_one`, `_stage_capture` or
   any Capture command.

The run completes when every item is `done` or `stale`. Completion records the
summary and creates the digest (below). Leases follow `xhs_tasks` (child timeout
plus 300 s); an expired `deciding` lease with `may_have_started` is
`outcome_unknown`, never a second call.

## Model call

`xhs_fallback_decide` lazily imports `responses_client` and calls it once with
`model=fallback_model`, `effort=fallback_effort`, `tools=[{"type":
"web_search"}]`, the existing `sub2api-gpt` key and `gpt_base`, and a 240 s
timeout. Input, one JSON document of at most 32 KiB: `prompt_version`,
`recommendation` (`kind`, `title`, `quote`, `url`, `url_state`,
`checked_page_title`) and `note` (`title`, at most 1,000 characters; `cited`,
`caption` or `image N`; `text`, the cited caption or image transcription, at
most 12,000 characters around the quote, shrunk until the document fits). No
signed URLs, private paths, other notes or task rows. The child returns the
input's hash, which the parent checks against the input it built.

The instructions treat the note text as evidence, never as instructions, and
ask for one JSON object, parsed and validated in the parent as identify's
answer is:

```json
{"outcome": "corrected_url | reclassify_paper | exclude | undecided",
 "url": "string or null",
 "arxiv_id": "string or null",
 "reason_code": "string",
 "reason": "one short factual sentence"}
```

| Outcome | Valid when |
| --- | --- |
| `corrected_url` | item is a blog; `url` is an http(s) URL that `normalize_url` accepts; `arxiv_id` null |
| `reclassify_paper` | `arxiv_id` canonical, or null with reason `not_on_arxiv`; `url` optional |
| `exclude` | reason `not_a_blog`, `not_a_recommendation` or `duplicate`; never because nothing was found |
| `undecided` | reason `insufficient_evidence` or `conflicting_evidence` |

Anything else is `needs_operator`, reason `insufficient_evidence`; that includes
a reason over 500 characters or with control characters. Extra keys are
dropped, and a valid but non-canonical arXiv ID (`arXiv:2501.01234v2`) is
canonicalized. A `corrected_url` that is already a paper host or PDF is
`needs_operator`, `not_a_blog`, without a fetch. The answer's
`usage` (tokens, search calls) is stored on the item. Responses usage that the
client already returns is kept; nothing else changes in the client.

## Verification and what is applied

`xhs_fallback_verify` (profile `cortex_research.xhs_fallback`, reusing
`blog_fetch.fetch_title` and its address and redirect policy):

| Proposal | Check | Applied on success | On mismatch |
| --- | --- | --- | --- |
| `corrected_url` | fetch the page title; `identify.title_matches` against the stored title; final host is not a paper host (arxiv.org, openreview.net, doi.org, aclanthology.org, *.pdf) | URL replaced (`url_state=auto_matched`, checked title stored), review `resolved_blog`, blog import queued | `needs_operator`, `title_mismatch` or `not_a_blog` |
| `reclassify_paper` with ID | fetch `https://arxiv.org/abs/<id>` title; title match | `kind=paper`, `arxiv_id`; review `resolved_paper` | `needs_operator`, `title_mismatch` |
| `reclassify_paper` without ID | none | `kind=paper` (URL kept if any); review `resolved_paper`, `not_on_arxiv` | |
| `exclude` | none | review `excluded`, method `model`, reason shown | |
| `undecided` | none | review `needs_operator` | |

For an item that is already a paper, `reclassify_paper` with an ID only adds the
ID; when another paper of the same note has that ID, the item is excluded as
`duplicate`. A policy refusal or failed fetch never leads to exclusion.

## Operator commands and import

Store commands follow the receipt, revision fence and audit pattern of
`set_xhs_recommendation_link`:

- `exclude_xhs_recommendation(note_source_id, recommendation_id, reason,
  expected_revision, actor_id, idempotency_key)`: review `excluded`, method
  `operator`, reason code `operator`.
- `restore_xhs_recommendation(...)`: review `operator_owned`; the reason is
  cleared. Only an `excluded` row is restored.

Both refuse imported, staged or importing rows. Every review write bumps the
recommendation's revision as a fence; only a correction also bumps the note's.
Exclude, restore and the link edit check the revision against the full
recommendation view, so a 409's `current` carries `review`.
`_xhs_import_refusal` refuses `excluded`. A link edit or import by the operator
leaves the review as it is; the Inbox list shows only items that are still
unimported.

Audit events: `xhs.recommendation.corrected`, `.excluded`, `.restored`,
`xhs.fallback.started`, `xhs.fallback.completed`, `xhs.fallback.digest` (only
for the final states `sent`, `suppressed` and `blocked`). A `needs_operator`
outcome has no audit event; the item row is the record.

## Telegram digest

On completion, if any run item ended `needs_operator`, the run's
`digest_state` becomes `pending`; otherwise `suppressed`. The transport drain
sends pending digests through the existing ledger with a non-run event ID
`xhs-fallback:<run_id>`, beside the `cmd:` replies, so a restart resumes the same
frozen message and an unknown send is never resent. One chunk, no buttons:

```text
XHS recommendations: 7 need your decision.
• short title one
• short title two
• short title three
Open: https://<web.public_origin>/?view=inbox
```

At most three titles of at most 60 characters, escaped. The link is built only
from `web.public_origin` and is plain text that Telegram detects. The count and
titles are computed again when the message is frozen, from the run's items that
are still `needs_operator` and unimported; with none left the digest becomes
`suppressed` and nothing is sent. The recipient is the single allowlisted
Telegram user whose private root chat is bound; allowlisted users without a
binding are ignored. With none or several, with the transport disabled or in
shadow, or without `web.public_origin`, the digest stays `pending` and the
Status view says why (`digest_reason`: `recipient_unavailable`,
`recipient_ambiguous`, `transport_disabled`, `shadow`, `web_origin_missing`).
`transport_disabled` is recorded by the transport window supervisor on ticks
without a window; with no Telegram adapter configured, the digest stays
`pending` with no reason. It is never sent by turning the transport on.

The digest pass runs in every transport drain pass with a window open and
reads owed digests from the run rows, so a digest owed while no daemon ran is
still sent. Each is sent separately, oldest first, at most 10 per pass. A
digest frozen for one recipient is never frozen again for another: if that
destination can no longer be resolved it stays `pending` with
`recipient_unavailable`. An unknown send outcome makes it `blocked` with
`outcome_unknown`, a send Telegram rejected `blocked` with
`delivery_rejected`; a rate limit or a refusal proven before any send keeps it
`pending` and retries on the next pass.

## API, CLI and configuration

- Recommendation DTO gains `review`: null or `{state, method, reason_code,
  reason, corrected_fields, updated_at}`.
- `POST /api/v1/sources/{note_source_id}/recommendations/{id}/exclude` with
  `{reason, expected_revision}`; `.../restore` with `{expected_revision}`.
- `GET /api/v1/xhs/recommendations?review=needs_operator&limit=N` (`review`
  required, `limit` 1..100, default 100): `{items, total}`, the unimported
  items needing the operator, newest review first, each with note source ID,
  note title, recommendation ID, kind, title, reason code, reason and
  `updated_at`.
- `GET /api/v1/xhs/status` gains `fallback`: enabled, running run (with its
  item count and how many remain), last run (started, finished, counts, digest
  state and reason), next start, backlog count (eligible minus already
  reviewed, after the planned rules), and the count needing the operator.
  `cortex xhs status` carries the same block.
- `cortex xhs fallback status`, `cortex xhs fallback run --dry-run` (rules and
  selection, read-only, no network), `cortex xhs fallback run --yes`.
- `[xhs]` gains `fallback_enabled = false`, `fallback_weekly_cap = 100` (1..100),
  `fallback_model = "gpt-6.1-sol"`, `fallback_effort = "xhigh"`. Read at
  daemon start; the `cortex xhs` commands read them each time. Older builds
  reject these keys, so a rollback restores `config.toml` with the state.
- `--dry-run` neither creates nor migrates the database. A start refusal
  (`running`, `too_soon`, `nothing_selected`) exits 1 with that category on
  stderr.

## Web

- A recommendation row shows its review: `Excluded` with the reason and a
  Restore button; `Corrected automatically` with the corrected fields; `Paper
  identified — import when ready`; `Not on arXiv — Cortex imports arXiv papers
  only`; `Needs your decision` with the reason. `Paper identified` and `Needs
  your decision` show only while the row is unimported; a restored row shows
  no review line. An unimported, unexcluded row gets an Exclude action, a
  reason field inside its expanded evidence. `importable()` refuses excluded
  rows.
- Inbox gains a "XHS recommendations" section above Ideas listing the
  needs-operator items; Open goes to the note in the Library (existing
  `openSource`). It reads on Inbox entry and whenever a shell command finishes,
  and is hidden when nothing waits and the read succeeded.
- Status adds one fallback line: off, on or running (items left), the last
  completed run's counts and digest state, and the next start. The backlog and
  needs-operator totals are decoded but not shown.
- Every string is in `copy.ts`. Decoders, client methods and the gateway
  allowlist change together.

## Tests

Python: migration 21 to 22 (fresh, upgrade, replay, interrupted, foreign keys,
backup gates; narrow tests that call 21 the newest); rules (arXiv URL forms,
duplicates, protected rows, re-identification keeps corrections); selection (cap,
order, exclusions, repeat suppression); decide and verify handlers with fake
providers, including malformed answers, prompt injection in the quote, unknown
outcome never retried, verification retries without a model call, arXiv and
OpenReview hosts never imported as blogs, paper corrections never creating a
Capture; apply staleness; exclude and restore commands; import refusal of
excluded rows; digest freeze, resume, suppression and blocked recipient; routes
and CLI.

Web: DTO decoding, review display, Exclude and Restore, importable, Inbox
section and Open, Status line, gateway allowlist, copy audit; `npm run
test:fast`.

## Release

Product 0.1.36 with Control schema 22; the worker stays gen10. Follow the 0.1.30
precedent: isolated fresh install, 21 to 22 upgrade with baseline archive and
proof, rollback refusal, restore of schema 21 state and configuration, then the
production cutover. After it, enable `fallback_enabled`, start the first run,
and check the corrections, imports and the digest.

## Known limits

- 100 items bound the model calls, not their price; one call may run several
  searches.
- A title match is a heuristic; verification leaves doubtful blogs to the
  operator rather than importing a wrong page.
- Papers not on arXiv stay unimportable.
- The scheduler is serial; a 240 s model call delays the next tick.
- Telegram has no delivery guarantee beyond the existing ledger: an unknown send
  outcome needs manual follow-up.
