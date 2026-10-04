"""Evaluate adopted-library retrieval and fresh research packets on a frozen snapshot.

Checkout-only. Every path is explicit; there are no production defaults:

    python -B -m tools.evaluate_retrieval --control /abs/snapshot/control.db \\
        --corpus /abs/snapshot/research/corpus \\
        --index /abs/snapshot/research/research.db --queries /abs/private/suite.json

The tool runs the product's SourceKnowledgeReader.search at limits 6 and 20 and
the real ResearchService.prepare fresh, unselected, library-only packet path.
It reads checkpointed SQLite files immutably, never traverses the registered
private_path, never initializes or writes a database and writes no file. The
JSON report goes to stdout and is private operator data. See
docs/runbooks/retrieval-evaluation.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from cortex_platform.product.artifacts import materializer as _materializer
from cortex_platform.product.artifacts.materializer import MaterializerError, _open_directory, _read_regular
from cortex_platform.product.control import CommandResult
from cortex_platform.product.research import context as _context
from cortex_platform.product.research import documents as _documents
from cortex_platform.product.research import service as _service
from cortex_platform.product.research.context import (
    ACTOR,
    ResearchFailure,
    canonical,
    digest,
    mode_for,
    validate_snapshot,
)
from cortex_platform.product.research.service import ResearchService
from cortex_platform.product.sources import adoption as _adoption
from cortex_platform.product.sources import models as _models
from cortex_platform.product.sources import reader as _reader
from cortex_platform.product.sources import search as _search
from cortex_platform.product.sources.models import _ARXIV_AUTHORITY_ID_RE, _DOI_AUTHORITY_ID_RE, _SHA256_RE
from cortex_platform.product.sources.reader import (
    KINDS,
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    SourceContentUnavailable,
    SourceKnowledgeReader,
    SourceQueryInvalid,
    _database,
    _directory,
)
from cortex_platform.product.sources.search import MAX_QUERY_BYTES

REPORT_VERSION = 1
SUITE_VERSION = 1
MAX_SUITE_BYTES = 1 << 20
MAX_QUERIES = 100
MAX_RELEVANCE_SETS = 100
MAX_RELEVANCE_IDS = 10_000
MAX_LISTED = 50
LIMITS = (6, 20)
LANGUAGES = ("en", "zh", "mixed")
_READ_CHUNK = 1 << 20
_NAME_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_PRIMARY_KINDS = ("notes", "full_text")
_CODE_MODULES = (
    sys.modules[__name__], _reader, _search, _adoption, _models, _service, _context,
    _documents, _materializer,
)
DEFINITIONS = {
    "k": ("SourceKnowledgeReader.search result slots at limit=k, followed by first-occurrence "
          "canonical-identity deduplication. Slots can be chunks or title hits, so these figures "
          "are not recall at the first k unique paper ranks."),
    "recall_at_k": "|P_k intersect R| / |R| over the complete declared relevance set R.",
    "relevant_papers_per_six_slots": "|P_6 intersect R| / 6: slot utilization, neither recall nor precision.",
    "packet_recall": ("|packet canonical_ids intersect R| / |R| for the fresh, unselected, "
                      "library-only ResearchService.prepare packet."),
    "language": "zh and mixed cohorts are reported separately; mixed English hits do not prove Chinese retrieval.",
}


class SuiteInvalid(ValueError):
    """The private query suite is malformed or exceeds its bounds (exit 2)."""


class EvaluationError(RuntimeError):
    """A named evaluation, storage or adapter-contract failure (exit 1)."""

    def __init__(self, category: str, message: str | None = None) -> None:
        self.category = category
        super().__init__(message or category)


class _Closed:
    """Fail closed on any attribute: product code must not reach this object."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attribute):
        raise EvaluationError("evaluation_contract_error", f"{self._name} has no {attribute}")


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


# -- Suite -------------------------------------------------------------------


def canonical_identity(value) -> str:
    """Normalize one arxiv:, doi: or sha256: canonical identity using models.py grammar."""
    if not isinstance(value, str):
        raise ValueError("canonical identity is invalid")
    authority, separator, identity = value.partition(":")
    if separator and authority == "arxiv":
        match = _ARXIV_AUTHORITY_ID_RE.fullmatch(identity)
        if match is not None and not identity.lower().startswith("arxiv:"):
            return f"arxiv:{match['work']}"
    elif separator and authority == "doi" and _DOI_AUTHORITY_ID_RE.fullmatch(identity):
        return f"doi:{identity.lower()}"
    elif separator and authority == "sha256" and _SHA256_RE.fullmatch(identity):
        return f"sha256:{identity.lower()}"
    raise ValueError("canonical identity is invalid")


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not set(value) <= _NAME_CHARACTERS \
            or value[0] in "._-":
        raise SuiteInvalid(f"{label} is invalid")
    return value


def _identities(values, label: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(values, list) or len(values) > MAX_RELEVANCE_IDS or (not values and not allow_empty):
        raise SuiteInvalid(f"{label} must be a bounded identity list")
    try:
        normalized = tuple(canonical_identity(value) for value in values)
    except ValueError:
        raise SuiteInvalid(f"{label} contains a malformed canonical identity") from None
    if len(set(normalized)) != len(normalized):
        raise SuiteInvalid(f"{label} contains duplicate identities")
    return normalized


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SuiteInvalid("suite contains a duplicate JSON object key")
        value[key] = item
    return value


def _reject_constant(name):
    raise SuiteInvalid(f"suite contains the non-JSON constant {name}")


def _validate_suite(data) -> dict:
    if not isinstance(data, dict) or set(data) != {"schema_version", "relevance_sets", "queries"}:
        raise SuiteInvalid("suite must contain exactly schema_version, relevance_sets and queries")
    if type(data["schema_version"]) is not int or data["schema_version"] != SUITE_VERSION:
        raise SuiteInvalid("suite schema_version is unsupported")
    sets = data["relevance_sets"]
    if not isinstance(sets, dict) or not 1 <= len(sets) <= MAX_RELEVANCE_SETS:
        raise SuiteInvalid("relevance_sets must be a bounded object")
    relevance = {
        _name(name, "relevance set name"): _identities(ids, f"relevance set {name!r}", allow_empty=False)
        for name, ids in sets.items()
    }
    queries = data["queries"]
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
        raise SuiteInvalid("queries must be a bounded list")
    normalized, seen = [], set()
    for item in queries:
        if not isinstance(item, dict) or not {"id", "query", "language", "relevance_set"} <= set(item) \
                or not set(item) <= {"id", "query", "language", "relevance_set", "must_find"}:
            raise SuiteInvalid("query fields are invalid")
        query_id = _name(item["id"], "query id")
        if query_id in seen:
            raise SuiteInvalid(f"query id {query_id!r} is duplicated")
        seen.add(query_id)
        text = item["query"]
        try:
            size = len(text.encode("utf-8")) if isinstance(text, str) else 0
        except UnicodeEncodeError:
            size = 0
        if not 1 <= size <= MAX_QUERY_BYTES:
            raise SuiteInvalid(f"query {query_id!r} text is empty, oversized or not valid Unicode")
        if item["language"] not in LANGUAGES:
            raise SuiteInvalid(f"query {query_id!r} language is unsupported")
        if item["relevance_set"] not in relevance:
            raise SuiteInvalid(f"query {query_id!r} references an unknown relevance set")
        must_find = _identities(item.get("must_find", []), f"query {query_id!r} must_find", allow_empty=True)
        if not set(must_find) <= set(relevance[item["relevance_set"]]):
            raise SuiteInvalid(f"query {query_id!r} must_find is not a subset of its relevance set")
        normalized.append({"id": query_id, "query": text, "language": item["language"],
                           "relevance_set": item["relevance_set"], "must_find": must_find})
    return {"schema_version": SUITE_VERSION, "relevance_sets": relevance, "queries": normalized}


def _read_input_file(path: Path, limit: int) -> bytes:
    with _directory(path.parent) as parent:
        return _read_regular(parent, path.name, limit=limit, require_private=False)


def load_suite(path: Path) -> dict:
    """Read and validate a bounded private suite. Query text is preserved verbatim."""
    try:
        raw = _read_input_file(path, MAX_SUITE_BYTES)
    except (OSError, RuntimeError):
        raise SuiteInvalid("suite file is unavailable, oversized or not a single-link regular file") from None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise SuiteInvalid("suite is not valid UTF-8 JSON") from None
    suite = _validate_suite(data)
    suite["sha256"] = hashlib.sha256(raw).hexdigest()
    return suite


# -- Snapshot access ---------------------------------------------------------


class CheckpointedEvaluationReader(SourceKnowledgeReader):
    """The product reader over an offline relocated snapshot.

    Registration, enabled state, byte limit, revision, adoption entries and
    engine-reference validation come from the Control snapshot through the
    product's own `_registration`. The registered private_path is dropped and
    never traversed; the explicit corpus argument replaces it.
    """

    def __init__(self, control: Path, corpus: Path) -> None:
        super().__init__(_Closed("evaluation reader store"))
        self._control = control
        self._corpus = corpus
        self.searches: list[dict] = []

    @contextmanager
    def _registered(self, source_id: str | None = None):
        with _database(self._control) as control:
            root, sources = self._registration(control, source_id)
            root = {key: value for key, value in root.items() if key != "private_path"}
            with _directory(self._corpus):
                yield root, self._corpus, sources

    def search(self, query, limit=10) -> dict:
        found = super().search(query, limit=limit)
        self.searches.append(found)
        return found


def registered_sources(reader: CheckpointedEvaluationReader) -> list[dict]:
    """Adopted registration rows of the snapshot, in the product's order."""
    with reader._registered() as (_, _, sources):
        return [dict(row) for row in sources.values()]


class EvaluationContextStore:
    """The narrow store protocol of a fresh, unselected ResearchService.prepare.

    Retains one context in memory. Unknown methods, identities and revisions
    fail closed; nothing is persisted.
    """

    MESSAGE_ID = "evaluation-message"

    def __init__(self, sources: list[dict], query: str) -> None:
        self._sources = {row["id"]: dict(row) for row in sources}
        self.run = {"id": "evaluation-run", "thread_id": "evaluation-thread",
                    "active_attempt_id": "evaluation-attempt", "revision": 1}
        self.message = {"id": self.MESSAGE_ID, "role": "user", "content": "/research " + query}
        self.context = None

    def __getattr__(self, name):
        raise EvaluationError("evaluation_contract_error", f"evaluation store does not provide {name}")

    def _require(self, condition: bool, what: str) -> None:
        if not condition:
            raise EvaluationError("evaluation_contract_error", f"unexpected {what}")

    def get_research_context(self, run_id):
        self._require(run_id == self.run["id"], "run")
        return self.context

    def previous_research_context(self, thread_id, message_id):
        self._require(thread_id == self.run["thread_id"] and message_id == self.MESSAGE_ID, "thread/message")
        return None

    def get_research_thread_item(self, thread_id):
        self._require(thread_id == self.run["thread_id"], "thread")
        return None

    def get_source(self, source_id):
        self._require(source_id in self._sources, "source")
        return dict(self._sources[source_id])

    def get_run(self, run_id):
        self._require(run_id == self.run["id"], "run")
        return dict(self.run)

    def record_research_context(self, *, run_id, thread_id, attempt_id, message_id, query, snapshot,
                                sha256, expected_revision, actor_id, idempotency_key):
        run = self.run
        self._require(self.context is None, "second context")
        self._require((run_id, thread_id, attempt_id, message_id)
                      == (run["id"], run["thread_id"], run["active_attempt_id"], self.MESSAGE_ID), "identity")
        self._require(expected_revision == run["revision"], "revision")
        self._require(actor_id == ACTOR and idempotency_key == digest(f"context:{run_id}"), "actor")
        try:
            validate_snapshot(snapshot, sha256)
        except ValueError:
            raise EvaluationError("packet_invalid") from None
        authority, question = mode_for([self.message])
        self._require(query == snapshot["query"] == question
                      and snapshot["authority"]["message_id"] == authority == message_id, "authority")
        for source in snapshot["sources"]:
            row = self._sources.get(source["source_id"])
            self._require(row is not None and (row["canonical_id"], row["engine_ref"])
                          == (source["canonical_id"], source["engine_ref"]), "packet source")
        self.context = {"run_id": run_id, "thread_id": thread_id, "attempt_id": attempt_id,
                        "message_id": message_id, "query": query,
                        "snapshot": json.loads(canonical(snapshot)), "sha256": sha256, "actor_id": actor_id}
        run["revision"] += 1
        return CommandResult(value=self.context, status_code=201)


def prepare_packet(reader: CheckpointedEvaluationReader, sources: list[dict], query: str):
    """Run the real fresh, unselected prepare path; return (packet, sha256)."""
    store = EvaluationContextStore(sources, query)
    service = ResearchService(store)
    service.reader = reader
    service.documents = _Closed("evaluation document reader")
    context = service.prepare(store.get_run(store.run["id"]), [store.message])
    if context is None:
        raise EvaluationError("evaluation_contract_error", "prepare did not select research mode")
    try:
        validate_snapshot(context["snapshot"], context["sha256"])
    except ValueError:
        raise EvaluationError("packet_invalid") from None
    return context["snapshot"], context["sha256"]


# -- Metrics -----------------------------------------------------------------


def retrieval_metrics(results, relevance, must_find=()) -> dict:
    """Distinct-paper metrics for one cutoff over the complete relevance set."""
    identities = list(dict.fromkeys(result["canonical_id"] for result in results))
    returned, wanted = set(identities), set(relevance)
    relevant = [identity for identity in identities if identity in wanted]
    missing = [identity for identity in relevance if identity not in returned]
    return {
        "returned_results_at_k": len(results), "distinct_papers_at_k": len(identities),
        "relevant_papers_at_k": len(relevant), "recall_at_k": _ratio(len(relevant), len(relevance)),
        "identities": identities, "relevant_identities": relevant,
        "missing_relevant_count": len(missing), "missing_relevant_identities": missing[:MAX_LISTED],
        "must_find_missing": [identity for identity in must_find if identity not in returned][:MAX_LISTED],
    }


def _search_outcome(reader, query: str, limit: int, relevance, must_find) -> dict:
    try:
        found = reader.search(query, limit=limit)
    except (SourceQueryInvalid, SourceContentUnavailable, EvaluationError) as error:
        return {"status": "failed", "failure": error.category}
    return {"status": "measured", "retrieval_mode": found["retrieval_mode"],
            **retrieval_metrics(found["results"], relevance, must_find)}


def _packet_outcome(control, corpus, sources, query, relevance, must_find, six) -> dict:
    empty = {"schema_version": None, "sha256": None, "bytes": None, "query": None,
             "retrieval_query": None, "retrieval_mode": None, "canonical_ids": [],
             "relevant_papers": 0, "must_find_missing": list(must_find)[:MAX_LISTED],
             "sources_not_in_search_at_6": [], "sources": []}
    reader = CheckpointedEvaluationReader(control, corpus)
    try:
        packet, sha256 = prepare_packet(reader, sources, query)
    except ResearchFailure as failure:
        if failure.category == "research_no_evidence":
            return empty | {"status": "research_no_evidence", "failure": None,
                            "recall": 0.0 if relevance else None}
        return empty | {"status": "failed", "failure": failure.category, "recall": None,
                        "relevant_papers": None, "must_find_missing": None}
    except EvaluationError as error:
        return empty | {"status": "failed", "failure": error.category, "recall": None,
                        "relevant_papers": None, "must_find_missing": None}
    ids = [source["canonical_id"] for source in packet["sources"]]
    relevant = [identity for identity in ids if identity in set(relevance)]
    searched = set(six.get("identities", ())) if six["status"] == "measured" else None
    return {
        "status": "built", "failure": None, "schema_version": packet["schema_version"],
        "sha256": sha256, "bytes": len(canonical(packet).encode("utf-8")),
        "query": packet["query"], "retrieval_query": packet["retrieval_query"],
        "retrieval_mode": packet["retrieval_mode"], "canonical_ids": ids,
        "relevant_papers": len(relevant), "recall": _ratio(len(relevant), len(relevance)),
        "must_find_missing": [identity for identity in must_find if identity not in ids][:MAX_LISTED],
        "sources_not_in_search_at_6": None if searched is None else [i for i in ids if i not in searched],
        "sources": [{
            "label": source["label"], "source_id": source["source_id"],
            "canonical_id": source["canonical_id"],
            "evidence": [{"kind": item["kind"], "locator": item["locator"],
                          "retained_sha256": item["retained_sha256"],
                          "content_sha256": item["content_sha256"],
                          "bytes": len(item["text"].encode("utf-8"))} for item in source["evidence"]],
        } for source in packet["sources"]],
    }


def evaluate_query(control: Path, corpus: Path, sources: list[dict], item: dict, relevance) -> dict:
    """Measure one suite query: two independent searches plus one fresh packet."""
    must_find = item["must_find"]
    reader = CheckpointedEvaluationReader(control, corpus)
    retrieval = {f"at_{limit}": _search_outcome(reader, item["query"], limit, relevance, must_find)
                 for limit in LIMITS}
    six = retrieval["at_6"]
    retrieval["relevant_papers_per_six_slots"] = (
        _ratio(six["relevant_papers_at_k"], 6) if six["status"] == "measured" else None)
    packet = _packet_outcome(control, corpus, sources, item["query"], relevance, must_find, six)
    failures = [f"search_at_{limit}:{retrieval[f'at_{limit}']['failure']}"
                for limit in LIMITS if retrieval[f"at_{limit}"]["status"] == "failed"]
    if packet["status"] == "failed":
        failures.append(f"packet:{packet['failure']}")
    return {"id": item["id"], "language": item["language"], "relevance_set": item["relevance_set"],
            "relevance_set_size": len(relevance), "query_sha256": digest(item["query"]),
            "retrieval": retrieval, "packet": packet, "failures": failures}


def _mean(values) -> dict:
    measured = [value for value in values if value is not None]
    return {"mean": round(sum(measured) / len(measured), 6) if measured else None, "n": len(measured)}


def _summary(rows: list[dict]) -> dict:
    def search(limit, key):
        return [row["retrieval"][f"at_{limit}"].get(key) for row in rows]

    built = [row["packet"] for row in rows if row["packet"]["status"] == "built"]
    summary = {
        "queries": len(rows), "failed_queries": sum(bool(row["failures"]) for row in rows),
        "recall_at_6": _mean(search(6, "recall_at_k")), "recall_at_20": _mean(search(20, "recall_at_k")),
        "relevant_papers_per_six_slots": _mean(
            row["retrieval"]["relevant_papers_per_six_slots"] for row in rows),
        "packet_recall": _mean(row["packet"]["recall"] for row in rows),
        "packets_built": len(built),
        "research_no_evidence": sum(row["packet"]["status"] == "research_no_evidence" for row in rows),
        "no_hits_at_6": sum(value == 0 for value in search(6, "returned_results_at_k")),
        "packet_sources": _mean(len(packet["sources"]) for packet in built),
    }
    for limit in LIMITS:
        distinct = search(limit, "distinct_papers_at_k")
        summary[f"distinct_papers_at_{limit}"] = _mean(distinct) | {
            "total": sum(value for value in distinct if value is not None)}
    return summary


# -- Preflight and fingerprints ---------------------------------------------


def _file_manifest(directory: int, name: str, limit: int) -> dict:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return {"status": "absent"}
    if stat.S_ISLNK(info.st_mode):
        return {"status": "symlink"}
    if not stat.S_ISREG(info.st_mode):
        return {"status": "not_regular"}
    if info.st_nlink != 1:
        return {"status": "hardlinked"}
    if info.st_size > limit:
        return {"status": "oversized"}
    try:
        raw = _read_regular(directory, name, limit=limit, require_private=False)
    except (OSError, MaterializerError):
        return {"status": "unreadable"}
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "invalid_utf8"}
    if raw.count(b"\n") + bool(raw and not raw.endswith(b"\n")) > MAX_FILE_LINES:
        return {"status": "too_many_lines"}
    return {"status": "available", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _source_manifest(corpus: int, paper_dir: str, limit: int) -> dict:
    try:
        info = os.stat(paper_dir, dir_fd=corpus, follow_symlinks=False)
    except FileNotFoundError:
        return {"directory": "missing", "files": {}}
    if stat.S_ISLNK(info.st_mode):
        return {"directory": "symlink", "files": {}}
    if not stat.S_ISDIR(info.st_mode):
        return {"directory": "not_directory", "files": {}}
    try:
        directory = _open_directory(corpus, paper_dir, create=False)
    except (OSError, MaterializerError):
        return {"directory": "unopenable", "files": {}}
    try:
        return {"directory": "available",
                "files": {kind: _file_manifest(directory, name, limit) for kind, name in KINDS.items()}}
    finally:
        os.close(directory)


def _availability(entry: dict) -> str | None:
    """None when available; otherwise the first named defect."""
    if entry["directory"] != "available":
        return f"directory:{entry['directory']}"
    for kind in KINDS:
        status = entry["files"][kind]["status"]
        if status not in {"available", "absent"}:
            return f"{kind}:{status}"
    if all(entry["files"][kind]["status"] == "absent" for kind in _PRIMARY_KINDS):
        return "no_primary_text"
    return None


def corpus_manifest(reader: CheckpointedEvaluationReader) -> dict:
    """Bounded, no-follow availability and hashes of every adopted supported text file."""
    with reader._registered() as (root, corpus, sources), _directory(corpus) as corpus_fd:
        limit = min(root["max_bytes"], MAX_FILE_BYTES)
        return {source["canonical_id"]: _source_manifest(corpus_fd, paper_dir, limit)
                for paper_dir, source in sorted(sources.items())}


def _database_fingerprint(path: Path) -> dict:
    try:
        with _directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise EvaluationError("database_unavailable")
            sidecars = {}
            for suffix in ("-wal", "-journal", "-shm"):
                try:
                    sidecar = os.stat(path.name + suffix, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(sidecar.st_mode) or (sidecar.st_size and suffix != "-shm"):
                    raise EvaluationError("database_not_checkpointed")
                sidecars[suffix] = sidecar.st_size
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                hasher, size = hashlib.sha256(), 0
                while chunk := os.read(descriptor, _READ_CHUNK):
                    hasher.update(chunk)
                    size += len(chunk)
            finally:
                os.close(descriptor)
    except EvaluationError:
        raise
    except (OSError, RuntimeError):
        raise EvaluationError("database_unavailable") from None
    return {"sha256": hasher.hexdigest(), "bytes": size, "sidecars": sidecars}


def _code_fingerprint() -> dict:
    return {module.__name__ if module.__name__ != "__main__" else "tools.evaluate_retrieval":
            hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for module in _CODE_MODULES}


def fingerprint_inputs(control: Path, index: Path, suite: Path, manifest: dict) -> dict:
    """Content fingerprints of every evaluation input; sizes and hashes, never mtimes alone."""
    try:
        suite_sha = hashlib.sha256(_read_input_file(suite, MAX_SUITE_BYTES)).hexdigest()
    except (OSError, RuntimeError):
        suite_sha = None
    return {
        "suite": {"sha256": suite_sha}, "control": _database_fingerprint(control),
        "index": _database_fingerprint(index),
        "corpus": {"sources": len(manifest), "manifest_sha256": digest(canonical(manifest))},
        "code": _code_fingerprint(),
    }


def _preflight(manifest: dict, suite: dict) -> dict:
    reasons, absent = {}, dict.fromkeys(KINDS, 0)
    unavailable = set()
    for identity, entry in manifest.items():
        reason = _availability(entry)
        if reason is not None:
            unavailable.add(identity)
            reasons[reason] = reasons.get(reason, 0) + 1
        for kind, item in entry["files"].items():
            absent[kind] += item["status"] == "absent"
    wanted = sorted({identity for ids in suite["relevance_sets"].values() for identity in ids})
    not_adopted = [identity for identity in wanted if identity not in manifest]
    defective = [identity for identity in wanted if identity in unavailable]
    return {
        "adopted_sources": len(manifest), "available_sources": len(manifest) - len(unavailable),
        "unavailable_sources": len(unavailable), "unavailable_by_reason": dict(sorted(reasons.items())),
        "absent_files": absent,
        "relevance": {"identities": len(wanted), "not_adopted_count": len(not_adopted),
                      "not_adopted": not_adopted[:MAX_LISTED], "unavailable_count": len(defective),
                      "unavailable": defective[:MAX_LISTED]},
    }


# -- Evaluation --------------------------------------------------------------


def _open_snapshot(control: Path, corpus: Path, index: Path) -> CheckpointedEvaluationReader:
    try:
        with _directory(corpus):
            pass
    except (OSError, RuntimeError):
        raise EvaluationError("corpus_unavailable") from None
    for path in (control, index):
        try:
            with _database(path):
                pass
        except (OSError, RuntimeError):
            raise EvaluationError("database_unavailable") from None
    return CheckpointedEvaluationReader(control, corpus)


def evaluate(control: Path, corpus: Path, index: Path, suite_path: Path) -> tuple[dict, int]:
    """Evaluate every suite query on one frozen snapshot; return (report, exit status)."""
    suite = load_suite(suite_path)
    for path in (control, index):
        _database_fingerprint(path)
    reader = _open_snapshot(control, corpus, index)
    try:
        manifest = corpus_manifest(reader)
        sources = registered_sources(reader)
    except (OSError, RuntimeError):
        raise EvaluationError("registration_unavailable") from None
    before = fingerprint_inputs(control, index, suite_path, manifest)
    if before["suite"]["sha256"] != suite["sha256"]:
        raise EvaluationError("input_drift")
    preflight = _preflight(manifest, suite)
    rows = [evaluate_query(control, corpus, sources, item, suite["relevance_sets"][item["relevance_set"]])
            for item in suite["queries"]]
    try:
        after = fingerprint_inputs(control, index, suite_path, corpus_manifest(reader))
    except (OSError, RuntimeError):
        after = None
    changed = sorted(part for part in before if after is None or before[part] != after[part])
    blockers = []
    if any(row["failures"] for row in rows):
        blockers.append("query_failures")
    if preflight["relevance"]["unavailable_count"]:
        blockers.append("relevance_identity_unavailable")
    if changed:
        blockers.append("input_drift")
    categories: dict[str, int] = {}
    for row in rows:
        for failure in row["failures"]:
            categories[failure] = categories.get(failure, 0) + 1
    report = {
        "report_version": REPORT_VERSION, "status": "incomplete" if blockers else "completed",
        "baseline_complete": not blockers, "blockers": blockers,
        "snapshot": {"kind": "offline_relocated_snapshot",
                     "note": "registered Control state with the corpus relocated to --corpus; "
                             "not current production authorization"},
        "definitions": DEFINITIONS,
        "inputs": {"fingerprint": before, "fingerprint_sha256": digest(canonical(before)),
                   "unchanged": not changed, "changed": changed},
        "preflight": preflight, "queries": rows,
        "summary": {"overall": _summary(rows), "by_language": {
            language: _summary([row for row in rows if row["language"] == language])
            for language in LANGUAGES}},
        "failures": {"count": sum(bool(row["failures"]) for row in rows),
                     "by_category": dict(sorted(categories.items()))},
    }
    return report, 1 if blockers else 0


def _validated_paths(parser, args) -> None:
    for name in ("control", "corpus", "index", "queries"):
        path = getattr(args, name)
        if not path.is_absolute() or ".." in path.parts:
            parser.error(f"--{name} must be an absolute path without '..'")
    if args.index != args.corpus.parent / "research.db":
        parser.error("--index must name research.db in the parent directory of --corpus")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -B -m tools.evaluate_retrieval",
        description="Evaluate retrieval and fresh research packets on a checkpointed snapshot. "
                    "Every path is required and explicit; the report is printed to stdout.")
    parser.add_argument("--control", type=Path, required=True, help="checkpointed Control snapshot control.db")
    parser.add_argument("--corpus", type=Path, required=True, help="relocated research corpus directory")
    parser.add_argument("--index", type=Path, required=True, help="research.db beside the corpus directory")
    parser.add_argument("--queries", type=Path, required=True, help="private schema_version 1 query suite")
    args = parser.parse_args(argv)
    _validated_paths(parser, args)
    try:
        report, status = evaluate(args.control, args.corpus, args.index, args.queries)
    except SuiteInvalid as error:
        print(f"evaluate_retrieval: {error}", file=sys.stderr)
        return 2
    except EvaluationError as error:
        report, status = {"report_version": REPORT_VERSION, "status": "failed", "error": error.category}, 1
    except Exception:  # noqa: BLE001 -- never print raw paths or source text
        report, status = {"report_version": REPORT_VERSION, "status": "failed", "error": "unexpected_error"}, 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
