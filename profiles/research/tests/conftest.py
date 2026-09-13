"""Fixtures for the retained `cortex_research` tests.

The suite is narrowed to the nine supported modules, so the autouse isolation
that used to guard the legacy engine (detached spawns, Telegram delivery, the
review expander, repo-grounding network legs, the director's decision log, the
ideas/projects vault) has no subject left in this tree. What remains is the
isolation the kept modules still need: a per-test research DB, corpus/state
roots that never resolve into the operator's home, embeddings off by default,
and the arXiv throttle disabled so real sleeps cannot slow the suite.
"""

import pytest


@pytest.fixture
def research_db(tmp_path, monkeypatch):
    db = tmp_path / "research.db"
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(db))
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research.db import connect, apply_schema
    conn = connect(db)
    apply_schema(conn)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _isolate_corpus_paths_when_unset(tmp_path, monkeypatch):
    """Default the research DB + agent-readings dir to tmp for tests that DON'T set
    them, so no test accidentally reads/writes the operator's corpus. Tests that DO
    set these env vars keep their own value (setenv-when-unset)."""
    import os
    if not os.environ.get("CORTEX_RESEARCH_DB"):
        monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "conftest-research.db"))
    if not os.environ.get("CORTEX_AGENT_READINGS"):
        monkeypatch.setenv("CORTEX_AGENT_READINGS", str(tmp_path / "conftest-readings"))


@pytest.fixture(autouse=True)
def _disable_arxiv_throttle(monkeypatch):
    """Disable the global arxiv min-interval throttle (C4) for the whole suite so
    real 1s sleeps never slow or flake tests. test_arxiv_throttle.py re-sets this
    env inside each test body (which overrides this autouse default)."""
    monkeypatch.setenv("CORTEX_ARXIV_MIN_INTERVAL", "0")
