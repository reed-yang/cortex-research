"""D4(4): the before/after proof that no bound tree was written.

⟦AMD-1⟧ rejected a listing digest: an effect that rewrites an existing
`notes.md` in place leaves the name set identical, and size plus mtime does not
close it either. The proof is the shape this repository already owns
(`sources/adoption.py:282-314`) -- a chunked SHA-256 per file keyed by relative
path, then one digest over that mapping -- with one deliberate difference:
`_directory_digests` skips symlinks, and a skipped symlink is a blind spot, so
each link records its target as its value instead.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

_READ_CHUNK = 1 << 20
_SYMLINK_PREFIX = "symlink:"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def content_tree(root: Path) -> dict[str, str]:
    """Digest every file under one tree, recording symlinks by their target."""

    root = Path(root)
    if not root.exists():
        return {}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            # A swapped link is a difference, not a blind spot.
            result[key] = _SYMLINK_PREFIX + str(Path.readlink(path))
        elif path.is_file():
            result[key] = _file_digest(path)
    return result


def tree_digest(files: Mapping[str, str]) -> str:
    """One digest over the whole per-file mapping."""

    return hashlib.sha256(
        json.dumps(
            dict(files), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def sample_trees(roots: Mapping[str, Path]) -> dict[str, dict[str, str]]:
    """Sample several named trees at one instant, with their tree digests."""

    sampled: dict[str, dict[str, str]] = {}
    for name, root in roots.items():
        files = content_tree(Path(root))
        sampled[name] = {
            "root": str(root),
            "tree_digest": tree_digest(files),
            "entry_count": str(len(files)),
        }
    return sampled


def differences(
    before: Mapping[str, Mapping[str, str]], after: Mapping[str, Mapping[str, str]]
) -> tuple[str, ...]:
    """Name every sampled tree whose content digest moved."""

    names = sorted(set(before) | set(after))
    return tuple(
        name
        for name in names
        if before.get(name, {}).get("tree_digest")
        != after.get(name, {}).get("tree_digest")
    )
