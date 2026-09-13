# Cortex Web and PWA

Local-first Assistant UI client for the Cortex Control API. The default route is
the shell: a sidebar holding the current Project and its Threads, and one main
region showing exactly one of Thread, Research, Library, Inbox or Status.

- **Project** is a Control workspace, one research topic. The switcher creates
  and renames them.
- **Thread** is one conversation in that project. The list groups threads by the
  day they were last active and offers Rename and Archive per thread; a run is
  the state of the open thread, never a separate object to navigate to.
- **Research** holds old Ideas, Explorations and Projects with their adopted
  dossiers. Open research conversation selects an item for a normal thread;
  this does not restart a legacy engine round. See the
  [operator guide](../../docs/runbooks/cortex-web-user-guide.md).
- **Library** is the adopted corpus, shared across projects: search, the record
  for one source, and its Notes / Full text / Grounding reader.
- **Inbox** is what is waiting for you: pending decisions from every thread, and
  the captures queue with its Capture entry point.
- **Status** reports the API version and capabilities, whether runtime dispatch
  is on, the connection state, and the toggle that reveals the research engine's
  own projects and threads.

Where you are lives in the query, so a reload and a PWA relaunch land back on
the same place: `?project=<workspace id>&thread=<thread id>&view=<research|library|inbox|status>`.
Research selection additionally uses `item=<research item id>`.
Omitting `view` opens the thread; an id that no longer exists falls back to the
first project with a notice. The shell writes the query with `history.replaceState`
and never navigates, so the route itself is unchanged.

The canonical G0/G1 fixtures remain available only as an explicit demo and test
mode.

## Run

For verification, use `npm run typecheck` for both app and Worker programs,
`npm run test:fast` for browser-free checks, and `npm test` for the full chain.
`npm run test:markdown-math` runs the real-browser math acceptance against a
built app. Prerequisites and coverage are in the
[verification runbook](../../docs/runbooks/web-verification.md).

```bash
npm ci
npm run dev
```

Open `http://127.0.0.1:3000` only for UI development or the explicit demo mode.
Direct local-browser Control requests do not possess the trusted access
bootstrap header and are rejected. P2-DEVREL-COMPOSE must provide an
authenticated local front door before the local browser can use real Control.
A product launcher must inject the loopback `CORTEX_CONTROL_API_URL` and
`CORTEX_CONTROL_TOKEN` into the Web server process, plus the private-access
boundary values described below. The daemon token is consumed only after that
server-side boundary succeeds. Never place it in a URL, browser storage, client
environment variable, or service worker.

Without gateway configuration, the default route shows an explicit Control
unavailable state. Deterministic fixtures are opt-in at
`http://127.0.0.1:3000/?mode=demo`; append `&scenario=g1` for the resolved case.

## Install on iPhone

P2-ACCESS provides a reviewed private Tailscale Serve path. Serve exposes only
the loopback Cortex access gateway, which validates tailnet identity, exact Host
and Origin, CSRF evidence, and forwarding headers before reaching this loopback
Web server. It never targets `cortexd`; only this server-side Web gateway holds
the daemon token. See `deployment/private_access/README.md` for offline plan,
doctor, policy-fragment, apply, rollback, and manual activation gates.
P2-ACCESS only stages owner-private service descriptors: it does not publish
`~/Library/LaunchAgents`, invoke `launchctl`, or open the real Serve apply gate.
P2-DEVREL-COMPOSE must first own the complete Web/Node, `cortexd`, Control-token,
health, rollback, and process-cleanup lifecycle.
The launcher must set one canonical HTTPS origin in `CORTEX_PUBLIC_ORIGIN`,
exactly `https://<fqdn>` (for example `https://node.tailnet.ts.net`, or the
public hostname a Cloudflare Tunnel serves). Since P7 the same process may also
carry `CORTEX_LOCAL_ORIGIN`: the two are two doors on one listener, told apart by
the request's exact `Host`, and a public-door request must additionally carry the
`x-cortex-access-identity` header that only the Node adapter sets, after it has
verified the Cloudflare Access assertion. There is no development-origin bypass. The private-access supervisor resolves the external
bootstrap secret and gives Web only the
43-character, base64url, domain-separated derived value through
`CORTEX_ACCESS_BOOTSTRAP_TOKEN`. Web does not resolve the raw secret or a
Keychain reference.

Every Control gateway request must carry the exact configured Host,
`X-Forwarded-Host`, `X-Forwarded-Proto`, `Sec-Fetch-Site`, and derived
`X-Cortex-Access-Bootstrap` value. Mutations additionally require the exact
configured Origin and `X-Cortex-Web-Client: v1`. Missing, duplicate, malformed,
or mismatched boundary values and untrusted forwarding headers are rejected
before the daemon token is read or used. Request Host is never a trust source.

The access gateway can prove this Web boundary without receiving a reusable
secret by calling `GET /api/cortex/access-boundary/health` with the normal
boundary headers, `Sec-Fetch-Site: none`, and
`X-Cortex-Access-Attestation-Challenge: <base64url(32 random bytes)>`. A valid
response has `service=cortex-web-access-boundary`, `version=1`,
`web_access_boundary_verified=true`, echoes the challenge, and includes:

```text
origin_fingerprint = base64url(sha256(public_origin))
attestation = base64url(hmac-sha256(
  key = ASCII CORTEX_ACCESS_BOOTSTRAP_TOKEN,
  message = "cortex-web-access-attestation-v1" + NUL
            + challenge + NUL + origin_fingerprint
))
```

The probe must compare every field and attestation in constant time, require
`Cache-Control: no-store`, and reject extra or stale challenge data. The
attestation response contains neither the bootstrap token nor the daemon token.

After those gates pass, open the exact `https://<node>.<tailnet>.ts.net` URL in
Safari, choose **Share → Add to Home Screen**, and launch Cortex from the new
icon. The installed surface uses standalone display, safe areas, dynamic
viewport sizing, and a touch-sized single-column navigator. The browser and
all Control mutations must remain on that exact origin; wildcard origins,
Funnel, public tunnels, LAN binds, and direct daemon URLs are unsupported.

If Tailscale is unavailable, remote access is explicitly degraded. Direct local
browser Control remains blocked until the composed product supplies its trusted
front door; no automatic public or LAN fallback is attempted.

The service worker caches only install assets and a truthful offline page. It
does not cache research pages, API responses, events, or decisions. If an
already open client loses connectivity, mutation controls are disabled and no
action is queued. The opaque event cursor is the only browser-persisted Control
value. Web Push is intentionally absent and disabled by default.

## Verify

```bash
npm test
npm run lint
```

`npm test` includes runtime DTO decoding, gateway/CSRF/token boundaries,
idempotent retry and revision-conflict behavior, deterministic manifest/icon
checks, desktop fixture gates, and real-Control plus demo iPhone-size Playwright
interaction and screenshot acceptance. Every temporary browser server binds to
`127.0.0.1:3000` and is stopped before the command exits.
The mobile test uses a test-only loopback proxy that injects the reviewed
public-origin boundary contract. It validates UI behavior and boundary
compatibility, but is not a packaged local front door and must not be described
as an installable release path.

## Shell

The shell is built on the official Assistant UI (shadcn) component set:
`components/ui` holds the shadcn primitives and `components/assistant-ui` the
registry's Thread and ThreadList, with `app/shell/` supplying the Cortex slots
around them (status strip, decision cards, run history, outputs). Styling is
Tailwind utilities plus the shadcn neutral tokens in `app/globals.css`; dark is
`prefers-color-scheme` only, with no toggle.

The thread runs on `useExternalStoreRuntime`, so Control is the single source of
the message list rather than a runtime-local copy: the store projects the
durable messages, and sending is still the two durable commands the API expects
(append the message under the thread's revision, then create the run from the
revision that append produced). Assistant UI never sees a Cortex DTO — Control
DTOs are runtime-decoded independently of Assistant UI types. SSE is not present
in the current backend, so the client labels and uses a bounded, non-overlapping
opaque-cursor polling fallback with backoff.

Below the shadcn tokens, `app/globals.css` still carries the July cockpit's
stylesheet, cut down to the rules its two surviving consumers need: the research
components under `app/control/` and the retained `?mode=demo` prototype. Its
palette tokens are aliases of the shadcn tokens, so both colour schemes follow
one theme.
