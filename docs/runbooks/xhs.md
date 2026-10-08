# XHS notes and blogs

The XHS plugin follows chosen Xiaohongshu bloggers, saves each image note with
a verbatim transcription of its carousel, lists the papers and blogs the note
recommends, and lets you import them. It is disabled by default. cortexd runs
it from its schedule table; no command here calls a provider, and nothing
reaches research evidence: notes and blogs are Library sources only, and
Library search stays papers-only. The design and its known limits are in
[the plan](../plans/xhs-sources.md). An optional weekly review corrects or
excludes recommendations that could not be imported
([Recommendation fallback](#recommendation-fallback)).

Read [Provider data disclosure](#provider-data-disclosure) before enabling it.

## Prerequisites

The plugin runs only when all of these hold:

- cortexd's engine is wired, which needs the same enabled `research-corpus`
  root that paper capture needs;
- runtime dispatch is enabled, the same gate the capture drain honours;
- `[xhs] enabled = true` in the configuration;
- both schedule rows are armed with `cortex xhs enable`;
- the `xhs-notes` and `blogs` asset roots are registered, enabled and outside
  the research corpus.

`cortex xhs status` shows both schedule rows and reports `refusal` as
`disabled_in_config` or `roots_not_ready` when one of those two conditions is
missing, with each root as `ready`, `disabled`, `missing` or
`overlaps_corpus`. Create and register both roots once with:

```sh
cortex xhs init-roots
```

It creates `<data>/sources/xhs-notes` and `<data>/sources/blogs` owner-only
(mode 0700), refuses a location that is a symlink, not a directory, owned by
another user or overlapping the `research-corpus` root, and registers each
enabled with a 20 MiB per-file bound. For each root it reports `registered`,
or `already_registered` or `registered_elsewhere` for a root that already
exists, which it leaves unchanged, so running it again is safe. Without the
two roots every scan refuses with `roots_not_ready`.

## Configuration

```toml
[xhs]
enabled = false            # also requires `cortex xhs enable`
tikhub_base = "https://api.tikhub.io"
gpt_base = ""              # HTTPS base for the Responses API; blank fails closed
gpt_model = "gpt-6-luna"
gpt_effort = "xhigh"
max_list_pages = 3         # 1 to 100
drain_units_per_tick = 10  # 1 to 100
daily_calls = { tikhub = 100, ocr = 1000, gpt = 300 }
fallback_enabled = false        # the weekly recommendation review
fallback_weekly_cap = 100       # recommendations per run, 1 to 100
fallback_model = "gpt-6.1-sol"
fallback_effort = "xhigh"

[secret_refs]
tikhub = "age://cortex/TIKHUB_API_KEY"
sub2api-gpt = "keychain://cortex-research/sub2api-gpt"
# novita, glm and glm-app-id are the existing OCR aliases; jina is optional
```

Both bases must be `https://` without credentials, query or fragment; plain
`http://` is accepted for a loopback host only. A blank `gpt_base` makes every
identification and link search fail until you set one. An unknown key in
`[xhs]` is refused. cortexd reads `[xhs]` when it starts, so restart it after a
change; the `cortex xhs` commands read the file each time.

Each provider operation receives only its own credentials:

| Operation | Credentials |
| --- | --- |
| list page, note detail | `tikhub` |
| image download | none |
| image OCR | `novita`, `glm`, `glm-app-id`; `sub2api-gpt`, optional |
| identification, link search | `sub2api-gpt` |
| blog fetch | `jina`, optional |
| weekly review decision | `sub2api-gpt` |
| weekly review page check | none |

Image OCR tries the Responses model (`gpt_model` at `gpt_base`, at low effort)
first: it reads the vertical arXiv stamp on a paper's first page, and a paper
recommendation is importable only with an arXiv ID its image writes. When it
fails or answers empty text, Novita (DeepSeek-OCR-2) transcribes the image,
and GLM is the last resort. Each engine runs only when it is configured: the
Responses model needs `gpt_base` and a resolvable `sub2api-gpt`, Novita needs
`novita`, and GLM needs `glm` with `glm-app-id`. Without any of them every OCR
task fails as `auth`. A missing XHS credential never affects arXiv
ingestion. See
[runtime credentials](runtime-credentials.md) for references and stores.

## Follow bloggers

```sh
cortex xhs follow <user_id> --role curator|author [--name "Display name"]
cortex xhs set-role <user_id> curator|author
cortex xhs unfollow <user_id>
cortex xhs list
```

`<user_id>` is the blogger's full 24-hex XHS user ID. The role is stored now
and has no effect yet: a curator's notes stay Library-only, and an author's
notes are meant to become research evidence in a later release. Unfollowing
stops new scans and keeps every saved note. Each command prints one JSON
object; `list` and `status` never create or migrate the Control database.

## Arm, cadence and scans

`cortex xhs enable` arms both schedule rows, each at the revision it just
read; `cortex xhs disable` disarms both. Migration 21 seeds them disabled:

| Job | Interval | Does |
| --- | --- | --- |
| `xhs-pull` | 86400 s | Queues page 1 of one scan per followed blogger, skipping a blogger whose previous scan still has a page to run |
| `xhs-drain` | 300 s | Runs at most `drain_units_per_tick` due tasks, oldest first, each as one engine child operation |

Each note then moves through list page, detail, image download, image OCR,
identification, link search for blogs without a link, and save. A scan
continues to the next page only while the provider says there is more, the
page is below `max_list_pages`, and the page had at least one unseen note
that is not pinned. A scan records `ok`, `no_new_notes` (page 1 only) or
`failed` with a category; a provider failure is never recorded as no new
notes. Video and other non-image notes are marked unsupported and skipped.

`cortex xhs scan [--user <user_id>]` queues a scan now, with the same page
limit and stop rule. It refuses while the plugin would refuse, and prints the
estimated calls with the newly queued scans.

## Caps

`daily_calls` caps provider calls per UTC day. `tikhub` counts list and detail
calls, `ocr` one per OCR task whichever engines it uses, and `gpt`
identification, link search and weekly review decision calls. Image downloads,
blog fetches and the weekly review's page checks are not capped. Calls refused
for `auth` or `payment` are not counted. When a provider reaches its cap, its
tasks stay pending until the next UTC day; the other providers' tasks keep
running. `cortex xhs status` shows today's calls against each cap.

## Backfill

A full scan ignores the stop-on-seen-page rule and reads up to N pages per
blogger:

```sh
cortex xhs scan --full --max-pages N            # prints the estimate, queues nothing
cortex xhs scan --full --max-pages N --yes      # queues it
```

`--max-pages` is required with `--full` and takes 1 to 1000. Without `--yes`
the command prints the estimate and exits non-zero. Only list pages are
bounded in advance (`tikhub_list_calls_at_most`); each new image note then
costs one detail call, one OCR call per image and one identification, plus one
link search per blog without a written link. The caps still apply, so a large
backfill spreads over several days.

## Failures and retries

| Category | Handling |
| --- | --- |
| `auth`, `payment` | The task fails and the drain stops for this tick. A configured credential that cannot be resolved is handled as `auth`. |
| `rate_limited`, `transient`, `outcome_unknown` | Retried after 10 min, then 1 h, then 6 h; the fourth failure is final |
| `upstream_error`, `not_found`, `invalid_response`, `url_expired` | Not retried automatically |

A child timeout is `outcome_unknown`; a paid call may then have been charged
twice. An expired image URL (HTTP 401, 403, 404 or 410 from the CDN) triggers
one fresh detail call, and the image is downloaded again from its new URL; a
second expiry is final. A failed image keeps its ordinal, later images
proceed, and the note is saved with the failure shown.

After fixing a cause, make failed tasks due again:

```sh
cortex xhs retry --failed [--kind list_page|detail|download|ocr|identify|resolve|save|blog_import|capture_link]
```

It answers how many tasks of each kind were retried and how many no longer
matched their rows and were skipped. A retried image is identified and saved
again as a new version. The Web note record also has a Retry button per failed
image.

A blog import extracts the page with trafilatura. When that yields under 2,000
characters, which is typical of a script-rendered page, Jina's copy is also
fetched and kept if it is longer. To fetch an imported blog again, for example
one imported before a fix, run:

```sh
cortex xhs refetch-blog <blog source id>
```

Its next version follows at the next drain. The blog stays imported throughout,
and a failed fetch leaves it at its current version.

## Recommendation fallback

Identification and link search leave some recommendations unimportable: blogs
without a link or with an unchecked one, blogs whose link is an arXiv page,
and papers without an arXiv ID. The fallback reviews them: free rules first,
then once a week a model review. It is off by default, adds no schedule row
and runs inside the `xhs-drain` tick, so it also needs everything the plugin
needs. The design is in [the plan](../plans/xhs-recommendation-fallback.md).

### Enable

Set `fallback_enabled = true` in `[xhs]` and restart cortexd, which reads
`[xhs]` only when it starts. Until the restart the drain neither applies the
rules nor starts a run. Before enabling, see what a run would do:

```sh
cortex xhs fallback run --dry-run   # rules and selection now; writes and calls nothing
cortex xhs fallback status          # switch, running and last run, next start, backlog
cortex xhs fallback run --yes       # start a run now under the weekly rule
```

`--dry-run` lists the changes the rules would make, the recommendations a run
would take (`eligible`, `already_reviewed`, `selected`, `waiting`), the cap,
model and effort, and why a start would refuse. Like `status`, it neither
creates nor migrates the Control database. `run --yes` starts a run only when
none is running and none started in the last 7 days; there is no force. It
refuses while the plugin would refuse or `fallback_enabled` is false, and
exits 1 with `running`, `too_soon` or `nothing_selected`. It only creates
the run: the restarted cortexd makes the calls at its next drain ticks.

`cortex xhs disable` stops the fallback with the drain. Setting
`fallback_enabled = false` and restarting cortexd stops the fallback alone.
Neither undoes a review, a correction or a queued import.

### Rules

At the start of each drain tick, a blog that is not imported, staged or
importing, and whose link is an arXiv abs, pdf or html page, becomes a paper
with that arXiv ID; its link is kept. When another paper in the same note
already has that ID, the blog is excluded as a duplicate instead and stays a
blog. A link you set yourself is left alone. The rules change at most 100 rows
per pass, one pass per tick and one more when a run starts; they make no
network call and import nothing.

### The weekly run

A tick starts a run when none is running, the last one started at least 7
days ago, and at least one recommendation can be selected; an empty backlog
starts no run. After the rules, the run takes up to `fallback_weekly_cap`
recommendations (never more than 100) from saved notes: unimported blogs
(never imported, or failed, with no blog import queued), then papers without
an arXiv ID that were never imported, oldest first within each. It skips a
recommendation that has any review already, one whose link you set, and one
an earlier run already sent to the model with the same input. The first run takes the existing backlog; the rest waits for
later runs.

Each tick runs at most two of the run's steps before ordinary tasks, counted
within `drain_units_per_tick`, so a run of 100 recommendations takes at least
50 to 100 ticks (about 4 to 8 hours at the 300 s interval).

1. **Decision.** One call to `fallback_model` at `fallback_effort` through
   `gpt_base`, with the provider's web search and a 240 s timeout. It uses one
   call of the `gpt` daily cap; when the cap is spent, the item waits for the
   next UTC day. The model may propose a blog's link, reclassify the item as a
   paper (with an arXiv ID, or as not on arXiv), exclude it as not a blog, not
   a recommendation or a duplicate, or leave it undecided. Any other answer
   leaves it to you as `insufficient_evidence`.
2. **Check.** A proposed blog link or arXiv ID is applied only after Cortex
   fetches the page itself (public addresses only, at most 2 MiB, 60 s) and
   its title matches the recommendation's. A link to arXiv, OpenReview, DOI,
   ACL Anthology or a PDF, or one that redirects there, is never taken as a
   blog. A failed fetch is tried again after 10 minutes and after 1 hour,
   without another model call; the third failure leaves the item to you as
   `fetch_failed`.

| Outcome | Applied |
| --- | --- |
| Blog link checked | The link is replaced and marked found and checked; the blog import is queued |
| arXiv ID checked | The item becomes a paper with that ID; it is not imported |
| Paper not on arXiv | The item becomes a paper without an ID; it stays unimportable |
| Exclude | The item is excluded with the model's reason |
| Undecided, title mismatch, paper page proposed as a blog, failed check, unknown outcome | The item is left to you with that reason |

A corrected paper is never imported and no Capture is staged: import it from
the note when you choose. A correction keeps the recommendation's identity and
writes the note's next saved version, and a later identification of the note
does not undo it. Before applying, Cortex checks that the recommendation, its
note's title and caption, and the cited transcription have not changed since
the run took it; otherwise the item is marked stale and nothing changes.

A decision call is made at most once. A failure after it was sent (a timeout,
a provider error, an answer that cannot be read) leaves the item to you as
`outcome_unknown`; the call may have been charged. A failure proven before the
model ran (no resolvable `sub2api-gpt`, `auth`, `payment`, or runtime dispatch
off) returns the call to the cap and stops the tick, and the item is tried
again later, after 1 hour unless dispatch was off. An item whose cited
transcription cannot be read waits 1 hour, and the run stays open until it
can.

The run completes when every item is concluded or stale. Its summary counts
blogs queued, papers corrected, papers kept (not on arXiv), exclusions, items
left to you and stale items. Corrections, imports and exclusions notify no one.

### Exclusion and restore

An excluded recommendation keeps its row and shows its reason; import and
`cortex xhs retry --failed` refuse it, and no link search runs for it. In the Web note record you can exclude any recommendation that is
not staged, importing or imported, with a reason of up to 500 characters, and
restore an excluded one. A restored recommendation is yours: it can be
imported, and automatic review never takes it again. Editing a link or
importing a recommendation leaves its review as it is; the Inbox lists only
recommendations that are still unimported. See the
[Web user guide](cortex-web-user-guide.md) for what each review shows.

### Telegram digest

When a completed run left at least one recommendation to you, cortexd sends
one Telegram message:

```text
XHS recommendations: 7 need your decision.
• short title one
• short title two
• short title three
Open: https://<web.public_origin>/?view=inbox
```

The count and up to three titles (at most 60 characters each) are counted
again just before sending, from the items still waiting and unimported; when
none remain, nothing is sent and the digest is `suppressed`. The message has
no buttons, and its link is built only from `[web] public_origin`.

It goes only to the one user in `telegram_allowed_user_ids` whose private chat
is bound at its root, never to a group or topic, and only while the transport
is open in `active` mode. Until then the run's digest stays `pending` with a
`digest_reason`:

| `digest_reason` | Meaning |
| --- | --- |
| `transport_disabled` | No transport window is open |
| `shadow` | `[transports] telegram_mode` is `shadow` |
| `recipient_unavailable` | No allowlisted user has a bound private chat, or the user the message was prepared for no longer has one |
| `recipient_ambiguous` | More than one allowlisted user has one |
| `web_origin_missing` | `[web] public_origin` is not set |

The digest never opens the transport. With no Telegram transport configured
at all, it stays `pending` with no reason. It is sent through the existing
delivery ledger as event `xhs-fallback:<run_id>`: after a restart the same
prepared message is sent to the same chat. A send whose outcome is unknown is
never repeated; the digest becomes `blocked` with `outcome_unknown`, and you
check the chat by hand. A send Telegram refused is `blocked` with
`delivery_rejected`. Several owed digests are sent separately, oldest first.

### Cost and limits

A run makes at most `fallback_weekly_cap` decision calls, one per item and
never repeated, but each may run several web searches: the cap bounds the
number of calls, not their price. The tokens and search calls the provider
reports are stored per item. A title match is a heuristic, so a doubtful page
is left to you rather than imported. Papers not on arXiv stay unimportable.
The scheduler is serial: a 240 s decision call delays the next tick.

### Rollback

Products before 0.1.36 refuse the four `fallback_*` keys, and Control schema
22 rolls back only by restoring the schema 21 baseline. A rollback must
restore `config.toml` together with the state, or remove the `fallback_*` keys
before the older product starts.

## Status

`cortex xhs status` and the Web Status page read the same state: whether the
plugin is enabled in the configuration and why it would refuse, the two roots,
both schedule rows, each followed blogger's last scan time and outcome, task
counts, today's usage against the caps, and the last failure category for
`tikhub`, `cdn`, `ocr`, `gpt` and `blog`. Neither shows a task payload, a path
or a credential.

Both also show the weekly review (`fallback` in `cortex xhs status`, the same
block as `cortex xhs fallback status`): whether `fallback_enabled` is on, the
running run with its item count and how many remain, the last run with its
model, effort, summary and digest state and reason, `next_start_at`,
`backlog` (what a run could still take after the rules) and `needs_operator`
(unimported recommendations left to you).

## Stored files

A saved note is `<xhs-notes>/<note_id>/v<N>/` with `note.md` (caption,
blogger, date, permalink and recommendations), `transcription.md` (one section
per image, verbatim OCR or its failure) and `assets/<ordinal>-<sha12>.<ext>`.
A blog is `<blogs>/<first 16 hex of the URL hash>/v<N>/` with `article.md`,
`notes.md` (the recommending notes), the screenshots copied from them, and the
article's own images as `assets/page-<NN>-<sha12>.<ext>`. An article image is
copied when it is a PNG, JPEG, GIF or WebP of at most 10 MiB, up to 60 images
and 60 MiB per article; any other image (an SVG, for example) keeps its URL,
and the Library reader names its host instead of loading it. A
version is written once; a retry, a link edit, new recommendations or a blog
refetch write the next one.

`raw/` (list and detail answers with signed URLs removed, and the fetched page
or Jina text), `ocr/` (raw OCR answers) and each `staging/` directory are
private: no route serves them, and model tools cannot read either root. Signed
image URLs exist only in private task rows.

## Disable

`cortex xhs disable` stops both jobs; queued tasks stay queued and resume after
`cortex xhs enable`. Setting `[xhs] enabled = false` and restarting cortexd
does the same regardless of the schedule rows. Either also stops the weekly
review. Neither deletes anything: saved notes, blogs, links, reviews and
imported papers stay, and there is no deletion command.

## Provider data disclosure

With the plugin enabled, these leave the machine:

| Recipient | What it receives |
| --- | --- |
| TikHub (`tikhub_base`) | The TikHub key, each followed blogger's user ID and list cursor, and each note ID for its detail |
| XHS image CDN | One request per carousel image at its signed URL, without cookies |
| Novita (DeepSeek-OCR-2) | An image the Responses model did not read (every image when `gpt_base` or `sub2api-gpt` is missing), with the `novita` key |
| GLM (`layout_parsing`) | An image that no earlier OCR engine read, with the GLM credentials |
| The Responses endpoint (`gpt_base`) | Every downloaded image, each note's caption and every image transcription, and the title of each blog recommendation without a link, which the model searches the web for |
| Blog sites and their image hosts | A request from this machine to check a found link (at most 2 MiB), to import a blog (at most 5 MiB of HTML) and to copy each of its images, without cookies, to public addresses only |
| Jina Reader (`r.jina.ai`) | A blog URL whose page could not be fetched or yielded under 2,000 characters, with the `jina` key when one is configured |
| The Responses endpoint, as `fallback_model` | For each recommendation a weekly run reviews: its kind, title, verbatim quote, link and link state, the checked page title, the note's title, and at most 12,000 characters of the cited caption or image transcription around the quote; at most 32 KiB per call |
| The Responses provider's web search | The search queries the model writes while reviewing those recommendations |
| Proposed pages and `arxiv.org` | A request from this machine to check each proposed blog link or `https://arxiv.org/abs/<id>` page, at most 2 MiB, without cookies, to public addresses only |
| Telegram | For a run that left recommendations to you: their count, up to three titles and the Web Inbox link, to the one bound operator chat |

The last four rows apply only with `fallback_enabled`; a weekly review sends no
image, signed URL, private path, other note or task row. Images and captions
are other people's posts; transcriptions may contain whatever the images show.
The Control token, other sources and your conversations are not sent.

## Live acceptance

All tests in this repository are provider-free. The TikHub detail response
shape follows a recorded probe. Before relying on the plugin, store the TikHub
and GPT keys as references, run a small test set of notes and check the
identification match rate, then approve a backfill budget.
