"""The per-effect engine child: one operation, one reply, then exit.

Everything this module does happens in a process cortexd will not survive
without: the engine detaches grandchildren that inherit the whole environment
(F7), `db.connect()` flips the copied database into WAL and mkdirs on open
(F5), and the OCR path dlopens and shells out. None of that may run in the
process that owns `control.db`.

⟦AMD-5⟧ fixes the order in which this process may claim success: close every
connection it opened on `research.db`, `PRAGMA wal_checkpoint(TRUNCATE)`, pass
the write-boundary assertion, pass the no-descendant assertion. A child that
has not checkpointed has not finished -- S1's reader refuses a non-empty `-wal`
and it is right to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from .protocol import EffectRequest, write_result
from . import digests, survivors

# Audit events that mean "a path is about to change". `open` is filtered by its
# mode and flags -- the same event fires for every read.
_WRITE_EVENTS = {
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.link",
    "os.symlink",
    "os.truncate",
    "shutil.copyfile",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.move",
}
_WRITE_MODES = frozenset("waxt+")


class _WriteAudit:
    """D3 layer 3: record every path this process changed.

    ⟦AMD-10⟧ names this honestly -- it is detection after the fact, not
    prevention before the write. Bindings catch the known cases and `HOME` the
    forgotten ones; this catches the ones no layer anticipated, and it catches
    them by reporting a write that already happened.
    """

    def __init__(self) -> None:
        self.paths: set[str] = set()
        self._enabled = False

    def install(self) -> None:
        self._enabled = True
        sys.addaudithook(self._hook)

    def _record(self, value: object) -> None:
        if isinstance(value, (str, bytes, os.PathLike)):
            try:
                self.paths.add(os.fspath(os.fsdecode(value)))
            except (TypeError, ValueError):
                pass

    def _hook(self, event: str, arguments: tuple) -> None:
        if not self._enabled:
            return
        try:
            if event == "open":
                path, mode, flags = (arguments + (None, None, None))[:3]
                writing = False
                if isinstance(mode, str):
                    writing = bool(_WRITE_MODES & set(mode))
                if isinstance(flags, int):
                    writing = writing or bool(
                        flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)
                    )
                if writing:
                    self._record(path)
                return
            if event in _WRITE_EVENTS:
                for argument in arguments:
                    self._record(argument)
        except Exception:  # pragma: no cover - an audit hook may never raise
            return


def assess_write_boundary(
    paths: set[str], write_roots: tuple[Path, ...]
) -> tuple[str, ...]:
    """Every recorded write that landed outside the bound roots."""

    resolved = tuple(Path(root).resolve(strict=False) for root in write_roots)
    violations: list[str] = []
    for raw in sorted(paths):
        if not raw or not raw.startswith("/"):
            # A relative path is resolved against the child's cwd, which the
            # supervisor pins inside a bound root.
            continue
        candidate = Path(raw).resolve(strict=False)
        if any(candidate == root or candidate.is_relative_to(root) for root in resolved):
            continue
        violations.append(str(candidate))
    return tuple(violations)


def checkpoint_research_db(database: Path) -> bool:
    """Truncate the write-ahead log so S1's reader can open the copy.

    Not an optimisation: `read_corpus` refuses a non-empty `-wal` because an
    `immutable=1` read of a database with an outstanding log silently returns
    the pre-commit row set (`sources/adoption.py:208-228`).
    """

    database = Path(database)
    if not database.exists():
        return True
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()
    log = database.with_name(database.name + "-wal")
    try:
        return not log.exists() or log.stat().st_size == 0
    except OSError:
        return False


def _chunk_count(database: Path, paper_dir: str) -> int:
    """How many chunks the ingest actually indexed, read back from the copy."""

    if not database or not Path(database).exists():
        return 0
    connection = sqlite3.connect(str(database))
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE paper_dir = ?", (paper_dir,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return 0
    finally:
        connection.close()


def _ingest_arxiv(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research.paper_ingest import (
        IngestError,
        TransientIngestError,
        _norm_id,
        ingest_arxiv,
    )

    identifier = str(payload["identifier"])
    try:
        arxiv_id = _norm_id(identifier)
    except IngestError as error:
        # The payload is not an arxiv identifier at all: the source is wrong,
        # which is a different refusal from a materialization that failed.
        raise _Refusal("invalid_source", str(error)) from error
    try:
        result = ingest_arxiv(arxiv_id, source="product", strict=True)
    except IngestError as error:
        raise _Refusal("materialization_failed", str(error)) from error
    except TransientIngestError as error:
        raise _Refusal("outcome_unknown", str(error)) from error
    if not result.get("ok"):
        # The URL path returns ok=False with exit 0, so the result field is the
        # verdict and the exit code is not.
        category = "outcome_unknown" if result.get("transient") else "materialization_failed"
        raise _Refusal(category, str(result.get("error") or "ingest reported ok=false"))
    paper_dir = result.get("paper_dir")
    engine = dict(result)
    if paper_dir:
        engine["chunk_count"] = _chunk_count(
            os.environ.get("CORTEX_RESEARCH_DB", ""), str(paper_dir)
        )
    return {"engine": engine, "paper_dirs": [paper_dir] if paper_dir else []}


def _reconcile_arxiv(payload: Mapping[str, Any]) -> dict[str, Any]:
    from cortex_research.paper_ingest import _corpus_lookup, _norm_id

    arxiv_id = _norm_id(str(payload["identifier"]))
    existing = _corpus_lookup(arxiv_id)
    return {
        "engine": {"arxiv_id": arxiv_id, "paper_dir": existing},
        "paper_dirs": [existing] if existing else [],
    }


def _self_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Write to a path the request names, so the boundary can be proven live.

    D9 item 7 requires the write boundary to fire on a deliberately unbound
    path, and the AST scan found no real one in the package -- so the
    unbound write has to be synthesised, under an operation that does nothing
    else and only ever touches the path it is handed.
    """

    target = payload.get("write_path")
    if target:
        path = Path(str(target))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("p4.2 write-boundary probe\n", encoding="utf-8")
    engine: dict[str, Any] = {"write_path": target}
    if payload.get("report_environment_names"):
        # NAMES only. A value is a credential until proven otherwise, and this
        # document is written to disk and echoed into the acceptance record.
        engine["environment_names"] = sorted(os.environ)
    return {"engine": engine, "paper_dirs": []}


class _Refusal(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


_HANDLERS = {
    "ingest_arxiv": _ingest_arxiv,
    "reconcile_arxiv": _reconcile_arxiv,
    "self_check": _self_check,
    "checkpoint": lambda payload: {"engine": {}, "paper_dirs": []},
}


def run(request: EffectRequest) -> dict[str, Any]:
    audit = _WriteAudit()
    audit.install()
    watch = {name: Path(root) for name, root in request.watch_roots.items()}
    before = digests.sample_trees(watch) if watch else None

    result: dict[str, Any] = {
        "schema_version": 1,
        "operation": request.operation,
        "marker": request.marker,
        "ok": False,
        "engine": None,
        "paper_dirs": [],
        "failure": None,
        "checkpointed": False,
        "write_boundary": {"ok": True, "violations": []},
        "survivors": {},
        "gdrive": None,
    }
    try:
        outcome = _HANDLERS[request.operation](request.payload)
        result["engine"] = outcome["engine"]
        result["paper_dirs"] = outcome["paper_dirs"]
        result["ok"] = True
    except _Refusal as refusal:
        result["failure"] = {"category": refusal.category, "message": refusal.message}
    except Exception as error:  # noqa: BLE001 - the category is the contract
        result["failure"] = {
            "category": "materialization_failed",
            "message": f"{type(error).__name__}: {error}",
        }

    # ⟦AMD-5⟧ the completion protocol, in order, whatever the outcome was.
    result["checkpointed"] = checkpoint_research_db(Path(request.research_db))
    violations = assess_write_boundary(
        audit.paths, tuple(Path(root) for root in request.write_roots)
    )
    result["write_boundary"] = {"ok": not violations, "violations": list(violations)}
    report = survivors.scan(
        state_dir=Path(request.state_dir),
        marker=request.marker,
        needles=(request.marker, "-m cortex_research", request.state_dir),
    )
    result["survivors"] = report.to_dict()
    if watch:
        after = digests.sample_trees(watch)
        result["gdrive"] = {
            "before": before,
            "after": after,
            "changed": list(digests.differences(before or {}, after)),
        }
    if result["ok"] and (
        not result["checkpointed"]
        or violations
        or not report.clean
        or (result["gdrive"] or {}).get("changed")
    ):
        result["ok"] = False
        result["failure"] = {
            "category": "outcome_unknown",
            "message": "completion protocol did not pass",
        }
    return result


def main(argv: list[str] | None = None) -> int:
    raw = json.loads(sys.stdin.read())
    request = EffectRequest.from_dict(raw)
    try:
        result = run(request)
    except Exception:  # pragma: no cover - a crash here is an unknown outcome
        result = {
            "schema_version": 1,
            "operation": request.operation,
            "marker": request.marker,
            "ok": False,
            "engine": None,
            "paper_dirs": [],
            "failure": {
                "category": "outcome_unknown",
                "message": traceback.format_exc(limit=1).strip().splitlines()[-1],
            },
            "checkpointed": False,
            "write_boundary": {"ok": True, "violations": []},
            "survivors": {},
            "gdrive": None,
        }
    write_result(Path(request.result_path), result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
