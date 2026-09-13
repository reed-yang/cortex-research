"""A stand-in for the wheel-shipped CPython staging kernel.

Its own module rather than the package's `conftest`: `tests/conftest.py` already
owns the name `conftest` on `sys.path`, so a fake defined there would be
shadowed. This package has an `__init__.py`, so the tests import it relatively.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cortex_platform.runtime_staging import PythonRuntime, RuntimeStagingError


@dataclass
class FakePythonStager:
    """A stand-in for the wheel-shipped staging kernel.

    The state machine `stage` owns — three-way reuse, mismatch refusal, pruning,
    containment — is independent of what CPython an archive holds, and unpacking
    38 MB per case would make that machine untestable in practice. So this fake
    keeps exactly the post-conditions the state machine reads and nothing else:
    the destination must not already exist, the archive's bytes must match the
    digest the manifest declared, the published tree is sealed 0o500, and the
    interpreter is measured. Everything it drops — the fd-bound unpack, the
    prefix substitution, the linkage check, the real probe — is proven against
    the real vendored archive in `test_interpreter_staging.py`.
    """

    expansions: list[dict[str, object]] = field(default_factory=list)
    verifications: list[Path] = field(default_factory=list)
    interpreter_body: bytes = b"#!/fake/python\n"
    fail_expansion: bool = False

    def expand(self, archive, destination, *, stage_root, expected, profile, **_kwargs):
        self.expansions.append(
            {
                "archive": Path(archive),
                "destination": Path(destination),
                "stage_root": Path(stage_root),
                "expected": dict(expected),
                "profile": profile.name,
            }
        )
        if self.fail_expansion:
            raise RuntimeStagingError("staging refused by the fake kernel")
        if Path(destination).exists():
            raise RuntimeStagingError("Python stage destination already exists")
        payload = Path(archive).read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected["sha256"]:
            raise RuntimeStagingError(
                "Python runtime archive identity does not match the manifest"
            )
        interpreter = Path(destination) / profile.interpreter_relative
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(self.interpreter_body)
        interpreter.chmod(0o500)
        for directory in sorted(
            Path(destination).rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if directory.is_dir():
                directory.chmod(0o500)
        Path(destination).chmod(0o500)
        return PythonRuntime(
            version=str(expected["version"]),
            platform="darwin",
            architecture="arm64",
            interpreter_sha256=hashlib.sha256(self.interpreter_body).hexdigest(),
            archive_sha256=str(expected["sha256"]),
        )

    def verify(self, tree, *, profile, **_kwargs) -> None:
        self.verifications.append(Path(tree))
        root = Path(tree)
        if not root.is_dir() or stat.S_IMODE(root.lstat().st_mode) != 0o500:
            raise RuntimeStagingError("staged Python runtime tree is unsafe")
        if not (root / profile.interpreter_relative).is_file():
            raise RuntimeStagingError("staged Python runtime interpreter is missing")
