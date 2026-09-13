"""Source identity, resolution, and durable import boundaries."""

from .identity import (
    canonicalize_arxiv_id,
    canonicalize_doi,
    canonicalize_locator,
    canonicalize_source_locator,
)
from .models import (
    CandidateObservation,
    CanonicalLocator,
    ImportDeliveryResult,
    ImportRequest,
    ImportResult,
    validate_engine_ref,
)
from .ports import ResearchImportAdapter, SourceResolver
from .service import SourceImportDispatcher

__all__ = [
    "CandidateObservation",
    "CanonicalLocator",
    "ImportDeliveryResult",
    "ImportRequest",
    "ImportResult",
    "ResearchImportAdapter",
    "SourceImportDispatcher",
    "SourceResolver",
    "canonicalize_arxiv_id",
    "canonicalize_doi",
    "canonicalize_locator",
    "canonicalize_source_locator",
    "validate_engine_ref",
]
