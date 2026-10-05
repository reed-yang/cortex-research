"""Closed evidence packet and durable conversation command semantics."""

import hashlib
import json
import re

ACTOR = "research-execution"
MAX_SNAPSHOT_BYTES = 131072
MODES = {"fts5_or", "unicode_title_fallback", "fts5_or+unicode_title_fallback"}
COMMAND = re.compile(r"^/(research|chat)(?:\s+(.*))?$", re.I | re.S)
#: Paper sources per packet and evidence items per paper source.
MAX_PACKET_SOURCES = 6
MAX_SOURCE_EVIDENCE = 4
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
    require(isinstance(sources, list)
            and (1 if version == 1 else 0) <= len(sources) <= MAX_PACKET_SOURCES)
    seen = set()
    for index, source in enumerate(sources, 1):
        require(isinstance(source, dict) and set(source) == {
            "label", "source_id", "canonical_id", "engine_ref", "evidence"})
        require(source["label"] == f"S{index}")
        for key in ("source_id", "canonical_id", "engine_ref"):
            require(isinstance(source[key], str) and 0 < len(source[key]) <= 500)
        require(source["source_id"] not in seen)
        seen.add(source["source_id"])
        require(isinstance(source["evidence"], list)
                and 1 <= len(source["evidence"]) <= MAX_SOURCE_EVIDENCE)
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


#: Link text with at most one level of nested brackets.
_LINK_TEXT = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
#: An immediate inline link, skipped whole: link text, a plain destination with
#: balanced parentheses or a <...> destination, and an optional "...", '...' or
#: (...) title. retrieval_query reads links and labels with the same scan.
_INLINE_LINK = (
    _LINK_TEXT
    + r"\(\s*(?:<[^<>\n]*>|(?:[^\s()]|\([^\s()]*\))*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
#: An ASCII bracket whose body starts like a label; it must be a group. A
#: following "(" that does not complete an inline link does not exempt it.
_LABEL_BRACKET = r"\[(\s*[sSdD]\s*[0-9][^\]]*)\]"
#: Links are tried first, so labels in their text, destination or title are skipped.
_CITATION_SCAN = re.compile(rf"{_INLINE_LINK}|{_LABEL_BRACKET}")
#: Label tokens separated by one comma (ASCII, ， or 、) or by whitespace alone.
_LABEL_GROUP = re.compile(r"\s*([sSdD][0-9]+(?:(?:\s*[,，、]\s*|\s+)[sSdD][0-9]+)*)\s*")
_LABEL_SEPARATOR = re.compile(r"[\s,，、]+")


def cited_labels(packet, text):
    """The labels a response cites under its packet's grammar; None if malformed.

    A v1 packet keeps its original scanner: every S-led bracket body is one
    indivisible label. Every later version reads complete groups such as
    ``[S1, D2]`` and ``[S1][D2]``. Brackets that do not start with a label and
    inline links ``[text](url "title")``, including any labels inside them, are
    not citations, while a label-led bracket outside the group grammar makes
    the response malformed. Tokens are kept verbatim, so authorization stays an
    exact match against citation_labels.
    """
    if packet["schema_version"] == 1:
        return frozenset(citation_labels(packet)[1].findall(text))
    cited = set()
    for match in _CITATION_SCAN.finditer(text):
        body = match.group(1)
        if body is None:
            continue
        group = _LABEL_GROUP.fullmatch(body)
        if group is None:
            return None
        cited.update(_LABEL_SEPARATOR.split(group.group(1)))
    return frozenset(cited)


def selection_identity(packet):
    """Which item revision a packet was selected under, or None for paper-only."""
    item = packet.get("item")
    return None if item is None else (item["id"], item["selection_revision"])
