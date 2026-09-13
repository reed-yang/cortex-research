"""Explicit local document adoption and verified reads for research dossiers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..artifacts.materializer import AssetRoot, FilesystemMaterializer, MaterializationRequest, MaterializerError
from ..artifacts.reader import ArtifactContentUnavailable, _read_reference
from ..control.errors import NotFound


ROOT_ID = "research-documents"
MAX_DOCUMENT_BYTES = 1048576


class ResearchDocumentUnavailable(RuntimeError):
    """The selected immutable document cannot currently be read."""


@dataclass(frozen=True)
class DocumentReference:
    private_root: Path
    relative_path: str
    byte_length: int


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class ResearchDocumentReader:
    def __init__(self, store):
        self.store = store

    def read(self, version_id):
        doc = self.store.get_research_document(version_id)
        try:
            root = self.store.get_asset_root(doc["asset_root_id"])
            if not root.enabled or doc["byte_length"] > min(root.max_bytes, MAX_DOCUMENT_BYTES):
                raise ResearchDocumentUnavailable("Research document root is unavailable")
            raw = _read_reference(DocumentReference(root.private_path, doc["relative_path"], doc["byte_length"]))
            if len(raw) != doc["byte_length"] or _digest(raw) != doc["sha256"]:
                raise ResearchDocumentUnavailable("Research document content changed")
            text = raw.decode("utf-8")
        except (OSError, ValueError, ArtifactContentUnavailable, MaterializerError) as exc:
            raise ResearchDocumentUnavailable("Research document content is unavailable") from exc
        return {"document_version_id": doc["id"], "media_type": doc["media_type"],
                "byte_length": doc["byte_length"], "sha256": doc["sha256"], "content": text}


class ResearchDocumentAdopter:
    """Operator-only preview/apply; no filesystem path is accepted by a public API."""

    def __init__(self, store, catalog):
        self.store, self.catalog = store, catalog

    @staticmethod
    def _source(root, registered_path):
        raw = Path(registered_path).expanduser()
        source = (raw if raw.is_absolute() else root / raw).resolve(strict=True)
        relative = source.relative_to(root)
        if source.suffix.lower() not in {".md", ".markdown", ".txt"} or not source.is_file():
            raise ValueError("unsupported research document")
        if not 0 < source.stat().st_size <= MAX_DOCUMENT_BYTES:
            raise ValueError("research document size is unsupported")
        content = source.read_bytes()
        if not 0 < len(content) <= MAX_DOCUMENT_BYTES:
            raise ValueError("research document size changed")
        content.decode("utf-8")
        return relative.as_posix(), content

    def preview(self, source_roots, *, item_ids=None, document_map=None):
        roots = {kind: Path(path).expanduser().resolve(strict=True) for kind, path in source_roots.items()}
        if any(not root.is_dir() for root in roots.values()):
            raise ValueError("source roots must be directories")
        items, skipped = [], []
        offset = 0
        while True:
            page = self.catalog.list_items(limit=100, offset=offset)
            for item in page["items"]:
                if item_ids is not None and item["id"] not in item_ids:
                    continue
                candidates = (document_map or {}).get(item["id"], self.catalog.document_candidates(item["id"]))
                entries = []
                seen = set()
                for candidate in candidates:
                    kind = candidate["kind"]
                    if kind not in roots:
                        skipped.append({"item_id": item["id"], "reason": "source_root_missing", "kind": kind})
                        continue
                    try:
                        relative, raw = self._source(roots[kind], candidate["registered_path"])
                    except (OSError, ValueError):
                        skipped.append({"item_id": item["id"], "reason": "document_unavailable", "reference": candidate["registered_path"]})
                        continue
                    origin = f"{kind}/{relative}"
                    if origin in seen:
                        continue
                    seen.add(origin)
                    entries.append({"source_kind": kind, "source_relative_path": relative,
                                    "origin_relative_path": origin, "title": Path(relative).stem,
                                    "sha256": _digest(raw), "byte_length": len(raw),
                                    "media_type": "text/plain" if Path(relative).suffix.lower() == ".txt" else "text/markdown"})
                if entries:
                    items.append({"item": {k: item[k] for k in ("id", "kind", "origin_id", "title")}, "documents": entries})
                elif not candidates:
                    skipped.append({"item_id": item["id"], "reason": "no_registered_documents"})
            offset += len(page["items"])
            if offset >= page["total"] or not page["items"]:
                break
        return {"format_version": 1, "source_roots": {k: str(v) for k, v in roots.items()}, "items": items, "skipped": skipped}

    def apply(self, manifest, *, destination, actor_id="local"):
        if manifest.get("format_version") != 1 or not manifest.get("items"):
            raise ValueError("no approved research documents")
        roots = {k: Path(v).resolve(strict=True) for k, v in manifest["source_roots"].items()}
        destination = Path(destination).expanduser().absolute()
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        materializer = FilesystemMaterializer((AssetRoot(ROOT_ID, destination, MAX_DOCUMENT_BYTES),))
        try:
            root = self.store.get_asset_root(ROOT_ID)
        except NotFound:
            root = self.store.register_asset_root(
                root_id=ROOT_ID, private_path=destination, max_bytes=MAX_DOCUMENT_BYTES,
                enabled=True, actor_id=actor_id, idempotency_key="research-root-" + _digest(str(destination).encode()),
            )
        if not root.enabled or root.private_path != destination:
            raise ValueError("research document destination differs from registered root")
        results = []
        for entry in manifest["items"]:
            item, adopted = entry["item"], []
            # A whole item's sources must still match the preview before its first copy.
            checked = []
            for doc in entry["documents"]:
                relative, raw = self._source(roots[doc["source_kind"]], doc["source_relative_path"])
                if _digest(raw) != doc["sha256"] or len(raw) != doc["byte_length"]:
                    raise ValueError("source changed after research adoption preview")
                if doc["origin_relative_path"] != f"{doc['source_kind']}/{relative}":
                    raise ValueError("research origin changed after preview")
                checked.append((doc, raw))
            for doc, raw in checked:
                document_id = "rd_" + _digest(f"{item['id']}\0{doc['origin_relative_path']}".encode())[:32]
                target = f"{document_id}/{doc['sha256']}.md"
                operation = "research-adopt-" + _digest(f"{document_id}\0{doc['sha256']}".encode())
                materializer.materialize(MaterializationRequest(
                    schema_version=1, operation_id=operation, root_id=ROOT_ID, relative_path=target,
                    sha256=doc["sha256"], byte_length=doc["byte_length"], media_type=doc["media_type"], parents=(),
                ), raw)
                adopted.append({**doc, "asset_root_id": ROOT_ID, "relative_path": target})
            results.append(self.store.register_research_documents(
                item=item, documents=adopted, actor_id=actor_id,
                idempotency_key="research-adopt-" + _digest(_canonical(entry)),
            ).value)
        return {"items": results, "skipped": manifest.get("skipped", [])}
