# Private Access Distribution and Supervision Plan

## Scope

Provide the P2-ACCESS wheel and access-only staging foundation without
activating Tailscale, launchd, or any production service. Complete service
publication remains a P2-DEVREL-COMPOSE blocker. Changes are limited to the
private-access package, its tests, and root wheel metadata.

## Invariants

- The installed wheel owns the complete `deployment.private_access` package and
  stable console entry points.
- LaunchAgent-shaped descriptors remain only inside the owner-private immutable
  service generation. This package never writes `~/Library/LaunchAgents`;
  publication and activation belong to P2-DEVREL-COMPOSE. Descriptors never
  contain a resolved secret, derived bootstrap token, or Cortex daemon token.
- Supervised service configs require a Keychain reference. Environment
  references remain available only to deliberate foreground commands because
  they cannot be resolved reliably from a cold LaunchAgent start.
- The Web bootstrap wrapper resolves the external secret only in memory and
  injects the derived token into the Web child process environment immediately
  before `exec`. It removes the config path, secret reference, Control token,
  development origin, and all non-allowlisted inherited environment values.
  Planning and installation never resolve the secret.
- All service operations are user-scoped, serialized, reversible, and testable
  with temporary roots. Uninstall retains the ownership marker and lock inode
  so a concurrent reinstall cannot create a second lock domain. Service roots
  must never equal or descend from the user's real `~/Library/LaunchAgents`, and
  every existing path component must be a non-symlink, non-writable trusted
  directory before any mutation. An unowned root is never chmodded or otherwise
  repaired; each missing component created by the tool is independently made
  and verified as owner-only even under a hostile umask. Its uninstall journal
  isolates targets through no-replace renames into an owner-private durable
  tombstone, survives partial progress, and preserves foreign replacements.
  Isolation is terminal for this package: it never unlinks or removes retained
  tombstone content. This package never publishes a plist or invokes
  `launchctl`.
- This package validates only the access boundary. Real activation remains
  blocked until P2-DEVREL-COMPOSE owns the Control token, Cortex daemon, Web
  build, Node runtime, health, rollback, and residual-process lifecycle.

## Steps

1. Include `deployment.private_access` in the Cortex wheel and publish stable
   operator, gateway, and supervisor console entry points.
2. Add closed-schema staged macOS LaunchAgent descriptors for the access gateway
   and Web process, with strict path/command validation and redacted output.
3. Implement dry-run, install, idempotent reinstall, upgrade, rollback, and
   uninstall transactions using immutable generations and private pointers.
4. Add a Web `exec` boundary that validates the managed environment, resolves
   the secret reference at runtime, and never persists or prints its value.
5. Add packaging, isolated-install smoke, lifecycle, rollback, tamper, and
   secret-redaction tests. Build and inspect the real wheel before handoff.

## Independent re-review corrections

1. Add failing regressions proving direct and nested temporary-HOME
   `Library/LaunchAgents` roots, ancestor symlinks, and writable ancestors fail
   before creating or changing any file or mode.
2. Prove an unmarked foreign service root is byte- and metadata-stable after a
   rejected operation; require exact private mode instead of chmod repair.
3. Restrict ordinary transaction recovery to exact before/before or after/after
   convergence. Mixed or foreign pointer states retain the journal without any
   automatic pointer write.
4. Replace validate-then-unlink uninstall with journal-owned, no-replace
   isolation. Race tests replace a target between inventory and isolation and
   prove the replacement is restored or retained without data loss. The third
   review correction below supersedes the initial post-isolation deletion.
5. Remove the incomplete development-origin contract. Until
   P2-DEVREL-COMPOSE supplies an authenticated local front door, diagnostics and
   documentation must report direct local-browser access as blocked rather than
   present a loopback URL as usable.

## Third independent review corrections

1. Make isolation the terminal uninstall state. Active pointers and generation
   objects move through no-replace renames into an owner-private durable
   tombstone; the pending journal itself becomes the closed tombstone manifest.
   P2-ACCESS never unlinks or removes tombstone content. Repeated uninstall
   validates the tombstone and converges, while physical GC is explicitly
   deferred to P2-DEVREL-COMPOSE under a future offline maintenance contract.
2. Treat an existing lock or generation directory as immutable security state:
   exact owner, link count, type, and mode must pass before any repair or write.
   Open `versions` and the selected generation through no-follow directory
   capabilities and read generation files relative to the verified capability.
3. Bind every pointer to its generation manifest across release ID, release
   sequence, version, and config fingerprint. Journal equality alone never
   proves generation identity.

P2-DEVREL-COMPOSE may revisit physical retained-state garbage collection only after
it owns an offline maintenance transaction that proves all composed processes
are stopped, the active pointer namespace is absent, every tombstone and
operation-history entry has exact ownership metadata and its required closed
manifest, and the protected data snapshot has been verified. Until those gates
exist and have dedicated interruption and foreign-replacement tests,
accumulated history is intentional retained state rather than P2-ACCESS cleanup
debt.

## Fourth static-audit corrections

1. Apply the same no-delete rule to failed generation candidates and completed
   ordinary transaction journals. They move by no-replace rename into a
   validated owner-private operation history and remain there; P2-ACCESS never
   cleans them by pathname.
2. Replace pointer unlink/overwrite with a transaction-owned staged pointer and
   atomic exchange/no-replace state transition. The displaced object remains in
   transaction history, and a racing replacement is retained and causes
   fail-closed recovery rather than deletion or overwrite. A desired absent
   pointer is only verified absent.
3. Create ownership markers and pending journals with exclusive no-replace
   publication. Deterministic replacement tests must prove that candidate,
   journal, and pointer races preserve every foreign byte and leave a recoverable
   or explicitly blocked state.

## Fifth static-audit corrections

1. Extend immutable security-state validation to the formal access CLI. A new
   per-user operation lock is created exclusively; an existing lock must already
   be a regular, single-link, exact mode-0600 file owned by the effective user.
   The CLI never repairs existing lock metadata.
2. Publish generated policy and plan files relative to one verified private
   directory capability with exclusive no-replace creation. Exact canonical
   bytes may be adopted idempotently, while metadata or content drift is
   retained and rejected without replacement or pathname cleanup.
3. Replace rollback-manifest whole-file rewrites with a bounded, locked,
   checksummed canonical append log. Complete corruption fails closed; an
   incomplete final frame remains untouched until the next valid transition is
   fully preflighted and appended through the already-open descriptor.
4. Bind every rollback transition to one immutable plan/config/command identity
   and a closed state graph. Rollback validates the operation lock, state-root
   capability, manifest metadata, complete history, and current pathname
   binding before invoking any Tailscale command.
5. Add deterministic unsafe-mode, hard-link, same-content replacement,
   interrupted-write, concurrent-operation, and no-replace/no-unlink
   regressions for the formal CLI in addition to the supervisor lifecycle.

## Lock-domain and callback-binding corrections

1. Keep every service operation inside the flock on the already-open service
   root as well as the exact service-lock inode. Revalidate the root pathname
   against its directory descriptor before and after root locking, before
   recovery and critical-section entry, and on exit. A same-metadata lock-path
   replacement may be adopted by a later operation only after the first
   operation has failed closed and released the one root lock domain.
2. Keep every formal access operation inside the flock on the verified parent
   directory of the per-user global lock. A replacement of the global lock
   pathname during yield therefore cannot let another state directory open and
   enter a second lock domain.
3. Route every apply/rollback command-runner call through a single rollback-log
   binding guard. Revalidate the already-open manifest after injectable
   callbacks and immediately before every fake or future real command runner;
   same-content pathname replacement must fail before the runner records a
   call.

## Trusted-anchor re-review corrections

1. A directly locked service root or global-lock parent is not a stable lock
   domain because the whole directory can be renamed and recreated. Select one
   explicit, already-existing anchor containing the requested path: the
   effective account home or the stable per-user temporary container whose
   parent is not writable by the effective user. Validate its directory
   capability, owner and non-writable metadata, acquire its flock first, and
   continuously bind its pathname to the opened descriptor. Do not recurse
   indefinitely toward filesystem root.
2. Under the anchor lock, also open, lock where distinct, and bind the immediate
   global-lock parent or service-root parent. Supervision then opens and binds
   the service root relative to that parent. Deterministic whole-parent and
   whole-root replacements must leave the second operation outside the critical
   section until the first has failed closed and released the anchor.
3. Open and lock an existing rollback log before composition, gateway,
   binary-availability, or runner callbacks. A new apply reserves an empty,
   owner-only locked log first, making the create-new behavior explicit and
   ensuring later callbacks cannot introduce an adopted inode.
4. Verify the same opened state-directory and rollback-log capabilities both
   before and after every runner invocation. A runner that replaces the public
   pathname may record its one injected call but can never produce a successful
   or idempotent lifecycle result.
5. Register descriptor close and flock release immediately through an
   `ExitStack`. An injected `BaseException` after the anchor/parent flock but
   before lock-file open must unwind every acquired layer, after which a second
   operation can acquire the same trusted lock hierarchy.

## Final anchor-path correction

1. Select the operation-lock anchor from the lexical absolute path, not a
   caller-controlled `realpath` result, and reject every symlink ancestor before
   attempting to enter the lock hierarchy. The default macOS temporary lock
   path is canonicalized once so `/var` and `/private/var` cannot become two
   spellings of the same trusted domain.
2. Starting from the already-open stable anchor, traverse each descendant with
   `dir_fd` and `O_NOFOLLOW`. Create missing directories with `mkdirat`, force
   mode `0700`, sync the new directory and its parent, and verify exact
   descriptor-to-path identity before proceeding.
3. Treat any path switch after the initial ancestor check as a fail-closed
   operation before critical-section entry. All traversal descriptors are
   registered for `BaseException` cleanup as soon as they are opened.
