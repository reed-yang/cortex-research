# Research product architecture

Cortex Research is a single-operator product. Web and Telegram share the same
Control state and orchestration path; a second investment/profile runtime is
not part of this repository.

## Processes and ownership

| Owner | Responsibility |
| --- | --- |
| `cortex_platform/product/api/` | Authenticated Control routes and public projections |
| `product/control/` | Transactions, revisions, command receipts and durable events |
| `product/orchestration/` | Runs/attempts and application of typed runtime results |
| `product/transports/managed_worker.py` | Transport authorization, credentials, windows and poller |
| `runtime/managed_hermes.py` | Lazy backend lifecycle, restart budget and session binding |
| `product/runtime_update/supervisor.py` | Worker process and frame routing |
| `product/runtime_update/service.py` | Stage, activate, rollback and pin release state |
| `apps/web/` | Loopback Node front door, scoped proxy, Web/PWA and Markdown presentation |
| `distribution/` | Sealed bundle and installed-generation lifecycle |

Paths beginning `product/` or `runtime/` above are relative to `cortex_platform/`.
The four worker owners are separate layers; merging them would combine process
I/O, release lifecycle and transport authorization responsibilities.

Managed tools have an explicit policy. The default `runtime.tools = "none"`
exposes session history only, alongside application-provided evidence. Opt-in
`local` mode admits terminal/file tools, system executables and the slot's Python
inside the same OS sandbox. Commands start in a private worker workspace; enabled
research asset roots and the configured readings destination are read-only.
Changes to registered read roots replace the worker on its next acquisition.
The worker advertises tool-policy v1 in local mode, removes provider/transport/
protocol credentials from child environments, and converts permanent execution
permission errors to exit 126 without retrying. An older worker is refused before
a local-tool turn. This requires a newly qualified worker payload, not an edit
to an installed slot. Hermes approval callbacks remain active.

## Data and effects

Control SQLite owns product workspaces, threads, runs, attempts, research items,
evidence contexts and transport receipts. The native Hermes session store is
separate. Retained research identities/dossiers and the paper corpus are
accessed through explicitly configured roots and the research bridge.

`/research` constructs a bounded evidence packet, invokes the configured managed
worker and commits a cited Output. Follow-ups remain in the selected research
mode; `/chat` leaves it. A catalog entry is not an active engine run. Opening an
Idea, Exploration or Project does not increment rounds or wake old schedules.

An attempt reserves its runtime identity and pin durably before an effect
starts. Runtime I/O occurs outside SQLite write transactions. Applying results
checks ownership and identity again; retries cannot replace those checks.

## Direct paper ingestion

The research bridge reads validated metadata from `arxiv.org/abs/<id>` and full
text from `arxiv.org/html/<id>`. A valid abs page requires no export API call.
If the abs request fails or its citation metadata is invalid, the product-bound
export API is the metadata fallback. Both paths validate the requested paper
identity; failure before corpus writes remains a known refusal. The existing
HTTP client implements these direct requests without spawning curl.

Direct PDF download alone does not provide structured text, formulas, figures or
an indexed source, so a paper without usable HTML needs OCR, and the bundle
ships none. `product/skills.py` lets the operator serve the `ocr` capability
slot with an installed Agent Skills package: `[skills] root` names where skills
live, `SKILL.md` metadata declares the slot, entry and interpreter, and
`cortex skills accept` records a digest of the package. The effect supervisor
recomputes that digest before every effect and binds the entry and interpreter
into the child only when it still matches. Otherwise strict ingestion refuses
the paper as `capability_unavailable`, distinct from `materialization_failed`.
See [operator skills](runbooks/operator-skills.md).

## External readings publication

`product/readings/` reconciles newly adopted sources into an explicitly configured
external library. The internal corpus remains the evidence root. A private
SQLite publication journal owns baseline/identity, staged manifests, file
ownership, retries and recovery; it does not change Control schema or Capture
success. Existing notes and manually modified files are preserved. Generated
files can update only against their last recorded hash, with both versions
retained before in-place writing.

A dedicated publisher child performs all external writes under a macOS
no-delete sandbox. Capture children get read-only access to the target and
publication state; Hermes keeps its separate existing sandbox. The trusted
controller/launchers are outside this new OS boundary. Library and the operator
CLI expose publication status independently of internal import. See the
[publication runbook](runbooks/readings-publication.md) for enablement, exact
process guarantees, interruption recovery and external indexing/backup limits.

## Composition and upgrade

The product, Control schema, installation sequence and Hermes worker generation
are independent identities. `distribution/release.toml` selects the composition;
build metadata derives source and artifact identities. The product's small Python
closure does not include the separately provisioned Hermes slot.

Updates use the official distribution lifecycle with a state backup appropriate
to the schema change. Keeping an old bundle does not by itself make a database
downgrade possible. Installed-generation rollback, restored state readability,
native-session continuation and delivered transport replies are distinct checks.

## Exposure

The browser never receives the Control token. Web routes forward only an
allowlist; the listeners remain loopback-bound. Optional remote access must
retain its configured identity/Origin validation. Research data, secrets,
session databases and operator context do not belong in the source repository.

## Runtime credential stores

The shared product resolver supports explicit Keychain and age references. Age
uses a per-user encrypted store and adjacent identity, decrypts only in memory,
and returns a redacted `SecretValue` for the requested assignment. Supervised
engine launches accept both durable stores; ambient environment references stay
foreground-only. Component binding tables determine which credentials reach
each effect. Managed model tools retain sanitized environments and cannot read
the credential store. See [runtime credentials](runbooks/runtime-credentials.md).
