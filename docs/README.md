# Project context map

Start with `../README.md` for supported behavior and `../CLAUDE.md` for contributor
instructions. The following files are intentionally tracked and public.

| Document | Authority |
| --- | --- |
| [Architecture](architecture.md) | Current process, ownership and state boundaries |
| [Product boundaries](specs/product-boundaries.md) | Invariants to preserve and executable contract pointers |
| [Roadmap](roadmap.md) | Next milestones and explicit deferred scope |
| [Web user guide](runbooks/cortex-web-user-guide.md) | Operator UI and research workflow |
| [Readings publication](runbooks/readings-publication.md) | Opt-in external publication, permissions, recovery and status |
| [Web verification](runbooks/web-verification.md) | Test tiers and their limits |
| [Hermes acceptance](runbooks/hermes-acceptance.md) | Worker qualification and continuity |
| [Runtime credentials](runbooks/runtime-credentials.md) | Keychain and age references, scoped checkout commands |
| [Managed tool access](runbooks/managed-tool-access.md) | Scoped terminal/file permissions and worker requirements |
| [Operator skills](runbooks/operator-skills.md) | OCR for PDF-only papers through an accepted, digest-pinned skill |
| [XHS notes and blogs](runbooks/xhs.md) | Opt-in XHS plugin: setup, follow, cadence, caps, backfill, retries, the weekly recommendation fallback, disable and provider data disclosure |
| [0.1.37](releases/0.1.37.md) | Readings publication survives a macOS update that renumbers the library volume's `st_dev` |
| [0.1.36](releases/0.1.36.md) | Weekly XHS recommendation fallback, recommendation exclusion and restore (Control schema 22) |
| [0.1.35](releases/0.1.35.md) | Source text keeps chain-of-thought and hidden-reasoning wording; credentials and private paths stay redacted |
| [0.1.34](releases/0.1.34.md) | The Web page no longer scrolls into blank space; resizable sidebar and list columns |
| [0.1.33](releases/0.1.33.md) | Short blog extractions are compared with Jina; `cortex xhs refetch-blog` |
| [0.1.32](releases/0.1.32.md) | Imported blogs keep copies of their article images |
| [0.1.31](releases/0.1.31.md) | XHS image OCR runs the Responses model first, then Novita and GLM |
| [0.1.30](releases/0.1.30.md) | Library Markdown reader, XHS notes and blogs, recommendation import (Control schema 21) |
| [0.1.29](releases/0.1.29.md) | Retrieval query and converted-title fixes after 0.1.28 |
| [0.1.28](releases/0.1.28.md) | Idea fragments, one-token Capture parsing and paper-level research evidence |
| [0.1.27](releases/0.1.27.md) | OCR for PDF-only papers through an accepted operator skill |
| [0.1.26](releases/0.1.26.md) | Durable age credential resolution |
| [0.1.25](releases/0.1.25.md) | Repeated paper capture repair and installed acceptance |
| [0.1.23](releases/0.1.23.md) | Readings publication release and validation boundaries |
| [0.1.22](releases/0.1.22.md) | Previous direct-page default and installed acceptance |
| [0.1.21](releases/0.1.21.md) | Previous ingestion repair and installed acceptance |
| [0.1.20](releases/0.1.20.md) | Previous composition and acceptance record |
| [PR review tooling](../tools/pr_review/README.md) | Standalone workflow consumer, English feedback, OAuth setup and review boundaries |
| [PR review quality](plans/pr-review-quality.md) | T3 Code case study and proposed context, feedback and publication improvements |
| [Library reader](plans/library-reader.md) | Rendered Markdown and source-bound figures for adopted sources |
| [XHS notes and blogs](plans/xhs-sources.md) | XHS note and blog sources, the acquisition plugin, recommendation import and known limits |
| [XHS recommendation fallback](plans/xhs-recommendation-fallback.md) | Free rules and a weekly model review for unimportable recommendations, exclusion and the Telegram digest |
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

- [Paper index maintenance](runbooks/paper-index-maintenance.md): audit and refresh corpus/index/adoption state while preserving notes and provenance, and audit Markdown asset references read-only.
- [Retrieval evaluation](runbooks/retrieval-evaluation.md): measure retrieval and fresh research packets on a frozen, checkpointed snapshot.
