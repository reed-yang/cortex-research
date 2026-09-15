"""Descriptor-relative reads and exclusive publication; never follow links."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_PAPER_BYTES = 512 * 1024 * 1024
MAX_FILES = 4096


class PublicationConflict(ValueError):
    """An existing file or directory cannot safely be claimed or updated."""


def parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or '\\' in relative or '\x00' in relative
            or any(p in ('', '.', '..') for p in relative.split('/'))):
        raise PublicationConflict('unsafe_relative_path')
    return path.parts


@contextmanager
def directory(path: Path):
    """Open every absolute path component without following a symlink."""
    if not path.is_absolute():
        raise PublicationConflict('absolute_path_required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


@contextmanager
def parent(root: Path, relative: str):
    components = parts(relative)
    with directory(root.joinpath(*components[:-1])) as fd:
        yield fd, components[-1]


def read_file(root: Path, relative: str) -> bytes:
    with parent(root, relative) as (fd, name):
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
                raise PublicationConflict('unsafe_or_oversized_file')
            with os.fdopen(os.dup(file_fd), 'rb') as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            after = os.fstat(file_fd)
            if len(data) > MAX_FILE_BYTES or stamp(before) != stamp(after):
                raise PublicationConflict('source_changed_during_read')
            return data
        finally:
            os.close(file_fd)


def stamp(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    result = {}
    total = 0
    entries = 0
    def visit(path: Path, prefix: str) -> None:
        nonlocal total, entries
        with directory(path) as fd:
            for name in sorted(os.listdir(fd)):
                entries += 1
                if entries > MAX_FILES:
                    raise PublicationConflict('paper_has_too_many_entries')
                relative = f'{prefix}/{name}' if prefix else name
                parts(relative)
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(entry.st_mode):
                    if len(parts(relative)) > 12:
                        raise PublicationConflict('paper_tree_too_deep')
                    visit(path / name, relative)
                else:
                    data = read_file(root, relative)
                    total += len(data)
                    if len(result) >= MAX_FILES or total > MAX_PAPER_BYTES:
                        raise PublicationConflict('paper_too_large')
                    result[relative] = digest(data)
    visit(root, '')
    if 'full_text.md' not in result:
        raise PublicationConflict('missing_full_text')
    return result


def exclusive_rename(source: Path, target: Path) -> None:
    """Publish on the same filesystem without ever replacing an entry."""
    library = ctypes.CDLL(None, use_errno=True)
    fn = library.renameatx_np
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    with directory(source.parent) as src, directory(target.parent) as dst:
        if fn(src, os.fsencode(source.name), dst, os.fsencode(target.name), 4):
            raise OSError(ctypes.get_errno(), 'exclusive publication failed')
        os.fsync(dst)


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def sync_tree(root: Path) -> None:
    """Flush staged directory entries before committing the journal intent."""
    for path, _, _ in os.walk(root, topdown=False):
        with directory(Path(path)) as fd:
            os.fsync(fd)
    with directory(root.parent) as fd:
        os.fsync(fd)


def update_file(root: Path, relative: str, expected: str, data: bytes) -> None:
    """No unlink/replace. Caller has durably retained both versions first."""
    with parent(root, relative) as (fd, name):
        target = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(target)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PublicationConflict('unsafe_update_target')
            current = os.read(target, MAX_FILE_BYTES + 1)
            named = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if digest(current) != expected or stamp(before) != stamp(named):
                raise PublicationConflict('externally_modified')
            os.lseek(target, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(target, view)
                if written == 0:
                    raise OSError('short_write')
                view = view[written:]
            os.ftruncate(target, len(data))
            os.fsync(target)
            after = os.fstat(target)
            named = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stamp(after) != stamp(named):
                raise PublicationConflict('target_replaced_during_update')
        finally:
            os.close(target)
    if digest(read_file(root, relative)) != digest(data):
        raise PublicationConflict('update_verification_failed')
