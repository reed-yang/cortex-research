"""Assemble the engine boundary for one running product instance.

This is the only place the pieces meet: a `PathRegistry`, the corpus asset root
`control.db` already carries, the config's `secret_refs`, and the supervisor
that runs children. Assembly is deliberately fail-closed -- an installation that
has not adopted a corpus gets no engine service at all rather than a service
pointed at a guessed directory.

⟦AMD-8⟧'s credential rule lives here: under the supervised path only
`keychain://` references resolve, because a cold start cannot depend on an
interactive shell's environment. `env://` stays available for a deliberate
foreground run, which is what `keychain_only=False` means.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cortex_platform.product.control.errors import NotFound
from cortex_platform.product.paths import PathRegistry
from cortex_platform.product.secrets import SecretResolutionError, SecretResolver, SecretValue

from .bindings import EngineRoots, engine_secret_aliases
from .capture_consumer import MACHINE_ACTOR, CaptureConsumer, LeasePlan
from .port import ProductResearchEngine
from .schedules import ResearchScheduleTick, read_live_jobs, seed_schedules
from .supervisor import ResearchEffectSupervisor

CORPUS_ROOT_ID = "research-corpus"
DEFAULT_TICK_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class EngineService:
    """Everything one instance needs to drain captures, already wired."""

    roots: EngineRoots
    supervisor: ResearchEffectSupervisor
    engine: ProductResearchEngine
    consumer: CaptureConsumer
    tick: ResearchScheduleTick
    # ⟦AMD-8⟧ The aliases this instance configured and cannot use. Never the
    # references themselves, and never a value.
    dropped_secret_aliases: tuple[str, ...] = ()


def unusable_secret_aliases(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Every configured engine alias the supervised daemon will not resolve.

    ⟦AMD-8⟧ is right that `env://` must not be read under launchd. It was the
    silence that was wrong: `validate_config` accepts an `env://` reference and
    `config.py` advertises the syntax, while `keychain_only` is never passed by
    any caller in this repository -- so the "deliberate foreground path" has no
    production call site and every such reference is dropped unconditionally,
    on no surface the operator can see.
    """

    return tuple(
        sorted(
            alias
            for alias, reference in dict(config.get("secret_refs") or {}).items()
            if alias in engine_secret_aliases()
            and not str(reference).startswith("keychain://")
        )
    )


def _secret_provider(
    config: Mapping[str, Any], *, keychain_only: bool
) -> tuple[Any, tuple[str, ...]]:
    references = {
        alias: reference
        for alias, reference in dict(config.get("secret_refs") or {}).items()
        if alias in engine_secret_aliases()
    }
    dropped = unusable_secret_aliases(config) if keychain_only else ()
    if keychain_only:
        # A reference the supervised path cannot honour is dropped rather than
        # attempted: resolving it would depend on whatever environment launchd
        # happened to give the daemon, which is the opposite of a binding. The
        # drop is now reported (`dropped`) rather than performed in silence.
        references = {
            alias: reference
            for alias, reference in references.items()
            if reference.startswith("keychain://")
        }

    def provide() -> dict[str, SecretValue]:
        # An `env://` reference means "read it from this process's
        # environment", so the supervised path -- which must not depend on
        # whatever launchd happened to export -- gets an empty one, and the
        # deliberate foreground path gets the real one.
        resolver = SecretResolver(environment={} if keychain_only else os.environ)
        return resolver.resolve_all(references)

    return provide, dropped


def build_engine_service(
    *,
    store: Any,
    paths: PathRegistry,
    config: Mapping[str, Any] | None = None,
    corpus_root_id: str = CORPUS_ROOT_ID,
    keychain_only: bool = True,
    literal_overrides: Mapping[str, str] | None = None,
    leases: LeasePlan | None = None,
) -> EngineService | None:
    """Wire the boundary, or return None when this installation has no corpus."""

    try:
        root = store.get_asset_root(corpus_root_id)
    except (NotFound, ValueError):
        return None
    if not root.enabled:
        return None
    plan = leases or LeasePlan()
    roots = EngineRoots.resolve(paths, corpus_root=Path(root.private_path))
    provider, dropped = _secret_provider(config or {}, keychain_only=keychain_only)
    supervisor = ResearchEffectSupervisor(
        store=store,
        roots=roots,
        secret_provider=provider,
        timeout_seconds=plan.child_timeout_seconds,
        # ⟦AMD-1⟧ Without this the child skips its gdrive block entirely and the
        # digest pair is never COMPUTED, not merely never recorded. Only the
        # acceptance ever set it, by hand.
        watch_roots=roots.watch_roots,
        literal_overrides=literal_overrides,
    )
    holder: dict[str, CaptureConsumer] = {}
    engine = ProductResearchEngine(
        store=store,
        supervisor=supervisor,
        roots=roots,
        corpus_root_id=corpus_root_id,
        actor_id=MACHINE_ACTOR,
        payload_resolver=lambda source_id: holder["consumer"].payload_for(source_id),
    )
    consumer = CaptureConsumer(store=store, engine=engine, leases=plan)
    holder["consumer"] = consumer
    return EngineService(
        roots=roots,
        supervisor=supervisor,
        engine=engine,
        consumer=consumer,
        tick=ResearchScheduleTick(store=store, consumer=consumer),
        dropped_secret_aliases=dropped,
    )


class ResearchScheduleRunner:
    """Run the bounded tick on its own thread, so cortexd keeps serving.

    Nothing detaches: the thread is owned, joined on stop, and the child it
    spawns is awaited inside the tick. This is the whole of P4.3's daemon
    wiring, kept in one class so `daemon.py` gains three lines rather than a
    scheduler.
    """

    def __init__(
        self,
        *,
        service: EngineService,
        store: Any,
        interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
    ) -> None:
        self._service = service
        self._store = store
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.failures = 0
        self.last_failure: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        seed_schedules(self._store, reading=read_live_jobs())
        self._thread = threading.Thread(
            target=self._loop, name="research-schedule-tick", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._service.tick.run()
                self.ticks += 1
            except SecretResolutionError:
                # A missing credential is an operator problem, not a crash: the
                # capture it would have consumed is already recorded refused.
                self._note_failure("SecretResolutionError")
                continue
            except Exception as error:  # noqa: BLE001 - not fatal to cortexd
                self._note_failure(type(error).__name__)
                continue

    def _note_failure(self, kind: str) -> None:
        """A tick that fails every interval must not do so silently.

        Only the exception's type is recorded. A message carries whatever the
        engine put in it and this line reaches the daemon's stderr, so the type
        is the whole of what is safe to say here.
        """

        self.failures += 1
        self.last_failure = kind
        print(f"research-schedule-tick failed: {kind}", file=sys.stderr, flush=True)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        # Before the join, not after: joining with a deadline long enough to
        # outlast the child's own hard timeout would break `cortex stop`,
        # `restart`, `doctor` and every cutover step whenever a drain is in
        # flight, and joining with a short one used to leave the child running.
        self._service.supervisor.terminate_in_flight()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def start_research_schedules(
    *, store: Any, paths: PathRegistry, config: Mapping[str, Any] | None = None
) -> ResearchScheduleRunner | None:
    """The daemon's whole engine wiring: build if possible, start, or do nothing."""

    if config is None:
        from cortex_platform.product.config import load_config

        try:
            config = load_config(paths.config_file) if paths.config_file.is_file() else {}
        except Exception:  # noqa: BLE001 - a bad config must not stop cortexd
            config = {}
    try:
        service = build_engine_service(store=store, paths=paths, config=config)
        if service is None:
            return None
        runner = ResearchScheduleRunner(service=service, store=store)
        runner.start()
    except Exception as error:  # noqa: BLE001 - engine wiring must not stop cortexd
        # The same guard the config read three lines above already carries. The
        # type only, never the message: `lifecycle.py` sends this stream to
        # `paths.daemon_log_file` and `cortex start` reports nothing but
        # `cortexd readiness timed out`, so this line is the whole diagnosis an
        # operator gets -- and it must not carry engine text.
        print(
            f"engine wiring failed: {type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return None
    return runner
