#!/usr/bin/env python3
"""Temporary-only fixture driver for the Research resumption Web acceptance.

Same shape as `workflow-control-fixture.py`: networking is denied before any
product import, the root is a capability-marked temporary directory the verifier
created, and every command prints one JSON summary line that carries no
filesystem path. What differs is the subject -- a SYNTHETIC legacy Research
database written with the real legacy DDL, two synthetic Markdown dossiers, and
the real query-only `ResearchCatalog` plus the real explicit
`ResearchDocumentAdopter` reading them.

No real research record, document or credential is copied here, and nothing
outside the verifier's temporary root is read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any


def _install_network_guard() -> None:
    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("research fixture networking is disabled")

    class GuardedSocket(socket.socket):
        def __init__(
            self,
            family: int = socket.AF_INET,
            type: int = socket.SOCK_STREAM,
            proto: int = 0,
            fileno: int | None = None,
        ) -> None:
            if family in {socket.AF_INET, socket.AF_INET6}:
                deny_network()
            super().__init__(family, type, proto, fileno)

        def connect(self, _address: object) -> None:
            deny_network()

        def connect_ex(self, _address: object) -> int:
            deny_network()
            return 1

        def bind(self, _address: object) -> None:
            deny_network()

        def listen(self, _backlog: int = 0) -> None:
            deny_network()

        def accept(self) -> tuple[socket.socket, object]:
            deny_network()
            raise AssertionError("unreachable")

        def sendto(self, *_args: object, **_kwargs: object) -> int:
            deny_network()
            return 0

        def sendmsg(self, *_args: object, **_kwargs: object) -> int:
            deny_network()
            return 0

    socket.socket = GuardedSocket
    socket.create_connection = deny_network  # type: ignore[assignment]
    socket.create_server = deny_network  # type: ignore[assignment]
    socket.getaddrinfo = deny_network  # type: ignore[assignment]


_install_network_guard()


def _expect_network_denied(operation: Any) -> None:
    try:
        result = operation()
    except PermissionError:
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()
    raise AssertionError("research fixture network operation was not denied")


def _assert_network_guard() -> int:
    probes = 0
    for family in (socket.AF_INET, socket.AF_INET6):
        _expect_network_denied(lambda family=family: socket.socket(family, socket.SOCK_DGRAM))
        probes += 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as guarded:
        socket.socket.setblocking(guarded, False)
        for operation in (
            lambda: guarded.connect(""),
            lambda: guarded.connect_ex(""),
            lambda: guarded.bind(""),
            lambda: guarded.listen(),
            lambda: guarded.accept(),
            lambda: guarded.sendto(b"probe", ""),
            lambda: guarded.sendmsg([b"probe"], [], 0, ""),
        ):
            _expect_network_denied(operation)
            probes += 1
    for operation in (
        lambda: socket.create_connection(("127.0.0.1", 0), timeout=0),
        lambda: socket.create_server(("127.0.0.1", 0)),
        lambda: socket.getaddrinfo("127.0.0.1", 0),
    ):
        _expect_network_denied(operation)
        probes += 1
    return probes

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import ThreadArchived
from cortex_platform.product.research.catalog import ResearchCatalog, item_id
from cortex_platform.product.research.documents import (
    ROOT_ID,
    ResearchDocumentAdopter,
    ResearchDocumentReader,
)

FIXTURE_MARKER = ".cortex-research-fixture-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
# The three legacy DDL files are not part of the supported `cortex_research`
# package, so their bytes live beside the tests that execute them.
# `cortex_platform/tests/product/research/test_catalog.py` reads the same
# directory for the same reason.
LEGACY_SCHEMA_DIR = (
    REPOSITORY_ROOT / "profiles/research/tests/fixtures/legacy_catalog_ddl"
)
LEGACY_SCHEMA_FILES = ("m1c_schema.sql", "m1d_schema.sql", "m1h_schema.sql")
# `_STATUS_ENUM` / `_ORIGIN_ENUM` in `m1d_schema.py`, which widens them on a
# live database by rebuilding the table. The preserved copy is a migrated one,
# so the fixture database has to carry the widened enums too.
WIDENED = (
    ("'dormant', 'awaiting_human'", "'dormant', 'exploring', 'awaiting_human'"),
    (
        "'pivot_of_kill', 'fork_of_round'",
        "'pivot_of_kill', 'fork_of_round', 'exploration', 'from_exploration', 'revival'",
    ),
)

IDEA_ORIGIN_ID = "id_synthetic_memory"
EXPLORATION_ORIGIN_ID = "id_synthetic_exploration"
PROJECT_ORIGIN_ID = "synthetic-bounded-memory-project"
IDEA_TITLE = "synthetic-bounded-memory-idea"
EXPLORATION_TITLE = "synthetic-live-exploration"

#: The one line the public projection has to withhold. It is a synthetic path
#: that exists nowhere: the acceptance asserts these exact bytes never reach the
#: browser, so it must not be a path the shell could legitimately carry.
PRIVATE_LINE = "Working copy: /Users/synthetic-operator/agent-research/bounded-memory.md"

IDEA_DOCUMENT = f"""# Bounded memory dossier

The controller keeps a bounded state, so the retained window is $w_t = \\sum_{{i=1}}^{{k}} m_i$.

{PRIVATE_LINE}

- Round 3 stopped on thin evidence.
- The successor has to measure drift before it scales the window.
"""

# The exploration registers a document too -- `idea_seeds.md_path` is NOT NULL --
# and is deliberately left out of the adoption allowlist, so the acceptance can
# show that an item with a perfectly good candidate stays unadopted until the
# operator names it.
EXPLORATION_DOCUMENT = """# Live exploration notes

Round 2 is still open, so nothing here is a conclusion.
"""

PROJECT_DOCUMENT = """# Bounded memory project dossier

The graduated project carries the same measurement plan, with $\\alpha = 0.5$ as
the retention floor.

1. Freeze the encoder and train the projector alone.
2. Compare recurrent memory against a frozen cache.
"""


def _safe_root(value: str) -> Path:
    root = Path(value).resolve(strict=True)
    expected_root = os.environ.get("CORTEX_RESEARCH_FIXTURE_ROOT")
    capability = os.environ.get("CORTEX_RESEARCH_FIXTURE_CAPABILITY")
    if expected_root is None or Path(expected_root).resolve(strict=True) != root:
        raise ValueError("root is not bound to the verifier")
    if capability is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", capability) is None:
        raise ValueError("fixture capability is invalid")
    temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        not root.is_dir()
        or root.parent != temporary_parent
        or re.fullmatch(r"cortex-research-resumption-[A-Za-z0-9_-]{6,}", root.name) is None
    ):
        raise ValueError("root must be a verifier-created temporary directory")
    observed = root.stat()
    if observed.st_uid != os.geteuid() or observed.st_mode & 0o077:
        raise ValueError("root must be owner-private")
    marker = root / FIXTURE_MARKER
    marker_stat = marker.lstat()
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or marker_stat.st_uid != os.geteuid()
        or stat.S_IMODE(marker_stat.st_mode) != 0o600
        or marker_stat.st_nlink != 1
        or marker.read_text(encoding="ascii") != capability
    ):
        raise ValueError("fixture capability marker is invalid")
    return root


def _child(root: Path, *parts: str) -> Path:
    value = root.joinpath(*parts).resolve(strict=False)
    if value == root or root not in value.parents:
        raise ValueError("fixture path escaped the safety root")
    return value


def _control_database(root: Path) -> Path:
    # The same owner-private data directory the daemon is started with; the
    # Control store refuses to open under anything looser.
    data = _child(root, "data")
    data.mkdir(mode=0o700, exist_ok=True)
    data.chmod(0o700)
    return _child(root, "data", "control.db")


def _research_database(root: Path) -> Path:
    # Where `ControlAPI` looks for the legacy catalog by default: beside the
    # Control database the daemon opens, in `research/research.db`.
    return _child(root, "data", "research", "research.db")


def _store(root: Path) -> ControlStore:
    store = ControlStore(_control_database(root))
    store.initialize()
    return store


def _catalog(root: Path) -> ResearchCatalog:
    return ResearchCatalog(_research_database(root))


def _adopter(root: Path) -> ResearchDocumentAdopter:
    return ResearchDocumentAdopter(_store(root), _catalog(root))


def _source_roots(root: Path) -> dict[str, Path]:
    return {"idea": _child(root, "legacy", "ideas"), "project": _child(root, "legacy", "projects")}


def _items() -> dict[str, str]:
    return {
        "idea": item_id("idea", IDEA_ORIGIN_ID),
        "exploration": item_id("exploration", EXPLORATION_ORIGIN_ID),
        "project": item_id("project", PROJECT_ORIGIN_ID),
    }


def _legacy_schema(connection: sqlite3.Connection) -> None:
    for name in LEGACY_SCHEMA_FILES:
        text = (LEGACY_SCHEMA_DIR / name).read_text(encoding="utf-8")
        for old, new in WIDENED:
            if old in text:
                text = text.replace(old, new)
        connection.executescript(text)
    widened = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idea_seeds'"
    ).fetchone()[0]
    if widened.count("'exploring'") != 1:
        raise AssertionError("legacy idea_seeds enum was not widened as production widens it")


def _write_legacy_documents(root: Path) -> dict[str, Path]:
    roots = _source_roots(root)
    for directory in roots.values():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    idea_document = roots["idea"] / f"{IDEA_TITLE}.md"
    idea_document.write_text(IDEA_DOCUMENT, encoding="utf-8")
    exploration_document = roots["idea"] / f"{EXPLORATION_TITLE}.md"
    exploration_document.write_text(EXPLORATION_DOCUMENT, encoding="utf-8")
    project_directory = roots["project"] / PROJECT_ORIGIN_ID
    project_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    project_document = project_directory / "dossier.md"
    project_document.write_text(PROJECT_DOCUMENT, encoding="utf-8")
    return {"idea": idea_document, "exploration": exploration_document, "project": project_document}


def _seed_legacy_database(root: Path, documents: dict[str, Path]) -> None:
    database = _research_database(root)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if database.exists():
        raise AssertionError("legacy research database already exists")
    connection = sqlite3.connect(database)
    try:
        _legacy_schema(connection)
        connection.execute(
            """INSERT INTO idea_seeds
               (idea_id, seed_text, slug, status, n_rounds_completed, md_path, origin,
                updated_at, dormant_reason, graduated_to_project_ref, completed_at)
               VALUES (?, ?, ?, 'dormant', 3, ?, 'manual', '2026-05-04T00:00:00Z',
                       'the operator stopped every running exploration', ?,
                       '2026-05-04T00:00:00Z')""",
            (
                IDEA_ORIGIN_ID,
                "Can a bounded memory controller keep a long context stable?",
                IDEA_TITLE,
                str(documents["idea"]),
                PROJECT_ORIGIN_ID,
            ),
        )
        connection.execute(
            """INSERT INTO idea_seeds
               (idea_id, seed_text, slug, status, n_rounds_completed, md_path, origin,
                updated_at)
               VALUES (?, ?, ?, 'exploring', 0, ?, 'exploration',
                       '2026-05-08T00:00:00Z')""",
            (
                EXPLORATION_ORIGIN_ID,
                "Where is the current work on bounded recurrent memory?",
                EXPLORATION_TITLE,
                str(documents["exploration"]),
            ),
        )
        connection.execute(
            """INSERT INTO idea_attempts
               (idea_id, round_n, papers_pulled, decompositions, convergence_verdict,
                convergence_reason, started_at, completed_at)
               VALUES (?, 3, '[]', '[]', 'not_converged', 'the evidence was too thin',
                       '2026-05-03T00:00:00Z', '2026-05-04T00:00:00Z')""",
            (IDEA_ORIGIN_ID,),
        )
        connection.execute(
            """INSERT INTO idea_challenges
               (idea_id, round_n, challenge_text, dimension, evidence_papers,
                evidence_quotes, importance, confidence, model_agreement, created_at)
               VALUES (?, 3, 'the baseline is unproven', 'feasibility', '[]', '[]',
                       4, 3, 'both', '2026-05-04T01:00:00Z')""",
            (IDEA_ORIGIN_ID,),
        )
        for round_n, completed_at in ((1, "2026-05-06T00:00:00Z"), (2, "2026-05-08T00:00:00Z")):
            connection.execute(
                """INSERT INTO exploration_rounds
                   (exploration_id, round_n, mode, digest_text, started_at, completed_at)
                   VALUES (?, ?, 'discover', 'bounded memory landscape',
                           '2026-05-05T00:00:00Z', ?)""",
                (EXPLORATION_ORIGIN_ID, round_n, completed_at),
            )
        connection.execute(
            """INSERT INTO exploration_angles
               (angle_id, exploration_id, title, rationale, status, created_at)
               VALUES ('ang_synthetic_1', ?, 'bounded cache angle', 'why', 'greenlit',
                       '2026-05-07T00:00:00Z')""",
            (EXPLORATION_ORIGIN_ID,),
        )
        connection.execute(
            "INSERT INTO spar_monitored_projects (project_ref, registered_at) VALUES (?, ?)",
            (PROJECT_ORIGIN_ID, "2026-03-02T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()
    database.chmod(0o600)


def seed(root: Path) -> dict[str, Any]:
    """Synthetic legacy state plus one Control project for the shell to open."""

    documents = _write_legacy_documents(root)
    _seed_legacy_database(root, documents)
    store = _store(root)
    workspace = store.create_workspace(
        title="Research resumption",
        actor_id="local",
        idempotency_key="research-resumption-workspace",
    ).value
    return {
        "state": "seeded",
        "workspace_revision": workspace["revision"],
        "items": _items(),
        "legacy_documents": len(documents),
    }


def assert_catalog(root: Path) -> dict[str, Any]:
    """The real query-only catalog classifies the synthetic legacy records."""

    catalog = _catalog(root)
    identities = _items()
    page = catalog.list_items(limit=100, offset=0)
    by_kind = {kind: catalog.list_items(kind=kind, limit=100, offset=0) for kind in
               ("idea", "exploration", "project")}
    if page["total"] != 3 or {item["id"] for item in page["items"]} != set(identities.values()):
        raise AssertionError("the catalog does not hold exactly the seeded items")
    for kind, identity in identities.items():
        listed = by_kind[kind]["items"]
        if [item["id"] for item in listed] != [identity]:
            raise AssertionError(f"{kind} is not the only item of its kind")
    idea = catalog.get_item(identities["idea"])
    exploration = catalog.get_item(identities["exploration"])
    project = catalog.get_item(identities["project"])
    if idea["status"] != "dormant" or idea["round_count"] != 3 or not idea["pause_reason"]:
        raise AssertionError("the idea did not keep its stopped state")
    if exploration["status"] != "exploring" or exploration["round_count"] != 2:
        raise AssertionError("the exploration did not keep its rounds")
    if project["status"] != "monitored" or project["kind"] != "project":
        raise AssertionError("the monitored project was reclassified")
    if not any(entry["kind"] == "graduated_idea" for entry in project["history"]):
        raise AssertionError("the project lost its graduation history")
    # A project registers no `md_path`, which is exactly why adoption needs an
    # explicit operator-side document map for it.
    if catalog.document_candidates(identities["project"]):
        raise AssertionError("a monitored project must register no document path")
    return {
        "state": "catalog_read",
        "total": page["total"],
        "kinds": {kind: by_kind[kind]["total"] for kind in by_kind},
        "idea_history": len(idea["history"]),
        "exploration_history": len(exploration["history"]),
        "project_history": len(project["history"]),
    }


def assert_unadopted(root: Path) -> dict[str, Any]:
    """Nothing is adopted before the operator says so."""

    store = _store(root)
    counts = {kind: len(store.list_research_documents(identity))
              for kind, identity in _items().items()}
    if any(counts.values()):
        raise AssertionError("Control already holds an adopted research document")
    with store._connect() as connection:
        versions = connection.execute(
            "SELECT COUNT(*) FROM research_document_versions"
        ).fetchone()[0]
    if versions:
        raise AssertionError("Control already holds a research document version")
    return {"state": "unadopted", "documents": counts, "versions": versions}


def adopt(root: Path) -> dict[str, Any]:
    """One explicit preview and one explicit apply, over named items only.

    The idea carries a registered `md_path`, so its candidate comes from the
    catalog. The project registers none, so the operator supplies the document
    map -- a dossier is never bound to a project by guessing at its title.
    """

    identities = _items()
    adopter = _adopter(root)
    project_document = _source_roots(root)["project"] / PROJECT_ORIGIN_ID / "dossier.md"
    preview = adopter.preview(
        _source_roots(root),
        item_ids={identities["idea"], identities["project"]},
        document_map={identities["project"]: [
            {"kind": "project", "registered_path": str(project_document)},
        ]},
    )
    previewed = {entry["item"]["id"]: len(entry["documents"]) for entry in preview["items"]}
    if previewed != {identities["idea"]: 1, identities["project"]: 1}:
        raise AssertionError("the preview did not cover exactly the named items")
    result = adopter.apply(preview, destination=_child(root, "data", "research-documents"))
    reader = ResearchDocumentReader(_store(root))
    adopted = {}
    for entry in result["items"]:
        for document in entry["documents"]:
            content = reader.read(document["id"])
            if content["sha256"] != document["sha256"]:
                raise AssertionError("the adopted document does not verify against its digest")
            adopted[entry["item_id"]] = {
                "document_version_id": document["id"],
                "version": document["version"],
                "byte_length": document["byte_length"],
                "sha256": document["sha256"],
                "title": document["title"],
            }
    if set(adopted) != {identities["idea"], identities["project"]}:
        raise AssertionError("adoption did not register both named items")
    # The exploration registers a document of its own and was not named, so it
    # must still hold nothing: adoption is what the operator asked for, not
    # everything that could have been taken.
    if _store(root).list_research_documents(identities["exploration"]):
        raise AssertionError("an item outside the allowlist was adopted")
    return {"state": "adopted", "documents": adopted, "skipped": len(result["skipped"])}


def assert_threads(root: Path, *, expected: int) -> dict[str, Any]:
    """The linked conversations exist, and nothing ran in them."""

    store = _store(root)
    identities = _items()
    workspace = store.list_workspaces()[0]
    threads = list(store.list_threads(workspace_id=workspace["id"], include_archived=True))
    linked = {}
    for thread in threads:
        selection = store.get_research_thread_item(thread["id"])
        if selection is None:
            continue
        linked[selection["id"]] = thread
        if store.list_thread_runs(thread_id=thread["id"]):
            raise AssertionError("a linked research conversation started a run")
        if store.list_messages(thread["id"]):
            raise AssertionError("a linked research conversation holds a message")
    if len(threads) != expected or len(linked) != expected:
        raise AssertionError("the linked conversation count is not the expected one")
    return {
        "state": "linked",
        "threads": len(threads),
        "linked_items": sorted(kind for kind, identity in identities.items() if identity in linked),
        "runs": 0,
        "messages": 0,
    }


def archive_thread(root: Path) -> dict[str, Any]:
    """Archive the idea's conversation; selection on it must then be refused."""

    store = _store(root)
    identity = _items()["idea"]
    thread_id = store.research_item_thread(identity)
    if thread_id is None:
        raise AssertionError("the idea has no linked conversation to archive")
    thread = store.get_thread(thread_id)
    store.archive_thread(
        thread_id=thread_id,
        expected_revision=thread["revision"],
        actor_id="local",
        idempotency_key="research-resumption-archive",
    )
    refused = False
    try:
        store.select_research_item(
            thread_id=thread_id, item_id=identity, actor_id="local",
            idempotency_key="research-resumption-archived-selection",
        )
    except ThreadArchived:
        refused = True
    if not refused:
        raise AssertionError("an archived conversation accepted a research selection")
    if store.research_item_thread(identity) is not None:
        raise AssertionError("an archived conversation is still offered for reuse")
    return {"state": "archived", "selection_refused": True}


def _set_root_enabled(root: Path, enabled: bool) -> dict[str, Any]:
    store = _store(root)
    record = store.get_asset_root(ROOT_ID)
    store.update_asset_root(
        root_id=ROOT_ID,
        private_path=record.private_path,
        max_bytes=record.max_bytes,
        enabled=enabled,
        expected_revision=record.revision,
        actor_id="local",
        idempotency_key=f"research-resumption-root-{'enabled' if enabled else 'revoked'}",
    )
    if store.get_asset_root(ROOT_ID).enabled is not enabled:
        raise AssertionError("the adopted document root did not change state")
    return {"state": "root_enabled" if enabled else "root_revoked", "enabled": enabled}


def revoke_root(root: Path) -> dict[str, Any]:
    return _set_root_enabled(root, False)


def restore_root(root: Path) -> dict[str, Any]:
    return _set_root_enabled(root, True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-resumption-fixture")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "assert-network-guard", "seed", "assert-catalog", "assert-unadopted", "adopt",
        "archive-thread", "revoke-root", "restore-root",
    ):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
    threads = commands.add_parser("assert-threads")
    threads.add_argument("--root", required=True)
    threads.add_argument("--expected", required=True, type=int)
    return parser


def main() -> int:
    network_guard_probes = _assert_network_guard()
    arguments = _parser().parse_args()
    root = _safe_root(arguments.root)
    handlers = {
        "seed": seed,
        "assert-catalog": assert_catalog,
        "assert-unadopted": assert_unadopted,
        "adopt": adopt,
        "archive-thread": archive_thread,
        "revoke-root": revoke_root,
        "restore-root": restore_root,
    }
    if arguments.command == "assert-network-guard":
        result = {"state": "network_guarded", "probes": network_guard_probes}
    elif arguments.command == "assert-threads":
        result = assert_threads(root, expected=arguments.expected)
    else:
        result = handlers[arguments.command](root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
