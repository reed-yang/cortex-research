"""D1/D6: one short-lived, fully bound child per engine effect.

The supervisor is where the activation gate finally acquires the second choke
point F12 showed it was missing: when this was written the gate had exactly
one consumer, the run-dispatch check now at `orchestration/service.py:351`
(`api/app.py`, `transports/bridge.py`, `engine/capture_consumer.py` and
`engine/schedules.py` have added their own since), and
`WorkflowCoordinator` dispatched engine effects without ever asking. Every
engine dispatch path -- the coordinator, the capture consumer, a future
scheduler tick -- crosses this one function, so the check lives here rather
than in three copies.

The refusal is typed on purpose (⟦AMD-7⟧): `runtime_activation_disabled` is
accepted by `_ARTIFACT_FAILURE_RE` and `EffectPermanentlyRejected` is what
`workflows/coordinator.py:477` catches, so a bare `RuntimeError` is a
forbidden route, not merely a worse one.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from cortex_platform.product.secrets import SecretResolutionError, SecretValue
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected

from . import survivors
from .bindings import (
    EFFECT_MARKER_VARIABLE,
    CorpusBindingError,
    EngineRoots,
    research_effect_environment,
)
from .protocol import EffectRequest, read_result

CHILD_MODULE = "cortex_platform.product.engine.child"
# What a detached engine descendant looks like in `ps` output. `detached_run.py`
# spawns `[sys.executable, "-m", "cortex_research.<module>", ...]`, so the module
# invocation is the shape; matching the bare package name would also match every
# wheel path and build directory that mentions it.
ENGINE_PROCESS_NEEDLES: tuple[str, ...] = ("-m cortex_research",)
# AMD-3: the capture lease has to be at least this much longer than the effect
# lease, because a child killed at the hard timeout leaves an unknown outcome.
DEFAULT_TIMEOUT_SECONDS = 900
_KILL_GRACE_SECONDS = 5
# `stop_daemon` has a 10 s deadline (`lifecycle.py:609`) and `stop()` still
# has to join the tick thread afterwards, so the grace a shutdown can afford
# is much shorter than the one a hard timeout can.
_STOP_GRACE_SECONDS = 1.5


@dataclass(frozen=True)
class EffectExecution:
    """What one effect child did, judged by its result rather than its exit."""

    ok: bool
    operation: str
    marker: str
    engine: Mapping[str, Any] | None
    paper_dirs: tuple[str, ...]
    failure_category: str | None
    failure_message: str | None
    checkpointed: bool
    write_boundary: Mapping[str, Any]
    survivors: Mapping[str, Any]
    gdrive: Mapping[str, Any] | None
    exit_code: int | None
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "marker": self.marker,
            "engine": dict(self.engine) if self.engine else None,
            "paper_dirs": list(self.paper_dirs),
            "failure_category": self.failure_category,
            "failure_message": self.failure_message,
            "checkpointed": self.checkpointed,
            "write_boundary": dict(self.write_boundary),
            "survivors": dict(self.survivors),
            "gdrive": dict(self.gdrive) if self.gdrive else None,
            "exit_code": self.exit_code,
        }


class ResearchEffectSupervisor:
    """Run exactly one engine operation in a child that inherits nothing."""

    def __init__(
        self,
        *,
        store: Any,
        roots: EngineRoots,
        python_executable: Path | None = None,
        secret_provider: Callable[[], Mapping[str, SecretValue]] | None = None,
        skip_embed: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        watch_roots: Mapping[str, Path] | None = None,
        literal_overrides: Mapping[str, str] | None = None,
        read_only_roots: tuple[Path, ...] = (),
    ) -> None:
        self._store = store
        self._roots = roots
        self._python = Path(python_executable or sys.executable)
        self._secret_provider = secret_provider
        self._skip_embed = skip_embed
        if not 1 <= int(timeout_seconds) <= 3_600:
            raise ValueError("timeout_seconds must be between 1 and 3600")
        self._timeout = int(timeout_seconds)
        self._watch_roots = dict(watch_roots or {})
        # Only slots the table already declares can move (`bindings.py` refuses
        # anything else), so the child environment stays describable by it.
        self._literal_overrides = dict(literal_overrides or {})
        self._read_only_roots = read_only_roots
        # The child of the effect currently running, so a daemon shutdown can
        # end it rather than orphan it.
        self._in_flight: subprocess.Popen[str] | None = None

    @property
    def timeout_seconds(self) -> int:
        return self._timeout

    @property
    def in_flight(self) -> subprocess.Popen[str] | None:
        """The effect child running right now, if any."""

        return self._in_flight

    def terminate_in_flight(self, *, grace_seconds: float = _STOP_GRACE_SECONDS) -> None:
        """End the running effect child instead of letting it outlive us.

        `cortexd` joins its tick thread with a deadline far shorter than the
        child's hard timeout, so without this the child kept running -- writing
        the copied `research.db` -- after `cortex stop` returned. Killing it
        first turns that orphan into the killed-child path V3 and ⟦AMD-5⟧
        already model: the supervisor reads no result, judges `outcome_unknown`,
        and the capture waits for the operator.
        """

        process = self._in_flight
        if process is None or process.poll() is not None:
            return
        process.terminate()
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.02)
        if process.poll() is None:
            process.kill()

    def require_activation(self) -> None:
        """D6's choke point, re-evaluated per effect.

        A bounded window expires as a stored fact on read (`control/store.py:1959`), so a
        batch can straddle its own expiry; the answer has to be asked again for
        every child rather than once for the batch.
        """

        if not self._store.runtime_dispatch_enabled():
            raise EffectPermanentlyRejected("runtime_activation_disabled")

    def _environment(self, marker: str) -> dict[str, str]:
        try:
            resolved = self._secret_provider() if self._secret_provider else {}
        except SecretResolutionError as error:
            # The reference was well formed and nothing stood behind it, or the
            # keychain refused. Never carries the value.
            raise EffectPermanentlyRejected("adapter_unavailable") from error
        return research_effect_environment(
            roots=self._roots,
            secrets=resolved,
            effect_marker=marker,
            skip_embed=self._skip_embed,
            literal_overrides=self._literal_overrides,
        )

    def run(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        watch_roots: Mapping[str, Path] | None = None,
    ) -> EffectExecution:
        self.require_activation()
        marker = _secrets.token_hex(16)
        environment = self._environment(marker)
        try:
            self._roots.prepare()
        except CorpusBindingError as error:
            # The two corpus names cannot be made to answer one directory,
            # so an ingest would write outside the adopted corpus. Refuse
            # before the child rather than split the corpus (F4).
            raise EffectPermanentlyRejected("adapter_unavailable") from error

        run_root = self._roots.state / "effects" / marker
        run_root.mkdir(parents=True, exist_ok=True)
        result_path = run_root / "result.json"
        request = EffectRequest(
            operation=operation,
            payload=dict(payload or {}),
            marker=marker,
            result_path=str(result_path),
            research_db=str(self._roots.research_db),
            state_dir=str(self._roots.state),
            write_roots=tuple(str(root) for root in self._roots.write_roots),
            watch_roots={
                name: str(root)
                for name, root in {**self._watch_roots, **(watch_roots or {})}.items()
            },
        )
        baseline = survivors.snapshot_processes()
        prefix: list[str] = []
        if self._read_only_roots:
            from ..readings.sandbox import read_only_profile

            prefix = ["/usr/bin/sandbox-exec", "-p", read_only_profile(self._read_only_roots)]
        try:
            process = subprocess.Popen(
                prefix + [
                    str(self._python),
                    "-I",
                    "-B",
                    "-X",
                    "utf8",
                    "-m",
                    CHILD_MODULE,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=environment,
                cwd=str(self._roots.state),
                close_fds=True,
            )
        except OSError as error:
            raise EffectPermanentlyRejected("adapter_unavailable") from error

        self._in_flight = process
        timed_out = False
        stderr = ""
        try:
            try:
                _, stderr = process.communicate(
                    json.dumps(request.to_dict()), timeout=self._timeout
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                try:
                    _, stderr = process.communicate(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover - kill is final
                    stderr = ""
        finally:
            self._in_flight = None
        exit_code = process.returncode

        report = survivors.scan(
            state_dir=self._roots.state,
            marker=marker,
            needles=(marker, *ENGINE_PROCESS_NEEDLES, str(self._roots.state)),
            baseline=baseline,
            exclude=(process.pid,),
        )
        return self._judge(
            request=request,
            result_path=result_path,
            timed_out=timed_out,
            exit_code=exit_code,
            stderr=stderr,
            report=report,
        )

    def _judge(
        self,
        *,
        request: EffectRequest,
        result_path: Path,
        timed_out: bool,
        exit_code: int | None,
        stderr: str,
        report: survivors.SurvivorReport,
    ) -> EffectExecution:
        """Judge by the result document, never by the exit code (⟦AMD-4⟧)."""

        tail = "\n".join((stderr or "").splitlines()[-20:])
        try:
            raw = read_result(result_path)
        except (OSError, ValueError):
            raw = None
        if timed_out or raw is None:
            # The child may have committed and lost the reply. That is the one
            # outcome that must never be retried: a duplicate ingest mints a
            # duplicate paper_dir.
            return EffectExecution(
                ok=False,
                operation=request.operation,
                marker=request.marker,
                engine=None,
                paper_dirs=(),
                failure_category="outcome_unknown",
                failure_message=(
                    "child exceeded its hard timeout"
                    if timed_out
                    else "child produced no readable result"
                ),
                checkpointed=False,
                write_boundary={"ok": True, "violations": []},
                survivors=report.to_dict(),
                gdrive=None,
                exit_code=exit_code,
                stderr_tail=tail,
            )
        failure = raw.get("failure") or {}
        ok = bool(raw.get("ok"))
        category = failure.get("category")
        if ok and not report.clean:
            # V3: a survivor is never a success path -- the refusal arrives
            # after the write, so the outcome is unknown rather than failed.
            ok, category = False, "outcome_unknown"
        child_report = raw.get("survivors") or {}
        merged = {
            "child": child_report,
            "supervisor": report.to_dict(),
        }
        return EffectExecution(
            ok=ok,
            operation=request.operation,
            marker=request.marker,
            engine=raw.get("engine"),
            paper_dirs=tuple(raw.get("paper_dirs") or ()),
            failure_category=None if ok else (category or "outcome_unknown"),
            failure_message=None if ok else failure.get("message"),
            checkpointed=bool(raw.get("checkpointed")),
            write_boundary=raw.get("write_boundary") or {},
            survivors=merged,
            gdrive=raw.get("gdrive"),
            exit_code=exit_code,
            stderr_tail=tail,
        )

    def discard(self, marker: str) -> None:
        """Remove one effect's scratch directory once its result is durable."""

        shutil.rmtree(self._roots.state / "effects" / marker, ignore_errors=True)
