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
