# Project context map

Start with `../README.md` for supported behavior and `../CLAUDE.md` for contributor
instructions. The following files are intentionally tracked and public.

| Document | Authority |
| --- | --- |
| [Architecture](architecture.md) | Current process, ownership and state boundaries |
| [Product boundaries](specs/product-boundaries.md) | Invariants to preserve and executable contract pointers |
| [Roadmap](roadmap.md) | Next milestones and explicit deferred scope |
| [Web user guide](runbooks/cortex-web-user-guide.md) | Operator UI and research workflow |
| [Web verification](runbooks/web-verification.md) | Test tiers and their limits |
| [Hermes acceptance](runbooks/hermes-acceptance.md) | Worker qualification and continuity |
| [0.1.21](releases/0.1.21.md) | Current ingestion repair and installed acceptance |
| [0.1.20](releases/0.1.20.md) | Previous composition and acceptance record |
| [PR review tooling](../tools/pr_review/README.md) | Hosted packet review, setup and unavailable-lane boundaries |
| [ADR index](adr/README.md) | Retained architectural decisions |

Release composition is defined in `../distribution/release.toml`; installed
state and hosted CI must be observed, not inferred from a document or branch name.

## Local operator context

An operator checkout may also contain `.cortex-context/` at the repository root.
It holds internal plans, original handoffs and deployment records. Its README
provides provenance, dates and an index. This directory is ignored and is not
required for building, testing or understanding the public product. A fresh
clone cannot restore ignored files; back them up through the operator's own
private storage process.

Do not import a previous private repository's complete documentation tree or
Git history. Review a proposed public document for private data and obsolete
behavior, then promote its relevant design into a current tracked document.
Adding a tracked file to `.gitignore` does not untrack it or remove its history.
Never force-add the operator directory, logs, research data or deployment archives.
