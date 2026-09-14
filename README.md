# Cortex Research

A single-operator research assistant you install and run on your own machine.
It keeps conversations, a research catalog, an adopted document library and
saved outputs in one local SQLite database, serves a Web/PWA client on loopback,
and dispatches model turns through one managed worker process. There is no
hosted service, no multi-user account system and no telemetry.

Everything the product does is driven from two surfaces — the Web client and an
optional Telegram bot — over one Control API owned by the `cortexd` daemon. The
browser never receives the Control token: the Web server holds it and forwards
only allowlisted routes. The Web listener is loopback-only; there is no public
entry point unless you configure the optional private-access path.
[`docs/runbooks/cortex-web-user-guide.md`](docs/runbooks/cortex-web-user-guide.md)
walks through the surfaces once it is running.

Product 0.1.20 uses installation sequence 20 and Control schema 19. The qualified
Hermes gen9 runtime is supplied separately. See
[`CHANGELOG.md`](CHANGELOG.md) for changes and
[`docs/releases/0.1.20.md`](docs/releases/0.1.20.md) for validation boundaries.

## Supported operations

- **Workspaces and threads.** Create, select, rename and archive workspaces and
  the conversations inside them. Messages, runs and decisions are durable and
  transactional; replay, cancellation, retry and idempotent commands are part of
  the API contract.
- **Research catalog.** Ideas, Explorations and Projects are read-only records
  with their history, plus item-linked conversations. Selecting an item does not
  start an autonomous process.
- **Adopted documents and sources.** Dossiers and papers are adopted as
  immutable versioned documents, readable through Preview/Source with their
  provenance and evidence labels.
- **Library and Inbox.** Search adopted sources, read their notes, full text and
  grounding, and triage captures and pending decisions.
- **Research turns.** `/research <question>` builds one bounded, hashed evidence
  packet from the adopted library and the selected item's documents, sends it to
  the configured model through the managed worker, and saves the answer as an
  artifact. `/chat` leaves research mode. Follow-ups reuse the same packet.
- **Saved Outputs.** Artifacts are immutable, render Markdown and KaTeX math,
  and expose exact source bytes and hashes.
- **arXiv ingestion.** The engine's supported child operations are
  `ingest_arxiv`, `checkpoint`, `reconcile_arxiv` and `self_check`. Ingestion is
  strict: a paper with no arXiv HTML is refused with a typed error rather than
  silently degraded.
- **Managed runtime lifecycle.** One attested worker generation, with protocol
  handshake, a restart-durable operation ledger, cancellation, session
  continuity and rollback. Health reports `unbound` and refuses dispatch when no
  worker is configured, instead of pretending to be ready.
- **Private access.** An optional, identity-aware Tailscale Serve path in front
  of the loopback Web server. It is validated and staged offline; it does not
  start by default and never publishes a launchd descriptor by itself.
- **Official local distribution.** Build, verify, install, start/stop, doctor,
  upgrade, rollback and a data-preserving uninstall of a self-contained bundle
  that includes its own CPython and Web payload, and stages an explicitly
  supplied Node runtime during installation.

### Not included

Investment or any second agent profile; multi-profile dispatch (`HERMES_PROFILE`
is denied); autonomous idea/exploration rounds, bulk revival, experiment
execution and schedulers beyond the existing capture drain; generic HTML or PDF
URL ingestion; a research MCP server, Kanban board, webhook or gateway;
multi-user hosting, public exposure and account management.

## Layout

| Path | Contents |
|---|---|
| `cortex_platform/product/` | Control store and API, orchestration, transports, engine, sources, artifacts, runtime updater |
| `cortex_platform/runtime/` | The managed Hermes worker backend |
| `profiles/research/src/cortex_research/` | Paper ingestion, arXiv client, indexing, chunking, embedding and catalog schemas |
| `apps/web/` | The Web and PWA client, its Node front door and its tests |
| `distribution/` | Bundle composition, verification and the installed lifecycle (`cortex-dist`) |
| `deployment/private_access/` | The optional private remote-access package |
| `docs/` | ADRs and runbooks |

## Build, test and install

Python work uses [uv](https://docs.astral.sh/uv/); the development interpreter
is 3.12. From the repository root:

```bash
uv sync --frozen --python 3.12
.venv/bin/python -m pytest -q
```

Confirm the checkout owns its imports before trusting a result — an editable
path from another checkout is the usual cause of a confusing pass:

```bash
.venv/bin/python -c 'import pathlib, cortex_platform, cortex_research; r = pathlib.Path.cwd(); assert all(pathlib.Path(m.__file__).resolve().is_relative_to(r) for m in (cortex_platform, cortex_research))'
```

The Web client needs Node 22.13 or newer:

```bash
npm --prefix apps/web ci
npm --prefix apps/web run test:fast   # browser-free tier, the one CI runs
npm --prefix apps/web test            # full chain: build, browser and process gates
```

`npm test` additionally needs a Chromium-family browser, `git`, and
`CORTEX_TEST_PYTHON` pointing at this checkout's interpreter. Tiers,
prerequisites and the gates no local command covers are in
[`docs/runbooks/web-verification.md`](docs/runbooks/web-verification.md).

`.github/workflows/fast-checks.yml` is the browser-free hosted tier: the
provider-free Control and artifact contracts, and `npm run test:fast`.

A release bundle is composed by the checked-in driver, which acquires the Web
payload, verifies the worker artifact and calls `python -m distribution build`:

```bash
python deployment/research_activation/build_release.py --help
python -m distribution verify --bundle <bundle>
```

The bundle's own `cortex-dist` owns the installed lifecycle — `install`,
`start`, `status`, `stop`, `doctor`, `upgrade`, `rollback`, `recover` and
`uninstall`, each taking explicit `--prefix`, `--runtime-root` and `--home`.

The wheel installs six console scripts: `cortex`, `cortexd`, `cortex-hermes`,
`cortex-private-access`, `cortex-private-access-gateway` and
`cortex-private-access-supervisor`. `cortex init`, `cortex doctor`,
`cortex start`, `cortex status` and `cortex stop` are the operator entry points.

## Configuration

All configuration is one versioned TOML file, parsed by
`cortex_platform/product/config.py`. `cortex init` writes a starting file;
`cortex doctor` explains what an installation is still missing. The sections are
`paths`, `asset_roots`, `secret_refs`, `transports`, `runtime` and `web`; an
unknown key is refused rather than ignored.

Filesystem roles come from `cortex_platform/product/paths.py`'s `PathRegistry`,
which resolves a config, data, state, cache and log directory. Each can be
overridden by `CORTEX_CONFIG_DIR`, `CORTEX_DATA_DIR`, `CORTEX_STATE_DIR`,
`CORTEX_CACHE_DIR` and `CORTEX_LOG_DIR`, and `CORTEX_CONFIG_FILE` selects the
file itself. `control.db` always lives under the data directory. No path is
hardcoded to a personal location.

**No secret value is ever written to configuration.** `secret_refs` holds
logical aliases pointing at an external store, in exactly two syntaxes:

```toml
[runtime]
model = "<model id>"
provider = "anthropic"          # or openai, openrouter, custom
base_url = "https://api.example.com"

[secret_refs]
anthropic = "keychain://cortex-provider/anthropic"
research_bot = "env://CORTEX_RESEARCH_BOT_TOKEN"
```

`keychain://service/account` is the only scheme the supervised daemon resolves,
because a launchd start has no shell environment to read. `env://VARIABLE_NAME`
is accepted for foreground use and reported — not silently dropped — by
`cortex doctor`. A provider alias must be one of `anthropic`, `openai` or
`openrouter`, which is what binds the resolved key to the variable the worker
reads; `research_bot` binds the Telegram token. Resolution happens only at the
effect boundary, in `cortex_platform/product/secrets.py`, and a resolved value
is carried by an object that refuses to print itself.

`[web]` carries the loopback port and, when a public door is configured, the one
exact HTTPS origin plus the Cloudflare Access issuer and audience the adapter
must verify. Those are public values; signing keys are fetched from the issuer.
`asset_roots` names the absolute document roots the engine may read. Without an
enabled corpus root the engine is simply not wired, rather than guessing a
corpus directory.

The optional private-access package has its own strict schema, documented with a
worked example in [`deployment/private_access/README.md`](deployment/private_access/README.md)
and [`deployment/private_access/example.config.json`](deployment/private_access/example.config.json).

## Data and privacy boundary

Conversations, runs, decisions, catalog records, document versions, sources and
artifact metadata live in one `control.db` under the data directory. Artifact
bytes live under the state directory; absolute asset paths never enter a public
DTO. The Web listener is loopback-only and the Control token stays server-side.
Nothing is uploaded anywhere except the model turns you send to the provider you
configured.

## Third-party material

`apps/web/THIRD_PARTY_NOTICES.md` inventories the Web dependencies and the
embedded assets, with license texts in `apps/web/licenses/` (Geist under the SIL
Open Font License 1.1, KaTeX under MIT). `distribution/vendor/acorn-LICENSE.txt`
covers the vendored ECMAScript parser the bundle verifier drives.

No license has been selected for this project's own code yet.
