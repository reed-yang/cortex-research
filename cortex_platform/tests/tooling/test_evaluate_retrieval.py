"""Checkout-only retrieval evaluation over a real, checkpointed synthetic snapshot.

Fixtures reuse the real research schema, adoption manifests and Control store
from the source tests. Every writer is closed before evaluation so the
immutable readers see a checkpointed database, which is the only state the
tool accepts. All paper titles, identities and queries are synthetic.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.engine.digests import content_tree, tree_digest
from cortex_platform.product.research import evidence
from cortex_platform.product.research.context import canonical, digest
from cortex_platform.product.research.service import ResearchService
from cortex_platform.product.sources.adoption import read_corpus
from cortex_platform.tests.product.research.test_execution import queued
from cortex_platform.tests.product.sources.fakes import make_store
from cortex_platform.tests.product.sources.test_adoption_reader import (
    _add_paper,
    _write,
    corpus,
    database,
)
from tools import evaluate_retrieval as tool
from tools.evaluate_retrieval import (
    CheckpointedEvaluationReader,
    EvaluationContextStore,
    SuiteInvalid,
    author_section_ranges,
    canonical_identity,
    evaluate,
    grounding_mirror_ranges,
    grounding_ranges,
    load_suite,
    main,
    packet_coverage,
    prepare_packet,
    registered_sources,
    retrieval_metrics,
    section_ranges,
)

REPOSITORY = Path(__file__).resolve().parents[3]
PAPERS = (
    ("20261001-Alpha", "Kestrel Alpha", "2610.00001", ["kestrel kestrel kestrel alpha"] * 3),
    ("20261001-Beta", "Kestrel Beta", "2610.00002", ["kestrel kestrel kestrel beta"] * 3),
    ("20261001-Gamma", "Gamma Study", "2610.00003", ["a long gamma passage " * 20 + "kestrel"]),
    ("20261001-Delta", "Delta Study", "2610.00004", ["unrelated delta content"]),
    ("20261001-中文", "机器人推测解码方法", "2610.00005", ["robot decoding method"]),
)
A, B, C, D, E = (f"arxiv:{paper[2]}" for paper in PAPERS)
F = "sha256:" + hashlib.sha256(b"blog body").hexdigest()
SETS = {"birds": [A, B, C, D], "robots": [E]}
QUERIES = [
    {"id": "en-birds", "query": "kestrel", "language": "en", "relevance_set": "birds",
     "must_find": [C]},
    {"id": "zh-robots", "query": "机器人", "language": "zh", "relevance_set": "robots"},
    {"id": "mixed-birds", "query": "kestrel 机器人", "language": "mixed", "relevance_set": "birds"},
    {"id": "zh-empty", "query": "不存在", "language": "zh", "relevance_set": "robots"},
    {"id": "en-empty", "query": "zzzunmatched", "language": "en", "relevance_set": "birds"},
]


def write_suite(path: Path, queries=None, sets=None) -> Path:
    path.write_text(json.dumps({
        "schema_version": 1, "relevance_sets": SETS if sets is None else sets,
        "queries": QUERIES if queries is None else queries,
    }, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def checkpointed_suite(tmp_path, corpus, database):
    """A registered root, six adopted papers and one high-ranking unadopted paper."""
    store = make_store(tmp_path)
    store.register_asset_root(
        root_id="research-corpus", private_path=corpus, max_bytes=1 << 30,
        enabled=True, actor_id="operator", idempotency_key="root-evaluation-001",
    )
    for paper_dir, title, arxiv_id, chunks in PAPERS:
        _add_paper(database, corpus, paper_dir=paper_dir, title=title, arxiv_id=arxiv_id)
        for index, text in enumerate(chunks):
            _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
                   (paper_dir, "Method", index, text))
    _add_paper(database, corpus, paper_dir="20261001-blog", title="Blog Notes", body="blog body")
    store.commit_adoption_manifest(
        manifest=read_corpus(database=database, corpus_root=corpus).manifest,
        corpus_root_id="research-corpus", actor_id="operator", idempotency_key="adopt-evaluation-001",
    )
    _add_paper(database, corpus, paper_dir="unadopted", title="Kestrel 机器人 Unadopted",
               arxiv_id="2610.09999")
    _write(database, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
           ("unadopted", "Method", 0, "kestrel kestrel kestrel kestrel kestrel"))
    for suffix in ("-wal", "-journal"):
        assert not (tmp_path / f"control.db{suffix}").exists()
        assert not (tmp_path / f"research.db{suffix}").exists()
    return SimpleNamespace(
        store=store, root=tmp_path, control=tmp_path / "control.db", corpus=corpus,
        index=database, suite=write_suite(tmp_path / "suite.json"),
    )


def run(snapshot, **paths):
    values = {"control": snapshot.control, "corpus": snapshot.corpus,
              "index": snapshot.index, "queries": snapshot.suite} | paths
    return evaluate(values["control"], values["corpus"], values["index"], values["queries"])


def cli(snapshot, capsys, **paths):
    values = {"control": snapshot.control, "corpus": snapshot.corpus,
              "index": snapshot.index, "queries": snapshot.suite} | paths
    code = main([f"--{name}={value}" for name, value in values.items()])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None), out


def by_id(report):
    return {row["id"]: row for row in report["queries"]}


def files_with_mtimes(root: Path):
    return tree_digest(content_tree(root)), {
        str(path.relative_to(root)): path.stat().st_mtime_ns
        for path in sorted(root.rglob("*")) if path.is_file()
    }


# -- Pure metric and suite contracts ---------------------------------------


def test_metric_denominators_and_duplicate_sources():
    six = [{"canonical_id": value} for value in (A, A, A, B, B, B)]
    metrics = retrieval_metrics(six, (A, B, C, D), must_find=(C,))
    assert metrics["returned_results_at_k"] == 6
    assert metrics["distinct_papers_at_k"] == 2
    assert metrics["relevant_papers_at_k"] == 2
    assert metrics["recall_at_k"] == 0.5
    assert metrics["identities"] == [A, B]
    assert metrics["missing_relevant_identities"] == [C, D]
    assert metrics["must_find_missing"] == [C]
    assert metrics["must_find_missing_count"] == 1 and metrics["must_find_missing_truncated"] is False
    twenty = retrieval_metrics(six + [{"canonical_id": C}, {"canonical_id": E}], (A, B, C, D))
    assert twenty["recall_at_k"] == 0.75
    assert twenty["distinct_papers_at_k"] == 4
    assert twenty["relevant_identities"] == [A, B, C]
    empty = retrieval_metrics([], (A, B, C, D))
    assert empty["recall_at_k"] == 0.0 and empty["distinct_papers_at_k"] == 0


MANY = tuple(f"arxiv:2610.{20000 + index:05d}" for index in range(60))


def test_must_find_misses_beyond_the_listing_bound_are_counted():
    metrics = retrieval_metrics([{"canonical_id": MANY[0]}], MANY, must_find=MANY)
    assert metrics["must_find_missing"] == list(MANY[1:51])
    assert metrics["must_find_missing_count"] == 59 and metrics["must_find_missing_truncated"] is True
    assert metrics["missing_relevant_count"] == 59


@pytest.mark.parametrize(("value", "expected"), [
    ("arxiv:2610.00001", "arxiv:2610.00001"),
    ("arxiv:2610.00001v3", "arxiv:2610.00001"),
    ("sha256:" + "AB" * 32, "sha256:" + "ab" * 32),
    ("doi:10.1000/Synthetic.ABC", "doi:10.1000/synthetic.abc"),
])
def test_canonical_identity_normalization(value, expected):
    assert canonical_identity(value) == expected


@pytest.mark.parametrize("value", [
    "2610.00001", "ARXIV:2610.00001", "arxiv:arxiv:2610.00001", "arxiv:abc", "sha256:abc",
    "doi:11.1000/x", "isbn:12345", "", None, 7, "paper:20261001-Alpha",
])
def test_canonical_identity_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        canonical_identity(value)


def _suite_bytes(**overrides) -> bytes:
    suite = {"schema_version": 1, "relevance_sets": {"birds": [A, B]},
             "queries": [{"id": "q1", "query": " kestrel ", "language": "en", "relevance_set": "birds"}]}
    suite.update(overrides)
    return json.dumps(suite, ensure_ascii=False).encode()


HUGE_INTEGER = b'{"schema_version": ' + b"1" * 5000 + b', "relevance_sets": {}, "queries": []}'


def _query(**overrides):
    return [{"id": "q1", "query": "kestrel", "language": "en", "relevance_set": "birds"} | overrides]


@pytest.mark.parametrize("raw", [
    b"not json",
    b'{"schema_version": 1, "schema_version": 1, "relevance_sets": {}, "queries": []}',
    _suite_bytes(schema_version=2),
    _suite_bytes(schema_version=True),
    _suite_bytes(extra=1),
    _suite_bytes(relevance_sets={"birds": []}),
    _suite_bytes(relevance_sets={"birds": [A, A]}),
    _suite_bytes(relevance_sets={"birds": [A, "arxiv:2610.00001v2"]}),
    _suite_bytes(relevance_sets={"birds": ["title:Kestrel Alpha"]}),
    _suite_bytes(relevance_sets={"bad name": [A]}),
    _suite_bytes(queries=[]),
    _suite_bytes(queries=_query(language="fr")),
    _suite_bytes(queries=_query(relevance_set="missing")),
    _suite_bytes(queries=_query(relevance_set=["birds"])),
    _suite_bytes(queries=_query(relevance_set={"birds": 1})),
    _suite_bytes(queries=_query(must_find=[C])),
    _suite_bytes(queries=_query(must_find=[A, A])),
    _suite_bytes(queries=_query(unknown=True)),
    _suite_bytes(queries=_query(query="")),
    _suite_bytes(queries=_query(query="x" * 1025)),
    _suite_bytes(queries=_query(query=7)),
    _suite_bytes(queries=_query() + _query()),
    _suite_bytes(queries=[{"id": "q1", "query": "kestrel", "language": "en"}]),
    _suite_bytes(queries=[{"id": f"q{i}", "query": "kestrel", "language": "en",
                           "relevance_set": "birds"} for i in range(101)]),
    b'{"schema_version": 1, "relevance_sets": {"birds": ["' + A.encode() + b'"]}, '
    b'"queries": [{"id": "q1", "query": "\\ud800", "language": "en", "relevance_set": "birds"}]}',
    b'{"schema_version": NaN, "relevance_sets": {}, "queries": []}',
    pytest.param(HUGE_INTEGER, id="integer-digit-limit"),
])
def test_suite_validation_rejects_malformed_suites(tmp_path, raw):
    path = tmp_path / "suite.json"
    path.write_bytes(raw)
    with pytest.raises(SuiteInvalid):
        load_suite(path)


def test_specific_suite_errors_keep_their_message(tmp_path):
    path = tmp_path / "suite.json"
    path.write_bytes(b'{"schema_version": 1, "schema_version": 1, "relevance_sets": {}, "queries": []}')
    with pytest.raises(SuiteInvalid, match="duplicate JSON object key"):
        load_suite(path)


def test_suite_bounds_and_preserved_query_text(tmp_path, monkeypatch):
    path = tmp_path / "suite.json"
    path.write_bytes(_suite_bytes(relevance_sets={"birds": [A, "sha256:" + "CD" * 32]},
                                  queries=_query(query=" kestrel ", must_find=["sha256:" + "cd" * 32])))
    suite = load_suite(path)
    assert suite["relevance_sets"]["birds"] == (A, "sha256:" + "cd" * 32)
    assert suite["queries"][0]["query"] == " kestrel "
    assert suite["queries"][0]["must_find"] == ("sha256:" + "cd" * 32,)
    assert suite["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(tool, "MAX_SUITE_BYTES", 16)
    with pytest.raises(SuiteInvalid):
        load_suite(path)
    monkeypatch.setattr(tool, "MAX_SUITE_BYTES", 1 << 20)
    monkeypatch.setattr(tool, "MAX_RELEVANCE_IDS", 1)
    with pytest.raises(SuiteInvalid):
        load_suite(path)
    with pytest.raises(SuiteInvalid):
        load_suite(tmp_path / "missing.json")


# -- Real snapshot evaluation -----------------------------------------------


def test_metric_denominators_on_real_six_slot_duplication(checkpointed_suite):
    report, code = run(checkpointed_suite)
    assert code == 0 and report["status"] == "completed" and report["baseline_complete"] is True
    birds = by_id(report)["en-birds"]
    six, twenty = birds["retrieval"]["at_6"], birds["retrieval"]["at_20"]
    assert six["status"] == "measured" and six["retrieval_mode"] == "fts5_or"
    assert six["returned_results_at_k"] == 6
    assert six["identities"] == [A, B]
    assert (six["distinct_papers_at_k"], six["relevant_papers_at_k"], six["recall_at_k"]) == (2, 2, 0.5)
    assert birds["retrieval"]["relevant_papers_per_six_slots"] == round(2 / 6, 6)
    assert six["must_find_missing"] == [C]
    assert twenty["identities"] == [A, B, C] and twenty["recall_at_k"] == 0.75
    assert twenty["must_find_missing"] == []
    # Prepare selects distinct papers: a third source beyond the two in six chunk slots.
    packet = birds["packet"]
    assert packet["status"] == "built"
    assert packet["canonical_ids"] == [A, B, C] and packet["recall"] == 0.75
    assert packet["query"] == packet["retrieval_query"] == "kestrel"
    assert packet["schema_version"] == 1 and len(packet["sha256"]) == 64 and packet["bytes"] > 0
    assert packet["sources_not_in_search_at_6"] == [C]
    assert [e["kind"] for e in packet["sources"][0]["evidence"]] == ["indexed_passage", "notes", "full_text"]
    assert all("text" not in e for s in packet["sources"] for e in s["evidence"])
    assert "arxiv:2610.09999" not in json.dumps(report)


def test_language_cohorts_title_fallback_and_empty_results(checkpointed_suite):
    report, code = run(checkpointed_suite)
    rows = by_id(report)
    zh = rows["zh-robots"]["retrieval"]["at_6"]
    assert zh["retrieval_mode"] == "unicode_title_fallback" and zh["identities"] == [E]
    assert rows["zh-robots"]["packet"]["canonical_ids"] == [E]
    mixed = rows["mixed-birds"]["retrieval"]
    assert mixed["at_6"]["retrieval_mode"] == "fts5_or+unicode_title_fallback"
    assert mixed["at_6"]["identities"] == [E, A, B] and mixed["at_6"]["recall_at_k"] == 0.5
    assert mixed["at_20"]["identities"] == [E, A, B, C]
    for name in ("zh-empty", "en-empty"):
        row = rows[name]
        assert row["retrieval"]["at_6"]["returned_results_at_k"] == 0
        assert row["retrieval"]["at_6"]["recall_at_k"] == 0.0
        assert row["packet"]["status"] == "research_no_evidence"
        assert row["packet"]["sha256"] is None and row["packet"]["recall"] == 0.0
        assert row["failures"] == []
    languages = report["summary"]["by_language"]
    assert languages["zh"]["queries"] == 2 and languages["mixed"]["queries"] == 1
    assert languages["zh"]["distinct_papers_at_6"] == {"mean": 0.5, "n": 2, "total": 1}
    assert languages["en"]["recall_at_6"] == {"mean": 0.25, "n": 2}
    assert report["summary"]["overall"]["failed_queries"] == 0
    assert report["failures"] == {"count": 0, "by_category": {}}


@pytest.mark.parametrize("question", ["kestrel", "机器人", "kestrel 机器人"])
def test_real_prepare_parity(checkpointed_suite, monkeypatch, question):
    # The writable test store is used only before evaluation takes its snapshot.
    store = checkpointed_suite.store
    run_row = queued(store, f"/research {question}")
    expected = ResearchService(store).prepare(run_row, store.list_messages(run_row["thread_id"]))["snapshot"]

    def forbidden(*args, **kwargs):
        pytest.fail("evaluation must not use a writable Control store")

    monkeypatch.setattr(ControlStore, "initialize", forbidden)
    monkeypatch.setattr(ControlStore, "_connect", forbidden)

    def normalized(packet):
        value = json.loads(canonical(packet))
        value["authority"]["message_id"] = "<message>"
        return value

    reader = CheckpointedEvaluationReader(checkpointed_suite.control, checkpointed_suite.corpus)
    packet, sha256 = prepare_packet(reader, registered_sources(reader), question)
    assert normalized(packet) == normalized(expected)
    assert sha256 == digest(canonical(packet))
    assert packet["authority"]["message_id"] == EvaluationContextStore.MESSAGE_ID
    assert packet["sources"]


def test_context_adapter_fails_closed_on_unknown_calls_and_identities(checkpointed_suite):
    reader = CheckpointedEvaluationReader(checkpointed_suite.control, checkpointed_suite.corpus)
    store = EvaluationContextStore(registered_sources(reader), "kestrel")
    with pytest.raises(tool.EvaluationError):
        store.list_research_documents("item")
    with pytest.raises(tool.EvaluationError):
        store.path
    with pytest.raises(tool.EvaluationError):
        store.get_run("other-run")
    with pytest.raises(tool.EvaluationError):
        store.get_source("unknown-source")
    with pytest.raises(tool.EvaluationError):
        store.previous_research_context("other-thread", EvaluationContextStore.MESSAGE_ID)
    packet, sha256 = prepare_packet(reader, registered_sources(reader), "kestrel")
    request = dict(run_id=store.run["id"], thread_id=store.run["thread_id"],
                   attempt_id=store.run["active_attempt_id"], message_id=store.MESSAGE_ID,
                   query="kestrel", snapshot=packet, sha256=sha256, expected_revision=1,
                   actor_id="research-execution", idempotency_key=digest(f"context:{store.run['id']}"))
    for change in ({"expected_revision": 2}, {"sha256": "0" * 64}, {"query": "other"},
                   {"actor_id": "operator"}, {"message_id": "other"}):
        with pytest.raises(tool.EvaluationError):
            store.record_research_context(**(request | change))
    assert store.record_research_context(**request).value["snapshot"] == packet
    with pytest.raises(tool.EvaluationError):
        store.record_research_context(**request)


def test_whitespace_query_is_preserved_for_search_and_reported_for_packet(checkpointed_suite):
    write_suite(checkpointed_suite.suite, queries=[
        {"id": "padded", "query": "  kestrel  ", "language": "en", "relevance_set": "birds"}])
    report, code = run(checkpointed_suite)
    row = report["queries"][0]
    assert code == 0 and row["query_sha256"] == digest("  kestrel  ")
    assert row["retrieval"]["at_6"]["identities"] == [A, B]
    assert row["packet"]["query"] == row["packet"]["retrieval_query"] == "kestrel"


def test_read_only_snapshot_inputs(checkpointed_suite, monkeypatch):
    from cortex_research import db as legacy

    root = checkpointed_suite.root
    before = files_with_mtimes(root)
    original = sqlite3.connect
    observed, statements = [], []

    class Checked(sqlite3.Connection):
        def close(self):
            assert self.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                self.execute("CREATE TABLE forbidden_write (id INTEGER)")
            super().close()

    def connect(path, **kwargs):
        assert kwargs["uri"] is True and "mode=ro" in path and "immutable=1" in path
        observed.append(path)
        connection = original(path, factory=Checked, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    def forbidden(*args, **kwargs):
        pytest.fail("evaluation must not initialize, dispatch, persist or connect")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(ControlStore, "initialize", forbidden)
    monkeypatch.setattr(ControlStore, "_connect", forbidden)
    monkeypatch.setattr(legacy, "connect", forbidden)
    monkeypatch.setattr(legacy, "apply_schema", forbidden)
    for name in ("system_message", "annotate", "persist_response"):
        monkeypatch.setattr(ResearchService, name, forbidden)
    report, code = run(checkpointed_suite)
    assert code == 0 and observed
    assert not any("embedding" in statement.lower() for statement in statements)
    assert files_with_mtimes(root) == before


def test_import_isolation_and_help_without_state(tmp_path):
    environment = dict(os.environ, HOME=str(tmp_path))
    help_run = subprocess.run(
        [sys.executable, "-B", "-m", "tools.evaluate_retrieval", "--help"],
        cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=60)
    assert help_run.returncode == 0 and "--control" in help_run.stdout
    probe = subprocess.run(
        [sys.executable, "-B", "-c",
         "import sys, tools.evaluate_retrieval; "
         "print(sorted(m for m in sys.modules if m.startswith(('cortex_research.index_papers', "
         "'cortex_research.embed', 'tools.maintain_paper_index', 'cortex_platform.runtime'))))"],
        cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0 and probe.stdout.strip() == "[]"
    assert list(tmp_path.iterdir()) == []


def test_repeated_frozen_runs_have_identical_reports(checkpointed_suite):
    first, _ = run(checkpointed_suite)
    second, _ = run(checkpointed_suite)
    assert canonical(first) == canonical(second)
    assert first["inputs"]["unchanged"] is True
    fingerprint = first["inputs"]["fingerprint"]
    assert fingerprint["suite"]["sha256"] == hashlib.sha256(checkpointed_suite.suite.read_bytes()).hexdigest()
    assert fingerprint["control"]["sha256"] == hashlib.sha256(checkpointed_suite.control.read_bytes()).hexdigest()
    assert fingerprint["index"]["sha256"] == hashlib.sha256(checkpointed_suite.index.read_bytes()).hexdigest()
    assert fingerprint["corpus"]["sources"] == 6
    assert "tools.evaluate_retrieval" in fingerprint["code"]


def test_code_fingerprint_covers_every_product_module_prepare_and_search_import():
    code = tool._code_fingerprint()
    # Packet contents depend on the derived retrieval query and the evidence
    # windows; canonical identities depend on the identity helpers.
    for name in ("research.service", "research.query", "research.evidence", "research.context",
                 "sources.reader", "sources.search", "sources.identity"):
        module = importlib.import_module(f"cortex_platform.product.{name}")
        assert code[module.__name__] == hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    assert code["tools.evaluate_retrieval"] == hashlib.sha256(Path(tool.__file__).read_bytes()).hexdigest()
    # Closed under use: any product function, class or module a fingerprinted
    # module binds comes from a fingerprinted module too.
    for name in code:
        module = tool if name == "tools.evaluate_retrieval" else importlib.import_module(name)
        if Path(module.__file__).name == "__init__.py":
            continue   # a package also holds every submodule other code loaded
        for value in vars(module).values():
            owner = value.__name__ if isinstance(value, ModuleType) else getattr(value, "__module__", None)
            if isinstance(owner, str) and owner.startswith("cortex_platform.product"):
                assert owner in code, (name, owner)


# -- Snapshot refusals and availability ---------------------------------------


@pytest.mark.parametrize("sidecar", ["control.db-wal", "control.db-journal",
                                     "research.db-wal", "research.db-journal"])
def test_nonempty_log_is_refused_without_touching_it(checkpointed_suite, capsys, sidecar):
    path = checkpointed_suite.root / sidecar
    path.write_bytes(b"outstanding frames")
    before = files_with_mtimes(checkpointed_suite.root)
    code, report, out = cli(checkpointed_suite, capsys)
    assert code == 1 and report["status"] == "failed"
    assert report["error"] == "database_not_checkpointed"
    assert files_with_mtimes(checkpointed_suite.root) == before
    assert str(checkpointed_suite.root) not in out


@pytest.mark.parametrize("sidecar", ["control.db-shm", "research.db-shm"])
def test_nonempty_shared_memory_alone_is_accepted(checkpointed_suite, sidecar):
    (checkpointed_suite.root / sidecar).write_bytes(b"\0" * 32)
    report, code = run(checkpointed_suite)
    assert code == 0 and report["status"] == "completed"


@pytest.mark.parametrize("arguments", [
    {"control": Path("control.db")},
    {"corpus": Path("/tmp/a/../corpus")},
    {"index": Path("/tmp/elsewhere/research.db")},
    {"queries": Path("relative.json")},
])
def test_invalid_path_arguments_exit_two(checkpointed_suite, capsys, arguments):
    with pytest.raises(SystemExit) as raised:
        cli(checkpointed_suite, capsys, **arguments)
    assert raised.value.code == 2


def test_index_must_be_the_corpus_parent_database_and_dotdot_is_refused(checkpointed_suite, capsys):
    dotted = checkpointed_suite.corpus / ".." / "research.db"
    with pytest.raises(SystemExit) as raised:
        cli(checkpointed_suite, capsys, index=dotted)
    assert raised.value.code == 2


@pytest.mark.parametrize("raw", [
    b"{}",
    _suite_bytes(queries=_query(relevance_set=["birds"])),
    pytest.param(HUGE_INTEGER, id="integer-digit-limit"),
])
def test_malformed_suite_exits_two(checkpointed_suite, capsys, raw):
    checkpointed_suite.suite.write_bytes(raw)
    code, report, _ = cli(checkpointed_suite, capsys)
    assert code == 2 and report is None


@pytest.mark.parametrize("defect", ["missing", "symlink", "hardlink"])
def test_database_file_defects_are_refused(checkpointed_suite, capsys, tmp_path_factory, defect):
    control = checkpointed_suite.control
    outside = tmp_path_factory.mktemp("outside") / "control.db"
    if defect == "missing":
        control.unlink()
    elif defect == "symlink":
        shutil.copyfile(control, outside)
        control.unlink()
        control.symlink_to(outside)
    else:
        os.link(control, outside)
    code, report, _ = cli(checkpointed_suite, capsys)
    assert code == 1 and report["error"] == "database_unavailable"


def test_disabled_root_is_a_named_refusal(checkpointed_suite, capsys):
    store = checkpointed_suite.store
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(root_id=root.root_id, expected_revision=root.revision,
                            private_path=root.private_path, max_bytes=root.max_bytes,
                            enabled=False, actor_id="operator", idempotency_key="disable-evaluation-1")
    code, report, _ = cli(checkpointed_suite, capsys)
    assert code == 1 and report["error"] == "registration_unavailable"


def _relocate(snapshot, destination: Path) -> SimpleNamespace:
    (destination / "research").mkdir(parents=True)
    shutil.copyfile(snapshot.control, destination / "control.db")
    shutil.copyfile(snapshot.index, destination / "research" / "research.db")
    shutil.copytree(snapshot.corpus, destination / "research" / "corpus")
    shutil.copyfile(snapshot.suite, destination / "suite.json")
    return SimpleNamespace(root=destination, control=destination / "control.db",
                           corpus=destination / "research" / "corpus",
                           index=destination / "research" / "research.db",
                           suite=destination / "suite.json")


def test_relocated_corpus_never_reads_the_registered_private_path(checkpointed_suite, tmp_path_factory):
    original, _ = run(checkpointed_suite)
    relocated = _relocate(checkpointed_suite, tmp_path_factory.mktemp("snapshot"))
    shutil.rmtree(checkpointed_suite.corpus)
    report, code = run(relocated)
    assert code == 0
    assert report["snapshot"]["kind"] == "offline_relocated_snapshot"
    assert [row["retrieval"] for row in report["queries"]] == [
        row["retrieval"] for row in original["queries"]]
    assert [row["packet"]["canonical_ids"] for row in report["queries"] if row["packet"]["status"] == "built"] \
        == [row["packet"]["canonical_ids"] for row in original["queries"] if row["packet"]["status"] == "built"]


def test_registered_byte_limit_still_applies_to_relocated_reads(checkpointed_suite, tmp_path_factory):
    store = checkpointed_suite.store
    root = store.get_asset_root("research-corpus")
    store.update_asset_root(root_id=root.root_id, expected_revision=root.revision,
                            private_path=root.private_path, max_bytes=8, enabled=True,
                            actor_id="operator", idempotency_key="limit-evaluation-01")
    relocated = _relocate(checkpointed_suite, tmp_path_factory.mktemp("snapshot"))
    report, code = run(relocated)
    assert code == 1 and report["status"] == "incomplete"
    assert report["preflight"]["unavailable_sources"] == 6
    assert "relevance_identity_unavailable" in report["blockers"]


@pytest.mark.parametrize(("defect", "reason"), [
    ("hardlink", "notes:hardlinked"),
    ("symlink-file", "notes:symlink"),
    ("missing-directory", "directory:missing"),
    ("symlink-directory", "directory:symlink"),
    ("invalid-utf8", "notes:invalid_utf8"),
])
def test_snapshot_defects_are_named_and_block_a_baseline(checkpointed_suite, tmp_path_factory, defect, reason):
    directory = checkpointed_suite.corpus / "20261001-Gamma"
    outside = tmp_path_factory.mktemp("outside")
    if defect == "hardlink":
        os.link(directory / "notes.md", outside / "notes.md")
    elif defect == "symlink-file":
        (outside / "notes.md").write_text("outside")
        (directory / "notes.md").unlink()
        (directory / "notes.md").symlink_to(outside / "notes.md")
    elif defect == "missing-directory":
        shutil.rmtree(directory)
    elif defect == "symlink-directory":
        shutil.move(directory, outside / "paper")
        directory.symlink_to(outside / "paper", target_is_directory=True)
    else:
        (directory / "notes.md").write_bytes(b"\xff")
    report, code = run(checkpointed_suite)
    assert code == 1 and report["status"] == "incomplete" and report["baseline_complete"] is False
    preflight = report["preflight"]
    assert preflight["unavailable_by_reason"] == {reason: 1}
    assert preflight["relevance"]["unavailable"] == [C]
    assert "relevance_identity_unavailable" in report["blockers"]
    # Metrics are still measured; the defect is not silently converted to recall.
    assert by_id(report)["en-birds"]["retrieval"]["at_6"]["status"] == "measured"


def test_unadopted_relevance_identity_blocks_a_baseline_without_shrinking_recall(checkpointed_suite):
    write_suite(checkpointed_suite.suite, sets={"birds": [A, B, C, D, "arxiv:2610.09999"], "robots": [E]})
    report, code = run(checkpointed_suite)
    assert code == 1 and report["status"] == "incomplete" and report["baseline_complete"] is False
    assert report["blockers"] == ["relevance_identity_not_adopted"]
    assert report["preflight"]["relevance"]["not_adopted"] == ["arxiv:2610.09999"]
    six = by_id(report)["en-birds"]["retrieval"]["at_6"]
    assert six["recall_at_k"] == 0.4 and six["missing_relevant_count"] == 3


def test_must_find_counts_on_search_and_packet_outcomes(checkpointed_suite):
    write_suite(checkpointed_suite.suite, sets={"many": list(MANY)}, queries=[
        {"id": "many", "query": "kestrel", "language": "en", "relevance_set": "many",
         "must_find": list(MANY)}])
    report, _ = run(checkpointed_suite)
    row = report["queries"][0]
    for outcome in (row["retrieval"]["at_6"], row["retrieval"]["at_20"], row["packet"]):
        assert len(outcome["must_find_missing"]) == 50
        assert outcome["must_find_missing_count"] == 60 and outcome["must_find_missing_truncated"] is True
    write_suite(checkpointed_suite.suite, queries=[
        {"id": "none", "query": "zzzunmatched", "language": "en", "relevance_set": "birds",
         "must_find": [A]}])
    report, _ = run(checkpointed_suite)
    packet = report["queries"][0]["packet"]
    assert packet["status"] == "research_no_evidence"
    assert (packet["must_find_missing"], packet["must_find_missing_count"],
            packet["must_find_missing_truncated"]) == ([A], 1, False)


def test_independent_limit_failures_stay_visible(checkpointed_suite):
    _write(checkpointed_suite.index,
           "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
           ("20261001-Delta", "Appendix", 1, "kestrel " + "filler " * 12000))
    report, code = run(checkpointed_suite)
    row = by_id(report)["en-birds"]
    assert row["retrieval"]["at_6"]["status"] == "measured"
    assert row["retrieval"]["at_20"] == {"status": "failed", "failure": "source_content_unavailable"}
    # Paper-level packet selection also reaches the oversized chunk's paper.
    assert row["packet"]["status"] == "failed" and row["packet"]["failure"] == "research_corpus_unavailable"
    assert row["failures"] == ["search_at_20:source_content_unavailable", "packet:research_corpus_unavailable"]
    assert code == 1 and report["status"] == "incomplete" and "query_failures" in report["blockers"]
    assert report["failures"]["by_category"]["search_at_20:source_content_unavailable"] >= 1
    overall = report["summary"]["overall"]
    assert overall["recall_at_20"]["n"] < overall["recall_at_6"]["n"]


def test_cli_reports_query_failures_without_private_paths(checkpointed_suite, capsys):
    write_suite(checkpointed_suite.suite, queries=QUERIES + [
        {"id": "no-terms", "query": "!!!", "language": "en", "relevance_set": "birds"}])
    code, report, out = cli(checkpointed_suite, capsys)
    row = by_id(report)["no-terms"]
    assert row["retrieval"]["at_6"] == {"status": "failed", "failure": "source_query_invalid"}
    assert row["packet"]["status"] == "failed" and row["packet"]["failure"] == "research_query_invalid"
    assert code == 1 and report["failures"]["count"] == 1
    assert report["summary"]["overall"]["failed_queries"] == 1
    assert report["summary"]["overall"]["recall_at_6"]["n"] == 5
    assert str(checkpointed_suite.root) not in out
    assert "notes for" not in out


def test_input_mutation_during_evaluation_refuses_a_completed_baseline(checkpointed_suite, monkeypatch):
    original = CheckpointedEvaluationReader.search
    target = checkpointed_suite.corpus / "20261001-Delta" / "notes.md"

    def mutating(self, query, limit=10, *, per_source=None):
        target.write_text("changed during evaluation", encoding="utf-8")
        return original(self, query, limit=limit, per_source=per_source)

    monkeypatch.setattr(CheckpointedEvaluationReader, "search", mutating)
    report, code = run(checkpointed_suite)
    assert code == 1 and report["status"] == "incomplete"
    assert report["inputs"]["unchanged"] is False
    assert report["inputs"]["changed"] == ["corpus"]
    assert "input_drift" in report["blockers"]


# -- Conservative section coverage --------------------------------------------

GROUNDING_FRONT = ("---\npaper_dir: synthetic\narxiv_id: \nfull_text_sha: " + "0" * 64
                   + "\nschema_version: 2\ndepth: deep\nmodel: synthetic\n"
                   "created_at: 2026-10-01T00:00:00+00:00\n---\n\n")
BRIEF = ("mechanism", "bottleneck", "rejects_assumes", "key_results", "open_threads", "anchors")


def grounding_sidecar(*, fenced=True, human=None, ascii=False, **fields) -> str:
    """The grounding v2 sidecar shape: front matter, fenced JSON, Markdown mirror."""
    payload = {key: fields.get(key, f"{key} text") for key in BRIEF}
    if human is not None:
        payload["human"] = human
        payload["keywords"] = ["synthetic"]
    body = json.dumps(payload, ensure_ascii=ascii, indent=2)
    mirror = "\n".join(f"## {key}\n\n{payload[key]}\n" for key in BRIEF if isinstance(payload[key], str))
    return GROUNDING_FRONT + ("```json\n" + body + "\n```\n\n" if fenced else body + "\n\n") + mirror


def body(raw: bytes, ranges) -> list[str]:
    return [raw[start:end].decode() for start, end in ranges]


def test_section_ranges_recognize_exact_heading_bodies():
    raw = ("---\ntitle: synthetic\nnote: |\n  ## Key Results\n---\n"
           "# Synthetic paper\n\n```\n## Limitations\n```\n~~~~\n## Key Results\n~~~~\n"
           "## Key Results\n\nResult body.\n\n### Detail\n\nDeeper 结果.\n\n"
           "## LIMITATIONS ##\r\n\r\nLimit body.\r\n## Keywords\nkestrel\n").encode()
    ranges = section_ranges(raw)
    # Nested heading lines split the body; only the text between them counts.
    assert body(raw, ranges["key_results"]) == ["\nResult body.\n\n", "\nDeeper 结果.\n\n"]
    (limit,) = body(raw, ranges["limitations"])
    assert limit.strip() == "Limit body." and "Keywords" not in limit


@pytest.mark.parametrize(("text", "key_results", "limitations"), [
    # Numbered or decorated headings are not the evidence-reads names: unknown, not absent.
    ("## 3. Key Results\n\nBody.\n\n## **Limitations**\n\nBody.\n", None, None),
    # Heading-only sections and prose mentions are known negatives.
    ("## Key Results\n\n## Limitations\n\nLimit.\n", [], ["\nLimit.\n"]),
    ("## Key Results\n\n### More detail\n\n## Limitations\n\nLimit.\n", [], ["\nLimit.\n"]),
    # Empty ATX and setext headings end a section; a thematic break after a blank line does not.
    ("## Key Results\n\nResult.\n\n##\n\nLater body.\n", ["\nResult.\n\n"], []),
    ("## Key Results\n\nResult.\n\nLater\n-----\n\nLater body.\n", ["\nResult.\n\n"], []),
    ("## Key Results\n\nResult.\n\nLater\n=====\n\nLater body.\n", ["\nResult.\n\n"], []),
    ("## Key Results\n\nResult.\n---\n", [], []),
    ("## Key Results\n\nResult.\n\n---\n\nMore.\n", ["\nResult.\n\n---\n\nMore.\n"], []),
    # A setext heading titled like a target is not the evidence-reads name: unknown.
    ("Key Results\n===========\n\nBody.\n", None, []),
    ("Our limitations and key results are discussed in prose only.\n", [], []),
    ("# Key Results\n   \n", [], []),
    # Unclosed front matter or fence cannot be classified.
    ("---\ntitle: open\n## Key Results\n\nBody.\n", None, None),
    ("```\n## Key Results\n\nBody.\n", None, None),
])
def test_section_ranges_keep_unknown_and_absent_distinct(text, key_results, limitations):
    raw = text.encode()
    ranges = section_ranges(raw)
    for name, expected in (("key_results", key_results), ("limitations", limitations)):
        assert (None if ranges[name] is None else body(raw, ranges[name])) == expected


def test_grounding_ranges_map_json_values_not_names_or_mirror():
    human = {"takeaway": "t", "key_results_human": "Human result.", "limitations": ["Human limit.", ""]}
    raw = grounding_sidecar(key_results="Result 结果.", open_threads="Open thread.", human=human).encode()
    ranges = grounding_ranges(raw)
    assert body(raw, ranges["key_results"]) == ["Result 结果."]
    assert body(raw, ranges["open_threads"]) == ["Open thread."]
    assert body(raw, ranges["key_results_human"]) == ["Human result."]
    assert body(raw, ranges["limitations_human"]) == ["Human limit."]
    bare = grounding_sidecar(fenced=False, key_results=["R1", {"note": "R2"}], open_threads="").encode()
    ranges = grounding_ranges(bare)
    assert body(bare, ranges["key_results"]) == ["R1", "R2"]
    assert ranges["open_threads"] == [] and ranges["limitations_human"] == []
    # JSON values only; grounding_mirror_ranges judges the trailing Markdown mirror.
    mirrored = (GROUNDING_FRONT + '```json\n{"mechanism": "m"}\n```\n\n## open_threads\n\nMirror only.\n').encode()
    assert grounding_ranges(mirrored)["open_threads"] == []
    assert body(mirrored, grounding_mirror_ranges(mirrored)["open_threads"]) == ["\nMirror only.\n"]


def test_grounding_mirror_ranges_use_the_packet_heading_rules():
    raw = grounding_sidecar(key_results="Result 结果.", open_threads="").encode()
    ranges = grounding_mirror_ranges(raw)
    assert body(raw, ranges["key_results"]) == ["\nResult 结果.\n\n"]
    assert ranges["open_threads"] == []
    # Exact casefolded ATX titles outside fences, as evidence-reads selects;
    # nested heading lines are not body.
    raw = (GROUNDING_FRONT + "```\n## key_results\nfenced\n```\n## Key_Results ##\n\nCased.\n"
           "## key results\n\nSpaced.\n## open_threads\n\n### sub\n\nNested.\n").encode()
    ranges = grounding_mirror_ranges(raw)
    assert body(raw, ranges["key_results"]) == ["\nCased.\n"]
    assert body(raw, ranges["open_threads"]) == ["\nNested.\n"]


@pytest.mark.parametrize("text", [
    GROUNDING_FRONT + "## key_results\n\nMarkdown only.\n",
    GROUNDING_FRONT + '```json\n{"key_results": "x",}\n```\n',
    GROUNDING_FRONT + '```json\n["key_results"]\n```\n',
    GROUNDING_FRONT + '```json\n{"key_results": "a", "key_results": "b"}\n```\n',
    GROUNDING_FRONT + '```json\n{"key_results": "x"}\n',
    "---\nunclosed front matter\n```json\n{}\n```\n",
])
def test_unsupported_grounding_layouts_are_unknown(text):
    assert set(grounding_ranges(text.encode()).values()) == {None}


@pytest.mark.parametrize(("text", "expected"), [
    # The first Limitations section before References; the document title
    # heading is never a candidate, and nested heading lines are not body.
    ("# Scope Limitations\n\n## 1 Intro\n\nIntro.\n\n## 5 Conclusion\n\nConcluded.\n\n"
     "## 6 Limitations\n\n### 6.1 Scope\n\nLimited.\n\n## References\n\n[1] Ref.\n", ["\nLimited.\n\n"]),
    # Conclusion is the fallback; a Limitations section after References is not.
    ("# Paper\n\n## 5 Conclusion\n\nConcluded.\n\n## References\n\n[1] Ref.\n\n## Limitations\n\nLate.\n",
     ["\nConcluded.\n\n"]),
    # A blank Limitations section is skipped.
    ("# Paper\n\n## Limitations\n\n## Conclusion\n\nConcluded.\n", ["\nConcluded.\n"]),
    # No candidate section is a known negative.
    ("# Paper\n\n## Method\n\nBody.\n", []),
])
def test_author_section_ranges_follow_the_packet_selection(text, expected):
    raw = text.encode()
    assert body(raw, author_section_ranges(raw)["author_section"]) == expected


def _notes(title: str, sections: str, *, pad: int = 0) -> str:
    filler = "".join(f"Background sentence {index} about {title}.\n" for index in range(pad))
    return f"---\ntitle: {title}\n---\n\n# {title}\n\n## Summary\n\n{filler}\n{sections}"


SECTIONS = "## Key Results\n\n{0} result.\n\n## Limitations\n\n{0} limitation.\n\n## Keywords\n\nk\n"


@pytest.fixture
def covered(checkpointed_suite):
    corpus = checkpointed_suite.corpus
    alpha, beta = corpus / "20261001-Alpha", corpus / "20261001-Beta"
    (alpha / "notes.md").write_text(_notes("Kestrel Alpha", SECTIONS.format("Alpha"), pad=150), encoding="utf-8")
    (beta / "notes.md").write_text(_notes("Kestrel Beta", SECTIONS.format("Beta")), encoding="utf-8")
    (alpha / "grounding.md").write_text(grounding_sidecar(
        mechanism="m" * 4500, key_results="Alpha grounded result.",
        human={"key_results_human": "Alpha human result.", "limitations": "Alpha human limit."}),
        encoding="utf-8")
    (beta / "grounding.md").write_text(grounding_sidecar(
        key_results="Beta grounded result.", open_threads="Beta thread.",
        human={"key_results_human": "", "limitations": "Beta human limit."}), encoding="utf-8")
    (alpha / "full_text.md").write_text(
        "# Kestrel Alpha\n\n## 1 Introduction\n\nAlpha intro.\n\n## 6 Limitations\n\n"
        "Alpha author limitation.\n\n## References\n\n[1] Synthetic reference.\n", encoding="utf-8")
    (beta / "full_text.md").write_text(
        "# Kestrel Beta\n\n## 1 Introduction\n\nBeta intro.\n\n## 5 Conclusion\n\nBeta conclusion.\n",
        encoding="utf-8")
    assert (alpha / "notes.md").read_bytes().index(b"## Key Results") > 4000
    return checkpointed_suite


def test_section_packet_counts_only_retained_section_bodies(covered):
    report, code = run(covered)
    assert code == 0
    packet = by_id(report)["en-birds"]["packet"]
    alpha, beta, gamma = packet["sources"]
    assert [item["kind"] for item in alpha["evidence"]] == ["indexed_passage", "notes", "full_text", "grounding"]
    # The notes window starts at its section headings, past the old 4,000-byte prefix.
    # The grounding window spans the Markdown mirror, whose key_results and
    # open_threads bodies count; human.* values appear only in the JSON block.
    # The full_text window is the paper's own Limitations (Alpha) or Conclusion (Beta).
    assert alpha["coverage"] == {
        "notes_key_results": True, "notes_limitations": True,
        "grounding_key_results": True, "grounding_key_results_human": False,
        "grounding_open_threads": True, "grounding_limitations_human": False,
        "indexed_results_section": False, "indexed_limitations_section": False,
        "has_key_results_text": True, "has_limitations_text": True, "full_text_author_section": True,
    }
    assert beta["coverage"] == {
        "notes_key_results": True, "notes_limitations": True,
        "grounding_key_results": True, "grounding_key_results_human": False,
        "grounding_open_threads": True, "grounding_limitations_human": False,
        "indexed_results_section": False, "indexed_limitations_section": False,
        "has_key_results_text": True, "has_limitations_text": True, "full_text_author_section": True,
    }
    assert gamma["canonical_id"] == C and not any(gamma["coverage"].values())
    assert packet["coverage"]["has_key_results_text"] == {
        "confirmed": 2, "sources": 3, "unknown": 0, "share": round(2 / 3, 6),
        "confirmed_lower_bound": round(2 / 3, 6)}
    assert packet["coverage"]["full_text_author_section"] == packet["coverage"]["has_key_results_text"]
    empty = by_id(report)["en-empty"]["packet"]
    assert empty["coverage"] is None
    overall = report["summary"]["overall"]["coverage"]
    assert list(overall) == ["has_key_results_text", "has_limitations_text", "full_text_author_section"]
    pooled = overall["has_limitations_text"]
    assert pooled["sources"] >= 2 and pooled["unknown"] == 0
    assert overall["full_text_author_section"]["confirmed"] >= 2


def test_packet_windows_from_the_grounding_mirror_count(covered):
    reader, row = _source(covered, "20261001-Gamma")
    raw = grounding_sidecar(key_results="Gamma result.", open_threads="", human={
        "key_results_human": "Gamma human result.", "limitations": "Gamma human limit."}).encode()
    (covered.corpus / "20261001-Gamma" / "grounding.md").write_bytes(raw)
    start, end = evidence._window("grounding", raw, True)
    assert raw[start:end].startswith(b"## mechanism")
    flags = _coverage(reader, row, [_window(raw, "grounding", start, end - start)])["sources"][0]
    assert flags["grounding_key_results"] is True and flags["has_key_results_text"] is True
    # A blank mirror body is a known negative, and the human values stay in the JSON block.
    assert flags["grounding_open_threads"] is False
    assert flags["grounding_key_results_human"] is False and flags["grounding_limitations_human"] is False
    heading = raw.index(b"## key_results")
    only = _coverage(reader, row, [_window(raw, "grounding", heading, len(b"## key_results\n"))])
    assert only["sources"][0]["grounding_key_results"] is False


def test_author_section_flag_is_reported_beside_but_outside_has_limitations_text(covered):
    reader, row = _source(covered, "20261001-Gamma")
    raw = ("# Gamma Study\n\n## 1 Introduction\n\nGamma intro.\n\n## 6 Limitations\n\n"
           "Gamma author limitation.\n\n## References\n\n[1] Synthetic reference.\n").encode()
    (covered.corpus / "20261001-Gamma" / "full_text.md").write_bytes(raw)
    start, end = evidence._window("full_text", raw, True)
    window = _window(raw, "full_text", start, end - start)
    flags = _coverage(reader, row, [window])["sources"][0]
    assert list(flags)[-2:] == ["has_limitations_text", "full_text_author_section"]
    assert flags["full_text_author_section"] is True
    # The aggregate keeps notes and grounding only: the section may be a Conclusion.
    assert flags["has_limitations_text"] is False
    prefix = _window(raw, "full_text", 0, raw.index(b"## 6 Limitations"))
    assert _coverage(reader, row, [prefix])["sources"][0]["full_text_author_section"] is False
    shifted = _window(raw, "full_text", start, end - start, shift=1)
    assert _coverage(reader, row, [shifted])["sources"][0]["full_text_author_section"] is None


def _source(snapshot, paper_dir):
    reader = CheckpointedEvaluationReader(snapshot.control, snapshot.corpus)
    (row,) = [row for row in registered_sources(reader) if row["paper_dir"] == paper_dir]
    return reader, row


def _window(raw: bytes, kind: str, start: int, size: int, *, shift: int = 0, content=None) -> dict:
    text = raw[start:start + size].decode()
    first = raw[:start].count(b"\n") + 1
    last = first + text.count("\n") - int(text.endswith("\n"))
    return {"kind": kind, "text": text, "retained_sha256": digest(text),
            "content_sha256": content or hashlib.sha256(raw).hexdigest(),
            "locator": f"{kind}:lines:{first}-{last}:offset:{start + shift}"}


def _coverage(reader, row, evidence, hits=()):
    packet = {"sources": [{"label": "S1", "source_id": row["id"], "canonical_id": row["canonical_id"],
                           "engine_ref": row["engine_ref"], "evidence": evidence}]}
    return packet_coverage(reader, packet, list(hits), {})


def test_located_windows_count_and_mislocated_windows_stay_unknown(covered):
    reader, row = _source(covered, "20261001-Alpha")
    raw = (covered.corpus / "20261001-Alpha" / "notes.md").read_bytes()
    start = raw.index(b"## Key Results")
    located = _coverage(reader, row, [_window(raw, "notes", start, 120)])
    assert located["sources"][0]["notes_key_results"] is True
    assert located["sources"][0]["notes_limitations"] is True
    heading = _coverage(reader, row, [_window(raw, "notes", start, len(b"## Key Results\n"))])
    assert heading["sources"][0]["notes_key_results"] is False
    shifted = _coverage(reader, row, [_window(raw, "notes", start, 120, shift=1)])
    assert shifted["sources"][0]["notes_key_results"] is None
    stale = _coverage(reader, row, [_window(raw, "notes", start, 120, content="0" * 64)])
    assert stale["sources"][0]["notes_key_results"] is None
    assert stale["sources"][0]["has_key_results_text"] is None
    assert stale["aggregate"]["has_key_results_text"] == {
        "confirmed": 0, "sources": 1, "unknown": 1, "share": None, "confirmed_lower_bound": 0.0}


@pytest.mark.parametrize(("sections", "retained", "expected"), [
    # A complete section whose only content is a nested heading.
    ("## Key Results\n\n### More detail\n\n## Keywords\n\nk\n", "## Key Results\n\n### More detail\n\n", False),
    # A partial excerpt that keeps only the heading and a nested heading.
    ("## Key Results\n\n### More detail\n\nDeep result.\n", "## Key Results\n\n### More detail\n", False),
    ("## Key Results\n\n### More detail\n\nDeep result.\n", "### More detail\n\nDeep result.\n", True),
    # A window that keeps only the next section's body after an empty ATX or setext heading.
    ("## Key Results\n\nResult.\n\n##\n\nLater body.\n", "Later body.\n", False),
    ("## Key Results\n\nResult.\n\nLater\n-----\n\nLater body.\n", "Later body.\n", False),
])
def test_heading_lines_and_later_sections_are_not_target_body(covered, sections, retained, expected):
    raw = _notes("Gamma Study", sections).encode()
    (covered.corpus / "20261001-Gamma" / "notes.md").write_bytes(raw)
    reader, row = _source(covered, "20261001-Gamma")
    start = raw.index(retained.encode())
    flags = _coverage(reader, row, [_window(raw, "notes", start, len(retained.encode()))])["sources"][0]
    assert flags["notes_key_results"] is expected


def test_escaped_grounding_strings_are_decoded_before_coverage(covered):
    reader, row = _source(covered, "20261001-Gamma")
    path = covered.corpus / "20261001-Gamma" / "grounding.md"
    raw = grounding_sidecar(ascii=True, key_results="中文结果。", open_threads="开放问题。",
                            human={"key_results_human": "\n\t ", "limitations": "人工限制。"}).encode()
    assert b"\\u4e2d" in raw
    path.write_bytes(raw)
    fence_end = raw.index(b"```\n\n") + 4
    full = _coverage(reader, row, [_window(raw, "grounding", 0, fence_end)])["sources"][0]
    assert full["grounding_key_results"] is True and full["grounding_open_threads"] is True
    assert full["grounding_limitations_human"] is True
    assert full["grounding_key_results_human"] is False
    # Escaped whitespace alone is not body; a cut inside an escape cannot be classified.
    raw = grounding_sidecar(ascii=True, key_results="\n\n中").encode()
    path.write_bytes(raw)
    value = raw.index(b"\\n\\n\\u4e2d")
    for size, expected in ((value + 4, False), (value + 8, None), (value + 10, True)):
        flags = _coverage(reader, row, [_window(raw, "grounding", 0, size)])["sources"][0]
        assert flags["grounding_key_results"] is expected


def test_missing_and_unsupported_grounding_audits_are_unknown(covered):
    reader, row = _source(covered, "20261001-Gamma")
    grounding = grounding_sidecar(key_results="Gamma result.").encode()
    evidence = [_window(grounding, "grounding", 0, 600)]
    missing = _coverage(reader, row, evidence)["sources"][0]
    assert missing["grounding_key_results"] is None and missing["has_key_results_text"] is None
    (covered.corpus / "20261001-Gamma" / "grounding.md").write_bytes(grounding)
    assert _coverage(reader, row, evidence)["sources"][0]["grounding_key_results"] is True
    markdown = (GROUNDING_FRONT + "## key_results\n\nGamma result.\n").encode()
    (covered.corpus / "20261001-Gamma" / "grounding.md").write_bytes(markdown)
    unsupported = _coverage(reader, row, [_window(markdown, "grounding", 0, len(markdown))])["sources"][0]
    # Without a supported JSON object, only a retained mirror body establishes a flag.
    assert unsupported["grounding_key_results"] is True
    assert unsupported["grounding_open_threads"] is None
    assert unsupported["grounding_limitations_human"] is None
    front = _coverage(reader, row, [_window(markdown, "grounding", 0, len(GROUNDING_FRONT))])["sources"][0]
    assert front["grounding_key_results"] is None
    assert unsupported["notes_key_results"] is False


def test_utf8_cursor_pages_are_reassembled_for_late_sections(covered, monkeypatch):
    directory = covered.corpus / "20261001-Gamma"
    text = "---\ntitle: g\n---\n" + "中文段落用于填充。\n" * 1300 + "## Key Results\n\n迟到的结果。\n"
    (directory / "notes.md").write_text(text, encoding="utf-8")
    raw = text.encode()
    assert raw.index("## Key Results".encode()) > 30_000
    reader, row = _source(covered, "20261001-Gamma")
    calls = []
    original = CheckpointedEvaluationReader.read

    def spy(self, source_id, kind="notes", cursor=None, limit=20000):
        calls.append(cursor)
        return original(self, source_id, kind=kind, cursor=cursor, limit=limit)

    monkeypatch.setattr(CheckpointedEvaluationReader, "read", spy)
    start = raw.index("## Key Results".encode())
    result = _coverage(reader, row, [_window(raw, "notes", start, len(raw) - start)])
    assert result["sources"][0]["notes_key_results"] is True
    assert len(calls) >= 2 and calls[0] is None and all(calls[1:])


def test_indexed_section_flags_use_the_hit_section_and_retained_body(covered):
    rows = [
        ("20261001-Delta", "5 Limitations", "Paper: Delta Study | Section: 5 Limitations\n\nzebrafinch limit body"),
        ("20261001-Gamma", "Limitations", "Paper: Gamma zebrafinch | Section: Limitations\n\n"),
        ("20261001-Alpha", "__catalog__", "Kestrel Alpha | zebrafinch | limitations summary"),
        ("20261001-中文", "Results", "Paper: 机器人推测解码方法 | Section: Results\n\nzebrafinch results"),
    ]
    for paper_dir, section, text in rows:
        _write(covered.index, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
               (paper_dir, section, 9, text))
    write_suite(covered.suite, queries=[
        {"id": "indexed", "query": "zebrafinch", "language": "en", "relevance_set": "birds"}])
    report, code = run(covered)
    flags = {source["canonical_id"]: source["coverage"] for source in report["queries"][0]["packet"]["sources"]}
    assert flags[D]["indexed_limitations_section"] is True
    assert flags[C]["indexed_limitations_section"] is False
    assert flags[A]["indexed_limitations_section"] is False
    assert flags[E]["indexed_results_section"] is True and flags[E]["indexed_limitations_section"] is False
    # Indexed full-text sections never stand in for notes or grounding coverage.
    assert flags[D]["has_limitations_text"] is False
    reader, row = _source(covered, "20261001-Delta")
    unmatched = _coverage(reader, row, [{"kind": "indexed_passage", "text": "zebrafinch limit body",
                                         "retained_sha256": digest("zebrafinch limit body"),
                                         "content_sha256": "1" * 64,
                                         "locator": "source:x:chunk:1:sha256:" + "1" * 64}])
    assert unmatched["sources"][0]["indexed_limitations_section"] is None


def test_indexed_flags_parse_the_prefix_beyond_truncated_section_metadata(covered):
    section = "Results " + "x" * 1000 + " and Limitations"
    text = f"Paper: Delta Study | Section: {section}\n\nzebrafinch results body"
    assert len(section.encode()) > 1000 and len(text.encode()) < 2000
    _write(covered.index, "INSERT INTO chunks (paper_dir, section, chunk_idx, text) VALUES (?, ?, ?, ?)",
           ("20261001-Delta", section, 9, text))
    write_suite(covered.suite, queries=[
        {"id": "indexed", "query": "zebrafinch", "language": "en", "relevance_set": "birds"}])
    report, _ = run(covered)
    (source,) = report["queries"][0]["packet"]["sources"]
    assert source["canonical_id"] == D
    assert source["coverage"]["indexed_results_section"] is True
    assert source["coverage"]["indexed_limitations_section"] is True


@pytest.mark.parametrize(("excerpt", "expected"), [
    # The prefix names another section, or lacks the generated blank-line separator.
    ("Paper: Delta Study | Section: Other\n\nzebrafinch body", None),
    ("Paper: Delta Study | Section: Results\nzebrafinch body", None),
    # The excerpt bound cut the prefix itself: no body was retained.
    ("Paper: Delta Study | Section: Results and more", False),
])
def test_unparseable_indexed_prefixes_are_unknown(covered, excerpt, expected):
    reader, row = _source(covered, "20261001-Delta")
    content = hashlib.sha256(excerpt.encode()).hexdigest()
    locator = f"source:{row['id']}:chunk:7:sha256:{content}"
    evidence = {"kind": "indexed_passage", "text": excerpt, "retained_sha256": digest(excerpt),
                "content_sha256": content, "locator": locator}
    hit = {"evidence_id": locator, "content_sha256": content, "excerpt": excerpt, "section": "Results"}
    flags = _coverage(reader, row, [evidence], [hit])["sources"][0]
    assert flags["indexed_results_section"] is expected
