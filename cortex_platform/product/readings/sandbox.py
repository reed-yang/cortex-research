"""Inherited macOS no-delete boundary and narrower publication capabilities."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys


class ReadingsBoundaryError(ValueError):
    """External publication cannot establish its filesystem boundary."""


def no_delete_rules(root: Path) -> list[str]:
    root = root.resolve(strict=True)
    return [
        f'(deny file-write-unlink (subpath {json.dumps(str(root))}))',
        f'(deny file-link (subpath {json.dumps(str(root))}))',
        *(f'(deny file-write-unlink (literal {json.dumps(str(p))}))' for p in root.parents),
    ]


def read_only_profile(roots: tuple[Path, ...]) -> str:
    lines = ['(version 1)', '(allow default)']
    for root in roots:
        root = root.resolve()
        lines.append(f'(deny file-write* (subpath {json.dumps(str(root))}))')
        lines.append(f'(deny file-link (subpath {json.dumps(str(root))}))')
        lines.extend(f'(deny file-write-unlink (literal {json.dumps(str(p))}))' for p in root.parents)
    return '\n'.join(lines)


def apply_profile(profile: str) -> None:
    """Constrain this process before starting threads; descendants inherit it."""
    if sys.platform != 'darwin':
        raise ReadingsBoundaryError('readings publication requires macOS sandbox support')
    library = ctypes.CDLL('/usr/lib/libsandbox.dylib')
    initialize = library.sandbox_init
    initialize.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
    initialize.restype = ctypes.c_int
    error = ctypes.c_char_p()
    if initialize(profile.encode(), 0, ctypes.byref(error)) != 0:
        if error:
            library.sandbox_free_error(error)
        raise ReadingsBoundaryError('readings sandbox initialization failed')


def verify_no_delete(root: Path) -> None:
    # Query the actual running policy without attempting a destructive probe.
    library = ctypes.CDLL('/usr/lib/libsandbox.dylib')
    check = library.sandbox_check
    check.restype = ctypes.c_int
    for path in (root, *root.parents):
        result = check(os.getpid(), ctypes.c_char_p(b'file-write-unlink'), 1,
                       ctypes.c_char_p(os.fsencode(path)))
        if result != 1:
            raise ReadingsBoundaryError('readings no-delete policy could not be verified')


def publisher_profile(root: Path, stage: Path, creates: list[Path], updates: list[Path]) -> str:
    lines = ['(version 1)', '(allow default)', '(deny file-write*)',
             '(deny network*)', '(deny process-fork)', '(deny file-link)',
             f'(allow file-write* (subpath {json.dumps(str(stage))}))']
    lines.extend(f'(allow file-write* (subpath {json.dumps(str(p))}))' for p in creates)
    lines.extend(f'(allow file-write-data (literal {json.dumps(str(p))}))' for p in updates)
    lines.extend(no_delete_rules(root))
    return '\n'.join(lines)
