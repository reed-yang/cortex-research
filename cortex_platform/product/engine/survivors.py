"""V3: prove no `start_new_session` descendant outlived the effect child.

`killpg` is ruled out as unsound -- a `setsid` grandchild is already in its own
session and process group -- so detection is the mechanism, and any survivor is
`outcome_unknown` rather than a refusal that arrives after the write.

Two signals. The engine's own registry of detached runs was
`CORTEX_RESEARCH_STATE_DIR/detached/*.lock`, each holding the pid it belongs to;
the module that wrote those locks is not part of the supported surface, so the
registry is now expected to be empty and a lock found there is itself a
finding. The second signal is a process scan, and it is the UNION of two
independent matches over the
processes that appeared since a snapshot taken before the spawn: the bound
roots and the engine package in the process's arguments, and the effect's own
marker in the process's environment.

The union is load-bearing. `KERN_PROCARGS2` is readable for an ordinary
same-uid child on darwin -- the platform does not refuse it -- but it returns a
short blob with no environment for a process whose environment the kernel
strips (a SIP platform binary such as `/bin/sh`). Intersecting the two matches,
which is what this module used to do, would therefore turn a survivor the
argument scan caught into a silent pass. Nothing here may ever narrow the
candidate set; `environment_scan` reports which matches contributed rather than
whether some capability probe succeeded.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROCARGS2 = 49
_PS_TIMEOUT = 10.0


@dataclass(frozen=True)
class ProcessSnapshot:
    """Every live process this user owns, with its arguments."""

    processes: Mapping[int, str]

    def matching(self, needles: Sequence[str]) -> dict[int, str]:
        return {
            pid: arguments
            for pid, arguments in self.processes.items()
            if any(needle and needle in arguments for needle in needles)
        }


@dataclass(frozen=True)
class SurvivorReport:
    locks: tuple[str, ...]
    processes: tuple[str, ...]
    environment_scan: str

    @property
    def clean(self) -> bool:
        return not self.locks and not self.processes

    def to_dict(self) -> dict[str, object]:
        return {
            "locks": list(self.locks),
            "processes": list(self.processes),
            "environment_scan": self.environment_scan,
            "clean": self.clean,
        }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def detached_locks(state_dir: Path) -> tuple[str, ...]:
    """Every detached-run lock whose recorded pid is still alive."""

    directory = Path(state_dir) / "detached"
    if not directory.is_dir():
        return ()
    live: list[str] = []
    for lock in sorted(directory.glob("*.lock")):
        try:
            pid = int((lock.read_text(encoding="utf-8").strip() or "0"))
        except (OSError, ValueError):
            # An unreadable or malformed lock is not evidence of absence.
            live.append(f"{lock.name}:unreadable")
            continue
        if _pid_alive(pid):
            live.append(f"{lock.name}:{pid}")
    return tuple(live)


def snapshot_processes() -> ProcessSnapshot:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-Ao", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ProcessSnapshot(processes={})
    processes: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        head, _, arguments = line.strip().partition(" ")
        try:
            processes[int(head)] = arguments.strip()
        except ValueError:
            continue
    return ProcessSnapshot(processes=processes)


def _argmax() -> int:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if libc.sysctl(mib, 2, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        return 0
    return int(value.value)


def process_environment_blob(pid: int) -> bytes | None:
    """Read one process's argv+environ blob, or None when the platform refuses.

    Returning None is a real answer: it says the environment marker could not be
    read, which is why the caller must not treat a silent scan as proof.
    """

    argmax = _argmax()
    if argmax <= 0:
        return None
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    buffer = ctypes.create_string_buffer(argmax)
    size = ctypes.c_size_t(argmax)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return buffer.raw[: size.value]


def scan(
    *,
    state_dir: Path,
    marker: str,
    needles: Iterable[str],
    baseline: ProcessSnapshot | None = None,
    exclude: Iterable[int] = (),
) -> SurvivorReport:
    """Report every survivor attributable to this effect."""

    locks = detached_locks(state_dir)
    current = snapshot_processes()
    excluded = set(exclude) | {os.getpid(), os.getppid()}
    known = set(baseline.processes) if baseline is not None else set()
    unknown = {
        pid: arguments
        for pid, arguments in current.processes.items()
        if pid not in excluded and pid not in known
    }
    by_arguments = {
        pid: arguments
        for pid, arguments in unknown.items()
        if any(needle and needle in arguments for needle in needles)
    }
    by_marker: dict[int, str] = {}
    if marker:
        encoded = marker.encode("utf-8")
        for pid, arguments in unknown.items():
            if encoded in (process_environment_blob(pid) or b""):
                by_marker[pid] = arguments
    # Union, never intersection: an unreadable environment must not retract a
    # survivor the arguments already named.
    candidates = {**by_arguments, **by_marker}
    if by_marker:
        scan = "arguments+marker"
    else:
        scan = "arguments-only"
    return SurvivorReport(
        locks=locks,
        processes=tuple(f"{pid}:{arguments}" for pid, arguments in sorted(candidates.items())),
        environment_scan=scan,
    )
