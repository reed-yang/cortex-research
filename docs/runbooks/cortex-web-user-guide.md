# Cortex Web user guide

What the Web surface offers, which object each control acts on, and what a
research turn does and does not change. It describes the application, not any
particular installation: the address, the workspaces, the catalog contents and
the configured model are yours.

Open the address your installation serves. A local development server binds
`http://127.0.0.1:3000`; an installed generation serves the loopback port its
launcher configures, optionally behind the private-access path described in
`deployment/private_access/README.md`. There is no public entry point.

## What this release does

Research holds the records this installation has imported: Ideas, Explorations
and Projects, each with the dossiers that were adopted as versioned documents.
You can inspect their recorded history and discuss a selected item's documents
with the configured model.

This release does not resume an autonomous research engine. A discussion saves
messages and Outputs in a linked Cortex thread; it does not increment a legacy
round counter, change a legacy status, rewrite the original dossier, create an
exploration angle or launch an experiment. Documents are retained versions, not
a live synchronization with their original files.

## Find the right surface

| UI label | Use |
|---|---|
| Project selector / New project… | Choose or create a workspace that contains conversations. This is separate from a legacy research project. |
| New thread | Start a normal conversation in that workspace. For an existing catalog item, use its Open research conversation button instead. |
| Research → Ideas | Inspect existing specific hypotheses and their recorded progress and stop reasons. |
| Research → Explorations | Inspect broader research directions and recorded exploration history. Exploration does not mean a runnable experiment. |
| Research → Projects | Inspect legacy research project records and their dossiers. |
| Library | Browse papers, blogs and XHS notes by kind, read their content, and import what an XHS note recommends. |
| Inbox | Save ideas as written, add arXiv sources, and review captures and pending decisions. It is not an automatic resume queue. |
| Status | Inspect availability and technical service state, including the XHS plugin's last scans. |
| Runs | Inspect execution history for a conversation. |
| Outputs | Read saved research artifacts for the selected run. |

On a narrow screen, use Open navigation to reach the sidebar. The Research
detail sits below the list on small screens and beside it on wide screens.
Search this page… filters only the currently loaded page. Clear it, select Any
status and use Previous/Next when an expected item is not visible.

## First guided operation: continue one existing item

1. Open the Research view (`?view=research` on your installation's address).
2. In the project selector, choose a workspace for the conversation, or create
   one with New project… → enter a name → Create. Creating a workspace does not
   create a new item in the research catalog.
3. Select Ideas, set Any status, and pick the item you want to revisit. Search
   accepts keywords from the item's title.
4. Read Summary, Stopped because, Documents and History. In Documents, Preview
   renders Markdown and math; Source shows the Markdown and LaTeX. Rounds and
   status describe the preserved research state, not the number of new chat
   messages.
5. Click Open research conversation. Cortex creates or reuses an item-associated
   conversation. No model runs merely because you opened it. If the button is
   disabled, check the displayed reason and whether a workspace is selected.
6. Send a question with the explicit prefix, regardless of the current
   Chat/Research selection:

   ```text
   /research From the retained material for this item, state the core
   hypothesis, the work already done, why it stopped, and the single question
   most worth testing now. Separate existing evidence from proposals and cite
   the material.
   ```

7. Watch the run status (normally Working… while executing). After the answer,
   open Outputs. Read Preview; switch to Source to inspect the Markdown and
   LaTeX. Source provenance holds the technical evidence record. `[D1]` labels
   identify dossier excerpts; `[S1]` labels identify retrieved paper evidence.
   One bracket may group labels, as in `[S1, D2]`; ordinary brackets and
   Markdown links are not citations, and an answer that cites no label, an
   unknown label or a malformed label bracket such as `[S1-S3]` is saved as an
   unverified draft. These labels do not establish that the research claims are
   true.
8. In the same conversation, send a follow-up without another command:

   ```text
   Using the same material, propose a minimal ablation: baseline, the single
   variable that changes, data and metrics, expected signal and the result that
   would falsify it. Produce a plan only; do not claim it has been run.
   ```

9. Return through the sidebar thread or the item's Open research conversation
   button in the same workspace. Check that messages and saved Outputs remain.
   The item should still have the same status and Rounds: this operation is
   manual research continuation, not a legacy incubation round.

A conversation opened earlier may already contain questions and results. That is
expected when the button reuses it. Different workspaces can hold different
conversations associated with the same item.

## Continue an exploration or project

Use the same steps, selecting Explorations or Projects before opening the item.
For an exploration, a useful first question is:

```text
/research From this exploration's retained material, summarize the directions
already explored, the routes ruled out and the open questions. Propose two
directions worth comparing further, with the basis for each and the missing
information. Cite the material.
```

Then follow up: `Choose one of those two directions and state the evidence and
decision criteria the next step needs.` For a project, ask for its current
milestones, blockers and next experiment plan.

These produce saved proposals; they do not restart an exploration worker or
execute an experiment. There is no New idea / New exploration engine-creation
flow. A new normal thread can develop a new concept, but it does not register
that concept as a catalog item.

## Research mode and commands

| Action | Behavior |
|---|---|
| `/research <question>` | Enter research mode or refresh the evidence selection for this question. Use a nonempty question. |
| Plain follow-up in research mode | Continue with the retained evidence packet while the selected item is unchanged. |
| `/chat <message>` | Leave research mode for this turn and subsequent ordinary conversation. Historical messages remain. |
| Composer Research / Chat buttons | Set the mode for the next submitted message; when switching, the UI adds the matching command. An explicitly typed command takes priority. |

Research mode retrieves bounded excerpts from the adopted library and from the
explicitly selected item's retained dossier, supplies those excerpts to the
configured model, and saves the response as an artifact. It does not mean
automatic web search, paper ingestion, full-file reading, engine advancement or
scheduling. Without an associated research item the question uses adopted-library
evidence only; mentioning an item's title in a generic chat does not select its
dossier.

Paper search does not use the question text verbatim. It joins lines, drops
citation labels such as `[S1]` or `[D1]`, explicit length requirements such as
"in 200 words" or "控制在500字内" and output-format words such as "summarize" or
"markdown", and puts the selected item's title first. Other numbers, years and
versions stay. If nothing searchable remains and no selected item's title adds
search words, paper search falls back to the question with its spacing normalized,
within the same limits, so those labels, length requirements and format words are
searched. It then selects up to six papers with up to two matching passages
each. The answer still receives the exact question, and plain follow-ups reuse
the retained packet without searching again.

Dossier excerpts match the exact question. Each retained dossier version
contributes its opening section plus up to two nonoverlapping windows of nearby
lines, ranked by how many distinct question terms each window contains, with
earlier windows first on ties. English terms are runs of two or more letters or
digits, matched case-insensitively anywhere in a line, including inside longer
words: "ai" also matches "chain". Chinese terms are pairs of adjacent
characters, never across spaces, punctuation or Latin text. A single Chinese
character is not a term, so a one-character question selects only the opening
section. This is substring matching, not semantic search.

Use `/research` again when the question changes enough to need new evidence. For
a follow-up that should reuse the same evidence, plain text is sufficient.
Selecting Research while already in that mode does not itself refresh evidence.
If no usable evidence is found, try exact paper or title keywords, or verify
Documents on the selected item. Missing retrieval is not proof that no relevant
work exists. Non-English title matching is limited.

## Save an idea

Inbox → Save an idea keeps the text exactly as typed, including leading spaces
and blank lines, with an optional note. Saving appends no message, starts no run
and creates no capture or source; nothing reads the idea afterwards. Add a source
(arXiv) is the separate composer for arXiv links and ids.

Ideas lists saved ideas newest first, marked Saved here or From Telegram, with
their ids under Details. The Inbox shows the newest 500. Ideas are reread on
Inbox entry, after a save and with Refresh ideas, not on a timer; an idea saved
from Telegram appears after Refresh ideas. Each save is a new idea, even with the
same words. When a save could not be confirmed, the text stays in the composer:
saving the same text and note again retries that save and does not add a second
idea. Saved ideas cannot be edited, deleted or turned into research items yet.

## Retry a failed paper capture

Inbox records describe individual import attempts. A failed capture stays failed
after a configuration repair or a separate library import; it is not the current
health of that paper. Compare its Created time and Capture id with the attempt
you are investigating, and check Library for an already imported source.

A paper capture names exactly one arXiv paper as one token: a modern ID such as
`2601.00042` or `2601.00042v2`, `arXiv:2601.00042`, or an `arxiv.org` or
`www.arxiv.org` abs, pdf or html link, with or without `https://` or `http://`.
A token ends at whitespace, at an ASCII bracket or quote (`()[]<>"'`) and at
any non-ASCII character, so Chinese text and full-width punctuation may touch
it: `看看2601.00042的方法`, `论文：https://arxiv.org/abs/2601.00042` and
`[x](https://arxiv.org/abs/2601.00042)。` each name 2601.00042. `.`, `,`, `;`,
`:`, `!` and `?` at either end of a token are not part of it. A query or
fragment on the link is ignored. Text before and after that token is shown on
the card as "Note from submitted text", separately from the note you typed;
neither the submission nor your note is rewritten, and the derived note has no
length limit of its own. Only the canonical paper ID is sent to ingestion. A
capture is not imported, and fails as `invalid_source`, when it names two
different papers (an ID-shaped number for another paper anywhere in the
surrounding text counts, with or without a space, for example `对比2602.00001`
or `ref2602.00001`), when the ID or link touches other ASCII text with no
whitespace, bracket or quote between them (for example `paper2601.00042` or
`see:https://arxiv.org/abs/2601.00042`), when a bracket or quote joins it to
earlier text that contains `/`, such as a link on another host (for example
`https://example.com/wiki/(arxiv.org/abs/2601.00042)`), or when the link uses
another host, a port, user information or an encoded path.

When the same paper is later adopted into the Library, for example by a new
capture of it, refreshing the Inbox shows "This paper is now in your library; this
capture remains failed." with an Open source button. Open source only navigates
to that Library source; it sends no command, and the failed capture keeps its
state and history. The link means the paper is currently adopted under an
enabled corpus root, not that this capture succeeded or that the source's files
and index are healthy.

The Inbox lists captures newest first by submission time, with each card's
state as a badge. While it is open, it rereads them every five seconds when a
capture is approved or being read, and every 30 seconds otherwise; a later
failure therefore appears on the card without a manual refresh.

After the cause is fixed, submit the paper link again and approve the new
capture. Terminal failed captures do not block a deliberate new submission.
Successful arXiv imports reuse an existing indexed paper when its identity is
already present, then record the new capture as consumed. An approved capture
waits for the import schedule; the default interval is five minutes.

Reopen applies to an uncertain capture, where the previous attempt might already
have written data. A failed capture instead needs a new submission. The generic
`materialization_failed` category does not identify a single cause: an operator
must inspect the matching engine result. For embedding credential failures, see
[paper index maintenance](paper-index-maintenance.md#credential-failures).
`capability_unavailable` means the paper has no HTML version and OCR is not
ready on this installation; the card says so, `cortex skills status` names the
reason, and the paper can be captured again once OCR is ready
([operator skills](operator-skills.md)).

## Read a Library source

The kind filter (All, Papers, Blogs, XHS notes) is kept in the address as
`kind=`; sources are listed newest first. Search is shown for All and Papers
only and says "Searches stored papers": blogs and notes are not searched.
Changing the filter rereads the list and keeps the open record.

A paper has the tabs Notes, Full text and Grounding. A blog has Article (the
extracted page) and Notes (the notes that recommended it, with their
screenshots), a "Not peer-reviewed" badge and "Recommended in". An XHS note has
Note and Transcription. A paper or blog that an XHS note recommended lists
that note under "Recommended in", with the image the recommendation came
from, or "caption"; Open goes to the note.

Each content tab opens in Preview, which renders
the whole stored document with headings, tables, math and the paper's own
figures. Source is the paged, line-numbered view with the line range and
`sha256` of the stored file; use it when citing. This browser remembers the
choice for the Library only. Copy source copies the whole document as shown,
with any redacted lines still redacted, in either view; it is available once
that document has loaded and is not empty. Reopen document reads it again.

Figures load only from the selected source's own stored `assets/` directory,
including references written with the older `papers/<dir>/assets/` prefix for
that same paper. A referenced figure that is not in the stored copy shows "Figure
not in the stored copy" with its path. Remote images are not loaded and appear as
the text "Image on <host>", not a link; `data:`, `file:` and protocol-relative images show
"Image not loaded from this reference". The stored Markdown is never rewritten.
A document larger than 2 MiB opens as paged Source only, with a one-line notice,
and Copy source is unavailable for it.

## Read an XHS note and import what it recommends

XHS notes appear once the operator has enabled the XHS plugin
([XHS runbook](xhs.md)). A note record shows the blogger, a role badge
(Curator: Library only; Author: research evidence later), the published date
and "Open on Xiaohongshu", then the recommendations, then the failed images,
then the Note and Transcription tabs. While Cortex is still processing a note,
the record says so; a new version is saved when it finishes.

Each recommendation row shows its kind (paper, blog or other), the image it
came from or "caption", a blog's link state (for example "Link found and
checked" or "Link found, not checked") and its import state. Expanding a row
shows that image, its part of the transcription labelled "Transcription ·
Image N" (or the caption, labelled "Caption"), and the fields the model
identified, labelled "Identified (auto)". The transcription is the OCR
provider's text, kept as recognized; only the identified fields come from the
model. A blog that is not yet importing or imported has an editable link that
accepts http and https links without credentials; saving it marks the link
"Link set by you" and saves a new version of the note.

A row has a checkbox only when it can be imported: a paper with an arXiv id, or
a blog with a link, that has not been imported or staged yet or whose import
failed. Import selected
does two steps. It first stages the selection in one command: each paper
becomes a pending Capture in the Inbox, an open Capture of the same paper is
reused, and each blog gets a queued blog import. It then approves each staged
paper that is still pending, one at a time, through the same approve action as
the Inbox. Every row reports its own outcome: approved, in the inbox but not
approved (with the reason), blog import queued, already in the inbox, or not
imported (with the reason). If the whole import fails, the record says nothing
was imported. The note is read again either way. An approved paper waits for
the import schedule like any other capture; a blog appears in the Library when
its import finishes, linked back to the note.

Each failed image has its own row, "Image N: download failed (reason)" or
"Image N: transcription failed (reason)", with Retry. Retry queues that image
again; the row then says the retry is queued, and the note is identified and
saved again as a new version when it finishes. An image downloaded after the
latest saved version shows "This image is not in the saved copy yet." until the
next save. A transcription already open is read again when you reopen the
record.

Status has one XHS notes line. It says whether scanning is on, or why it is
off (not enabled in the configuration, the note and blog folders not ready, or
the schedule disabled), and lists each followed blogger's last scan time with
"last scan succeeded", "last scan found no new notes" or "last scan failed"
and the failure, such as "credentials refused" or "rate limited". A scan that
found nothing new is not a failure, and a provider failure is never shown as
nothing new.

## Read and manage results

Preview is the readable Markdown and math view. Source shows the Markdown and
LaTeX; Copy source copies the shown document, including its appendix where
present. Escape a literal currency dollar as `\$` in math-enabled Markdown.
Original dossier previews may omit private details and say so visibly. A saved
Output is a new artifact, not an overwrite of the original dossier.

Runs selects execution history; Outputs shows the selected run's artifacts.
Thread rename and archive organize conversations, not an item's research state.
If execution fails or dispatch is off, inspect the visible status before
retrying; a saved user message alone is not a completed research run.

## Telegram counterpart

If the installation has a Telegram transport configured, expand Telegram command
in the item's detail and use Copy command. Send the displayed
`/research-item <id>` in the bound bot chat, then `/research <question>`. This
selects the same item for the bot's bound conversation; it does not move the bot
into an arbitrary Web thread. Follow up normally and use `/open` to return to
that conversation on the Web.

`/idea <text>` in a bound chat or topic saves the text as an idea and replies
that nothing was started; it appends no message to the bound thread and asks for
no turn. After `/idea` (or `/idea@<bot>`), exactly one space, tab or line break
separates the command from the idea, and everything after it is kept as sent.
Any other separator, an empty idea, an unbound chat and a photo, file, video or
animation captioned `/idea` are refused, and nothing is saved. Cortex also
refuses a message whose text, command included, is over 16384 bytes or contains
a carriage return. A redelivered update saves nothing twice. `/capture` and
plain messages still add the text to the bound thread.

Telegram limits one text message to 4096 characters, and the official apps send
longer text as several messages. Only the first of them starts with `/idea`, so
only that part is saved as an idea. Each later part arrives in the bound chat as
a plain message, so Cortex appends it to the bound thread and asks for a turn.
Save an idea longer than one Telegram message from Inbox → Save an idea instead.

Successful Web research does not prove Telegram delivery, and the absence of a
typing indicator does not prove that a run was not received. Keep receive, run
and delivery acceptance separate.

## What a later milestone must add

Actual engine continuation needs an explicit one-step action with eligible item
state, a visible budget and a stop condition, followed by persistent engine
results and updated item history. Exploration continuation and idea incubation
need their own semantics. Only after one-step execution is accepted should
recurring automation be offered.
