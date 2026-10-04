"""Save-only idea fragments using ControlStore transactions."""

from __future__ import annotations

from typing import Any

from .errors import NotFound

FRAGMENT_TEXT_MAX = 16_384
FRAGMENT_NOTE_MAX = 2_000
FRAGMENT_ORIGIN_REF_MAX = 200
FRAGMENT_ORIGINS = frozenset({"web", "telegram"})

#: The public projection. `origin_ref` (the adapter's update key) and
#: `actor_id` are transport data and never leave the store.
_FRAGMENT_COLUMNS = (
    "id",
    "text",
    "note",
    "origin",
    "thread_id",
    "context_item_id",
    "created_at",
)


class IdeaFragmentsStore:
    """Methods inherited by the sole Control writer; no independent connections.

    Saving a fragment appends no message, creates no run, writes no capture
    and asks nothing to answer it: a fragment is text kept for later.
    """

    def create_fragment(
        self,
        *,
        text: str,
        note: str,
        origin: str,
        thread_id: str | None,
        origin_ref: str | None,
        actor_id: str,
        idempotency_key: str,
    ):
        # Validate without rebinding: `_required_text` strips, and the stored
        # text is the operator's submission verbatim.
        self._required_text(text, "text", maximum=FRAGMENT_TEXT_MAX)
        if "\x00" in text:
            raise ValueError("text is invalid")
        if not isinstance(note, str) or len(note) > FRAGMENT_NOTE_MAX or "\x00" in note:
            raise ValueError("note is invalid")
        if origin not in FRAGMENT_ORIGINS:
            raise ValueError("origin is invalid")
        if origin == "telegram":
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("thread_id is required for a Telegram fragment")
            if (
                not isinstance(origin_ref, str)
                or not 1 <= len(origin_ref) <= FRAGMENT_ORIGIN_REF_MAX
            ):
                raise ValueError("origin_ref is required for a Telegram fragment")
        elif thread_id is not None or origin_ref is not None:
            raise ValueError("a Web fragment has no thread or origin reference")
        request = {"text": text, "note": note, "origin": origin, "thread_id": thread_id}
        operation = "POST:/api/v1/fragments"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            context_item_id = None
            if thread_id is not None:
                self._thread(conn, thread_id)
                # Provenance, not a link decision: whichever item the thread
                # was about when the idea arrived.
                selected = conn.execute(
                    "SELECT item_id FROM research_thread_items WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                context_item_id = str(selected["item_id"]) if selected else None
            fragment_id = self._id_factory("fragment")
            conn.execute(
                """INSERT INTO idea_fragments
                   (id, text, note, origin, origin_ref, thread_id,
                    context_item_id, actor_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fragment_id,
                    text,
                    note,
                    origin,
                    origin_ref,
                    thread_id,
                    context_item_id,
                    actor_id,
                    # Fixed microsecond precision: the list orders by this
                    # string, and `_now()` drops a zero fraction, which would
                    # sort a whole-second save after a later one.
                    self._registry_now(),
                ),
            )
            # The text never enters the audit trail: only where it came from.
            self._audit(conn, "fragment", fragment_id, "fragment.saved", {"origin": origin})
            value = self._fragment(conn, fragment_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def get_fragment(self, fragment_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._fragment(conn, fragment_id)

    def list_fragments(self, *, limit: int = 500, cursor: str | None = None):
        """One bounded page, newest first, with a cursor proven by one extra row.

        `(created_at, id)` is a total order, so the boundary is stable when
        two fragments share a timestamp.
        """

        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            before_created_at: str | None = None
            if cursor is not None:
                row = conn.execute(
                    "SELECT created_at FROM idea_fragments WHERE id = ?", (cursor,)
                ).fetchone()
                if row is None:
                    raise ValueError("cursor is invalid")
                before_created_at = str(row["created_at"])
            rows = conn.execute(
                """SELECT id FROM idea_fragments
                   WHERE (? IS NULL OR (created_at, id) < (?, ?))
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (cursor, before_created_at, cursor, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            items = [self._fragment(conn, str(row["id"])) for row in rows[:limit]]
        # Imported here because store.py imports this mixin.
        from .store import _CursorPage

        return _CursorPage(
            items, next_cursor=str(items[-1]["id"]) if has_more and items else None
        )

    def _fragment(self, conn, fragment_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {', '.join(_FRAGMENT_COLUMNS)} FROM idea_fragments WHERE id = ?",
            (fragment_id,),
        ).fetchone()
        if row is None:
            raise NotFound("fragment", fragment_id)
        return {key: row[key] for key in _FRAGMENT_COLUMNS}
