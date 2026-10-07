# XHS notes and blogs

The XHS plugin follows chosen Xiaohongshu bloggers, saves each image note with
a verbatim transcription of its carousel, lists the papers and blogs the note
recommends, and lets you import them. It is disabled by default. cortexd runs
it from its schedule table; no command here calls a provider, and nothing
reaches research evidence: notes and blogs are Library sources only, and
Library search stays papers-only. The design and its known limits are in
[the plan](../plans/xhs-sources.md).

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

`daily_calls` caps provider calls per UTC day. `tikhub` counts list and
detail calls, `ocr` one per OCR task whichever engines it uses, and `gpt`
identification and link search calls. Image downloads and blog fetches are not capped. Calls refused
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

## Status

`cortex xhs status` and the Web Status page read the same state: whether the
plugin is enabled in the configuration and why it would refuse, the two roots,
both schedule rows, each followed blogger's last scan time and outcome, task
counts, today's usage against the caps, and the last failure category for
`tikhub`, `cdn`, `ocr`, `gpt` and `blog`. Neither shows a task payload, a path
or a credential.

## Stored files

A saved note is `<xhs-notes>/<note_id>/v<N>/` with `note.md` (caption,
blogger, date, permalink and recommendations), `transcription.md` (one section
per image, verbatim OCR or its failure) and `assets/<ordinal>-<sha12>.<ext>`.
A blog is `<blogs>/<first 16 hex of the URL hash>/v<N>/` with `article.md`,
`notes.md` (the recommending notes) and the screenshots copied from them. A
version is written once; a retry, a link edit or new recommendations write the
next one.

`raw/` (list and detail answers with signed URLs removed, and the fetched page
or Jina text), `ocr/` (raw OCR answers) and each `staging/` directory are
private: no route serves them, and model tools cannot read either root. Signed
image URLs exist only in private task rows.

## Disable

`cortex xhs disable` stops both jobs; queued tasks stay queued and resume after
`cortex xhs enable`. Setting `[xhs] enabled = false` and restarting cortexd
does the same regardless of the schedule rows. Neither deletes anything: saved
notes, blogs, links and imported papers stay, and there is no deletion
command.

## Provider data disclosure

With the plugin enabled, these leave the machine:

| Recipient | What it receives |
| --- | --- |
| TikHub (`tikhub_base`) | The TikHub key, each followed blogger's user ID and list cursor, and each note ID for its detail |
| XHS image CDN | One request per carousel image at its signed URL, without cookies |
| Novita (DeepSeek-OCR-2) | An image the Responses model did not read (every image when `gpt_base` or `sub2api-gpt` is missing), with the `novita` key |
| GLM (`layout_parsing`) | An image that no earlier OCR engine read, with the GLM credentials |
| The Responses endpoint (`gpt_base`) | Every downloaded image, each note's caption and every image transcription, and the title of each blog recommendation without a link, which the model searches the web for |
| Blog sites | A request from this machine to check a found link (at most 2 MiB) and to import a blog (at most 5 MiB of HTML), without cookies, to public addresses only |
| Jina Reader (`r.jina.ai`) | A blog URL whose page could not be fetched or yielded under 200 characters, with the `jina` key when one is configured |

Images and captions are other people's posts; transcriptions may contain
whatever the images show. The Control token, other sources and your
conversations are not sent.

## Live acceptance

All tests in this repository are provider-free. The TikHub detail response
shape follows a recorded probe. Before relying on the plugin, store the TikHub
and GPT keys as references, run a small test set of notes and check the
identification match rate, then approve a backfill budget.
