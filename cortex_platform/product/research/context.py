"""Closed evidence packet and durable conversation command semantics."""

import hashlib
import json
import re

ACTOR = "research-execution"
MAX_SNAPSHOT_BYTES = 131072
MODES = {"fts5_or", "unicode_title_fallback", "fts5_or+unicode_title_fallback"}
COMMAND = re.compile(r"^/(research|chat)(?:\s+(.*))?$", re.I | re.S)
#: v2 dossier bounds. Readable evidence with version identity and locators,
#: never a whole uncontrolled document tree.
MAX_DOCUMENTS = 6
MAX_EXCERPTS = 3
MAX_EXCERPT_BYTES = 6000
DOCUMENT_KINDS = {"idea", "exploration", "project"}
EXCERPT_KINDS = {"document_prefix", "document_match"}
MEDIA_TYPES = {"text/markdown", "text/plain"}


class ResearchFailure(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(packet):
    return json.dumps(packet, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def mode_for(messages):
    """Return the latest question and the explicit command granting selection."""
    authority = None
    question = None
    for message in messages:
        if message["role"] != "user":
            continue
        content = message["content"]
        match = COMMAND.fullmatch(content.strip())
        if match:
            authority = message["id"] if match[1].lower() == "research" else None
            question = (match[2] or "").strip()
        else:
            question = content.strip()
    return authority, question


def _validate_dossier(require, packet):
    """v2 only: the explicitly selected item and its retained document evidence.

    Documents carry their own immutable version identity and D-labels. They are
    never paper sources and are never resolved through `sources`.
    """
    from ..control.research_store import research_item_id

    item = packet["item"]
    require(isinstance(item, dict) and set(item) == {
        "id", "kind", "origin_id", "title", "selection_revision"})
    require(item["kind"] in DOCUMENT_KINDS)
    for key in ("origin_id", "title"):
        require(isinstance(item[key], str) and 0 < len(item[key].encode("utf-8")) <= 2000)
    require(item["id"] == research_item_id(item["kind"], item["origin_id"]))
    require(type(item["selection_revision"]) is int and item["selection_revision"] >= 1)
    documents = packet["documents"]
    require(isinstance(documents, list) and len(documents) <= MAX_DOCUMENTS)
    seen = set()
    for index, document in enumerate(documents, 1):
        require(isinstance(document, dict) and set(document) == {
            "label", "document_version_id", "document_id", "title", "version",
            "media_type", "byte_length", "sha256", "excerpts"})
        require(document["label"] == f"D{index}")
        for key in ("document_version_id", "document_id"):
            require(isinstance(document[key], str) and 0 < len(document[key]) <= 200)
        require(document["document_version_id"] not in seen)
        seen.add(document["document_version_id"])
        require(isinstance(document["title"], str)
                and 0 < len(document["title"].encode("utf-8")) <= 500)
        require(type(document["version"]) is int and document["version"] >= 1)
        require(document["media_type"] in MEDIA_TYPES)
        require(type(document["byte_length"]) is int and 0 < document["byte_length"] <= 1048576)
        require(re.fullmatch(r"[0-9a-f]{64}", document["sha256"]) is not None)
        excerpts = document["excerpts"]
        require(isinstance(excerpts, list) and 1 <= len(excerpts) <= MAX_EXCERPTS)
        for excerpt in excerpts:
            require(isinstance(excerpt, dict) and set(excerpt) == {
                "kind", "text", "retained_sha256", "locator"})
            require(excerpt["kind"] in EXCERPT_KINDS)
            require(isinstance(excerpt["text"], str) and bool(excerpt["text"].strip())
                    and len(excerpt["text"].encode("utf-8")) <= MAX_EXCERPT_BYTES)
            require(excerpt["retained_sha256"] == digest(excerpt["text"]))
            require(isinstance(excerpt["locator"], str) and 0 < len(excerpt["locator"]) <= 2000)


def validate_snapshot(packet, sha256):
    """Validate structure and retained bytes independently of mutable corpus files."""
    def require(condition):
        if not condition:
            raise ValueError("invalid research snapshot")

    require(isinstance(packet, dict))
    version = packet.get("schema_version")
    require(version in (1, 2))
    fields = {"schema_version", "query", "retrieval_query", "retrieval_mode", "authority", "sources"}
    require(set(packet) == (fields if version == 1 else fields | {"item", "documents"}))
    require(packet["retrieval_mode"] in MODES)
    query = packet["query"]
    require(isinstance(query, str) and 0 < len(query.encode("utf-8")) <= 1024)
    require(isinstance(packet["retrieval_query"], str)
            and 0 < len(packet["retrieval_query"].encode("utf-8")) <= 1024)
    authority = packet["authority"]
    require(isinstance(authority, dict) and set(authority) == {"kind", "message_id"})
    require(authority["kind"] == "user_requested_adopted_library_read_only")
    require(isinstance(authority["message_id"], str) and bool(authority["message_id"]))
    sources = packet["sources"]
    # A v2 dossier can carry the whole turn, so a paper match is no longer
    # required; v1 still refuses an empty packet exactly as before.
    require(isinstance(sources, list) and (1 if version == 1 else 0) <= len(sources) <= 6)
    seen = set()
    for index, source in enumerate(sources, 1):
        require(isinstance(source, dict) and set(source) == {
            "label", "source_id", "canonical_id", "engine_ref", "evidence"})
        require(source["label"] == f"S{index}")
        for key in ("source_id", "canonical_id", "engine_ref"):
            require(isinstance(source[key], str) and 0 < len(source[key]) <= 500)
        require(source["source_id"] not in seen)
        seen.add(source["source_id"])
        require(isinstance(source["evidence"], list) and 1 <= len(source["evidence"]) <= 4)
        for evidence in source["evidence"]:
            require(isinstance(evidence, dict) and set(evidence) == {
                "kind", "text", "retained_sha256", "content_sha256", "locator"})
            require(evidence["kind"] in {"indexed_passage", "notes", "grounding", "full_text"})
            require(isinstance(evidence["text"], str) and bool(evidence["text"].strip())
                    and len(evidence["text"].encode("utf-8")) <= 6000)
            require(evidence["retained_sha256"] == digest(evidence["text"]))
            require(isinstance(evidence["content_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", evidence["content_sha256"]) is not None)
            require(isinstance(evidence["locator"], str) and 0 < len(evidence["locator"]) <= 2000)
    if version == 2:
        _validate_dossier(require, packet)
        require(len(sources) + len(packet["documents"]) >= 1)
    encoded = canonical(packet)
    require(len(encoded.encode("utf-8")) <= MAX_SNAPSHOT_BYTES and digest(encoded) == sha256)
    return encoded


def citation_labels(packet):
    """The local labels this exact packet authorizes, and how they are written.

    A v1 packet keeps its original S-only reading, so an old response is judged
    by the rule it was produced under.
    """
    labels = {source["label"] for source in packet["sources"]}
    labels |= {document["label"] for document in packet.get("documents", ())}
    prefixes = "sSdD" if packet["schema_version"] >= 2 else "sS"
    return labels, re.compile(rf"\[([{prefixes}][^\]]*)\]")


def selection_identity(packet):
    """Which item revision a packet was selected under, or None for paper-only."""
    item = packet.get("item")
    return None if item is None else (item["id"], item["selection_revision"])
