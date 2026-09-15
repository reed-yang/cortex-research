# Publish captured papers to an existing readings library

This opt-in macOS feature copies adopted paper artifacts from the private
`research-corpus` to an operator-owned directory. Internal source identities,
adopted evidence and Capture outcomes remain unchanged. Publication has its own
durable status and retries; a successful Capture does not by itself mean that
an external copy exists.

## Enable and inspect

Back up the original library and product state first. Configure an existing,
canonical absolute path (no symlink components) in the product configuration:

```toml
[readings]
papers_root = "/absolute/path/to/readings/papers"
```

The destination must be outside all product directories and the private corpus.
It must share a filesystem with the product state directory so completed trees
can be published without partial-copy fallback. Missing roots, path overlaps,
changed root identity and unsupported platforms fail closed. Restart through
the installation's normal lifecycle; this PR does not deploy or enable it.

On first initialization, already adopted sources are recorded as a baseline and
are not bulk-exported. Sources adopted afterward are discovered automatically,
including after a restart or a missed scheduling tick. Publication runs every
30 seconds independently of capture dispatch, with at most eight attempts per
tick. Retrying publication never runs metadata retrieval, parsing or a model.

Inspect status in Library, or use:

```sh
cortex readings status
cortex readings retry --source-id <source-id>
```

`retry` explicitly schedules an existing source as well as retrying a failed
publication. It requires a configured target and adopted source; the daemon
performs the work on its next publication tick. It does not reopen the Capture.
`status` is read-only and does not initialize a journal or establish a baseline.
Common path overrides such as `--config-file` go after `status` or `retry`.
The authenticated `GET /api/v1/readings` endpoint exposes the same publication
records without staging paths or file contents. No HTTP write/retarget/delete
endpoint is added.

States are `pending`, `publishing`, `published`, `failed` and `conflict`.
Operational failures back off up to one hour; a conflict waits for explicit
reconciliation/retry. The original directory is never deleted to make a retry
succeed. If another publisher currently owns the writer lock, a CLI retry
reports busy instead of bypassing serialization.

## Existing files and ownership

- New papers publish as complete directories using exclusive rename. A path
  collision never replaces an existing directory, including an empty one.
- For an existing directory without publisher ownership, arXiv identity must
  match an explicit arXiv reference in its `notes.md`. A title/directory match
  alone cannot adopt it. Unverified or ambiguous destinations require operator
  reconciliation; do not rename arbitrary library folders automatically.
- Missing files and asset subtrees can be added automatically. Source files
  absent from a later version are retained in the destination.
- Existing `notes.md` files are always protected, including an initial template
  Cortex created. Cortex never appends to or overwrites those notes through
  this interface. Additional notes can be separate files in the private paper
  and will be published normally.
- Existing generated files update only when a durable publication receipt owns
  them and the current destination hash matches the last published hash.
  Identical pre-existing files are left alone without claiming ownership.
- A manually changed/unowned file is preserved and reported as a conflict.
  Other missing files may still publish successfully in that job. Restore or
  reconcile the file deliberately, then retry; retry is not force-overwrite.

The publisher copies whatever artifacts actually exist in the private paper,
including PDFs or translations when available. Current HTML ingestion does not
produce `reference.pdf`; this feature adds no PDF download, OCR or translation
call and does not claim that those artifacts exist.

## Enforcement and limits

All external writes in this flow run in a fixed, network-free publisher child.
Its macOS sandbox permits only the current staging area, exact new destinations
and data writes to proven generated files. It denies unlink throughout the
library, ancestor moves, hardlinks and process spawning. Directory traversal
uses no-follow directory/file descriptors and refuses symlinks, multi-link
files, special files and over-limit inputs. Each paper is limited to 512 MiB,
128 MiB per file, 4,096 entries and 12 directory levels.

Configured Capture effects run under a separate read-only sandbox for the
external library, publication journal and product configuration. Their children
inherit those denials. Hermes retains its existing deny-by-default sandbox,
whose writable worker state is disjoint from the configured library.

The trusted controller and distribution/maintenance processes remain outside
this new sandbox; the Web front door is also not newly sandboxed. The controller
prepares private snapshots and launches the fixed publisher rather than writing
external files. This is a guarantee about the import/publication execution
paths, not a claim that every Cortex process or the entire user account has no
deletion authority. Extending OS enforcement to the controller requires a
separate process-launch design: macOS refuses nested sandbox initialization,
and wrapping the daemon would break existing Hermes sandbox launches. External
editors, cloud sync clients and a malicious process under the same UID remain
outside this boundary. Do not change filesystem ACLs for the operator account.

Generated canonical files update in place because replacing an existing file
requires unlink authority. Before updating, complete old/new copies and an
intent are persisted under `<state_dir>/readings/staging/`. This is not a
crash-atomic replacement: readers may briefly see partial content. A restart
can reconcile exact old/new hashes, but unknown partial bytes or external edits
stop automatic recovery and preserve both complete copies for the operator.
Locks serialize Cortex writers; editors/cloud clients that ignore these locks
can still race with an update. Detected races report conflict, not success.

## Journal, indexing and rollback

`<state_dir>/readings/publication.sqlite3` stores the pinned roots, initial
baseline, per-source identity, generated-file receipts, attempts and pending
intents. It has an independent schema; Control schema does not change. Back up
this entire state subtree, including staged versions. Recovery copies are
retained; no automatic retention/deletion job is added in this version.

The journal survives a missed enqueue by reconciling new adopted sources and
survives a post-rename crash by checking the recorded destination hashes. An
interrupted update with ambiguous bytes requires operator reconciliation.
Retargeting a running publisher or reusing its journal for a different root is
refused. There is no automatic destination path migration.

Publishing files does not refresh a separate readings search index or prove
cloud synchronization. Run the original library's index utility through its
normal environment. Its incremental mode may add new papers without re-embedding
changed full text; use its supported rebuild path for updated text. Neither
external script execution, provider credentials nor synchronization settings
are introduced by this feature. Library reports publication separately from
internal import and does not claim external index/cloud completion.

Disabling the section and restarting stops further publication. Product rollback
or internal source removal does not propagate deletion to the library. The
external library needs its own backup; the normal product state backup includes
the journal but is not an external-library backup. Restore a matching journal
and library together, or reconcile their hashes before resuming. Downgrades that
ignore the new config section do not publish; preserve the configuration before
an older product rewrites it.

## Verification

Tests under `cortex_platform/tests/product/readings/` exercise real sandboxed
publisher processes in disposable directories: create, update, supplement,
note/manual-edit protection, identity/link refusal, deletion, concurrent
publishers and crash recovery. The engine integration test uses local arXiv
fixtures through the real Capture consumer and proves both the private source
and the external copy survive. No test mutates an operator library.
