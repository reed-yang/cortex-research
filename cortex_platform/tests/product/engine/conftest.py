"""Shared fixtures for the engine boundary.

Every fixture that needs a `research.db` builds it with the real
`cortex_research` schema code -- S1's prod-consistent rule. A hand-written
schema would let a test pass against a shape production does not have, which is
the exact failure mode the adoption reader was written to refuse.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.engine.bindings import EngineRoots
from cortex_platform.product.paths import resolve_paths


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


class MovableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MovableClock) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db", clock=clock, id_factory=DeterministicIds()
    )
    value.initialize()
    return value


@pytest.fixture
def roots(tmp_path: Path) -> EngineRoots:
    registry = resolve_paths(
        environ={
            "HOME": str(tmp_path / "home"),
            "CORTEX_DATA_DIR": str(tmp_path / "data"),
            "CORTEX_STATE_DIR": str(tmp_path / "state"),
        },
        platform="darwin",
    )
    # S1's frozen shape, and deliberately NOT named `papers`: the operator's
    # only enabled asset root is `research-corpus` -> `<data_dir>/research/corpus`,
    # and a fixture whose last component is `papers` hides the F4 split.
    value = EngineRoots.resolve(
        registry, corpus_root=registry.data_dir / "research" / "corpus"
    )
    value.corpus_root.mkdir(parents=True, exist_ok=True)
    value.prepare()
    return value


def build_research_db(path: Path) -> Path:
    """Create a real research.db using the shipped engine schema code."""

    from cortex_research.db import apply_schema, connect
    from cortex_research.radar_schema import ensure_radar_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        apply_schema(connection)
        ensure_radar_schema(connection)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return path


@pytest.fixture
def research_db(roots: EngineRoots) -> Path:
    return build_research_db(roots.research_db)


class ActivationGate:
    """The smallest stand-in for the durable gate the supervisor consults."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.reads = 0

    def runtime_dispatch_enabled(self) -> bool:
        self.reads += 1
        return self.enabled
