"""Verified bounded reads for immutable committed artifact text."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .materializer import (
    _open_directory,
    _open_secure_root,
    _read_regular,
    _relative_segments,
    _require_owner_private,
)

if TYPE_CHECKING:
    from ..control import ControlStore


_ROUTE_MAX_BYTES = 1_048_576


class ArtifactContentUnavailable(RuntimeError):
    """Committed content cannot be returned through the closed public route."""


@dataclass(frozen=True)
class ArtifactContentReference:
    """Private store-validated filesystem capability for one exact version."""

    artifact_version_id: str
    private_root: Path
    root_max_bytes: int
    relative_path: str
    media_type: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class ArtifactVersionContent:
    artifact_version_id: str
    media_type: str
    byte_length: int
    sha256: str
    content: str

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_version_id": self.artifact_version_id,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "content": self.content,
        }


class ArtifactReader:
    """Resolve and read one immutable text version without exposing its path."""

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    def read(self, artifact_version_id: str) -> ArtifactVersionContent:
        try:
            reference = self._store.read_artifact_content_reference(artifact_version_id)
            media_type = _text_media_type(reference.media_type)
            if (
                reference.byte_length > reference.root_max_bytes
                or reference.byte_length > _ROUTE_MAX_BYTES
            ):
                raise ArtifactContentUnavailable(
                    "artifact content exceeds its read limit"
                )
            raw = _read_reference(reference)
            if len(raw) != reference.byte_length:
                raise ArtifactContentUnavailable(
                    "artifact content length does not match"
                )
            if hashlib.sha256(raw).hexdigest() != reference.sha256:
                raise ArtifactContentUnavailable(
                    "artifact content digest does not match"
                )
            try:
                content = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise ArtifactContentUnavailable(
                    "artifact content is not valid UTF-8"
                ) from None
            return ArtifactVersionContent(
                artifact_version_id=reference.artifact_version_id,
                media_type=media_type,
                byte_length=reference.byte_length,
                sha256=reference.sha256,
                content=content,
            )
        except ArtifactContentUnavailable:
            raise
        # Collapse every private storage/filesystem failure at the public boundary.
        except Exception:  # noqa: BLE001
            raise ArtifactContentUnavailable(
                "artifact content is unavailable"
            ) from None


def _text_media_type(value: str) -> str:
    parts = [part.strip() for part in value.split(";")]
    base = parts[0]
    if base not in {"text/markdown", "text/plain"}:
        raise ArtifactContentUnavailable("artifact media type is not readable text")
    parameters = parts[1:]
    if parameters and (
        len(parameters) != 1 or parameters[0].lower() != "charset=utf-8"
    ):
        raise ArtifactContentUnavailable("artifact text charset is unsupported")
    return base


def _read_reference(reference: ArtifactContentReference) -> bytes:
    segments = _relative_segments(reference.relative_path)
    root_fd, root_identity, _ = _open_secure_root(reference.private_root)
    current_fd = root_fd
    try:
        for segment in segments[:-1]:
            next_fd = _open_directory(current_fd, segment, create=False, private=True)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        raw = _read_regular(
            current_fd,
            segments[-1],
            limit=reference.byte_length,
            require_single_link=True,
            require_private=True,
        )
        opened_root = os.fstat(root_fd)
        if not stat.S_ISDIR(opened_root.st_mode) or (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != (root_identity.st_dev, root_identity.st_ino):
            raise ArtifactContentUnavailable("artifact root changed during read")
        _require_owner_private(opened_root, "asset root mode")
        verified_fd, verified_root, _ = _open_secure_root(reference.private_root)
        try:
            if (verified_root.st_dev, verified_root.st_ino) != (
                root_identity.st_dev,
                root_identity.st_ino,
            ):
                raise ArtifactContentUnavailable("artifact root changed during read")
        finally:
            os.close(verified_fd)
        return raw
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)
