"""The daemon's one managed Hermes worker, and the window that owns it.

S3.3 shipped `ManagedHermesBackend` written and tested with zero non-test
callers, and said so plainly: "placing the approval gate on the managed-backend
launch path is an explicit precondition of the P4 daemon wiring". This is that
caller, and the gate is here.

Three rules shape everything below.

* **One supervisor per daemon, for the ACTIVE release only.** The descriptor is
  resolved through the attempt-free path, so binding leaves no attempt row and
  writes nothing into the updater's evidence.
* **Lazy launch, gate-first.** The worker process starts on the first frame a
  window needs and never at daemon start, so a product that has never opened a
  window has never executed the release's own code.
* **Every launch re-asks.** D6's approval and the durable transport gate are
  read again for each acquisition, so a release revoked while a window is open
  refuses the next frame and the worker bound to it is closed.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from ..control import ControlStore
from ..control.errors import InvalidTransition, NotFound
from ..paths import PathRegistry
from ..redaction import redact
from ..runtime_update.approval import ControlReleaseApprovals
from ..runtime_update.sandbox import DEFAULT_EGRESS_PORT
from ..runtime_update.service import DigestPinVerifier, RuntimeUpdateService
from ..runtime_update.supervisor import (
    WorkerEnvironmentError,
    worker_environment,
)
from ..runtime_update.worker_launch import (
    WorkerLaunchError,
    build_active_descriptor,
    descriptor_document,
)
from ..secrets import SecretResolutionError, SecretResolver
from .worker_rpc import (
    POLLER_FAILURE_LOG_LIMIT,
    TELEGRAM_SECRET_ALIAS,
    GatedWorkerTransportRPC,
    TelegramInboundPoller,
    transport_credential_bindings,
)

#: The env key `cortex_worker.telegram` reads. Never operator-facing config: it
#: exists so an acceptance can point the transport at a loopback stand-in, and
#: the distribution supervisor hands the daemon a wiped environment, so on the
#: production path it is unset and the worker uses `https://api.telegram.org`.
TELEGRAM_API_BASE_URL_KEY = "TELEGRAM_API_BASE_URL"

#: ⟦P5.4c⟧ The model-provider credentials a Hermes TURN needs, by the alias an
#: operator writes in `secret_refs` and the env key `worker_environment` already
#: allowlists. Deliberately NOT gated on the transport window: the bot token is
#: what ⟦AMD-4⟧ scopes to a window, because the window is the decision about who
#: may speak as the bot. Whether a turn may run at all is migration 12's
#: decision, taken in Control before any runtime call -- not the presence of a
#: key in an environment. Gating both on the window would also mean the gate a
#: turn depends on could only change by killing the poller's worker.
PROVIDER_SECRET_ALIASES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

#: The provider endpoints the worker may be pointed at, alongside the
#: transport's. ⟦BLOCK-1⟧ These are the LOOPBACK OVERRIDE and nothing else: the
#: distribution supervisor replaces `cortexd`'s environment with seven keys
#: (`distribution/lifecycle.py`), so on every deployed start path they are
#: unset and the endpoint comes from `[runtime] base_url` instead. An
#: acceptance that starts the daemon itself can still point the worker at a
#: stand-in through them.
PROVIDER_BASE_URL_KEYS = (
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
)

#: What `[runtime] provider` says when the operator does not. `custom` is the
#: fork's "an OpenAI-compatible endpoint I am telling you about", which is what
#: every P5.4c turn used; naming `anthropic` selects its Messages API instead.
DEFAULT_PROVIDER_NAME = "custom"

#: Which endpoint goes with which credential, so a turn is told one provider
#: rather than a set. Ordered: the first alias the operator configured with a
#: matching endpoint is the one a turn uses.
PROVIDER_ENDPOINTS = {
    "openai": "OPENAI_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
    "openrouter": "OPENROUTER_BASE_URL",
}

#: What one transport frame gets when the caller names no deadline. Every
#: production caller does name one (`telegram_frame_deadline` for a poll, the
#: frozen projection's own bound for a send); this exists so the supervisor's
#: required argument never has to be invented at the call site.
DEFAULT_FRAME_TIMEOUT_SECONDS = 30.0

#: Why the daemon is not bound to a worker. A closed set, because health reports
#: it and an operator has to be able to tell "nothing is activated" from "the
#: bytes that are activated were never approved".
UNBOUND_NO_ACTIVE_RELEASE = "no_active_release"
UNBOUND_NOT_APPROVED = "release_not_approved"
UNBOUND_RUNTIME_UNAVAILABLE = "active_runtime_unavailable"

#: ⟦P5.4c⟧ Another daemon's worker already owns this slot's operation ledger.
#: The P5.4b real run produced this state by purging a live daemon's state tree:
#: the second daemon binds happily and then every frame comes back
#: `WorkerProtocolError`, which reads like a broken worker and is two products.
UNBOUND_LEDGER_HELD = "slot_ledger_held_elsewhere"

#: Every endpoint the worker may reach has to agree on one port, because the
#: seatbelt permits exactly one.
UNBOUND_EGRESS_CONFLICT = "egress_port_conflict"

#: ⟦P54A-8⟧ An endpoint whose port cannot be read at all. Unreachable while the
#: endpoints came only from a wiped environment; the moment ⟦BLOCK-1⟧ made one
#: configurable, an operator typo became a daemon that would not start -- and a
#: product that stops starting has lost `doctor`, `backup` and `upgrade` too.
UNBOUND_EGRESS_INVALID = "egress_endpoint_invalid"

#: ⟦BLOCK-1⟧ Half a provider is not a provider. Both halves are the operator's
#: own configuration, so both are answerable at bind: a credential alias with
#: no endpoint to spend it against, or an endpoint with no credential alias. A
#: `[runtime] model` the product cannot deliver refuses here rather than
#: reaching the operator as `runtime_execution_failed` on a live window.
UNBOUND_PROVIDER_ENDPOINT = "provider_endpoint_missing"
UNBOUND_PROVIDER_CREDENTIAL = "provider_credential_missing"

#: Why an otherwise bound worker refused a frame.
REFUSED_GATE_CLOSED = "transport_gate_closed"
REFUSED_NOT_APPROVED = UNBOUND_NOT_APPROVED
REFUSED_NO_CREDENTIAL = "transport_credential_unavailable"
#: ⟦BLOCK-1⟧ The provider alias resolved to nothing. Distinct from the bot
#: token's refusal on purpose: `_agent_options` used to swallow exactly this
#: and return `{}`, which reaches the operator as the identical symptom as
#: naming no provider at all -- every turn `runtime_execution_failed`, and no
#: surface anywhere saying the keychain lookup was what failed.
REFUSED_PROVIDER_CREDENTIAL = "provider_credential_unavailable"

_log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_LSOF = "/usr/sbin/lsof"
#: `record_transport_window_closed` bounds `proof` at 200 characters.
_PROOF_LIMIT = 200


class ManagedWorkerUnavailable(RuntimeError):
    """A transport frame was refused before it reached the worker.

    Typed and reason-coded: every one of these is a decision the operator has
    made (or not made) rather than a fault, and the poller, the delivery path
    and health all branch on the reason rather than on a message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReleaseProof:
    """The derivation D-P5-5 forbids being written as a literal."""

    poller_stopped: bool
    worker_launched: bool
    worker_pid: int | None
    exit_status: int | None
    established_sockets: tuple[str, ...] | None
    proof: str
    #: ⟦P54A-6⟧ Whether `lsof` answered at all. `established_sockets` returns
    #: `None` for "could not answer" and `()` for "found nothing", and the two
    #: are different evidence; this says which one the audit row is reading
    #: without anyone having to parse the proof sentence.
    sockets_observed: bool = True
    #: When the three observations were made, which is not necessarily when the
    #: window was recorded closed: a release forced by a revocation happens the
    #: moment the operator revokes, and `close-window` may be minutes later.
    observed_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "poller_stopped": self.poller_stopped,
            "worker_launched": self.worker_launched,
            "worker_pid": self.worker_pid,
            "exit_status": self.exit_status,
            "established_sockets": (
                None
                if self.established_sockets is None
                else list(self.established_sockets)
            ),
            "proof": self.proof,
            "sockets_observed": self.sockets_observed,
            "observed_at": self.observed_at,
        }


def established_sockets(pid: int, *, run=None) -> tuple[str, ...] | None:
    """The provider connections ONE process still holds, or None if unanswered.

    `-a` is the whole predicate: lsof ORs its selection options, so `-p <pid>
    -i` without it lists every socket on the machine -- the P5.3 real run
    recorded 190 host-wide connections as the worker's, and the contract was
    amended to carry the flag for exactly that reason.

    `None` rather than `()` when lsof itself could not answer. The contract
    records the residual that `out=$(lsof ... || true)` cannot tell "found
    nothing" from "lsof failed"; here the two are different values, and the
    caller decides what an unanswered question is worth.
    """

    invoke = subprocess.run if run is None else run
    try:
        completed = invoke(
            [
                _LSOF,
                "-nP",
                "-a",
                "-p",
                str(pid),
                "-i",
                "TCP",
                "-s",
                "TCP:ESTABLISHED",
                "-Fn",
            ],
            capture_output=True,
            text=True,
            # ⟦P54A-2⟧ Bounded, because this call moved. It used to run only on
            # the operator's `close-window`; a release now also happens on the
            # 1 s reconcile thread that drives the outbound drain, and an `lsof`
            # blocked on an unresponsive mount would stop the window loop.
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 both for "nothing matched" and for "the pid is gone", which
    # is the answer this is asked for, so 1 is not a failure here. Anything
    # else -- and any output on stderr with a non-zero exit -- is unanswered.
    if completed.returncode not in (0, 1):
        return None
    if completed.returncode == 1 and completed.stderr.strip():
        return None
    return tuple(
        line[1:] for line in completed.stdout.splitlines() if line.startswith("n")
    )


def foreign_ledger_holder(state_dir: Path) -> int | None:
    """The pid holding the active slot's worker lock, when it is not ours.

    ⟦P5.4b finding⟧ Two daemons can drive one slot: the lifetime lock is a file
    in the state dir, so purging that dir under a live `cortexd` lets the next
    `cortex start` take a fresh lock while the first daemon keeps its fd. The
    second daemon's worker then cannot open the operation ledger the first
    one's worker holds under `flock`, and EVERY frame comes back
    `WorkerProtocolError` -- a failure that reads like a broken worker and is
    actually two products.

    This is the cheap half of the answer: at bind time this daemon has launched
    nothing, so anybody holding that lock is somebody else. Probed by taking
    the lock non-blockingly and dropping it again, which is exactly what the
    worker itself does and therefore cannot be more disruptive than a launch.
    Returns the pid when one can be named, `-1` when the lock is held by a
    process `lsof` cannot name, and `None` when nothing holds it.
    """

    lock = state_dir / "worker.lock"
    if not lock.is_file():
        return None
    try:
        descriptor = os.open(lock, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _lock_holder(lock)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return None
    finally:
        os.close(descriptor)


def _lock_holder(lock: Path) -> int:
    try:
        completed = subprocess.run(
            [_LSOF, "-t", "--", str(lock)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    for line in completed.stdout.split():
        try:
            return int(line)
        except ValueError:
            continue
    return -1


class EgressEndpointInvalid(ValueError):
    """An endpoint whose port cannot be derived. Never carries the URL."""


def _egress_port(base_url: str | None) -> int:
    """The single port the seatbelt permits, from the endpoint actually used.

    ⟦P54A-8⟧ A scheme this does not know is refused rather than defaulted to
    443: guessing would hand the sandbox a rule for a port the worker is not
    going to use, and the denial that follows names neither the endpoint nor
    the configuration.
    """

    if not base_url:
        return DEFAULT_EGRESS_PORT
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise EgressEndpointInvalid("endpoint port is not readable") from exc
    if port is not None:
        return int(port)
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return DEFAULT_EGRESS_PORT
    raise EgressEndpointInvalid("endpoint names no scheme this can serve")


@dataclass(frozen=True)
class ManagedWorkerHealth:
    """What `/api/v1/health` says about the worker. No paths, no secrets."""

    bound: bool
    reason: str | None
    release_id: str | None
    slot_digest: str | None
    launched: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": "bound" if self.bound else "unbound",
            "reason": self.reason,
            "release_id": self.release_id,
            "slot_digest": self.slot_digest,
            "launched": self.launched,
        }


class ManagedTransportWorker:
    """The active release's worker, launched on demand and gated every time."""

    def __init__(
        self,
        *,
        store: ControlStore,
        paths: PathRegistry,
        config: Mapping[str, object],
        environ: Mapping[str, str],
        backend_factory: Callable[..., object] | None = None,
        service: RuntimeUpdateService | None = None,
    ) -> None:
        self._store = store
        self._paths = paths
        self._environ = dict(environ)
        # Only the transport alias. An unrelated `secret_refs` entry never
        # reaches this process's memory, let alone a worker's environment.
        references = dict(config.get("secret_refs") or {})  # type: ignore[arg-type]
        self._secret_refs = {
            alias: str(reference)
            for alias, reference in references.items()
            if alias == TELEGRAM_SECRET_ALIAS
        }
        self._resolver = SecretResolver(environment=self._environ)
        base_url = self._environ.get(TELEGRAM_API_BASE_URL_KEY)
        self._base_urls = (
            {TELEGRAM_API_BASE_URL_KEY: base_url} if base_url else {}
        )
        # ⟦P5.4c⟧ Provider credentials the operator named, resolved the same way
        # and through the same resolver as the transport's. Only aliases this
        # module knows how to bind: an unrelated `secret_refs` entry still never
        # reaches a worker's environment.
        runtime_section = dict(config.get("runtime") or {})  # type: ignore[arg-type]
        self._model = str(runtime_section.get("model") or "") or None
        self._provider_declared = bool(runtime_section.get("provider"))
        self._provider_name = (
            str(runtime_section.get("provider") or "") or DEFAULT_PROVIDER_NAME
        )
        self._provider_endpoint = str(runtime_section.get("base_url") or "") or None
        self._provider_refs = {
            alias: str(reference)
            for alias, reference in references.items()
            if alias in PROVIDER_SECRET_ALIASES
        }
        # ⟦BLOCK-1⟧ Configuration first, then the loopback override. The
        # supervised daemon's environment is replaced with seven keys, so
        # `[runtime] base_url` is the only route a deployed installation has;
        # the `*_BASE_URL` variables stay ahead of it so an acceptance that
        # starts `cortexd` itself can still aim the worker at a stand-in.
        if self._provider_endpoint:
            for alias in self._provider_refs:
                self._base_urls.setdefault(
                    PROVIDER_ENDPOINTS[alias], self._provider_endpoint
                )
        for key in PROVIDER_BASE_URL_KEYS:
            configured = self._environ.get(key)
            if configured:
                self._base_urls[key] = configured
        # The seatbelt permits exactly one outbound port, so every endpoint the
        # worker is pointed at has to agree on one. Refusing here is the whole
        # value: the alternative is a launch that succeeds and then denies the
        # provider connection with an error that names the sandbox and not the
        # configuration.
        self._egress_port = DEFAULT_EGRESS_PORT
        self._egress_conflict = False
        self._egress_invalid = False
        try:
            self._egress_port = _egress_port(base_url)
            self._egress_conflict = any(
                _egress_port(value) != self._egress_port
                for value in self._base_urls.values()
            )
        except EgressEndpointInvalid:
            # ⟦P54A-8⟧ Recorded, never raised out of `__init__`: `cortexd`
            # constructs this before it serves anything, and a typo in one
            # config key must not be the reason `cortex doctor` stops working.
            self._egress_invalid = True
        if backend_factory is None:
            from ...runtime.managed_hermes import ManagedHermesBackend

            backend_factory = ManagedHermesBackend
        self._backend_factory = backend_factory
        self._lock = threading.RLock()
        self._backend: object | None = None
        self._process: object | None = None
        self._last_release: ReleaseProof | None = None
        # ⟦P8⟧ Whether the running backend was launched inside a window, i.e.
        # from an environment that bound the bot token. See `acquire`.
        self._launched_windowed = False
        # Written once by `bind`, read without the lock by health.
        self._reason: str | None = UNBOUND_NO_ACTIVE_RELEASE
        self._release_id: str | None = None
        self._manifest_sha256: str | None = None
        self._slot_digest: str | None = None
        self._descriptor = None
        self._descriptor_path: Path | None = None
        self._ledger_holder: int | None = None
        self._ledger_state_dir: Path | None = None
        self._service = service if service is not None else self._build_service()

    # -- binding -----------------------------------------------------------

    def _build_service(self) -> RuntimeUpdateService:
        return RuntimeUpdateService(
            self._paths.runtime_update_root,
            # The two document verifiers guard `import`, which this never
            # performs: binding reads the pointer an earlier import already
            # verified. The same pinning `runtime_update.cli` uses for the
            # commands that do not take a catalog.
            catalog_verifier=DigestPinVerifier("0" * 64),
            attestation_verifier=DigestPinVerifier("0" * 64),
            approvals=ControlReleaseApprovals(self._store),
        )

    def bind(self) -> ManagedWorkerHealth:
        """Resolve the ACTIVE release, or record why there is no worker.

        Nothing is launched here. What this establishes is the identity every
        later launch is checked against -- and the identity is the pair D6
        binds, `(release_id, manifest_sha256)`, never the release id alone.
        """

        with self._lock:
            self._derive_binding()
        return self.health()

    def _derive_binding(self) -> None:
        """The derivation itself, so ⟦P54A-5⟧ can run it again later.

        Called under the lock, from `bind()` at start and from the lazy
        re-derivation an approval that lands afterwards triggers. Everything it
        establishes is idempotent: the same descriptor document is written to
        the same daemon-owned path.
        """

        if not self._paths.runtime_update_root.is_dir():
            # Asked before `status()`, which takes the updater's lock and
            # therefore CREATES its root. A product that has never imported a
            # release has no active one, and a daemon start must not leave an
            # empty updater tree behind to say so.
            self._reason = UNBOUND_NO_ACTIVE_RELEASE
            return
        try:
            status = self._service.status()
        except Exception:  # noqa: BLE001 - an unreadable updater root is a reason
            self._reason = UNBOUND_RUNTIME_UNAVAILABLE
            return
        active = status.get("active")
        if not isinstance(active, Mapping):
            self._reason = UNBOUND_NO_ACTIVE_RELEASE
            return
        release_id = str(active.get("release_id") or "")
        slot_digest = str(active.get("slot_digest") or "")
        releases = status.get("releases")
        record = (
            releases.get(release_id) if isinstance(releases, Mapping) else None
        )
        manifest_sha256 = (
            str(record.get("manifest_digest") or "")
            if isinstance(record, Mapping)
            else ""
        )
        if not release_id or not manifest_sha256:
            self._reason = UNBOUND_RUNTIME_UNAVAILABLE
            return
        self._release_id = release_id
        self._manifest_sha256 = manifest_sha256
        self._slot_digest = slot_digest
        # ⟦S3.4/D6⟧ on the launch path, as S3.3's notes require. The refusal is
        # recorded rather than raised: a daemon whose active release was never
        # approved still starts, still serves Control, and says in health why
        # every transport frame is going to be refused.
        if not self._store.runtime_release_approved(release_id, manifest_sha256):
            self._reason = UNBOUND_NOT_APPROVED
            return
        try:
            descriptor = build_active_descriptor(self._service)
        except WorkerLaunchError:
            # `build_active_descriptor` already funnels every failure of the
            # attempt-free derivation -- including the D6 refusal raised inside
            # `preview_attempt_pin` -- into this one type, which is why the
            # approval question is asked above rather than inferred from here.
            self._reason = UNBOUND_RUNTIME_UNAVAILABLE
            return
        if self._egress_invalid:
            self._reason = UNBOUND_EGRESS_INVALID
            return
        if self._egress_conflict:
            self._reason = UNBOUND_EGRESS_CONFLICT
            return
        provider_problem = self._provider_problem()
        if provider_problem is not None:
            self._reason = provider_problem
            return
        holder = foreign_ledger_holder(descriptor.state_dir)
        if holder is not None:
            # Before the descriptor is written and long before anything is
            # launched. This daemon has started no worker yet, so whoever holds
            # that lock is somebody else's -- and binding anyway is how one slot
            # ends up with two products and every frame a protocol error.
            self._reason = UNBOUND_LEDGER_HELD
            self._ledger_holder = holder
            # ⟦P54A-9⟧ Remembered so the lock can be asked again. The cutover's
            # own preconditions run `assert-transport-capability`, which
            # launches a worker on the same ACTIVE release and therefore the
            # same `worker.lock`, immediately before step 4's `cortex start` --
            # so a lock held for a few hundred milliseconds used to cost the
            # daemon its worker for the whole of the window.
            self._ledger_state_dir = descriptor.state_dir
            return
        self._descriptor = descriptor
        self._descriptor_path = self._write_descriptor(descriptor)
        self._reason = None

    def _provider_problem(self) -> str | None:
        """⟦BLOCK-1⟧ Whether the operator named only half of a provider.

        Both halves are configuration this process already holds, so the answer
        is available at bind -- which is the only place it is useful. The
        alternative is what the P5.4c real run produced twice: a daemon that
        reports `bound`, a `turn_bridge` block that is present, every runbook
        predicate green, and then `runtime_execution_failed` on the first
        message of an operator-present window.

        Naming nothing at all is still not an error. This adds a refusal for an
        answer that cannot be delivered, never a requirement to answer -- and
        `[runtime]` is what expresses the asking. The provider ALIASES are not:
        `anthropic`, `openai` and `openrouter` are the engine's own credential
        names too (P4 resolves the same three), so an installation whose
        research profiles have a key and whose turns have no provider would
        otherwise stop binding for a reason that has nothing to do with it.
        """

        if not (self._model or self._provider_endpoint or self._provider_declared):
            return None
        if not self._provider_refs:
            return UNBOUND_PROVIDER_CREDENTIAL
        if not any(
            self._base_urls.get(PROVIDER_ENDPOINTS[alias])
            for alias in self._provider_refs
        ):
            return UNBOUND_PROVIDER_ENDPOINT
        return None

    def _write_descriptor(self, descriptor) -> Path:
        """Serialize the descriptor into daemon-owned state, never the slot.

        A supervisor takes a descriptor *path*, and the only place the daemon
        may write is its own state directory: the slot is sealed and the
        updater's evidence belongs to the updater.
        """

        directory = self._paths.state_dir / "managed-worker"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "descriptor.json"
        path.write_text(
            json.dumps(descriptor_document(descriptor), sort_keys=True),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    # -- health ------------------------------------------------------------

    def health(self) -> ManagedWorkerHealth:
        """Read without the lock: an acquisition holds it across a launch.

        The one exception is ⟦P54A-5⟧'s re-derivation, which does take the lock
        -- but only on the transition from `release_not_approved` to approved,
        which happens at most once per approval an operator makes.
        """

        reason = self._rederive_if_cleared()
        if reason is None and not self._approved():
            # A revoke lands after the binding was made, and the binding is
            # made once per process. Health that reported the start-time answer
            # would say `bound` for the whole life of a daemon whose release
            # the operator withdrew minutes in -- while every frame was already
            # being refused. One indexed read is what that costs.
            reason = UNBOUND_NOT_APPROVED
        return ManagedWorkerHealth(
            bound=reason is None,
            reason=reason,
            release_id=self._release_id,
            slot_digest=self._slot_digest,
            launched=self._backend is not None,
        )

    def _rederive_if_cleared(self) -> str | None:
        """The two refusals an operator can lift without restarting the daemon.

        ⟦P54A-5⟧ The approval re-read used to be one-way, and therefore sticky:
        a revoke that landed after the binding turned `bound` into
        `release_not_approved`, and an approval that landed after `cortex start`
        did nothing at all until a restart -- while `cortex-product-deploy`
        advertised exactly the symmetric behaviour for revocation.

        ⟦P54A-9⟧ The slot's operation-ledger lock was asked once, at bind, so a
        pre-window `assert-transport-capability` worker that had not quite
        exited unbound the daemon for its whole life, with a reason code the
        runbook does not cover.

        Nothing is re-resolved speculatively: the derivation runs again only
        when the one thing that refused it is observably gone.
        """

        reason = self._reason
        if not self._recoverable(reason):
            return reason
        with self._lock:
            if self._recoverable(self._reason):
                self._derive_binding()
            return self._reason

    def _recoverable(self, reason: str | None) -> bool:
        if reason == UNBOUND_NOT_APPROVED:
            return self._approved()
        if reason == UNBOUND_LEDGER_HELD and self._ledger_state_dir is not None:
            return foreign_ledger_holder(self._ledger_state_dir) is None
        return False

    def _approved(self) -> bool:
        if self._release_id is None or self._manifest_sha256 is None:
            return False
        try:
            return bool(
                self._store.runtime_release_approved(
                    self._release_id, self._manifest_sha256
                )
            )
        except Exception:  # noqa: BLE001 - an unreadable decision is not approval
            return False

    @property
    def bound(self) -> bool:
        return self.health().bound

    # -- the launch path ---------------------------------------------------

    def _environment(self) -> dict[str, str]:
        assert self._descriptor is not None
        # ⟦AMD-4⟧ The bot token is scoped to the window, not the process; the
        # provider keys are not, because they are not the authority the window
        # grants. See `PROVIDER_SECRET_ALIASES`.
        bindings = dict(transport_credential_bindings(self._store))
        bindings.update(
            {
                alias: PROVIDER_SECRET_ALIASES[alias]
                for alias in self._provider_refs
            }
        )
        return worker_environment(
            state_dir=self._descriptor.state_dir,
            # Overridden by the supervisor with its own per-launch token; a
            # placeholder here rather than a constant, so a future change that
            # stops overriding it cannot silently ship a fixed one.
            token=secrets.token_urlsafe(32),
            secret_refs=self._secret_refs | self._provider_refs,
            credential_bindings=bindings,
            resolver=self._resolver,
            base_urls=self._base_urls,
        )

    def _agent_options(self) -> dict[str, object]:
        """Which model provider a turn talks to, from the operator's own config.

        ⟦P5.4c⟧ The fork's own auto-detection cannot answer this inside the
        seatbelt -- `config.yaml` sits under a HERMES_HOME whose `hooks`
        directory is deny-write, so `ensure_hermes_home()` fails and every leg
        of the chain comes back empty, including the one that reads
        `OPENAI_BASE_URL`. Observed on the real gen 9 slot: `AIAgent.__init__`
        raises "No LLM provider configured" before any request leaves the
        sandbox. So the product says it, from the same `secret_refs` alias and
        the same allowlisted base URL the worker's environment already carries.

        An installation that names no provider gets `{}`, which is exactly what
        the previous behaviour was -- this adds a way to answer, never a
        default answer.

        ⟦BLOCK-1⟧ The endpoint now comes from `[runtime] base_url` on every
        deployed path, and `[runtime] provider` says which of the fork's API
        modes to take: `custom` is an OpenAI-compatible endpoint, `anthropic`
        selects the Messages API. A reference that cannot be resolved is a
        typed refusal rather than `{}` -- returning nothing here produced the
        identical symptom as naming no provider at all, on every surface.
        """

        for alias, url_key in PROVIDER_ENDPOINTS.items():
            reference = self._provider_refs.get(alias)
            base_url = self._base_urls.get(url_key)
            if not reference or not base_url:
                continue
            try:
                secret = self._resolver.resolve(alias, reference)
            except SecretResolutionError as exc:
                raise ManagedWorkerUnavailable(REFUSED_PROVIDER_CREDENTIAL) from exc
            options: dict[str, object] = {
                "provider": self._provider_name,
                "base_url": base_url,
                "api_key": secret.reveal(),
            }
            if self._model:
                options["model"] = self._model
            return options
        return {}

    # -- the seams the turn bridge binds to --------------------------------

    def backend(self, *, window_required: bool = True):
        """The `ManagedHermesBackend` a turn runs in, after the same checks.

        ⟦P5.4c⟧ `acquire()` is the whole launch policy -- gate, approval,
        credential -- and it returns the RPC supervisor because that is what a
        transport frame needs. A turn needs the backend around it, so this asks
        the same question and hands back the other half. Never a second backend:
        one slot has one operation ledger and one `flock`, so a second one would
        be a second worker fighting the first for it.

        ⟦P8⟧ `window_required=False` is the turn's form: a cockpit-created run
        has no bot to speak as and needs no window, only the provider key --
        which is bound whenever the worker launches. See `acquire`.
        """

        with self._lock:
            self.acquire(window_required=window_required)
            return self._backend

    @property
    def releases(self):
        """The updater service, which is the orchestrator's `ReleasePinPort`.

        The same object the binding resolved the ACTIVE release through, so the
        pin an attempt commits and the identity the running worker measured come
        from one authority rather than two that could disagree.
        """

        return self._service

    def acquire(self, *, window_required: bool = True):
        """The gate, the approval, and the launch -- in that order, every time.

        ⟦P8⟧ `window_required` is what a transport frame needs and a turn does
        not. The window is the decision about who may speak as the bot
        (⟦AMD-4⟧); whether a turn may run at all is migration 12's decision,
        which the bridge reads before it asks for a backend. A turn acquired
        outside a window launches the worker from the environment
        `transport_credential_bindings` builds with the gate shut -- provider
        key, no bot token -- and a window that opens afterwards must not
        inherit that process: the first frame that needs the token closes it
        and launches again, so the window's worker is one that holds it.
        """

        with self._lock:
            reason = self._rederive_if_cleared()
            if reason is not None:
                raise ManagedWorkerUnavailable(reason)
            windowed = bool(self._store.telegram_dispatch_enabled())
            if window_required and not windowed:
                raise ManagedWorkerUnavailable(REFUSED_GATE_CLOSED)
            assert self._release_id is not None
            assert self._manifest_sha256 is not None
            if not self._store.runtime_release_approved(
                self._release_id, self._manifest_sha256
            ):
                # A worker already running on a release the operator has since
                # revoked keeps running until this point. It is closed here, so
                # the revocation reaches the process and not merely the ledger.
                self._release_locked()
                raise ManagedWorkerUnavailable(REFUSED_NOT_APPROVED)
            if (
                window_required
                and self._backend is not None
                and not self._launched_windowed
            ):
                # The worker a turn launched before this window holds no bot
                # token. `begin_window` normally closed it already; a turn that
                # launched between the window opening and its first frame
                # lands here. Closed and derived like every other close, and
                # the proof forgotten for the same reason `begin_window`
                # forgets one: it is a pre-window process's, not this window's.
                # A turn still running on it ends typed through the runtime's
                # own uncertain-outcome path, which is why this is logged.
                _log.warning(
                    "managed worker launched outside a window pid=%s is closed "
                    "for the window; a turn in flight on it ends typed",
                    getattr(self._process, "pid", None),
                )
                self._release_locked()
                self._last_release = None
            if self._backend is None:
                assert self._descriptor_path is not None
                self._backend = self._backend_factory(
                    self._descriptor_path,
                    environment_factory=self._environment,
                    egress_port=self._egress_port,
                    agent_options_factory=self._agent_options,
                )
                # This acquisition's own reading of the gate. `_environment`
                # re-reads it at the launch below (⟦AMD-4⟧: a relaunch must
                # build from the gate as it is THEN), so the two can disagree
                # in one direction only -- a gate that opened in between binds
                # the token while this says it did not -- and the next
                # window-required acquisition corrects that with one relaunch.
                self._launched_windowed = windowed
            try:
                supervisor = self._backend.worker()  # type: ignore[union-attr]
            except (WorkerEnvironmentError, SecretResolutionError) as exc:
                # The gate is open and the operator named no usable reference
                # for the bot token, so the worker could not be given one. A
                # typed refusal rather than a launched worker that will fail
                # every send with an unrelated provider error.
                #
                # ⟦BLOCK-1⟧ Which credential failed is the whole difference
                # between "open a window" and "fix the keychain entry", and the
                # resolver already knows: the error carries its alias.
                self._backend = None
                alias = getattr(exc, "alias", None)
                raise ManagedWorkerUnavailable(
                    REFUSED_PROVIDER_CREDENTIAL
                    if alias in PROVIDER_SECRET_ALIASES
                    else REFUSED_NO_CREDENTIAL
                ) from exc
            process = supervisor.process
            if process is not self._process:
                # ⟦P5.6⟧ Every launch and relaunch, by pid, so a window's
                # "the worker pid never changed" is a log fact and not a
                # recollection.
                _log.info(
                    "managed worker launched pid=%s release=%s",
                    getattr(process, "pid", None),
                    self._release_id,
                )
            self._process = process
            return supervisor

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> object:
        supervisor = self.acquire()
        return supervisor.request(
            method,
            dict(params),
            timeout=(
                DEFAULT_FRAME_TIMEOUT_SECONDS if timeout is None else float(timeout)
            ),
        )

    # -- the release action ------------------------------------------------

    def begin_window(self) -> None:
        """Forget the previous window's proof, so it cannot be read as this one's.

        ⟦P8⟧ And close a worker a turn launched before the window: it holds no
        bot token, so the window must not start with it attached -- the
        poller's first frame would otherwise find it and have to relaunch. A
        turn still running on it ends typed, and is logged for that reason.
        """

        with self._lock:
            if self._backend is not None and not self._launched_windowed:
                _log.warning(
                    "managed worker launched outside a window pid=%s is closed "
                    "at window open; a turn in flight on it ends typed",
                    getattr(self._process, "pid", None),
                )
                self._release_locked()
            self._last_release = None

    def release(self) -> ReleaseProof:
        """D-P5-5's release action, with the derivation it demands.

        `poller_stopped` is three observations conjoined: `close()` returned,
        the process is gone, and the amended `lsof -nP -a -p <pid> -i TCP -s
        TCP:ESTABLISHED` predicate reports nothing. A window in which no worker
        was ever launched satisfies all three by construction, and says so.
        """

        with self._lock:
            return self._release_locked(token_holders_only=True)

    def _release_locked(self, *, token_holders_only: bool = False) -> ReleaseProof:
        """Close whatever is running and DERIVE, on every path that closes.

        Every internal close derives too, and remembers the answer. The real
        acceptance found why: a revocation closed the worker without recording
        anything, so the `close-window` that followed found no backend and wrote
        "no managed worker was launched" into the audit -- of a window in which
        one demonstrably had been. A proof is only evidence if the code that
        ends the process is the code that observes it.

        ⟦P8⟧ `token_holders_only` is the window's release, and it decides the
        PROOF only: a worker a turn launched outside a window never held the
        token, so the window's proof is the one remembered from the worker
        that did (or "nothing held the token" when none did). The CLOSE is
        unconditional -- no path returns a proof while a backend this object
        owns is still running; `close-window` must leave no `runtime_worker.py`
        behind (cutover step 5), whoever launched it. A turn in flight on it
        ends typed, and is logged for that reason.
        """

        if (
            token_holders_only
            and self._backend is not None
            and not self._launched_windowed
        ):
            backend = self._backend
            process = self._process
            pid = getattr(process, "pid", None)
            _log.warning(
                "managed worker launched outside a window pid=%s is closed at "
                "window release; a turn in flight on it ends typed",
                pid,
            )
            backend.close()  # type: ignore[union-attr]
            # ⟦V-4⟧ Only once the close returned. A close that raised (the
            # supervisor's `wait` after `kill` is not guarded) leaves the
            # worker owned, so `health()` still reports it and the next
            # release reaches it again -- rather than an orphan nothing can.
            self._backend = None
            self._process = None
            # ⟦V-4⟧ Derived the way the full path derives, from the process
            # this code just ended: the window's remembered proof below is
            # about the token holder and is not replaced, but the observation
            # is recorded beside it rather than assumed.
            exit_status = None if process is None else process.poll()  # type: ignore[union-attr]
            sockets = None if pid is None else established_sockets(int(pid))
            _log.warning(
                "managed worker launched outside a window pid=%s closed at "
                "window release: exit_status=%s established_sockets=%s",
                pid,
                exit_status,
                "unanswered" if sockets is None else len(sockets),
            )
            if self._last_release is not None:
                return self._last_release
            proof = ReleaseProof(
                poller_stopped=True,
                worker_launched=False,
                worker_pid=None,
                exit_status=None,
                established_sockets=(),
                proof="no managed worker was launched for the window; nothing held the token",
                observed_at=_now(),
            )
            self._last_release = proof
            return proof
        backend = self._backend
        process = self._process
        self._backend = None
        self._process = None
        if backend is None:
            if self._last_release is not None:
                return self._last_release
            proof = ReleaseProof(
                poller_stopped=True,
                worker_launched=False,
                worker_pid=None,
                exit_status=None,
                established_sockets=(),
                proof="no managed worker was launched; nothing held the token",
                observed_at=_now(),
            )
            self._last_release = proof
            return proof
        pid = getattr(process, "pid", None)
        backend.close()  # type: ignore[union-attr]
        exit_status = None if process is None else process.poll()  # type: ignore[union-attr]
        sockets = None if pid is None else established_sockets(int(pid))
        _log.info(
            "managed worker released pid=%s exit_status=%s established_sockets=%s",
            pid,
            exit_status,
            "unanswered" if sockets is None else len(sockets),
        )
        gone = exit_status is not None
        # ⟦P54A-6⟧ An unanswered question is not an answer. `clear` used to
        # spend `None` as `true`, so an audit row could read `poller_stopped:
        # true` beside a proof sentence ending "unanswered".
        clear = sockets is not None and not sockets
        proof = ReleaseProof(
            poller_stopped=bool(gone and clear),
            worker_launched=True,
            worker_pid=None if pid is None else int(pid),
            exit_status=exit_status,
            established_sockets=sockets,
            proof=(
                f"close() returned; pid {pid} poll()={exit_status}; "
                f"lsof -nP -a -p {pid} -i TCP -s TCP:ESTABLISHED "
                + (
                    "unanswered"
                    if sockets is None
                    else ("empty" if not sockets else ",".join(sockets))
                )
            )[:_PROOF_LIMIT],
            sockets_observed=sockets is not None,
            observed_at=_now(),
        )
        self._last_release = proof
        return proof

    def close(self) -> None:
        with self._lock:
            self._release_locked()


class TransportWindowSupervisor:
    """The daemon's owner of the inbound poller and the window's release.

    The gate is durable state a CLI in another process writes, so the daemon
    watches it rather than being told: a window opened by `cortex transport
    enable-window` starts the poller here, and a `disable` -- or an expiry the
    operator never got to -- stops it and releases the worker.

    ⟦P54A-2⟧ That last clause used to be false. Only `close_window` released,
    so a `transport disable` (and every expiry, which no operator is present
    for by definition) stopped the poller and left the worker process running
    with the bot token in its environment -- indefinitely, if the ceremony was
    abandoned or `rollback.sh` was used, since that script never calls
    `close-window`. The release now happens on the tick that observes the
    window end, and the proof derived at that instant is remembered so the
    operator's later `close-window` records what was true when the window
    actually ended rather than what is true minutes afterwards.
    """

    #: How often the durable decision is re-read. Bounded rather than
    #: instantaneous on purpose: this is one indexed row, and a window is
    #: minutes long.
    POLL_INTERVAL_SECONDS = 1.0
    #: ⟦P5.4b/F5⟧ How many times a poller that ENDED badly is restarted inside
    #: one window. `TelegramInboundPoller.run` gives up after five consecutive
    #: failures, and the supervisor used to start a poller only on a gate
    #: transition -- so a burst of refusals inside a live window left the window
    #: open with no inbound loop and nothing saying so. Bounded rather than
    #: endless: a window is minutes long and a fault that survives five restarts
    #: is not transient.
    MAX_POLLER_RESTARTS = 5
    POLLER_BACKOFF_BASE_SECONDS = 1.0
    POLLER_BACKOFF_MAX_SECONDS = 30.0

    def __init__(
        self,
        *,
        store: ControlStore,
        worker: ManagedTransportWorker,
        rpc: GatedWorkerTransportRPC,
        handle_update: Callable[[Mapping[str, object]], object],
        interval: float | None = None,
        poller_factory: Callable[..., TelegramInboundPoller] | None = None,
        drain: object | None = None,
    ) -> None:
        self._store = store
        self._worker = worker
        self._rpc = rpc
        self._handle_update = handle_update
        self._interval = (
            self.POLL_INTERVAL_SECONDS if interval is None else float(interval)
        )
        self._poller_factory = poller_factory or TelegramInboundPoller
        # ⟦P5.4b⟧ The outbound half, driven from this same single thread: a
        # second thread claiming chunks would be a second worker id competing
        # for the same leases, which the ledger would treat as a foreign claim.
        self._drain = drain
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poller: TelegramInboundPoller | None = None
        self._poller_thread: threading.Thread | None = None
        self._window_id: str | None = None
        self.outcomes: list[str] = []
        self._restarts = 0
        self._resume_at = 0.0
        self._poller_state = "idle"
        self._terminal_reason: str | None = None
        # ⟦P5.6⟧ The failures of EVERY poller this window ran, with their
        # messages. Owned here rather than read off the poller because a
        # restart replaces the poller object and would take its log with it.
        self._failure_log: deque[dict[str, object]] = deque(
            maxlen=POLLER_FAILURE_LOG_LIMIT
        )
        self._last_error_detail: str | None = None
        self._drain_failures = 0
        self._drain_last_failure: dict[str, object] | None = None

    # -- the watch loop ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watch, name="cortexd-transport-window", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)
        self.stop_poller()
        self._worker.close()

    def _watch(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001 - a watch loop that dies is silent
                continue

    def reconcile(self) -> None:
        """One observation of the durable gate, applied to the running process."""

        record = self._store.transport_activation("telegram")
        window_id = None if record is None else record.id
        with self._lock:
            if window_id != self._window_id:
                if self._window_id is not None:
                    _log.info(
                        "transport window ended window_id=%s outcomes=%s restarts=%s",
                        self._window_id,
                        list(self.outcomes[-5:]),
                        self._restarts,
                    )
                    self._stop_poller_locked()
                    if window_id is None:
                        # ⟦P54A-2⟧ The window ended, so the authority to speak
                        # as the bot ended with it and the process holding the
                        # token has to go. Reached in the established
                        # supervisor -> worker order, and never held for long:
                        # the worker's lock is taken across an acquisition, not
                        # across a turn.
                        self._release_worker_locked()
                self._window_id = window_id
                self._restarts = 0
                self._resume_at = 0.0
                self._terminal_reason = None
                self._poller_state = "idle"
                if window_id is not None:
                    _log.info("transport window opened window_id=%s", window_id)
                    self._worker.begin_window()
                    # ⟦P5.6⟧ The line's counters start at zero with the window
                    # (batch D D-4). The drain is NOT per window -- it is built
                    # once per daemon and its `delivered`/`categories` are
                    # daemon-lifetime ledger state that must not be zeroed
                    # (`manual_required` from a previous window is still a
                    # lost answer); cutover step 4b reads those as deltas.
                    serializer = getattr(self._rpc, "serializer", None)
                    if serializer is not None:
                        serializer.reset_window()
                    # ⟦Batch F P56-OBS-6⟧ The failure ring, the error text and
                    # the drain's failure count are the window's story, read at
                    # cutover step 4b; a verification window's entries must not
                    # be mistaken for the live window's. Cleared here, under
                    # the lock, so nothing appended between the two reads.
                    self._failure_log.clear()
                    self._last_error_detail = None
                    self._drain_failures = 0
                    self._drain_last_failure = None
                    self._start_poller_locked()
            elif window_id is not None:
                self._recover_poller_locked()
            self._drain_locked()

    def _release_worker_locked(self) -> None:
        """Close the worker and keep the derivation, without dying trying.

        A release that raised here would take the whole watch loop's tick with
        it -- `_watch` swallows the exception and the next tick would find
        `_window_id` already updated, so nothing would ever try again.
        """

        try:
            self._worker.release()
        except Exception:  # noqa: BLE001 - a release that failed is not a dead loop
            return

    def _drain_locked(self) -> None:
        """One outbound pass, and only while a window authorizes one.

        Outside a window the gated RPC would refuse every frame anyway; not
        entering at all is what keeps a closed gate from consuming claims and
        moving chunks through states for sends that cannot happen.
        """

        if self._drain is None or self._window_id is None:
            # ⟦P5.6⟧ A pass that never reports leaves the line's last answer
            # standing (batch D D-3): outside a window there is no outbound
            # work the poller should stay short for.
            self._report_outbound_pending(False)
            return
        try:
            self._drain.drain()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - one bad pass is not a dead loop
            self._drain_failures += 1
            self._drain_last_failure = {
                "at": _now(),
                "error": type(exc).__name__,
                "detail": redact(str(exc)),
            }
            _log.warning(
                "transport drain pass failed: %s: %s",
                type(exc).__name__,
                self._drain_last_failure["detail"],
            )
            return

    def _report_outbound_pending(self, pending: bool) -> None:
        serializer = getattr(self._rpc, "serializer", None)
        if serializer is not None:
            serializer.outbound_pending(pending)

    def _recover_poller_locked(self) -> None:
        """⟦F5⟧ A window that is still in force must still have a poller."""

        thread = self._poller_thread
        if thread is not None and thread.is_alive():
            self._poller_state = "running"
            return
        outcome = self.outcomes[-1] if self.outcomes else None
        if outcome in {"stopped", "gate_closed", None}:
            # The loop ended because it was asked to, or because it observed the
            # same gate this method just read as open -- a race the next tick
            # resolves. Neither is a fault to recover from.
            self._poller_state = "idle"
            return
        if self._restarts >= self.MAX_POLLER_RESTARTS:
            if self._poller_state != "exhausted":
                _log.error(
                    "telegram poller exhausted its %s restarts; last error %s: %s",
                    self.MAX_POLLER_RESTARTS,
                    self._last_error(),
                    self._last_error_detail,
                )
            self._poller_state = "exhausted"
            self._terminal_reason = (
                f"{outcome} after {self._restarts} restarts; "
                f"last error {self._last_error()}"
            )
            return
        now = time.monotonic()
        if now < self._resume_at:
            self._poller_state = "backoff"
            return
        self._restarts += 1
        self._resume_at = now + min(
            self.POLLER_BACKOFF_BASE_SECONDS * (2 ** (self._restarts - 1)),
            self.POLLER_BACKOFF_MAX_SECONDS,
        )
        self._terminal_reason = None
        _log.warning(
            "telegram poller restarted (%s of %s) after outcome %s; last error %s: %s",
            self._restarts,
            self.MAX_POLLER_RESTARTS,
            outcome,
            self._last_error(),
            self._last_error_detail,
        )
        self._start_poller_locked()
        self._poller_state = "running"

    def _last_error(self) -> str | None:
        poller = self._poller
        return None if poller is None else poller.last_error

    @property
    def window_id(self) -> str | None:
        return self._window_id

    @property
    def polling(self) -> bool:
        thread = self._poller_thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, object]:
        """What an operator may read about the loop. Counts, never payloads."""

        poller = self._poller
        return {
            "window_id": self._window_id,
            "poller_state": self._poller_state,
            "poller_restarts": self._restarts,
            "poller_outcomes": list(self.outcomes[-5:]),
            "poller_terminal_reason": self._terminal_reason,
            "polls": 0 if poller is None else poller.polls,
            "handled": 0 if poller is None else poller.handled,
            "poller_failures": 0 if poller is None else poller.failures,
            "poller_last_error": None if poller is None else poller.last_error,
            # ⟦P5.6⟧ The message behind the class, and the last ten failures
            # of every poller this window ran -- bounded and redacted, never
            # a payload. `poller_line_busy` is contention, not a fault (D-6).
            "poller_last_error_detail": (
                None
                if poller is None
                else getattr(poller, "last_error_detail", None)
            ),
            # ⟦Batch F P56-OBS-4⟧ `status()` deliberately takes no lock: health
            # must answer while `reconcile()` holds it across a poller join.
            # `list(deque)` is a single C-level call, atomic against the
            # poller thread's `append` under the GIL -- an argument that needs
            # revisiting on a free-threaded build.
            "poller_failure_log": [dict(entry) for entry in list(self._failure_log)],
            "poller_line_busy": (
                0 if poller is None else int(getattr(poller, "line_busy", 0))
            ),
            "poller_worker_busy": (
                0 if poller is None else int(getattr(poller, "worker_busy", 0))
            ),
            "poller_worker_busy_seconds": (
                None if poller is None else getattr(poller, "worker_busy_seconds", None)
            ),
            "drain_failures": self._drain_failures,
            "drain_last_failure": (
                None
                if self._drain_last_failure is None
                else dict(self._drain_last_failure)
            ),
            "drain": (
                None
                if self._drain is None
                else dict(self._drain.status())  # type: ignore[attr-defined]
            ),
            # ⟦P5.5⟧ Counts from the shared transport line. `sends_refused`
            # rising while `drain.delivered` also rises is contention working;
            # `sends_refused` rising with nothing delivered is the window an
            # operator has to look at.
            "transport_line": (
                None
                if getattr(self._rpc, "serializer", None) is None
                else dict(self._rpc.serializer.status())
            ),
        }

    # -- the poller --------------------------------------------------------

    def _record_poller_failure(self, entry: Mapping[str, object]) -> None:
        self._failure_log.append(dict(entry))
        detail = entry.get("detail")
        self._last_error_detail = None if detail is None else str(detail)

    def _start_poller_locked(self) -> None:
        poller = self._poller_factory(
            rpc=self._rpc,
            store=self._store,
            handle_update=self._handle_update,
            on_failure=self._record_poller_failure,
        )
        self._poller = poller
        _log.info("telegram poller started window_id=%s", self._window_id)

        def drive() -> None:
            outcome = poller.run()
            self.outcomes.append(outcome)
            _log.info(
                "telegram poller ended outcome=%s polls=%s handled=%s failures=%s",
                outcome,
                getattr(poller, "polls", None),
                getattr(poller, "handled", None),
                getattr(poller, "failures", None),
            )

        thread = threading.Thread(
            target=drive, name="cortexd-telegram-poller", daemon=True
        )
        self._poller_thread = thread
        thread.start()

    def stop_poller(self) -> None:
        with self._lock:
            self._stop_poller_locked()

    def _stop_poller_locked(self) -> None:
        poller = self._poller
        thread = self._poller_thread
        self._poller = None
        self._poller_thread = None
        if poller is not None:
            poller.stop()
        if thread is not None:
            thread.join(timeout=10.0)
        self._poller_state = "idle"

    # -- the operator's close ----------------------------------------------

    def close_window(self, *, window_id: str, actor_id: str) -> dict[str, object]:
        """Derive `poller_stopped` and record the close, in that order.

        Every refusal comes first and touches nothing: `close-window` while a
        window still authorizes a send would otherwise stop the poller and kill
        the worker before discovering the store was going to refuse.

        ⟦P54A-4⟧ All three of the store's preconditions are hoisted here, and
        the last one is widened: ANY window in force refuses, not only this id.
        A stale `$WINDOW` from an earlier step named a superseded decision, and
        closing it stopped the LIVE window's poller and killed its worker on
        the way to an error -- with `transport status` afterwards reporting
        only `idle`. Widened rather than narrowed to "the id this supervisor
        owns": the daemon reconciles at 1 s and cutover step 5 disables before
        it closes, so by the time the legitimate close arrives `self._window_id`
        is already `None` and an ownership test would refuse the correct close.
        """

        record = self._store.transport_activation_decision(window_id)
        if record is None:
            raise NotFound("transport activation decision", window_id)
        if record.scope != "window":
            raise InvalidTransition(str(record.scope), "closed")
        if self._store.transport_activation(record.transport) is not None:
            raise InvalidTransition("open", "closed")
        self.stop_poller()
        with self._lock:
            if self._window_id == window_id:
                self._window_id = None
        proof = self._worker.release()
        self._store.record_transport_window_closed(
            window_id=window_id,
            poller_stopped=proof.poller_stopped,
            proof=proof.proof,
            actor_id=actor_id,
        )
        return {
            "recorded": "transport_window_closed",
            "window_id": window_id,
            "derivation": proof.to_dict(),
        }
