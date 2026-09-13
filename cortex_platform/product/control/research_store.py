"""Research adoption and selection commands using ControlStore transactions."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from .errors import InvalidTransition, NotFound, ThreadActiveRun, ThreadArchived


def research_item_id(kind: str, origin_id: str) -> str:
    return "ri_" + hashlib.sha256(f"legacy-research\0{kind}\0{origin_id}".encode()).hexdigest()[:32]


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) != value or "\\" in value:
        raise ValueError("research document path is invalid")
    return value


class ResearchItemsStore:
    """Methods inherited by the sole Control writer; no independent connections."""

    def register_research_documents(self, *, item, documents, actor_id, idempotency_key):
        kind = item["kind"]
        origin_id = self._required_text(item["origin_id"], "origin_id", maximum=2000)
        if kind not in {"idea", "exploration", "project"} or item["id"] != research_item_id(kind, origin_id):
            raise ValueError("research item identity is invalid")
        title = self._required_text(item["title"], "title", maximum=500)
        if not 1 <= len(documents) <= 1000:
            raise ValueError("research document count is invalid")
        rows = []
        for doc in documents:
            if not re.fullmatch(r"[a-f0-9]{64}", doc["sha256"]) or not 0 < doc["byte_length"] <= 1048576:
                raise ValueError("research document identity is invalid")
            if doc["media_type"] not in {"text/markdown", "text/plain"}:
                raise ValueError("research document media type is invalid")
            origin_path = _relative(doc["origin_relative_path"])
            document_id = "rd_" + hashlib.sha256(f"{item['id']}\0{origin_path}".encode()).hexdigest()[:32]
            rows.append({
                "document_id": document_id,
                "title": self._required_text(doc["title"], "document title", maximum=500),
                "asset_root_id": doc["asset_root_id"],
                "relative_path": _relative(doc["relative_path"]),
                "origin_relative_path": origin_path,
                "media_type": doc["media_type"], "byte_length": doc["byte_length"], "sha256": doc["sha256"],
            })
        request = {"item_id": item["id"], "kind": kind, "origin_id": origin_id, "title": title, "documents": rows}
        operation = "INTERNAL:research-documents/adopt"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            now = self._now()
            conn.execute(
                "INSERT OR IGNORE INTO research_items(id,kind,origin_id,title,created_at) VALUES(?,?,?,?,?)",
                (item["id"], kind, origin_id, title, now),
            )
            saved = []
            for row in rows:
                root = self._asset_root(conn, row["asset_root_id"])
                if not root.enabled or row["byte_length"] > root.max_bytes:
                    raise InvalidTransition("asset_root_unavailable", "research_adoption")
                existing = conn.execute(
                    "SELECT * FROM research_document_versions WHERE document_id=? AND sha256=?",
                    (row["document_id"], row["sha256"]),
                ).fetchone()
                if existing is None:
                    version = conn.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM research_document_versions WHERE document_id=?",
                        (row["document_id"],),
                    ).fetchone()[0]
                    version_id = "rdv_" + hashlib.sha256(f"{row['document_id']}\0{row['sha256']}".encode()).hexdigest()[:32]
                    conn.execute(
                        """INSERT INTO research_document_versions(
                            id,document_id,item_id,version,title,asset_root_id,relative_path,
                            origin_relative_path,media_type,byte_length,sha256,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (version_id, row["document_id"], item["id"], version, row["title"],
                         row["asset_root_id"], row["relative_path"], row["origin_relative_path"],
                         row["media_type"], row["byte_length"], row["sha256"], now),
                    )
                    existing = conn.execute("SELECT * FROM research_document_versions WHERE id=?", (version_id,)).fetchone()
                saved.append(self._research_document_public(existing))
            self._audit(conn, "research_item", item["id"], "research.documents_adopted", {"document_ids": [d["id"] for d in saved]})
            return self._save_receipt(conn, actor_id, operation, idempotency_key, request, {"item_id": item["id"], "documents": saved}, 200)

    def list_research_documents(self, item_id):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT d.* FROM research_document_versions d WHERE d.item_id=?
                   AND d.version=(SELECT MAX(v.version) FROM research_document_versions v WHERE v.document_id=d.document_id)
                   ORDER BY d.title,d.document_id""", (item_id,),
            ).fetchall()
            return [self._research_document_public(row) for row in rows]

    def get_research_document(self, version_id):
        """Private capability metadata, never a public DTO."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM research_document_versions WHERE id=?", (version_id,)).fetchone()
            if row is None:
                raise NotFound("research document", version_id)
            return dict(row)

    @staticmethod
    def _research_document_public(row):
        return {key: row[key] for key in ("id", "document_id", "title", "version", "media_type", "byte_length", "sha256")}

    def _validate_research_documents(self, conn, snapshot, *, thread_id, admission):
        """Recheck a packet's dossier authority inside the caller's transaction.

        `admission` distinguishes selecting evidence for a new context -- which
        must match the item this thread has selected *now* -- from replaying an
        already frozen one, whose retained excerpts a later re-selection cannot
        retroactively invalidate. Document versions and their asset root are
        rechecked either way, because an unreadable or disowned version is not
        evidence this run may still be credited with.
        """
        item = snapshot.get("item")
        documents = snapshot.get("documents", ())
        if item is None:
            if documents:
                raise InvalidTransition("research_authority_mismatch", "research_selection")
            return
        if admission:
            selected = conn.execute(
                """SELECT i.id, i.kind, i.origin_id, i.title, b.revision
                   FROM research_thread_items b JOIN research_items i ON i.id = b.item_id
                   WHERE b.thread_id = ?""", (thread_id,),
            ).fetchone()
            if selected is None or selected["revision"] != item["selection_revision"] or any(
                selected[key] != item[key] for key in ("id", "kind", "origin_id", "title")
            ):
                raise InvalidTransition("research_authority_mismatch", "research_selection")
        for document in documents:
            row = conn.execute(
                """SELECT d.*, r.enabled, r.max_bytes FROM research_document_versions d
                   JOIN asset_roots r ON r.root_id = d.asset_root_id WHERE d.id = ?""",
                (document["document_version_id"],),
            ).fetchone()
            if row is None or row["item_id"] != item["id"] or not row["enabled"] or any(
                row[key] != document[key] for key in
                ("document_id", "title", "version", "media_type", "byte_length", "sha256")
            ) or row["byte_length"] > row["max_bytes"]:
                raise InvalidTransition("research_source_not_ready", "research_selection")

    def get_research_thread_item(self, thread_id):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT i.*, b.revision AS selection_revision, b.selected_at
                   FROM research_thread_items b JOIN research_items i ON i.id=b.item_id WHERE b.thread_id=?""",
                (thread_id,),
            ).fetchone()
            return dict(row) if row else None

    def research_item_thread(self, item_id, workspace_id=None):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT t.id FROM threads t JOIN research_thread_items b ON b.thread_id=t.id
                   WHERE b.item_id=? AND t.archived_at IS NULL AND (? IS NULL OR t.workspace_id=?)
                   ORDER BY b.selected_at DESC,t.id LIMIT 1""", (item_id, workspace_id, workspace_id),
            ).fetchone()
            return row["id"] if row else None

    def open_research_thread(self, *, item_id, workspace_id, expected_revision, actor_id, idempotency_key):
        request = {"workspace_id": workspace_id, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/research-items/{item_id}/thread"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            item = conn.execute("SELECT * FROM research_items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFound("adopted research item", item_id)
            workspace = self._workspace(conn, workspace_id)
            existing = conn.execute(
                """SELECT t.id FROM threads t JOIN research_thread_items b ON b.thread_id=t.id
                   WHERE b.item_id=? AND t.workspace_id=? AND t.archived_at IS NULL
                   ORDER BY b.selected_at DESC,t.id LIMIT 1""", (item_id, workspace_id),
            ).fetchone()
            if existing:
                return self._save_receipt(conn, actor_id, operation, idempotency_key, request, self._thread(conn, existing["id"]), 200)
            self._expect_revision(workspace, expected_revision)
            now, thread_id = self._now(), self._id_factory("thread")
            conn.execute(
                """INSERT INTO threads(id,workspace_id,title,status,revision,created_at,updated_at)
                   VALUES(?,?,?,'idle',0,?,?)""", (thread_id, workspace_id, item["title"], now, now),
            )
            self._update_revision(conn, "workspaces", workspace_id, expected_revision, now)
            conn.execute("INSERT INTO research_thread_items VALUES(?,?,1,?,?)", (thread_id, item_id, now, actor_id))
            self._audit(conn, "thread", thread_id, "thread.created", {"workspace_id": workspace_id, "title": item["title"]})
            self._audit(conn, "thread", thread_id, "research.item_selected", {"item_id": item_id})
            return self._save_receipt(conn, actor_id, operation, idempotency_key, request, self._thread(conn, thread_id), 201)

    def select_research_item(self, *, thread_id, item_id, actor_id, idempotency_key):
        request = {"item_id": item_id}
        operation = f"INTERNAL:threads/{thread_id}/research-item"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            thread = self._thread(conn, thread_id)
            if thread["archived_at"] is not None:
                raise ThreadArchived(thread)
            if thread["active_run_id"] is not None:
                raise ThreadActiveRun(thread)
            if conn.execute("SELECT 1 FROM research_items WHERE id=?", (item_id,)).fetchone() is None:
                raise NotFound("adopted research item", item_id)
            now = self._now()
            conn.execute(
                """INSERT INTO research_thread_items VALUES(?,?,1,?,?)
                   ON CONFLICT(thread_id) DO UPDATE SET item_id=excluded.item_id,
                   revision=research_thread_items.revision+1,selected_at=excluded.selected_at,actor_id=excluded.actor_id""",
                (thread_id, item_id, now, actor_id),
            )
            self._audit(conn, "thread", thread_id, "research.item_selected", {"item_id": item_id})
            return self._save_receipt(conn, actor_id, operation, idempotency_key, request, {"thread_id": thread_id, "item_id": item_id}, 200)
