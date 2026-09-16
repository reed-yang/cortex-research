"""Managed tool integration; OS confinement remains the security boundary."""

from __future__ import annotations

import errno
import json
import os
import subprocess

TOOL_POLICY_VERSION = 1
_CHILD_ENVIRONMENT = frozenset({
    "HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "COLORTERM", "TZ", "SHELL", "PWD", "HERMES_HOME", "TERMINAL_CWD",
    "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE",
})


def child_environment(environment):
    """Do not forward provider, transport or private protocol credentials."""
    source = os.environ if environment is None else environment
    return {key: value for key, value in source.items() if key in _CHILD_ENVIRONMENT}


def install_subprocess_environment():
    """Apply before importing tools, including their security helper launchers."""
    original = subprocess.Popen
    if getattr(original, "_cortex_environment", False):
        return

    class ManagedPopen(original):
        _cortex_environment = True

        def __init__(self, *args, **kwargs):
            # Popen's env is positional argument 11 (including args itself).
            if len(args) > 10:
                positional = list(args)
                positional[10] = child_environment(positional[10])
                args = tuple(positional)
            else:
                kwargs["env"] = child_environment(kwargs.get("env"))
            super().__init__(*args, **kwargs)

    subprocess.Popen = ManagedPopen


def install_terminal_denials():
    """Make a permanent OS refusal a command result, avoiding fork retries."""
    from tools.environments.local import LocalEnvironment

    original = LocalEnvironment.execute
    if getattr(original, "_cortex_denials", False):
        return

    def execute(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EACCES}:
                raise
            return {
                "output": "Permission denied by the managed runtime policy; retrying cannot grant access.",
                "returncode": 126,
            }

    execute._cortex_denials = True
    LocalEnvironment.execute = execute


PRIVATE_ENVIRONMENT_FD = "CORTEX_PRIVATE_ENVIRONMENT_FD"
PRIVATE_ENVIRONMENT_KEYS = frozenset({
    "CORTEX_WORKER_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN_RESEARCH",
})
MAX_PRIVATE_ENVIRONMENT_BYTES = 65536


def receive_private_environment():
    """Receive credentials after exec so the kernel's startup env has none.

    macOS KERN_PROCARGS2 exposes a same-user process's initial stack even when
    os.environ later removes a key. An inherited anonymous pipe avoids placing
    these values on that stack in the first place; it closes before tools load.
    """
    raw = os.environ.pop(PRIVATE_ENVIRONMENT_FD, None)
    if raw is None:
        return
    if os.environ.get("CORTEX_LOCAL_TOOLS") != "1" or not raw.isdigit() or int(raw) < 3:
        raise ValueError("Invalid private environment channel")
    descriptor = int(raw)
    os.set_inheritable(descriptor, False)
    with os.fdopen(descriptor, "rb") as handle:
        payload = handle.read(MAX_PRIVATE_ENVIRONMENT_BYTES + 1)
    if len(payload) > MAX_PRIVATE_ENVIRONMENT_BYTES:
        raise ValueError("Private environment exceeds its bound")
    values = json.loads(payload)
    if (not isinstance(values, dict) or not set(values) <= PRIVATE_ENVIRONMENT_KEYS
            or any(not isinstance(v, str) or "\x00" in v for v in values.values())):
        raise ValueError("Invalid private environment fields")
    os.environ.update(values)
