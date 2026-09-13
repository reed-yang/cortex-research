# Cortex Private Access

This package prepares a private, identity-aware HTTPS path to the Cortex Web
and PWA surface. It never exposes `cortexd`: the daemon and its token stay on
loopback, and only the Web server calls the Control API through its server-side
gateway.

The traffic path is:

```text
iPhone / laptop on the tailnet
  -> Tailscale Serve HTTPS + tailnet grants
  -> 127.0.0.1 Cortex access gateway
  -> 127.0.0.1 Cortex Web server
  -> server-only 127.0.0.1 cortexd gateway
```

The access gateway requires Tailscale's app-capability evidence for every
request. Human traffic also needs an exact login allowlist match; tagged client
nodes are accepted only when the tailnet policy grants the configured app
capability. It enforces one exact `https://...ts.net` Host and Origin, rejects
cross-site mutations, strips caller-supplied identity/forwarding headers, and
synthesizes the reviewed `X-Forwarded-Host` and `X-Forwarded-Proto` values.
At startup it resolves the configured external secret reference and injects
only a domain-separated derived bootstrap token into the Web-side request. A
caller-supplied bootstrap header is always stripped.

## Offline preparation

Copy `example.config.json` outside the repository, replace every example
identity and hostname, and keep the file mode `0600`. Store only a secret
reference (`keychain://service/account` or `env://NAME`), never a secret value.

```bash
python -m deployment.private_access --config private-access.json validate
python -m deployment.private_access --config private-access.json plan
python -m deployment.private_access --config private-access.json generate \
  --output private-access-review
```

`generate` writes a deterministic plan and a tailnet-policy fragment. Cortex
does not apply the policy fragment. Merge it manually in the Tailscale policy
editor, review the diff and policy tests, confirm the service node tag and tag
owners, and verify that Funnel is disabled.

For foreground boundary diagnostics only, start the Web server and access
gateway on their configured loopback ports:

```bash
cortex-private-access-gateway --config private-access.json
cortex-private-access --config private-access.json doctor
```

This proves internal process reachability, not direct browser usability. A
browser calling the loopback Web server has no trusted bootstrap header and is
rejected. Until P2-DEVREL-COMPOSE supplies an authenticated local front door,
`doctor` reports local-browser access as blocked and exposes no `local_url`.

The `plan` command is the default dry-run surface and executes no external
command. The installed CLI's real `apply` path is disabled by a separate
P2-DEVREL-COMPOSE gate even when every access-boundary check passes; this package
has no flag that opens that gate. Injected fake-runner tests retain coverage of
the future transaction, but do not authorize a real Serve mutation. `rollback`
remains available for exact tool-owned legacy state so a failed or superseded
deployment can still be removed safely.

Before mutation, `apply` also requires the dedicated loopback gateway health
endpoint to return the exact configuration fingerprint and an affirmative Web
boundary attestation. The gateway sends a random challenge to the Web-owned
loopback health route and verifies its exact origin fingerprint, echoed
challenge, domain-separated HMAC and `Cache-Control: no-store`. Missing,
unavailable, malformed, stale or forged Web responses keep apply fail-closed.
A fixed per-user operation lock serializes Serve mutations even when two callers
choose different rollback state directories. Rollback reads current Serve state
first: empty is already converged, the exact managed configuration may be
removed, and any foreign or drifted configuration is left untouched for manual
review.

The global lock path is resolved beneath one fixed account-home or per-user
temporary anchor. Every ancestor must be a real directory rather than a
symlink, descendants are opened or created one component at a time relative to
the locked anchor descriptor, and every directory and lock inode remains bound
to its public pathname for the full operation. This prevents a same-user path
switch from moving a second caller into a different lock domain.

The implementation follows Tailscale's current
[Serve](https://tailscale.com/docs/features/tailscale-serve) and
[Serve app-capability](https://tailscale.com/docs/reference/examples/serve#forward-app-capabilities-to-a-local-service)
contracts. App-capability headers require Tailscale 1.92 or newer; the real
activation gate must confirm the installed client version before applying.

## Staged service descriptors

The Cortex wheel installs stable `cortex-private-access`,
`cortex-private-access-gateway`, and `cortex-private-access-supervisor` console
entry points. The supervisor renders two LaunchAgent-shaped descriptors, one
for the loopback access gateway and one for the loopback Web process, but stores
both only inside the owner-private immutable service generation. It
does not copy anything into `~/Library/LaunchAgents` and never invokes
`launchctl`. Publication belongs exclusively to P2-DEVREL-COMPOSE after it owns
the complete `cortexd` + Control token + Web build + Node runtime lifecycle.

Use `plan` first, then repeat the same arguments with `install --dry-run` and
`install`. The launcher and Web executable paths must be absolute, regular,
owner-controlled executables. The source config must be an owner-only regular
file.
Supervised installs accept only `keychain://service/account` bootstrap
references because a LaunchAgent cold start cannot depend on an interactive
shell environment. Manual validation, gateway, and diagnostic commands retain
`env://NAME` support for deliberate foreground use.

```bash
cortex-private-access-supervisor plan \
  --config "$HOME/.config/cortex/private-access.json" \
  --gateway-launcher "/installed/bin/cortex-private-access-gateway" \
  --supervisor-launcher "/installed/bin/cortex-private-access-supervisor" \
  --web-working-directory "/installed/cortex-web" \
  --web-executable "/usr/local/bin/node" \
  --web-argument "dist/server/index.js" \
  --release-id "cortex-private-access-1" \
  --release-sequence 1
```

`install` stages an immutable owner-only generation and changes no launchd
directory. `upgrade` requires a strictly larger release sequence. `rollback`
switches only the private current/previous staging pointers; `uninstall` removes
only journal-owned generation files and pointer state from the active namespace.
The service root and every existing ancestor are validated without mutation;
both the account-home and environment-home `Library/LaunchAgents` trees and
their descendants, symlinked components, writable ancestors, and unmarked roots
with foreign state are rejected before any directory creation, chmod, pointer
write, or descriptor write.
It deliberately retains the owner-only service root, ownership marker, and the
same lock inode so a concurrent or later install cannot escape serialization.
Interrupted install/upgrade/rollback/uninstall transactions recover from a
durable journal. Ordinary pointer transitions stage the new value in an
owner-private transaction directory, use atomic exchange or no-replace rename,
and retain the displaced value. Recovery closes its journal as an aborted or
committed operation-history manifest only for an exact before/before or
after/after state; mixed and foreign states remain untouched for manual
recovery. A pointer whose desired value is absent is only verified absent and
is never unlinked. Failed generation candidates are likewise moved into
operation history rather than deleted. Uninstall atomically moves every exact
inode/digest claim
into a journal-owned, no-replace durable tombstone and closes the transaction
journal as that tombstone's manifest. The tombstone is the terminal state for
this package: P2-ACCESS never unlinks or removes its retained files or
directories. Interrupted isolation converges, while foreign replacements are
restored when possible or retained without deletion. Plan, doctor, and lifecycle
results expose `cleanup_deferred_to_p2_devrel=true` so this retained state is not
mistaken for reclaimed disk space. All lifecycle commands support `--dry-run`
and leave Tailscale, launchd, network listeners and running processes untouched.

Physical garbage collection of tombstones, completed transaction history,
displaced pointers, and failed generation candidates is intentionally absent.
It may be added only by P2-DEVREL-COMPOSE as a separate offline maintenance
transaction after it can prove all composed processes are stopped, no active
service pointer remains, every retained object has exact ownership metadata and
its required closed manifest, and the protected data snapshot has been
verified. Until those gates and their interruption/race regressions exist, all
history must be retained indefinitely.

The staged plists contain only the managed config path, exact public origin and
the external secret reference. They never contain the resolved secret, derived
bootstrap token or Cortex daemon token. At process start the Web wrapper resolves
the same reference in memory, overwrites any untrusted inherited access values,
does not pass an inherited development-origin override, injects the derived token
only into the Web child environment and immediately replaces itself with that
process. The child does not receive `CORTEX_ACCESS_BOOTSTRAP_SECRET_REF`,
`CORTEX_PRIVATE_ACCESS_CONFIG`, `CORTEX_CONTROL_TOKEN`, or arbitrary inherited
environment variables.

## Security boundary

- Tailscale Serve is the only supported remote provider in v1. Funnel, public
  DNS tunnels, LAN binds, `0.0.0.0`, wildcard origins, and direct daemon targets
  fail validation.
- The access and Web processes bind to `127.0.0.1`. Local processes on the host
  remain inside the trusted machine boundary; OS process isolation is a later
  distribution hardening gate.
- Browser requests never receive the Cortex daemon token. The access package
  neither reads nor stores that token.
- The bootstrap secret reference is never resolved by plan, generation, doctor,
  installation, rollback, or Tailscale commands. The gateway and Web wrapper
  resolve it independently at process start; the raw secret is not logged,
  persisted, placed in a plist, or forwarded.
- This is not a home-grown multi-user authentication system. Broader users,
  public access, account management, and recovery flows require a separate
  threat model and review.
- Without Tailscale, `doctor` reports degraded remote access. It also reports
  direct local-browser Control access as blocked until P2-DEVREL-COMPOSE owns an
  authenticated local front door; it never treats raw loopback reachability as
  a usable browser path or falls back to a public or LAN listener.

Real activation requires a reviewed policy diff, a current Tailscale client
with Serve app-capability headers, HTTPS enabled for the tailnet, a supervised
loopback gateway, distribution-owned secret resolution, and physical iPhone
Safari plus installed-PWA acceptance.

The repository wheel now includes this module and its installed console entry
points, and the access-only staging lifecycle is locally testable. This does not
make the existing developer bundle a complete application distribution: it
still lacks a bundled Web build and reviewed Node runtime, automatic composition
with `cortexd`, signed/notarized release metadata, residual-process checks, and
clean-host/physical-iPhone acceptance. Do not describe the wheel or staged
descriptors as a service installer, and do not run real `apply`, plist
publication, or `launchctl` operations from this package.
