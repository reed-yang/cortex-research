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

## Later

Idea incubation, new item creation, recurring automation, broader ingestion and
large-scale documentation/history retirement are separate milestones. Keep the
previous private repository as retained history; remove neither worktrees nor
archives as a side effect of a product release.
