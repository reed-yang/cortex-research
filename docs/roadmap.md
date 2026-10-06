# Roadmap after research-only extraction

## Release 0.1.20

The standalone product is built and installed, retains the existing qualified
worker/session state, and has passed live Web/Telegram research checks. Public
release status and CI are recorded in [the release note](releases/0.1.20.md).
No autonomous engine capability was added by extraction or simplification.

## Next: one explicit exploration continuation

Design a single user-requested continuation of one eligible exploration, over
already adopted evidence. Separate managed-model reasoning from a deterministic
persistence stage. The result should add a durable round, grounded angles,
history and an Output with provenance. It must not silently start discovery,
new ingestion, successor creation, a scheduler or an idea kill/graduation gate.

The earlier private design referenced legacy modules that were intentionally
removed during extraction. Rebase that design onto the current engine boundary;
do not restore the legacy dependency tree or directly import its old orchestrator.

Acceptance must include duplicate requests, a crash between persistence and
acknowledgment, preserved human pauses/terminal states, artifact-write failure,
restart reconciliation, and one isolated real-state copy followed by an explicit
installed round. A model answer alone does not close this milestone.

## XHS notes and blogs

A bounded acquisition milestone, implemented on `feat/xhs-sources` and not yet
in a release: follow chosen XHS bloggers, save each image note with verbatim
image OCR, identify the papers and blogs it recommends, and import them on the
operator's choice (papers through the existing Capture flow). It is disabled
by default, capped per provider per day, and adds no research evidence. See
the [plan](plans/xhs-sources.md) and the [runbook](runbooks/xhs.md).

Before it counts as accepted: a command that registers its two asset roots,
operator-approved live provider acceptance (keys stored as references, a small
test set with a measured identification match rate, then an approved backfill
budget), and a release composition that includes Control schema 21.

Deferred, each its own decision: notes and blogs in search and `/research`
evidence (the stored blogger role takes effect only with a new research
context packet schema), an unread marker for new notes, reclassifying the
legacy web items adopted as papers, and downloading images inside blog pages.

## Independent work

- Hermes update candidate: reproducible qualification and upgrade/rollback/native
  continuity before replacing the accepted gen9 slot.
- Model roles: retain the configured primary model; any escalation is explicit
  policy. Two roles using the same model are not independent corroboration.
- Telegram long-poll reliability: trace the observed45-second frame timeouts.
  The current poller recovers automatically, but the timeout source and delay
  require a dedicated diagnosis. Keep native session/worker ownership intact.
- Product ergonomics: clearer bounded research results, transport rendering and
  useful failure recovery, with live acceptance for affected paths.
- Public distribution: choose an owned-code license, resolve any upstream notice
  gaps, and add signed/notarized installers only when their own gates exist.
- Idea intake: save-only idea fragments from the Web Inbox and Telegram `/idea`,
  stored verbatim in a new Control table and starting nothing. Admitting a
  fragment as a new research item is the first slice of new item creation below
  and needs its own decision.

## Later

Idea incubation, new item creation, recurring automation, broader ingestion and
large-scale documentation/history retirement are separate milestones. Keep the
previous private repository as retained history; remove neither worktrees nor
archives as a side effect of a product release.
