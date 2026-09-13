# Cortex P1 UX Notes

## Direction

The prototype uses a research-cockpit information architecture rather than a
generic dashboard. Long-lived context stays in a dark workspace/thread rail,
the evolving research record occupies the document-like center, and durable
human gates stay in a dedicated Decision Inbox.

Amber is reserved for blocked human decisions. Green is reserved for committed
or verified state. This avoids treating every event as equally urgent.

## Gate-review remediation

The first integration review failed because the prototype blurred fixture state
and UI-local state. It rendered G1 artifacts in G0, treated every G0 action as a
generic running state, updated only a confirmation toast, drew lineage as a
three-node chain, and shipped screenshots with JPEG bytes under `.png` names.
The remediation makes those distinctions executable and testable:

- G0 renders only durable control state, one pending source decision, and a
  research side-effect ledger with zero transactions and materializations.
  Living Brief, Evidence, Training Plan, and Snapshot remain unavailable.
- Keep-both and replace resolve revision 2 and move the run to `resuming`;
  cancel moves it to `canceled`. Timeline, pending count, both ledgers, source
  plan, Assistant message, and Raw Log update from the same UI-local state.
- A curated UI snapshot is projected from the canonical productization JSON at
  commit `8a9c32c`. A drift gate pins full fixture hashes, canonical IDs,
  decision revisions, event sequences/types, lineage, threads, and ADR0005
  control/research scopes.
- G1 draws two direct `successor_reuses` links from the active successor to the
  dormant and graduated nodes. It does not imply dormant-to-graduated history.
- G0 thread buttons are explicitly disabled because no artifact versions exist.
  In G1, selecting Evidence, Architecture, or Training changes the visible
  artifact panel instead of changing only the breadcrumb.
- The LingBot paper shows `arxiv:2607.07675` as its canonical ID and
  `LingBot-Video` separately as a model alias.
- Dev and start commands bind to `127.0.0.1`; a safety gate rejects wildcard or
  localhost bindings. Screenshot bytes are converted and verified as PNG.

Assistant UI runtime types still terminate at the mock adapter. The canonical
snapshot is a UI-local test dependency, not a new backend or public contract.

## Second gate-review remediation

The second review found three remaining trust failures:

1. The in-app browser reported a 1600 CSS-pixel viewport at DPR 2, while its
   default screenshot call returned a bitmap clipped to the first half of the
   CSS page. The earlier PNG dimensions were therefore valid but the product
   composition was not. The deterministic capture URL is now
   `/?capture=full`: at a 1600×1000 CSS viewport and DPR 2 it applies a 0.5
   capture-only transform. The resulting 1600-pixel PNG contains a 252-pixel
   workspace rail, 998-pixel main workspace, and 350-pixel Decision Inbox.
   Each scenario uses the explicit clip recorded in `capture-spec.json` rather
   than the browser's order-dependent `fullPage` calculation. The verifier decodes PNG
   pixels to confirm all three regions are present rather than trusting the
   image header alone.
2. Fixture verification previously compared full canonical file hashes but
   projected only selected fields. The projector now rebuilds the complete
   UI-local snapshot from canonical JSON and deep-compares the entire result,
   including G0 persisted control, all G1 artifact versions, the immutable
   snapshot artifact, events, lineage, threads, and ADR0005 scopes. Negative
   tests mutate each representative area and require the verifier to fail.
3. The first mock transition labeled its synthetic events as durable and
   counted them together with canonical events. UI-local resolution events now
   use `ui_local` durability. Keep, replace, and cancel retain six canonical
   durable events and add two explicitly non-persisted UI-local transitions.
   The timeline, header, ledger, Assistant copy, Decision Inbox, and Raw Log all
   label the post-click state as a preview.

The capture-only transform is intentionally isolated behind a query parameter;
normal product rendering and responsive breakpoints remain unchanged.

## Rejected directions

- Chat-first layout: rejected because artifacts, decisions, and run state would
  become prose buried in a transcript.
- Kanban-first layout: rejected because stage columns obscure the relationship
  between one run, its decisions, and its evolving artifacts.
- Raw-event console as the primary surface: rejected because it optimizes for
  operators rather than research judgment; Raw Log remains available but
  collapsed by default.
- Silently merging the two G0 sources into one card: rejected because it would
  reproduce the identity error the golden case is designed to prevent.

## Usability hypotheses

1. A fixed Decision Inbox should let a user identify the blocking action in
   under five seconds without reading the timeline.
2. Showing canonical IDs directly beneath both source titles should reduce
   accidental source replacement compared with title-only cards.
3. A lineage diagram with preserved status labels should make “create
   successor” distinguishable from “continue in place.”
4. A one-line “What changed” block should let returning users assess a Living
   Brief update without rereading the full artifact.
5. Showing “control state durably saved” beside “research store: 0 changes”
   should prevent users from interpreting the G0 gate as either data loss or a
   partially completed import.
6. Disabling G0 artifact threads should be less misleading than allowing
   navigation to empty or non-existent artifact panels.
7. A visible `resuming` state should prevent users from reading decision
   resolution as research completion; cancel must remain visually terminal.
8. Repeating “UI preview” at the status, decision result, ledger, and event row
   should keep users from interpreting a mock click as backend persistence.

## Metrics to validate

- Time to first correct action for G0.
- Source-choice error rate and confidence after resolution.
- Correct interpretation rate for pause versus cancel and successor versus
  in-place continuation.
- Time to locate the latest evidence change and its linked experiment.
- Raw Log open rate during normal use versus debugging.
- Decision completion rate after refresh/reconnect in the P2 persisted build.
- Correct understanding of what is durable versus what has not yet mutated at
  the G0 source gate.
- Rate of attempts to open disabled G0 threads and time to understand why they
  are unavailable.
- Correct interpretation of `resuming` versus `completed` after keep/replace.
- Panel-switch success rate and breadcrumb/panel consistency across all three
  G1 threads.
- Correct persistence interpretation after each G0 action, measured separately
  for the status chip, ledger, and Raw Log.

## 2026-09 shell

The sections above describe the July research cockpit. That surface was retired
in 2026-09; what follows describes the shell that replaced it. The cockpit's
notes are kept as the record of why its decisions were made, not as a
description of the product.

### Model

One noun per Control row, and nothing invented on top of them:

| Product | Control | Note |
| --- | --- | --- |
| Project | `workspaces` row | One research topic. Renamed from the product. |
| Thread | `threads` row | One conversation in a project. Renamed and archived from the product. |
| Turn | `messages` + `runs` | A run is the state of a thread, not a place you navigate to. |
| Decision | `decisions` row | Rendered in the thread it belongs to and aggregated in Inbox. |
| Library | `sources` | Cross-project. |
| Inbox | pending `decisions` + `captures` | Cross-project. |
| Status | `health` | The only place the system's own vocabulary is allowed. |

The cockpit exposed the run as the primary object: a run card, a run selector of
bare ids, a workflow panel and an event log stacked above the conversation. The
question an operator actually arrives with is about the conversation, so the
conversation is the page and the run is a line of state above the composer.

### Layout

Two regions on desktop: a 260px sidebar (project switcher, thread list, then
Library / Inbox / Status), and one main region that is exactly one of Thread,
Library, Inbox or Status. There is no permanent third column; decisions render
in the thread directly above the status strip, where the turn they belong to is.
Below 1024px the sidebar becomes a drawer and the five-segment bottom navigation
is gone.

Run history and Outputs are collapsed panels under the thread header. They are
the cockpit's run card and workflow panel, kept because the information is real,
demoted because it is not what a turn is about.

### Copy

Control's vocabulary — revision, compare-and-swap, DTO, replay, cursor,
idempotency, durable, and raw `run_…` / `thread_…` / `attempt_…` ids — may not
appear in product chrome. It is allowed inside a Details disclosure and in
Status, which is where a person goes to ask a system question. An error is one
plain sentence naming what did not happen and what to do, with the protocol
category under Details.

### Colour

The cockpit's amber/green register (amber for a blocked human decision, green
for committed state) does not survive, because the shell is on the shadcn
neutral palette and that palette carries no hues. Emphasis is now carried by
weight and by the theme's own levels, and the one semantic colour left is
destructive. Dark follows `prefers-color-scheme`; there is no toggle, because a
toggle is a preference the product would then have to store somewhere.

### Evidence

Rendered evidence for this section lives in `apps/web/artifacts/shell/`, named
`<view>-<scheme>-<viewport>.png`: eight scenes -- empty project, thread with a
pending decision, failed run, sidebar with the archived group open, Library with
a document open, Inbox, Status, and the mobile drawer -- in both colour schemes,
at 1440x900 and at 390x844 for the two phone scenes. `npm run capture:shell`
records them against the read-only mock Control world in
`scripts/mock-control-server.mjs`; `npm run verify:shell-screenshots` re-derives
every digest and geometry from the committed files and is part of `npm test`.

That gate is over the committed bytes, not over a re-render: the shell is a live
view of a daemon it polls, so two captures of the same scene minutes apart
differ in a few files even with the update poll frozen. Treat the images as
dated evidence of a design, not as a pixel baseline.

### Rejected directions

1. **Patching the cockpit's CSS toward the Assistant UI look.** 2,810 hand-written
   lines with no utility layer cannot converge on a component library's theme by
   editing; the rebuild replaced the layer instead.
2. **Keeping the Decision Inbox as a permanent right column.** It made every
   thread look like it was waiting on something. Decisions belong beside their
   turn, and the cross-thread view belongs in Inbox.
3. **Keeping the run as a navigable object.** A run selector of ids taught the
   operator to think in run identity; runs are now thread state plus a history
   panel.
4. **`useLocalRuntime` with a runtime-local message copy.** Every durable reload
   had to reset it. `useExternalStoreRuntime` makes Control the only source.
5. **A Chinese UI copy layer.** Chrome stays English to match the component
   library; content is whatever the operator writes.
6. **Fixing a deployed installation's workspace title and probe threads with SQL.** The gap was
   that Control had no rename or archive command. Adding the commands fixed the
   product; the data is then the operator's to clean.
