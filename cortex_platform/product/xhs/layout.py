"""The retained files of a saved XHS note or imported blog version.

A version lives at `<directory>/v<N>/` under its asset root and is written
once: it is built whole beside its final name and renamed into place, so a
reader never sees half of one. Its tree digest covers every file by relative
path and content hash; the Control binding records it.

A version directory left by a crash between the rename and the registration
is recognised by its digest and reused. One with other content was never
registered (its version is the next one), so nothing reads it and it is
replaced. Writers of one directory take turns under a lock on it, and the
caller's guard runs under that lock before anything is staged or replaced:
a holder whose lease passed on, or a version registered meanwhile, stops
there.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from cortex_platform.product.sources.identity import xhs_note_permalink


def tree_sha256(files: Mapping[str, bytes]) -> str:
    """One digest over every file's relative path and content hash."""

    lines = sorted(f"{name}\0{hashlib.sha256(data).hexdigest()}" for name, data in files.items())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def read_tree(directory: Path) -> dict[str, bytes]:
    """Every regular file below `directory`; a link makes the tree unequal."""

    files: dict[str, bytes] = {}
    for current, directories, names in os.walk(directory, followlinks=False):
        base = Path(current)
        links = [entry for entry in directories if (base / entry).is_symlink()]
        for name in names + links:
            path = base / name
            relative = path.relative_to(directory).as_posix()
            files[relative] = b"\0link" if path.is_symlink() else path.read_bytes()
    return files


def _check_name(name: str) -> None:
    parts = name.split("/")
    if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("version file name is invalid")


def write_version(
    parent: Path,
    version: int,
    files: Mapping[str, bytes],
    *,
    guard: Callable[[], None] | None = None,
) -> str:
    """Write `<parent>/v<version>` from `files` (stage, then rename); return its digest.

    `guard` runs under the directory lock before anything is staged or
    replaced, and raises to stop the write.
    """

    if type(version) is not int or version < 1:
        raise ValueError("content version is invalid")
    for name in files:
        _check_name(name)
    digest = tree_sha256(files)
    parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    lock = os.open(parent, flags)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _write_locked(parent, version, files, digest, guard)
    finally:
        os.close(lock)


def _write_locked(
    parent: Path,
    version: int,
    files: Mapping[str, bytes],
    digest: str,
    guard: Callable[[], None] | None,
) -> str:
    target = parent / f"v{version}"
    stale = parent / f".v{version}.stale"
    if target.is_dir() and not target.is_symlink():
        if tree_sha256(read_tree(target)) == digest:
            return digest
        if guard is not None:
            guard()
        if stale.exists():
            shutil.rmtree(stale)
        os.rename(target, stale)
    elif target.exists() or target.is_symlink():
        raise ValueError("version path is not a directory")
    elif guard is not None:
        guard()
    partial = parent / f".v{version}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    directories = {partial}
    for name, data in files.items():
        path = partial / name
        path.parent.mkdir(parents=True, exist_ok=True)
        directories.update(p for p in path.parents if p.is_relative_to(partial))
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    # The caller registers the version once this returns, so its entries and
    # the rename must survive a host crash, not only its file contents.
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        sync_directory(directory)
    os.rename(partial, target)
    sync_directory(parent)
    if stale.exists():
        shutil.rmtree(stale)
    return digest


def sync_directory(path: Path) -> None:
    """fsync one directory, so the entries made in it outlive a host crash."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in (text or "").splitlines()] or [">"]


def _where(image_ordinal: int | None) -> str:
    return f"image {image_ordinal}" if image_ordinal is not None else "caption"


def note_title(note: Mapping[str, Any]) -> str:
    return " ".join(str(note["title"]).split()) or f"XHS note {note['note_id']}"


def source_title(value: str, fallback: str, *, maximum: int = 1_000) -> str:
    """A title the source registry accepts.

    The registry refuses every control and format character, and an emoji
    sequence often carries a zero-width joiner, so each becomes a space.
    """

    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in unicodedata.normalize("NFC", value or "")
    )
    return " ".join(cleaned.split())[:maximum].strip() or fallback


def render_note(
    note: Mapping[str, Any],
    blogger: Mapping[str, Any],
    images: Iterable[Mapping[str, Any]],
    recommendations: Iterable[Mapping[str, Any]],
) -> str:
    """`note.md`: the header, the caption, then the identified recommendations."""

    images = list(images)
    failed = sum(
        1
        for image in images
        if image["download_state"] == "failed" or image["ocr_state"] == "failed"
    )
    lines = [
        f"# {note_title(note)}",
        "",
        f"- Blogger: {blogger['display_name'] or blogger['user_id']} ({blogger['role']})",
        f"- Published: {note['published_at'] or 'unknown'}",
        f"- Permalink: <{xhs_note_permalink(str(note['note_id']))}>",
        f"- Images: {len(images)}" + (f", {failed} failed" if failed else ""),
        "",
        "## Caption",
        "",
        str(note["caption"]).strip() or "(empty)",
        "",
        "## Recommendations",
        "",
    ]
    recommendations = list(recommendations)
    if not recommendations:
        lines.extend(["None identified.", ""])
    for index, recommendation in enumerate(recommendations, start=1):
        lines.extend(
            [
                f"### {index}. {recommendation['title']}",
                "",
                f"Identified (auto): {recommendation['kind']}, from the "
                f"{_where(recommendation['image_ordinal'])}",
                "",
                *_quote(str(recommendation["quote"])),
                "",
            ]
        )
        if recommendation["arxiv_id"]:
            lines.extend([f"arXiv: {recommendation['arxiv_id']}", ""])
        if recommendation["url"]:
            lines.extend([f"Link: <{recommendation['url']}> ({recommendation['url_state']})", ""])
    return "\n".join(lines).rstrip("\n") + "\n"


def render_transcription(
    images: Iterable[Mapping[str, Any]], texts: Mapping[int, str]
) -> str:
    """`transcription.md`: one section per image, in order, verbatim or its failure."""

    lines = ["# Transcription", ""]
    for image in images:
        ordinal = int(image["ordinal"])
        lines.extend([f"## Image {ordinal}", ""])
        if image["download_state"] != "ok":
            lines.extend([f"Download failed ({image['download_error'] or 'unknown'}).", ""])
            continue
        lines.extend([f"![Image {ordinal}](assets/{image['asset_name']})", ""])
        if image["ocr_state"] != "ok":
            lines.extend([f"Transcription failed ({image['ocr_error'] or 'unknown'}).", ""])
            continue
        if image["ocr_flags"]:
            lines.extend([f"Flags: {', '.join(image['ocr_flags'])}", ""])
        text = texts.get(ordinal, "")
        lines.extend([_escape_headings(text) if text.strip() else "(no text)", ""])
    return "\n".join(lines).rstrip("\n") + "\n"


_IMAGE_HEADING_RE = re.compile(r"^## Image [0-9]+$", re.MULTILINE)


def _escape_headings(text: str) -> str:
    """A line of OCR text that reads like a section heading is escaped, so it
    renders the same and never starts another image's section."""

    return _IMAGE_HEADING_RE.sub(lambda match: "\\" + match.group(0), text)


def render_blog_notes(
    *,
    title: str,
    normalized_url: str,
    final_url: str | None,
    content_source: str,
    recommended_in: Iterable[Mapping[str, Any]],
) -> str:
    """`notes.md`: the source link, then each recommending note with its
    screenshot and the verbatim excerpt that names the blog."""

    lines = [f"# {title}", "", f"- Source: <{normalized_url}>"]
    if final_url and final_url != normalized_url:
        lines.append(f"- Fetched from: <{final_url}>")
    lines.append(
        "- Article text: "
        + ("Jina Reader" if content_source == "jina" else "extracted from the page")
    )
    lines.extend(["- Not peer-reviewed", "", "## Recommended in", ""])
    for entry in recommended_in:
        lines.extend([f"### {entry['note_title']} · {_where(entry['image_ordinal'])}", ""])
        if entry.get("asset_name"):
            label = _where(entry["image_ordinal"]).capitalize()
            lines.extend([f"![{label}](assets/{entry['asset_name']})", ""])
        lines.extend([*_quote(str(entry["quote"])), ""])
    return "\n".join(lines).rstrip("\n") + "\n"
