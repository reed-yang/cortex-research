"""D3 layer 2: write confinement inside the worker process.

Contract D3 keeps the in-process audit hook and takes away its job of being the
whole boundary. F4 is explicit about why: an audit hook is "a containment for
trusted code, not a boundary against untrusted code" — anything that can reach
the C level can step around it. What it is good at is exactly what the seatbelt
is bad at: raising a *typed* refusal, in the worker, naming the path, instead of
an EPERM that surfaces as whatever the fork happens to do with an `OSError`.

So the two layers are deliberately the same set. `install_write_confinement`
takes the writable root and the denied paths the profile was generated from, and
`cortex_platform.product.runtime_update.sandbox` renders SBPL from the same
values — with a test pinning that the two name the same four `HERMES_HOME`
entries, because a hook that confined a different set would be a second policy
pretending to be a second layer.

Reads are not confined here. The profile owns read containment; this owns the
write confinement F4 says the hook keeps.
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Sequence

#: The message every refusal carries, unchanged from the v1 hook so an operator
#: grepping logs for one sandbox finds both.
SANDBOX_DENIED = "sandbox_denied"

#: Replicated, not imported: this module runs inside the slot under the release's
#: own interpreter, where `cortex_platform` does not exist. The product-side
#: constant is `sandbox.EVIDENCE_DIRNAME` and a test pins the two together, the
#: same discipline `content_tree_digest` is held to.
EVIDENCE_DIRNAME = ".cortex-sandbox"

#: The one path outside the writable root that a write may name. `/dev/null` is
#: in the generated profile for the same reason: libraries redirect to it, and
#: it cannot carry state.
WRITABLE_DEVICES = ("/dev/null",)

#: Audit events that create, move, remove or re-permission a path. `open` is
#: handled separately because only some of its calls are writes.
_PATH_EVENTS = (
    "os.mkdir",
    "os.rmdir",
    "os.remove",
    "os.rename",
    "os.link",
    "os.symlink",
    "os.truncate",
    "os.chmod",
    "os.chown",
    "os.utime",
)


class WriteConfinementError(PermissionError):
    """A write named a path outside the worker's writable set."""


def _fspath(value: object) -> str | None:
    if isinstance(value, int):
        # An already-open descriptor. The open that produced it was audited.
        return None
    if isinstance(value, (str, bytes, os.PathLike)):
        return os.fsdecode(value)
    return None


class WriteConfinement:
    """One resolved writable root, minus the paths that may never be written."""

    def __init__(
        self,
        *,
        write_roots: Iterable[str],
        denied_paths: Iterable[str],
        writable_files: Iterable[str] = WRITABLE_DEVICES,
    ) -> None:
        self.write_roots = tuple(sorted({os.path.realpath(p) for p in write_roots}))
        self.denied_paths = tuple(sorted({os.path.realpath(p) for p in denied_paths}))
        self.writable_files = tuple(sorted({os.path.realpath(p) for p in writable_files}))
        if not self.write_roots:
            raise ValueError("write confinement needs at least one writable root")

    @staticmethod
    def _under(path: str, root: str) -> bool:
        return path == root or path.startswith(root + os.sep)

    def permits(self, path: str) -> bool:
        """Resolve, then decide. An unresolved path is V3's footgun again."""

        resolved = os.path.realpath(path)
        if resolved in self.writable_files:
            return True
        if any(self._under(resolved, denied) for denied in self.denied_paths):
            return False
        return any(self._under(resolved, root) for root in self.write_roots)

    def check(self, path: object) -> None:
        target = _fspath(path)
        if target is None or not os.path.isabs(target):
            # A relative path cannot be judged from an audit hook. The `open`
            # event carries `(path, mode, flags)` and never the `dir_fd` the
            # call may have used, so `openat(dir_fd, "worker.lock")` and
            # `open("worker.lock")` are indistinguishable here -- and the
            # operation ledger opens every one of its files through a directory
            # descriptor. Resolving against the current directory would refuse
            # the worker's own ledger; refusing outright would refuse it twice.
            # Layer 1 has no such blind spot: the kernel resolves `openat`
            # itself, so a relative escape is denied there. Recorded rather than
            # papered over -- this is exactly the "containment, not boundary"
            # F4 describes.
            return
        if not self.permits(target):
            raise WriteConfinementError(f"{SANDBOX_DENIED}: {target}")

    def audit(self, event: str, arguments: Sequence[object]) -> None:
        if event == "open":
            if not arguments:
                return
            mode = arguments[1] if len(arguments) > 1 else None
            flags = arguments[2] if len(arguments) > 2 else 0
            writes = (
                isinstance(mode, str) and any(marker in mode for marker in "wax+")
            ) or (
                isinstance(flags, int)
                and not isinstance(flags, bool)
                and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            )
            # Resolution only happens for writes: the fork opens thousands of
            # files at import and every one of them would otherwise pay for a
            # `realpath` that answers a question nobody asked.
            if writes:
                self.check(arguments[0])
            return
        if event in _PATH_EVENTS:
            for argument in arguments[:2]:
                self.check(argument)


def confinement_for(state_dir: str, hermes_home: str, denied_names: Sequence[str]):
    """The set the generated profile writes, derived from the same two paths."""

    state_root = os.path.realpath(state_dir)
    home = os.path.realpath(hermes_home)
    denied = [os.path.join(home, name) for name in denied_names]
    denied.append(os.path.join(state_root, EVIDENCE_DIRNAME))
    return WriteConfinement(write_roots=(state_root,), denied_paths=denied)


def install_write_confinement(confinement: WriteConfinement) -> WriteConfinement:
    """Install the hook. There is no uninstall, which is the point."""

    sys.addaudithook(confinement.audit)
    return confinement
