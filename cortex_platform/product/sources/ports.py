"""Injected source resolution and research-import ports."""

from __future__ import annotations

from typing import Protocol

from .models import CandidateObservation, ImportRequest, ImportResult


class SourceResolver(Protocol):
    def resolve(
        self,
        *,
        title: str | None,
        locator: str | None,
        locator_sha256: str | None = None,
    ) -> tuple[CandidateObservation, ...]: ...


class ResearchImportAdapter(Protocol):
    def execute(self, request: ImportRequest) -> ImportResult: ...
