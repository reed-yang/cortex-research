# Release notes

## 0.1.22

Read arXiv metadata directly from the abs page by default and retain direct HTML
full-text ingestion. A valid page makes no export API request, avoiding API
retry delays. Use the API only after page request or validation failure, and
validate its returned identity before ingestion.

## 0.1.21

Recover transient arXiv metadata failures from validated abs-page metadata and
use HTTPS directly. Report known metadata failures with both endpoint errors.
Exclude preexisting unrelated processes from the effect child survivor scan.
The product retains its strict HTML-only ingestion boundary and existing worker.

## 0.1.20

Cortex Research is now a standalone single-operator research product. Existing
Web and Telegram conversations, research dossiers, library sources, immutable
Outputs and Markdown/KaTeX rendering are retained.

- Remove the investment and legacy multi-profile composition from the product.
  On the measured macOS arm64 installation, product-side Python distributions
  decrease from 160 to 13 and wheel bytes from 310 MB to 10.2 MB. The separately
  managed Hermes runtime keeps its own interpreter and dependencies.
- Preserve installed-generation upgrade and rollback across the narrower package
  composition without requiring the predecessor to match the new workspace.
- Fix long shutdown timeouts being rejected by an installed supervisor's
  60-second shutdown protocol. The caller retains its full operation deadline.
- Retain the simplified transport store, narrower packaging boundary, fast Web
  verification tier and Telegram poller busy-state handling.

Product version: **0.1.20**. Installation sequence: **20**. Control schema:
**19**, unchanged. Managed runtime: **hermes-0.15.0-gen9**, unchanged.

This release continues research over adopted evidence. It does not restart legacy
autonomous idea/exploration rounds or upgrade Hermes. An initial installation
needs a separately provisioned and approved compatible runtime; installing the
controller alone does not enable model dispatch.

Published validation and platform/package availability are recorded in the GitHub
release. Local tests, installed continuity and real transport acceptance are
separate checks; no release status is inferred solely from a version number.
