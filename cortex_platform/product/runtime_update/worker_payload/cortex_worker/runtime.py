"""Binding the fork inside the worker, where the product cannot see it.

`_NativeHermesBackend.run` is the specification for everything below: the same
five callbacks, the same approval park, the same result shape. What changes is
where it runs. In-process, the adapter could hand the fork a Python callable and
block on a `threading.Event`; across the boundary the callbacks become frames
and the park becomes `turn.resolve`.

⟦AMD-2⟧ is enforced here rather than in the caller, because this module is the
one that performs the import: `run_agent` loads `$HERMES_HOME/.env` with
`override=True` at import time, which would overwrite the credential the parent
just revealed and can redirect a provider base URL, and the plugin loader
`exec_module`s `$HERMES_HOME/plugins/*` outside the manifest and outside
`content_tree_sha256`. The assertion has to be the last thing before the import.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from .approval import fork_choice

# The names the fork loads code, configuration or prompt text from inside
# `HERMES_HOME`, in the order the design lists them. `.env` is a file; the rest
# are directories. Hand-maintained on purpose: the product process cannot import
# fork code to derive it, and a runtime-derived list would make `profile_digest`
# non-deterministic. It is NOT the complete set of HERMES_HOME children the fork
# reads — `config.yaml`, `cli-config.yaml` and `shell-hooks-allowlist.json` are
# reached only through loaders that `ensure_hermes_home()` makes fail under this
# policy, because that call mkdirs `hooks`, which is denied.
HERMES_HOME_FORBIDDEN = (".env", "plugins", "hooks", "scripts", "skills")


class HermesHomeUnsafe(RuntimeError):
    """`HERMES_HOME` could contribute code or configuration to the fork."""


class RuntimeUnavailable(RuntimeError):
    """The fork is not importable from the slot's content root."""


def assert_hermes_home(home: Path) -> Path:
    """Refuse to run while `HERMES_HOME` holds anything the fork would execute.

    An empty directory is tolerated because the fork itself creates one at
    import time and `hermes-home` persists across worker restarts within a
    generation; a non-empty one, a file, or a symlink is not.
    """

    home = Path(home)
    try:
        details = home.lstat()
    except OSError as exc:
        raise HermesHomeUnsafe("HERMES_HOME is unavailable") from exc
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise HermesHomeUnsafe("HERMES_HOME is not a private directory")
    for name in HERMES_HOME_FORBIDDEN:
        entry = home / name
        try:
            entry_details = entry.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HermesHomeUnsafe(f"HERMES_HOME {name} is unreadable") from exc
        if not stat.S_ISDIR(entry_details.st_mode):
            raise HermesHomeUnsafe(f"HERMES_HOME carries {name}")
        if any(entry.iterdir()):
            raise HermesHomeUnsafe(f"HERMES_HOME {name} is not empty")
    return home


def _duration_ms(duration: object) -> int | None:
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration >= 0
    ):
        return round(duration * 1000)
    return None


def load_runtime(home: Path):
    """Import the fork, once the home directory has been proved inert."""

    assert_hermes_home(home)
    try:
        import hermes_state  # type: ignore[import-not-found]
        from run_agent import AIAgent  # type: ignore[import-not-found]
        from tools.terminal_tool import (  # type: ignore[import-not-found]
            set_approval_callback,
        )
    except BaseException as exc:  # noqa: BLE001 - the fork raises broadly at import
        raise RuntimeUnavailable("hermes_not_installed") from exc
    return hermes_state, AIAgent, set_approval_callback


class ForkRunner:
    """One fork import per worker process, one turn at a time through it."""

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._loaded: tuple[Any, Any, Any] | None = None
        self._db: Any = None

    def _runtime(self) -> tuple[Any, Any, Any]:
        if self._loaded is None:
            self._loaded = load_runtime(self._home)
        return self._loaded

    def _session_db(self, hermes_state: Any, path: str | None) -> Any:
        if self._db is None:
            self._db = (
                hermes_state.SessionDB(path) if path else hermes_state.SessionDB()
            )
        return self._db

    def __call__(self, request: Mapping[str, object], context) -> dict[str, object]:
        hermes_state, agent_class, set_approval_callback = self._runtime()
        session_ref = str(request.get("session_ref", ""))
        approval_index = 0
        completions: dict[str, list[tuple[bool, int | None]]] = {}

        def tool_progress(*args: Any, **kwargs: Any) -> None:
            event = str(args[0]).strip().lower() if args else ""
            if event == "tool.completed":
                name = args[1] if len(args) > 1 else None
                if isinstance(name, str) and name:
                    completions.setdefault(name, []).append(
                        (bool(kwargs.get("is_error", False)), _duration_ms(kwargs.get("duration")))
                    )
                return
            if event == "tool.progress":
                context.emit("tool.progress", {})
            elif event and event != "tool.started":
                context.emit("reasoning.available", {})

        def tool_start(tool_call_id: str, tool_name: str, arguments: Any) -> None:
            context.emit(
                "tool.started",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    # Carried so the adapter's event shape is unchanged; Control
                    # drops it before anything is written, and the projection
                    # digest excludes it.
                    "arguments": arguments if isinstance(arguments, Mapping) else {},
                },
            )

        def tool_complete(tool_call_id: str, tool_name: str, arguments: Any, result: Any) -> None:
            _ = arguments
            pending = completions.get(tool_name)
            if pending:
                is_error, duration_ms = pending.pop(0)
            else:
                is_error = isinstance(result, str) and result.lower().startswith("error")
                duration_ms = None
            context.emit(
                "tool.completed",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "is_error": is_error,
                    "duration_ms": duration_ms,
                },
            )

        def step(iteration: int, previous_tools: Any) -> None:
            _ = previous_tools
            context.emit("step.completed", {"iteration": iteration})

        def approval(command: str, description: str, *, allow_permanent: bool = True) -> str:
            """The single mapping point onto the fork's approval vocabulary.

            `allow_permanent` is ignored because this product has no permanent
            answer to give: `session` and `always` are the fork's standing
            grants, and the closed set in `approval.py` does not contain them.
            """

            nonlocal approval_index
            _ = allow_permanent
            if context.canceled:
                return fork_choice(None)
            digest = hashlib.sha256(
                f"{request.get('task_id', '')}\x1fapproval\x1f{approval_index}".encode("utf-8")
            ).hexdigest()
            approval_index += 1
            decision_id = f"decision-{digest}"
            context.emit(
                "decision.required",
                {
                    "decision_id": decision_id,
                    "decision_kind": "approval",
                    "prompt": "Approve this runtime tool action?",
                    "command": command,
                    "description": description,
                },
            )
            return fork_choice(context.await_decision(decision_id))

        def stream(text: Any) -> None:
            if isinstance(text, str) and text:
                context.emit("token.delta", {"text": text})

        options = dict(request.get("agent_options") or {})
        # ⟦AMD-5⟧: the fork has 151 gated `print` sites. fd 1 is the frame
        # stream; a single un-gated line would corrupt it. The native backend
        # never needed this because nothing was reading its stdout.
        options["quiet_mode"] = True
        options.update(
            {
                "session_id": session_ref,
                "session_db": self._session_db(hermes_state, request.get("session_db_path")),
                "parent_session_id": request.get("parent_session_ref"),
                "tool_progress_callback": tool_progress,
                "tool_start_callback": tool_start,
                "tool_complete_callback": tool_complete,
                "step_callback": step,
            }
        )
        agent = agent_class(**options)
        installed = False
        try:
            set_approval_callback(approval)
            installed = True
            context.raise_if_canceled()
            result = agent.run_conversation(
                str(request.get("user_message", "")),
                system_message=request.get("system_message"),
                conversation_history=list(request.get("conversation_history") or ()),
                task_id=request.get("task_id"),
                stream_callback=stream,
            )
        finally:
            if installed:
                set_approval_callback(None)
        final_response = result.get("final_response") if isinstance(result, Mapping) else None
        interrupted = bool(
            (result.get("interrupted", False) if isinstance(result, Mapping) else False)
            or getattr(agent, "interrupted", False)
        )
        failed = bool(
            isinstance(result, Mapping)
            and not interrupted
            and (
                result.get("failed") is True
                or result.get("partial") is True
                or ("completed" in result and result.get("completed") is False)
            )
        )
        return {
            "session_ref": str(getattr(agent, "session_id", session_ref) or session_ref),
            "final_response": (
                str(final_response)
                if final_response is not None and not interrupted and not failed
                else None
            ),
            "canceled": interrupted,
            "failed": failed,
        }
