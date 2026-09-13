"""Query-only catalog over the product-owned legacy Research database.

R1a of `docs/plans/2026-09-07-legacy-research-resumption.md`: make the preserved
ideas, exploration threads and monitored projects discoverable without waking
anything. This module reads six legacy tables (`idea_seeds`, `idea_attempts`,
`idea_challenges`, `exploration_rounds`, `exploration_angles`,
`spar_monitored_projects`) through a read-only SQLite snapshot. It never imports
`cortex_research`, whose modules initialize/migrate schemas and start engines on
import, and it never writes, checkpoints or migrates the database.

Public identity is derived, not invented: `id` is a stable hash of the original
record identifier, and `origin_id` carries that identifier through unchanged, so
Control can map product state onto legacy records by `(origin, kind, id)`.
Registered Markdown paths stay out of every public field; `document_candidates`
returns them separately as coordinator-only adoption input.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ORIGIN = "legacy-research"
KINDS = ("idea", "exploration", "project")
# All six persisted idea states, including the ones the current inventory does
# not contain. A state is not "supported" only because a row exists today.
IDEA_STATUSES = (
    "incubating", "graduated", "killed", "aborted", "dormant", "awaiting_human",
)
# `exploring` rides idea_seeds for exploration threads (m1h spec D4); an
# exploration thread that was parked carries an ordinary idea state instead.
EXPLORATION_STATUS = "exploring"
# spar_monitored_projects records membership only; it has no state column.
PROJECT_STATUS = "monitored"
# An exploration identity that has rounds/angles but no idea_seeds row.
UNKNOWN_STATUS = "unknown"
STATUSES = (*IDEA_STATUSES, EXPLORATION_STATUS, PROJECT_STATUS, UNKNOWN_STATUS)

MAX_LIMIT = 200
# The whole catalog is gathered per call so ordering and totals are exact; the
# legacy inventory is ~30 items. A larger database is refused, not truncated.
MAX_ITEMS = 5_000
MAX_HISTORY = 50
MAX_TEXT = 1_000


class CatalogUnavailable(RuntimeError):
    """The legacy database cannot be read as a catalog snapshot."""

    category = "research_catalog_unavailable"


class CatalogItemNotFound(LookupError):
    """No legacy record carries this catalog identity."""

    category = "research_catalog_item_not_found"


def item_id(kind: str, origin_id: str) -> str:
    """Derive the public identity of one legacy record.

    Stable across processes and databases: the same legacy record always gets
    the same id, and no id can collide across kinds because the kind is inside
    the hashed material.
    """

    material = f"{ORIGIN}\0{kind}\0{origin_id}".encode("utf-8")
    return "ri_" + hashlib.sha256(material).hexdigest()[:32]


def _text(value, *, default=""):
    """Bound one legacy text column for public display."""

    if value is None:
        return default
    value = str(value)
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"


def _entry(kind, label, text, created_at):
    return {
        "kind": kind,
        "label": _text(label),
        "text": _text(text),
        "created_at": None if created_at is None else str(created_at),
    }


@contextmanager
def _snapshot(database: Path):
    """Open one read-only, query-only transaction over the live database.

    `mode=ro` (not `immutable=1`) because the legacy database runs in WAL: an
    immutable read would silently ignore committed frames still in the log and
    report stale rows. This mirrors `sources/reader.py::_control_database`,
    which reads live WAL state the same way. `query_only` plus the absence of
    any DDL keeps the file's bytes and journal state exactly as found; SQLite
    may only coordinate through the shared-memory index.
    """

    if not database.is_file():
        raise CatalogUnavailable("research database is unavailable")
    try:
        connection = sqlite3.connect(
            database.absolute().as_uri() + "?mode=ro", uri=True, timeout=5,
        )
    except sqlite3.Error as error:
        raise CatalogUnavailable(f"research database cannot be opened: {error}") from error
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        # One deferred transaction, so every table in a call sees one snapshot
        # and pagination stays stable while the legacy writers run.
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as error:
        # Surfaced as an explicit failure. A corrupt or unreadable database is
        # never reported as an empty catalog.
        raise CatalogUnavailable(f"research database cannot be read: {error}") from error
    finally:
        connection.close()


def _tables(connection):
    return {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _rows(connection, tables, table, columns, where="", parameters=(), limit=None):
    """Read a bounded projection of one legacy table, tolerating its absence.

    The legacy schema is additive and self-migrating, so an older copy can be
    missing a whole table. A missing table contributes nothing; a database
    missing every table is refused by `_gather`. The row bound is read at call
    time so the limits stay one authority.
    """

    if table not in tables:
        return []
    projection = ", ".join(columns)
    return connection.execute(
        f"SELECT {projection} FROM {table} {where} LIMIT ?",
        (*parameters, MAX_ITEMS + 1 if limit is None else limit),
    ).fetchall()


def _exploration_rounds(connection, tables):
    """Count rounds and find the latest activity per exploration identity.

    Aggregated by SQLite, not in Python: a bound on raw round rows would
    undercount an exploration's rounds and drop later identities while the
    catalog still looked small (the preserved database holds 229 rounds today
    and grows per round). What is bounded here is the number of exploration
    IDENTITIES, and an overflow is refused rather than truncated.

    Returns `None` when the table is absent, which means "round count unknown"
    rather than "zero rounds".
    """

    if "exploration_rounds" not in tables:
        return None
    rounds = {
        row[0]: (row[1], None if row[2] is None else str(row[2]))
        for row in _rows(
            connection, tables, "exploration_rounds",
            ("exploration_id", "COUNT(*)", "MAX(COALESCE(completed_at, started_at))"),
            "GROUP BY exploration_id",
        )
    }
    if len(rounds) > MAX_ITEMS:
        raise CatalogUnavailable("research catalog is larger than the read limit")
    return rounds


def _seed_item(row, rounds, explorations):
    """Project one idea_seeds row, choosing idea versus exploration identity.

    Exploration threads are idea_seeds rows written by the exploration
    orchestrator with `origin='exploration'` and `status='exploring'`. Either
    marker alone is decisive, because a parked exploration keeps `origin` while
    its status becomes an ordinary idea state. Recorded exploration rounds or
    angles are the last fallback for a row that predates both markers; the same
    membership set decides here and in `_gather`, so one legacy record can never
    be listed twice.
    """

    origin_id = row["idea_id"]
    exploration = (
        row["origin"] == "exploration"
        or row["status"] == EXPLORATION_STATUS
        or origin_id in explorations
    )
    kind = "exploration" if exploration else "idea"
    if kind == "exploration" and rounds is not None:
        round_count = rounds.get(origin_id, (0, None))[0]
    else:
        round_count = row["n_rounds_completed"]
    return {
        "id": item_id(kind, origin_id),
        "kind": kind,
        "origin_id": origin_id,
        "title": _text(row["slug"]),
        "status": _text(row["status"], default=UNKNOWN_STATUS),
        "summary": _text(row["seed_text"]),
        "pause_reason": (
            _text(row["dormant_reason"] or row["kill_reason"], default=None) or None
        ),
        "round_count": int(round_count or 0),
        "updated_at": None if row["updated_at"] is None else str(row["updated_at"]),
    }


def _gather(connection):
    """Project every legacy record into public items, plus internal columns.

    Returns `(items, internal)` where `internal` maps a public id onto the
    record's registered Markdown path and graduation reference. Nothing in
    `internal` is public.
    """

    tables = _tables(connection)
    if not tables & {"idea_seeds", "exploration_rounds", "spar_monitored_projects"}:
        raise CatalogUnavailable("research schema is not present")

    rounds = _exploration_rounds(connection, tables)
    # Every known exploration membership, collected BEFORE seed classification
    # so a seed reached only through its angles is classified as that same
    # exploration instead of being listed twice: once as an idea and once as an
    # identity with no seed row.
    angles = {
        row["exploration_id"] for row in _rows(
            connection, tables, "exploration_angles", ("DISTINCT exploration_id",)
        )
    }
    if len(angles) > MAX_ITEMS:
        raise CatalogUnavailable("research catalog is larger than the read limit")
    explorations = set(rounds or ()) | angles

    items, internal = [], {}
    seen_explorations = set()
    for row in _rows(
        connection, tables, "idea_seeds",
        ("idea_id", "seed_text", "slug", "status", "n_rounds_completed",
         "graduated_to_project_ref", "md_path", "updated_at", "completed_at",
         "dormant_reason", "kill_reason", "origin"),
    ):
        item = _seed_item(row, rounds, explorations)
        if item["kind"] == "exploration":
            seen_explorations.add(row["idea_id"])
        items.append(item)
        internal[item["id"]] = {
            "kind": item["kind"],
            "origin_id": row["idea_id"],
            "md_path": row["md_path"],
            "graduated_to_project_ref": row["graduated_to_project_ref"],
            "completed_at": row["completed_at"],
        }

    # Exploration identities whose idea_seeds row is absent stay visible with an
    # explicit unknown state rather than being dropped or given a made-up one.
    for origin_id in sorted(explorations - seen_explorations):
        count, latest = (rounds or {}).get(origin_id, (0, None))
        identity = item_id("exploration", origin_id)
        items.append({
            "id": identity, "kind": "exploration", "origin_id": origin_id,
            "title": _text(origin_id), "status": UNKNOWN_STATUS, "summary": "",
            "pause_reason": None, "round_count": count, "updated_at": latest,
        })
        internal[identity] = {
            "kind": "exploration", "origin_id": origin_id, "md_path": None,
            "graduated_to_project_ref": None, "completed_at": None,
        }

    # A monitored project stays a project. It is never rendered as an idea, and
    # graduation never turns it back into one.
    for row in _rows(
        connection, tables, "spar_monitored_projects", ("project_ref", "registered_at"),
    ):
        origin_id = row["project_ref"]
        identity = item_id("project", origin_id)
        items.append({
            "id": identity, "kind": "project", "origin_id": origin_id,
            "title": _text(origin_id), "status": PROJECT_STATUS, "summary": "",
            "pause_reason": None, "round_count": 0,
            "updated_at": None if row["registered_at"] is None else str(row["registered_at"]),
        })
        internal[identity] = {
            "kind": "project", "origin_id": origin_id, "md_path": None,
            "graduated_to_project_ref": None, "completed_at": None,
        }

    if len(items) > MAX_ITEMS or len(internal) != len(items):
        raise CatalogUnavailable("research catalog is larger than the read limit")
    # Total order: newest first, ties resolved by kind then original identity,
    # so a page boundary never depends on SQLite's row order.
    items.sort(key=lambda item: (item["kind"], item["origin_id"]))
    items.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return items, internal


def _history(connection, item, internal):
    """Read the bounded per-kind activity trail of one item."""

    tables = _tables(connection)
    origin_id = internal["origin_id"]
    entries = []
    if item["kind"] == "idea":
        for row in _rows(
            connection, tables, "idea_attempts",
            ("round_n", "convergence_verdict", "convergence_reason", "started_at", "completed_at"),
            "WHERE idea_id = ? ORDER BY round_n DESC", (origin_id,), MAX_HISTORY,
        ):
            entries.append(_entry(
                "round", f"round {row['round_n']}",
                row["convergence_reason"] or row["convergence_verdict"],
                row["completed_at"] or row["started_at"],
            ))
        for row in _rows(
            connection, tables, "idea_challenges",
            ("dimension", "challenge_text", "status", "created_at"),
            "WHERE idea_id = ? ORDER BY created_at DESC, id DESC", (origin_id,), MAX_HISTORY,
        ):
            entries.append(_entry(
                "challenge", f"{row['dimension']} ({row['status']})",
                row["challenge_text"], row["created_at"],
            ))
        if internal["graduated_to_project_ref"]:
            entries.append(_entry(
                "graduation", "graduated_to_project_ref",
                internal["graduated_to_project_ref"], internal["completed_at"],
            ))
    elif item["kind"] == "exploration":
        for row in _rows(
            connection, tables, "exploration_rounds",
            ("round_n", "mode", "digest_text", "started_at", "completed_at"),
            "WHERE exploration_id = ? ORDER BY round_n DESC", (origin_id,), MAX_HISTORY,
        ):
            entries.append(_entry(
                "round", f"round {row['round_n']} ({row['mode']})",
                row["digest_text"], row["completed_at"] or row["started_at"],
            ))
        for row in _rows(
            connection, tables, "exploration_angles",
            ("title", "status", "updated_at", "created_at"),
            "WHERE exploration_id = ? ORDER BY created_at DESC, angle_id DESC",
            (origin_id,), MAX_HISTORY,
        ):
            entries.append(_entry(
                "angle", row["status"], row["title"], row["created_at"],
            ))
    else:
        # The Research DB records no project rounds; graduation references are
        # the only project association it holds.
        for row in _rows(
            connection, tables, "idea_seeds",
            ("idea_id", "slug", "status", "completed_at"),
            "WHERE graduated_to_project_ref = ? ORDER BY completed_at DESC, idea_id DESC",
            (origin_id,), MAX_HISTORY,
        ):
            entries.append(_entry(
                "graduated_idea", f"{row['slug']} ({row['status']})",
                row["idea_id"], row["completed_at"],
            ))
    entries.sort(key=lambda entry: entry["created_at"] or "", reverse=True)
    return entries[:MAX_HISTORY]


class ResearchCatalog:
    """Read the product-owned legacy Research database; never change it."""

    def __init__(self, database: Path) -> None:
        self._database = Path(database)

    def list_items(self, *, kind=None, status=None, limit=100, offset=0) -> dict:
        """Return one bounded, stably ordered page of legacy research items."""

        if kind is not None and kind not in KINDS:
            raise ValueError("kind is invalid")
        if status is not None and status not in STATUSES:
            raise ValueError("status is invalid")
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ValueError("limit is out of range")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset is out of range")
        with _snapshot(self._database) as connection:
            items, _ = _gather(connection)
        selected = [
            item for item in items
            if (kind is None or item["kind"] == kind)
            and (status is None or item["status"] == status)
        ]
        return {
            "items": selected[offset:offset + limit],
            "total": len(selected),
            "limit": limit,
            "offset": offset,
        }

    def get_item(self, item_id: str) -> dict:
        """Return one item plus its bounded history; no filesystem paths."""

        with _snapshot(self._database) as connection:
            items, internal = _gather(connection)
            item = self._find(items, item_id)
            return {**item, "history": _history(connection, item, internal[item["id"]])}

    def document_candidates(self, item_id: str) -> list[dict]:
        """Return the legacy document references an adoption preview may use.

        Internal, coordinator-only output: these paths are never part of a
        public DTO and are never read by this module. Ideas and explorations
        carry a registered Markdown path; monitored projects do not — their
        documents are resolved from the legacy document tree, which this
        catalog does not inspect.

        `kind` is the item's own kind, so an adoption source root matches the
        owning research item's vocabulary exactly.
        """

        with _snapshot(self._database) as connection:
            items, internal = _gather(connection)
            record = internal[self._find(items, item_id)["id"]]
        if not record["md_path"]:
            return []
        return [{"kind": record["kind"], "registered_path": str(record["md_path"])}]

    @staticmethod
    def _find(items, identity):
        if not isinstance(identity, str) or not identity:
            raise ValueError("item_id is invalid")
        for item in items:
            if item["id"] == identity:
                return item
        raise CatalogItemNotFound("research item is not in the catalog")
