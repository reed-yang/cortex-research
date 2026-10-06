"""XHS plugin state, content bindings and source links using ControlStore transactions.

Most commands here take the caller's connection, so one drain step can record
a child operation's result, the rows it proved and its task's completion in a
single transaction. Provider I/O never happens inside one. A task's
`payload_json` may hold a signed CDN URL: it is returned only to the drain and
never belongs in a DTO, a log line or a public file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..sources.identity import canonicalize_arxiv_id, normalize_url
from ..sources.models import (
    CONTENT_SOURCE_KINDS,
    normalize_source_text,
    normalize_xhs_id,
)
from .errors import InvalidTransition, NotFound, RevisionConflict

XHS_ROLES = frozenset({"curator", "author"})
XHS_SCAN_OUTCOMES = frozenset({"ok", "no_new_notes", "failed"})
XHS_NOTE_STATES = frozenset(
    {
        "discovered",
        "detail_ok",
        "assets_done",
        "ocr_done",
        "identified",
        "saved",
        "unsupported",
        "failed",
    }
)
XHS_IMAGE_STATES = frozenset({"pending", "ok", "failed"})
XHS_RECOMMENDATION_KINDS = frozenset({"paper", "blog", "other"})
XHS_URL_STATES = frozenset(
    {
        "none",
        "from_text",
        "auto_matched",
        "unverified",
        "not_found",
        "operator_set",
        "failed",
    }
)
_URL_BEARING_STATES = frozenset({"from_text", "auto_matched", "unverified", "operator_set"})
XHS_RECOMMENDATION_ORIGINS = frozenset({"rule", "model", "rule+model"})
XHS_IMPORT_STATES = frozenset({"none", "staged", "importing", "imported", "failed"})
#: The most recommendations one import command takes.
XHS_IMPORT_BATCH_MAX = 100
#: Every failure a child operation may report, as a category and never as text.
XHS_FAILURE_CATEGORIES = frozenset(
    {
        "auth",
        "payment",
        "rate_limited",
        "transient",
        "outcome_unknown",
        "upstream_error",
        "not_found",
        "invalid_response",
        "url_expired",
    }
)
#: Retried after 10 min, then 1 h, then 6 h; the next such failure is final.
XHS_RETRYABLE_FAILURES = frozenset({"rate_limited", "transient", "outcome_unknown"})
XHS_RETRY_BACKOFF_SECONDS = (600, 3_600, 21_600)
#: Each task kind and the prefix its idempotent subject key carries.
XHS_TASK_KINDS: Mapping[str, str] = {
    "list_page": "scan:",
    "detail": "detail:",
    "download": "download:",
    "ocr": "ocr:",
    "identify": "identify:",
    "resolve": "resolve:",
    "save": "save:",
    "blog_import": "blog:",
    "capture_link": "capture:",
}
XHS_TASK_STATES = frozenset({"pending", "running", "done", "failed", "canceled"})
XHS_USAGE_PROVIDERS = frozenset({"tikhub", "ocr", "gpt"})
XHS_ROOT_IDS = tuple(kind.root_id for kind in CONTENT_SOURCE_KINDS.values())

_SUBJECT_KEY_RE = re.compile(r"[a-z_]+:[\x21-\x7e]{1,290}\Z")
_ITEM_KEY_RE = re.compile(r"[\x21-\x7e]{1,200}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_NAME_RE = re.compile(r"[1-9][0-9]{0,2}-[0-9a-f]{12}\.(?:jpg|png|webp|gif)\Z")
_ENGINE_NAME_RE = re.compile(r"[0-9a-z.-]{1,64}\Z")
_JSON_MAX_BYTES = 65_536
_METADATA_MAX_BYTES = 16_384

_NOTE_UPDATE_FIELDS = frozenset(
    {
        "note_type",
        "state",
        "title",
        "caption",
        "caption_complete",
        "published_at",
        "evidence_override",
        "last_error",
    }
)
_IMAGE_UPDATE_FIELDS = frozenset(
    {
        "fileid",
        "upstream_width",
        "upstream_height",
        "sha256",
        "byte_size",
        "media_type",
        "width",
        "height",
        "asset_name",
        "download_state",
        "download_error",
        "ocr_state",
        "ocr_error",
        "ocr_engine",
        "ocr_flags",
        "ocr_text_sha256",
    }
)
_RECOMMENDATION_UPDATE_FIELDS = frozenset(
    {
        "url",
        "url_state",
        "url_checked_title",
        "capture_id",
        "import_state",
        "imported_source_id",
    }
)


def _paths_overlap(first: str, second: str) -> bool:
    """Whether one directory is inside the other once `..` and links resolve."""

    first_path, second_path = Path(os.path.realpath(first)), Path(os.path.realpath(second))
    return first_path.is_relative_to(second_path) or second_path.is_relative_to(first_path)


def _category(value: Any, name: str = "failure category") -> str:
    if value not in XHS_FAILURE_CATEGORIES:
        raise ValueError(f"{name} is unsupported")
    return value


def _optional_category(value: Any, name: str) -> str | None:
    return None if value is None else _category(value, name)


def _bounded_text(value: Any, name: str, *, maximum: int, minimum: int = 0) -> str:
    # Stored verbatim: captions and quotes are evidence, so nothing is stripped.
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_int(value: Any, name: str, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _json_text(value: Any, name: str, *, maximum: int) -> str:
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise ValueError(f"{name} is invalid") from None
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"{name} is too large")
    return text


def xhs_identify_digest(caption: str, transcriptions: Collection[tuple[int, str]]) -> str:
    """The input sha256 an `identify` task is keyed by.

    It covers the caption and each transcribed image's ordinal and text hash.
    A text hash commits to its text, so equal digests mean equal input, and a
    retried image that changes any transcription gives a new task.
    """

    value = {
        "caption": caption,
        "transcriptions": sorted(
            [int(ordinal), str(text_sha256)] for ordinal, text_sha256 in transcriptions
        ),
    }
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Note states in pipeline order; a retried image moves a note back.
_NOTE_ORDER = ("detail_ok", "assets_done", "ocr_done", "identified", "saved")
#: Capture states after which nothing more happens to it.
_CAPTURE_TERMINAL = frozenset({"consumed", "dismissed", "failed"})
#: A blog recommendation whose link may still be searched for.
_RESOLVABLE_URL_STATES = frozenset({"none", "not_found", "failed"})
#: The task kinds whose failures each provider's Status line reports.
_XHS_PROVIDER_KINDS: Mapping[str, tuple[str, ...]] = {
    "tikhub": ("list_page", "detail"),
    "cdn": ("download",),
    "ocr": ("ocr",),
    "gpt": ("identify", "resolve"),
    "blog": ("blog_import",),
}


class XhsStore:
    """Methods inherited by the sole Control writer; no independent connections."""

    # -- bloggers ------------------------------------------------------------

    def follow_xhs_blogger(
        self,
        *,
        user_id: str,
        role: str,
        display_name: str | None = None,
        actor_id: str,
        idempotency_key: str,
    ):
        user_id = normalize_xhs_id(user_id, "user_id")
        if role not in XHS_ROLES:
            raise ValueError("role is unsupported")
        if display_name is not None:
            display_name = self._required_text(display_name, "display_name", maximum=200)
        request = {"user_id": user_id, "role": role, "display_name": display_name}
        operation = f"INTERNAL:xhs/bloggers/{user_id}/follow"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            now = self._registry_now()
            existing = conn.execute(
                "SELECT * FROM xhs_bloggers WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO xhs_bloggers
                       (user_id, display_name, role, followed, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (user_id, display_name, role, now, now),
                )
            else:
                conn.execute(
                    """UPDATE xhs_bloggers
                       SET followed = 1, role = ?, display_name = ?,
                           revision = revision + 1, updated_at = ?
                       WHERE user_id = ?""",
                    (
                        role,
                        display_name if display_name is not None else existing["display_name"],
                        now,
                        user_id,
                    ),
                )
            self._audit(conn, "xhs_blogger", user_id, "xhs.blogger.followed", {"role": role})
            value = self._xhs_blogger(conn, user_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def unfollow_xhs_blogger(self, *, user_id: str, actor_id: str, idempotency_key: str):
        """Stop scanning a blogger. Notes already saved are kept."""

        return self._update_xhs_blogger(
            user_id=user_id,
            assignments={"followed": 0},
            request={"followed": False},
            event="xhs.blogger.unfollowed",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def set_xhs_blogger_role(
        self, *, user_id: str, role: str, actor_id: str, idempotency_key: str
    ):
        if role not in XHS_ROLES:
            raise ValueError("role is unsupported")
        return self._update_xhs_blogger(
            user_id=user_id,
            assignments={"role": role},
            request={"role": role},
            event="xhs.blogger.role_set",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def _update_xhs_blogger(
        self,
        *,
        user_id: str,
        assignments: Mapping[str, Any],
        request: Mapping[str, Any],
        event: str,
        actor_id: str,
        idempotency_key: str,
    ):
        user_id = normalize_xhs_id(user_id, "user_id")
        request = {"user_id": user_id, **request}
        operation = f"INTERNAL:xhs/bloggers/{user_id}/{event.rpartition('.')[2]}"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            self._xhs_blogger(conn, user_id)
            columns = ", ".join(f"{name} = ?" for name in assignments)
            conn.execute(
                f"""UPDATE xhs_bloggers
                    SET {columns}, revision = revision + 1, updated_at = ?
                    WHERE user_id = ?""",
                (*assignments.values(), self._registry_now(), user_id),
            )
            self._audit(conn, "xhs_blogger", user_id, event, dict(request))
            value = self._xhs_blogger(conn, user_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def get_xhs_blogger(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._xhs_blogger(conn, normalize_xhs_id(user_id, "user_id"))

    def list_xhs_bloggers(self, *, followed_only: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT user_id FROM xhs_bloggers
                   WHERE (? = 0 OR followed = 1) ORDER BY created_at, user_id""",
                (int(bool(followed_only)),),
            ).fetchall()
            return [self._xhs_blogger(conn, str(row["user_id"])) for row in rows]

    def _xhs_record_scan(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        outcome: str,
        error: str | None = None,
        new_note_at: str | None = None,
    ) -> dict[str, Any]:
        """Record one scan's answer: ok, no new notes, or a typed failure."""

        if outcome not in XHS_SCAN_OUTCOMES:
            raise ValueError("scan outcome is unsupported")
        if (outcome == "failed") != (error is not None):
            raise ValueError("a failed scan, and only a failed scan, carries a category")
        _optional_category(error, "scan error")
        self._xhs_blogger(conn, user_id)
        now = self._registry_now()
        conn.execute(
            """UPDATE xhs_bloggers
               SET last_scan_at = ?, last_scan_outcome = ?, last_scan_error = ?,
                   last_new_note_at = COALESCE(?, last_new_note_at),
                   revision = revision + 1, updated_at = ?
               WHERE user_id = ?""",
            (now, outcome, error, new_note_at, now, user_id),
        )
        return self._xhs_blogger(conn, user_id)

    def _xhs_blogger(self, conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM xhs_bloggers WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("xhs blogger", user_id)
        value = self._row(row)
        value["followed"] = bool(value["followed"])
        return value

    # -- notes ----------------------------------------------------------------

    def _xhs_insert_note(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: str,
        user_id: str,
        note_type: str,
        title: str,
        caption: str,
        caption_complete: bool,
        published_at: str | None,
        state: str = "discovered",
    ) -> bool:
        """Insert an unseen note; return False, changing nothing, for a seen one."""

        note_id = normalize_xhs_id(note_id, "note_id")
        user_id = normalize_xhs_id(user_id, "user_id")
        values = self._xhs_note_values(
            {
                "note_type": note_type,
                "state": state,
                "title": title,
                "caption": caption,
                "caption_complete": caption_complete,
                "published_at": published_at,
            }
        )
        if conn.execute(
            "SELECT 1 FROM xhs_notes WHERE note_id = ?", (note_id,)
        ).fetchone() is not None:
            return False
        now = self._registry_now()
        conn.execute(
            """INSERT INTO xhs_notes
               (note_id, user_id, note_type, state, title, caption,
                caption_complete, published_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note_id,
                user_id,
                values["note_type"],
                values["state"],
                values["title"],
                values["caption"],
                values["caption_complete"],
                values["published_at"],
                now,
                now,
            ),
        )
        return True

    def _xhs_update_note(
        self,
        conn: sqlite3.Connection,
        note_id: str,
        *,
        expected_revision: int,
        **fields: Any,
    ) -> dict[str, Any]:
        if not fields or not set(fields) <= _NOTE_UPDATE_FIELDS:
            raise ValueError("xhs note fields are invalid")
        values = self._xhs_note_values(fields)
        return self._xhs_fenced_update(
            conn,
            table="xhs_notes",
            where={"note_id": note_id},
            expected_revision=expected_revision,
            values=values,
            current=lambda: self._xhs_note(conn, note_id),
        )

    def _xhs_note_values(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, value in fields.items():
            if name == "note_type":
                if not isinstance(value, str) or re.fullmatch(r"[a-z_]{1,32}", value) is None:
                    raise ValueError("note_type is invalid")
            elif name == "state":
                if value not in XHS_NOTE_STATES:
                    raise ValueError("note state is unsupported")
            elif name == "title":
                value = _bounded_text(value, "title", maximum=500)
            elif name == "caption":
                value = _bounded_text(value, "caption", maximum=20_000)
            elif name == "caption_complete":
                if type(value) is not bool:
                    raise ValueError("caption_complete must be boolean")
                value = int(value)
            elif name == "published_at":
                if value is not None:
                    value = self._required_text(value, "published_at", maximum=64)
            elif name == "evidence_override":
                if value not in {None, "include", "exclude"}:
                    raise ValueError("evidence_override is unsupported")
            elif name == "last_error":
                value = _optional_category(value, "note error")
            values[name] = value
        return values

    def get_xhs_note(self, note_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._xhs_note(conn, normalize_xhs_id(note_id, "note_id"))

    def _xhs_note(self, conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM xhs_notes WHERE note_id = ?", (note_id,)).fetchone()
        if row is None:
            raise NotFound("xhs note", note_id)
        value = self._row(row)
        value["caption_complete"] = bool(value["caption_complete"])
        return value

    def xhs_note_id_for_source(self, source_id: str) -> str:
        with self._connect() as conn:
            return str(self._xhs_note_by_source(conn, source_id)["note_id"])

    def _xhs_note_by_source(self, conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
        """The saved note behind one `xhs_note` source; no other source has one."""

        row = conn.execute(
            "SELECT note_id FROM xhs_notes WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise NotFound("xhs note", source_id)
        return self._xhs_note(conn, str(row["note_id"]))

    def xhs_note_view(self, source_id: str) -> dict[str, Any]:
        """One saved note with its blogger, images and recommendations, each
        recommendation with its Capture's state and its imported source's kind.

        Rows only, for the caller to project. No task is read, so no payload
        (where a signed URL may live) and no lease is part of the answer.
        """

        with self._connect() as conn:
            note = self._xhs_note_by_source(conn, source_id)
            blogger = conn.execute(
                "SELECT * FROM xhs_bloggers WHERE user_id = ?", (note["user_id"],)
            ).fetchone()
            return {
                "note": note,
                "blogger": None if blogger is None else self._row(blogger),
                "images": self._xhs_images(conn, str(note["note_id"])),
                "recommendations": [
                    self._xhs_recommendation_view(conn, str(recommendation["id"]))
                    for recommendation in self._xhs_recommendations(conn, str(note["note_id"]))
                ],
            }

    def _xhs_recommendation_view(
        self, conn: sqlite3.Connection, recommendation_id: str
    ) -> dict[str, Any]:
        """One recommendation with its Capture's state and revision and the
        kind of the source it imported."""

        recommendation = self._xhs_recommendation(conn, recommendation_id)
        capture = imported = None
        if recommendation["capture_id"] is not None:
            capture = conn.execute(
                "SELECT state, revision FROM captures WHERE id = ?",
                (recommendation["capture_id"],),
            ).fetchone()
        if recommendation["imported_source_id"] is not None:
            imported = conn.execute(
                "SELECT source_kind FROM sources WHERE id = ?",
                (recommendation["imported_source_id"],),
            ).fetchone()
        recommendation["capture_state"] = None if capture is None else capture["state"]
        recommendation["capture_revision"] = None if capture is None else capture["revision"]
        recommendation["imported_source_kind"] = (
            None if imported is None else imported["source_kind"]
        )
        return recommendation

    # -- images ---------------------------------------------------------------

    def _xhs_upsert_image(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: str,
        ordinal: int,
        fileid: str | None,
        upstream_width: int | None = None,
        upstream_height: int | None = None,
    ) -> dict[str, Any]:
        """Record one carousel image at its original ordinal.

        A refreshed detail may bring new signed URLs for the same image, never
        a different image at a known ordinal: a changed `fileid` is refused.
        """

        if type(ordinal) is not int or not 1 <= ordinal <= 100:
            raise ValueError("ordinal is invalid")
        if fileid is not None:
            fileid = _bounded_text(fileid, "fileid", minimum=1, maximum=200)
        _optional_int(upstream_width, "upstream_width", minimum=1, maximum=100_000)
        _optional_int(upstream_height, "upstream_height", minimum=1, maximum=100_000)
        self._xhs_note(conn, note_id)
        row = conn.execute(
            "SELECT * FROM xhs_note_images WHERE note_id = ? AND ordinal = ?",
            (note_id, ordinal),
        ).fetchone()
        now = self._registry_now()
        if row is None:
            conn.execute(
                """INSERT INTO xhs_note_images
                   (note_id, ordinal, fileid, upstream_width, upstream_height,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (note_id, ordinal, fileid, upstream_width, upstream_height, now, now),
            )
        elif row["fileid"] is not None and fileid is not None and row["fileid"] != fileid:
            raise InvalidTransition("xhs_image_identity_changed", "detail")
        elif (row["fileid"], row["upstream_width"], row["upstream_height"]) != (
            fileid if fileid is not None else row["fileid"],
            upstream_width,
            upstream_height,
        ):
            conn.execute(
                """UPDATE xhs_note_images
                   SET fileid = COALESCE(?, fileid), upstream_width = ?,
                       upstream_height = ?, revision = revision + 1, updated_at = ?
                   WHERE note_id = ? AND ordinal = ?""",
                (fileid, upstream_width, upstream_height, now, note_id, ordinal),
            )
        return self._xhs_image(conn, note_id, ordinal)

    def _xhs_update_image(
        self,
        conn: sqlite3.Connection,
        note_id: str,
        ordinal: int,
        *,
        expected_revision: int,
        **fields: Any,
    ) -> dict[str, Any]:
        if not fields or not set(fields) <= _IMAGE_UPDATE_FIELDS:
            raise ValueError("xhs image fields are invalid")
        values: dict[str, Any] = {}
        for name, value in fields.items():
            if name in {"download_state", "ocr_state"}:
                if value not in XHS_IMAGE_STATES:
                    raise ValueError(f"{name} is unsupported")
            elif name in {"download_error", "ocr_error"}:
                value = _optional_category(value, name)
            elif name in {"sha256", "ocr_text_sha256"}:
                if value is not None and (
                    not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
                ):
                    raise ValueError(f"{name} is invalid")
            elif name == "asset_name":
                if value is not None and (
                    not isinstance(value, str) or _ASSET_NAME_RE.fullmatch(value) is None
                ):
                    raise ValueError("asset_name is invalid")
            elif name == "ocr_engine":
                if value is not None and (
                    not isinstance(value, str) or _ENGINE_NAME_RE.fullmatch(value) is None
                ):
                    raise ValueError("ocr_engine is invalid")
            elif name == "ocr_flags":
                if not isinstance(value, (list, tuple, frozenset, set)) or not set(
                    value
                ) <= {"truncated", "empty"}:
                    raise ValueError("ocr_flags are invalid")
                value = ",".join(sorted(set(value)))
            elif name == "fileid":
                if value is not None:
                    value = _bounded_text(value, "fileid", minimum=1, maximum=200)
            elif name == "byte_size":
                value = _optional_int(value, name, minimum=1, maximum=20 * 1024 * 1024)
            elif name == "media_type":
                if value not in {None, "image/jpeg", "image/png", "image/webp", "image/gif"}:
                    raise ValueError("media_type is unsupported")
            else:
                value = _optional_int(value, name, minimum=1, maximum=100_000)
            values[name] = value
        return self._xhs_fenced_update(
            conn,
            table="xhs_note_images",
            where={"note_id": note_id, "ordinal": ordinal},
            expected_revision=expected_revision,
            values=values,
            current=lambda: self._xhs_image(conn, note_id, ordinal),
        )

    def _xhs_image(self, conn: sqlite3.Connection, note_id: str, ordinal: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM xhs_note_images WHERE note_id = ? AND ordinal = ?",
            (note_id, ordinal),
        ).fetchone()
        if row is None:
            raise NotFound("xhs note image", f"{note_id}:{ordinal}")
        value = self._row(row)
        value["ocr_flags"] = [flag for flag in value["ocr_flags"].split(",") if flag]
        return value

    def _xhs_images(self, conn: sqlite3.Connection, note_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT ordinal FROM xhs_note_images WHERE note_id = ? ORDER BY ordinal",
            (note_id,),
        ).fetchall()
        return [self._xhs_image(conn, note_id, int(row["ordinal"])) for row in rows]

    def list_xhs_note_images(self, note_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            note_id = normalize_xhs_id(note_id, "note_id")
            self._xhs_note(conn, note_id)
            return self._xhs_images(conn, note_id)

    # -- recommendations ------------------------------------------------------

    def _xhs_upsert_recommendation(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: str,
        item_key: str,
        image_ordinal: int | None,
        kind: str,
        title: str,
        quote: str,
        arxiv_id: str | None,
        url: str | None,
        url_state: str,
        origin: str,
        identify_run: str,
    ) -> dict[str, Any]:
        """Insert or refresh one identified item, keyed by its note and item key.

        Import progress is never touched here, and a link the operator set is
        kept: a re-identification refreshes only what identification owns.
        """

        if not isinstance(item_key, str) or _ITEM_KEY_RE.fullmatch(item_key) is None:
            raise ValueError("item_key is invalid")
        _optional_int(image_ordinal, "image_ordinal", minimum=1, maximum=100)
        if kind not in XHS_RECOMMENDATION_KINDS:
            raise ValueError("recommendation kind is unsupported")
        if origin not in XHS_RECOMMENDATION_ORIGINS:
            raise ValueError("recommendation origin is unsupported")
        title = _bounded_text(title, "title", minimum=1, maximum=1_000)
        quote = _bounded_text(quote, "quote", minimum=1, maximum=4_000)
        identify_run = self._required_text(identify_run, "identify_run", maximum=300)
        if arxiv_id is not None:
            if kind != "paper":
                raise ValueError("only a paper carries an arXiv ID")
            arxiv_id = canonicalize_arxiv_id(arxiv_id).authority_id
        url, url_state = self._xhs_url_values(url, url_state)
        self._xhs_note(conn, note_id)
        row = conn.execute(
            "SELECT * FROM xhs_recommendations WHERE note_id = ? AND item_key = ?",
            (note_id, item_key),
        ).fetchone()
        now = self._registry_now()
        identified = {
            "image_ordinal": image_ordinal,
            "kind": kind,
            "title": title,
            "quote": quote,
            "arxiv_id": arxiv_id,
            "origin": origin,
            "identify_run": identify_run,
        }
        if row is None:
            recommendation_id = self._id_factory("xhs_rec")
            conn.execute(
                """INSERT INTO xhs_recommendations
                   (id, note_id, item_key, image_ordinal, kind, title, quote,
                    arxiv_id, url, url_state, origin, identify_run,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    recommendation_id,
                    note_id,
                    item_key,
                    image_ordinal,
                    kind,
                    title,
                    quote,
                    arxiv_id,
                    url,
                    url_state,
                    origin,
                    identify_run,
                    now,
                    now,
                ),
            )
            return self._xhs_recommendation(conn, recommendation_id)
        changes = dict(identified)
        # A link found in the text replaces an unresolved one; a link the
        # search resolved, or the operator set, survives a re-identification.
        # So does the link a blog is importing or was imported from: its
        # source link names that page.
        if row["import_state"] not in {"staged", "importing", "imported"} and (
            row["url_state"] in {"none", "from_text"}
            or (url_state == "from_text" and row["url_state"] != "operator_set")
        ):
            changes.update(url=url, url_state=url_state)
        if any(row[name] != value for name, value in changes.items()):
            columns = ", ".join(f"{name} = ?" for name in changes)
            conn.execute(
                f"""UPDATE xhs_recommendations
                    SET {columns}, revision = revision + 1, updated_at = ?
                    WHERE id = ?""",
                (*changes.values(), now, row["id"]),
            )
        return self._xhs_recommendation(conn, str(row["id"]))

    def _xhs_update_recommendation(
        self,
        conn: sqlite3.Connection,
        recommendation_id: str,
        *,
        expected_revision: int,
        **fields: Any,
    ) -> dict[str, Any]:
        if not fields or not set(fields) <= _RECOMMENDATION_UPDATE_FIELDS:
            raise ValueError("xhs recommendation fields are invalid")
        values = dict(fields)
        if "url" in values or "url_state" in values:
            if not {"url", "url_state"} <= set(values):
                raise ValueError("url and url_state change together")
            values["url"], values["url_state"] = self._xhs_url_values(
                values["url"], values["url_state"]
            )
        if "url_checked_title" in values and values["url_checked_title"] is not None:
            values["url_checked_title"] = _bounded_text(
                values["url_checked_title"], "url_checked_title", minimum=1, maximum=1_000
            )
        if "import_state" in values and values["import_state"] not in XHS_IMPORT_STATES:
            raise ValueError("import_state is unsupported")
        before = self._xhs_recommendation(conn, recommendation_id) if "url" in values else None
        updated = self._xhs_fenced_update(
            conn,
            table="xhs_recommendations",
            where={"id": recommendation_id},
            expected_revision=expected_revision,
            values=values,
            current=lambda: self._xhs_recommendation(conn, recommendation_id),
        )
        if before is not None and before["url"] != updated["url"]:
            # An import checks the note's revision: a page that chose this
            # item with its old link is refused rather than importing the new one.
            conn.execute(
                """UPDATE xhs_notes SET revision = revision + 1, updated_at = ?
                   WHERE note_id = ?""",
                (self._registry_now(), updated["note_id"]),
            )
        return updated

    @staticmethod
    def _xhs_url_values(url: Any, url_state: Any) -> tuple[str | None, str]:
        """A URL-bearing state carries a normalized http(s) URL; others carry none."""

        if url_state not in XHS_URL_STATES:
            raise ValueError("url_state is unsupported")
        if url_state in _URL_BEARING_STATES:
            if url is None:
                raise ValueError("url is required for this url_state")
            return normalize_url(url), url_state
        if url is not None:
            raise ValueError("url is not allowed for this url_state")
        return None, url_state

    def get_xhs_recommendation(self, recommendation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._xhs_recommendation(conn, recommendation_id)

    def xhs_save_inputs_sha256(self, note_id: str) -> str:
        """A digest of the Control state a note's saved version renders.

        A save reads it before it reads that state; its record compares it
        with the state then, and a change made meanwhile queues the next
        version.
        """

        with self._connect() as conn:
            return self._xhs_save_inputs_sha256(conn, normalize_xhs_id(note_id, "note_id"))

    def _xhs_save_inputs_sha256(self, conn: sqlite3.Connection, note_id: str) -> str:
        note = self._xhs_note(conn, note_id)
        blogger = self._xhs_blogger(conn, str(note["user_id"]))
        state = {
            "note": [note[name] for name in ("title", "caption", "published_at", "user_id")],
            "blogger": [blogger["display_name"], blogger["role"]],
            "images": [
                [
                    image[name]
                    for name in (
                        "ordinal", "download_state", "download_error", "asset_name",
                        "ocr_state", "ocr_error", "ocr_flags", "ocr_text_sha256",
                    )
                ]
                for image in self._xhs_images(conn, note_id)
            ],
            "recommendations": [
                [
                    item[name]
                    for name in (
                        "id", "kind", "title", "image_ordinal", "quote", "arxiv_id",
                        "url", "url_state",
                    )
                ]
                for item in self._xhs_recommendations(conn, note_id)
            ],
        }
        text = json.dumps(state, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def list_xhs_recommendations(self, note_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            note_id = normalize_xhs_id(note_id, "note_id")
            self._xhs_note(conn, note_id)
            return self._xhs_recommendations(conn, note_id)

    def _xhs_recommendations(
        self, conn: sqlite3.Connection, note_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT id FROM xhs_recommendations WHERE note_id = ?
               ORDER BY image_ordinal IS NULL, image_ordinal, created_at, id""",
            (note_id,),
        ).fetchall()
        return [self._xhs_recommendation(conn, str(row["id"])) for row in rows]

    def _xhs_recommendation(
        self, conn: sqlite3.Connection, recommendation_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM xhs_recommendations WHERE id = ?", (recommendation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("xhs recommendation", recommendation_id)
        return self._row(row)

    # -- tasks ----------------------------------------------------------------

    def create_xhs_task(
        self, *, kind: str, subject_key: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        with self._transaction() as conn:
            return self._xhs_create_task(
                conn, kind=kind, subject_key=subject_key, payload=payload
            )

    def _xhs_create_task(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        subject_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Create a task once per subject key; a repeat returns the first, unchanged."""

        prefix = XHS_TASK_KINDS.get(kind)
        if prefix is None:
            raise ValueError("xhs task kind is unsupported")
        if (
            not isinstance(subject_key, str)
            or _SUBJECT_KEY_RE.fullmatch(subject_key) is None
            or not subject_key.startswith(prefix)
        ):
            raise ValueError("xhs task subject key is invalid")
        if not isinstance(payload, Mapping):
            raise ValueError("xhs task payload is invalid")
        payload_json = _json_text(dict(payload), "xhs task payload", maximum=_JSON_MAX_BYTES)
        # The prefix names the kind, so a matching key is the same task.
        existing = conn.execute(
            "SELECT id FROM xhs_tasks WHERE subject_key = ?", (subject_key,)
        ).fetchone()
        if existing is not None:
            return self._xhs_task(conn, str(existing["id"])), False
        task_id = self._id_factory("xhs_task")
        now = self._registry_now()
        conn.execute(
            """INSERT INTO xhs_tasks
               (id, kind, subject_key, payload_json, state, next_attempt_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (task_id, kind, subject_key, payload_json, now, now, now),
        )
        return self._xhs_task(conn, task_id), True

    def claim_xhs_task(
        self, *, lease_seconds: int, kinds: Collection[str] | None = None
    ) -> dict[str, Any] | None:
        """Lease the oldest due task, or reclaim one whose lease expired.

        A claim counts as an attempt. An expired lease is an unknown outcome;
        once the retry budget is spent it fails rather than running again.
        """

        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        selected = sorted(XHS_TASK_KINDS if kinds is None else set(kinds))
        if not selected or not set(selected) <= set(XHS_TASK_KINDS):
            raise ValueError("xhs task kinds are invalid")
        placeholders = ", ".join("?" for _ in selected)
        with self._transaction() as conn:
            now_value = self._utc_now()
            now = self._format_registry_time(now_value)
            while True:
                row = conn.execute(
                    f"""SELECT id, state, attempts FROM xhs_tasks
                        WHERE kind IN ({placeholders})
                          AND ((state = 'pending' AND next_attempt_at <= ?)
                               OR (state = 'running' AND lease_until <= ?))
                        ORDER BY created_at, id LIMIT 1""",
                    (*selected, now, now),
                ).fetchone()
                if row is None:
                    return None
                if row["state"] == "running" and int(row["attempts"]) > len(
                    XHS_RETRY_BACKOFF_SECONDS
                ):
                    conn.execute(
                        """UPDATE xhs_tasks
                           SET state = 'failed', lease_until = NULL,
                               last_error = 'outcome_unknown',
                               revision = revision + 1, updated_at = ?
                           WHERE id = ?""",
                        (now, row["id"]),
                    )
                    # The rows the task was working on learn of it as they
                    # would from a recorded failure.
                    self._xhs_apply_failure(
                        conn, self._xhs_task(conn, str(row["id"])), "outcome_unknown"
                    )
                    continue
                lease_until = self._format_registry_time(
                    now_value + timedelta(seconds=lease_seconds)
                )
                conn.execute(
                    """UPDATE xhs_tasks
                       SET state = 'running', lease_until = ?, attempts = attempts + 1,
                           revision = revision + 1, updated_at = ?
                       WHERE id = ?""",
                    (lease_until, now, row["id"]),
                )
                return self._xhs_task(conn, str(row["id"]))

    def complete_xhs_task(
        self, task_id: str, *, expected_revision: int, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            return self._xhs_complete_task(
                conn, task_id, expected_revision=expected_revision, result=result
            )

    def _xhs_complete_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        *,
        expected_revision: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise ValueError("xhs task result is invalid")
        result_json = _json_text(dict(result), "xhs task result", maximum=_JSON_MAX_BYTES)
        self._xhs_running_task(conn, task_id, expected_revision, "done")
        return self._xhs_task_transition(
            conn,
            task_id,
            expected_revision,
            state="done",
            next_attempt_at=None,
            last_error=None,
            result_json=result_json,
        )

    def fail_xhs_task(
        self, task_id: str, *, expected_revision: int, category: str
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            return self._xhs_fail_task(
                conn, task_id, expected_revision=expected_revision, category=category
            )

    def _xhs_fail_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        *,
        expected_revision: int,
        category: str,
    ) -> dict[str, Any]:
        """Back a retryable failure off, or fail the task for good.

        A retryable category waits 10 min, then 1 h, then 6 h; after the third
        retry it is final. Every other category is final at once.
        """

        category = _category(category)
        task = self._xhs_running_task(conn, task_id, expected_revision, "failed")
        retries = int(task["attempts"]) - 1
        if category in XHS_RETRYABLE_FAILURES and retries < len(XHS_RETRY_BACKOFF_SECONDS):
            next_attempt_at = self._format_registry_time(
                self._utc_now() + timedelta(seconds=XHS_RETRY_BACKOFF_SECONDS[retries])
            )
            return self._xhs_task_transition(
                conn,
                task_id,
                expected_revision,
                state="pending",
                next_attempt_at=next_attempt_at,
                last_error=category,
                result_json=None,
            )
        return self._xhs_task_transition(
            conn,
            task_id,
            expected_revision,
            state="failed",
            next_attempt_at=None,
            last_error=category,
            result_json=None,
        )

    def release_xhs_task(
        self,
        task_id: str,
        *,
        expected_revision: int,
        not_before: str | None = None,
        delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Return a claimed task that never ran, without spending an attempt.

        For a provider at its daily cap, `not_before` is the next UTC day; a
        task waiting on something else comes back after `delay_seconds`.
        """

        with self._transaction() as conn:
            task = self._xhs_running_task(conn, task_id, expected_revision, "pending")
            if delay_seconds is not None:
                _optional_int(delay_seconds, "delay_seconds", minimum=1, maximum=86_400)
                next_attempt_at = self._format_registry_time(
                    self._utc_now() + timedelta(seconds=delay_seconds)
                )
            elif not_before is not None:
                next_attempt_at = self._required_text(not_before, "not_before", maximum=64)
            else:
                next_attempt_at = self._registry_now()
            conn.execute(
                """UPDATE xhs_tasks
                   SET state = 'pending', lease_until = NULL, next_attempt_at = ?,
                       attempts = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (
                    next_attempt_at,
                    max(0, int(task["attempts"]) - 1),
                    self._registry_now(),
                    task_id,
                    expected_revision,
                ),
            )
            return self._xhs_task(conn, task_id)

    def _xhs_reset_task(
        self,
        conn: sqlite3.Connection,
        subject_key: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a finished task due again with a fresh retry budget.

        A pending task is returned unchanged and a running one is refused: the
        lease owner records its own result.
        """

        row = conn.execute(
            "SELECT id, state FROM xhs_tasks WHERE subject_key = ?", (subject_key,)
        ).fetchone()
        if row is None:
            raise NotFound("xhs task", subject_key)
        if row["state"] == "running":
            raise InvalidTransition("running", "pending")
        if row["state"] == "pending" and payload is None:
            return self._xhs_task(conn, str(row["id"]))
        now = self._registry_now()
        payload_json = (
            _json_text(dict(payload), "xhs task payload", maximum=_JSON_MAX_BYTES)
            if payload is not None
            else None
        )
        conn.execute(
            """UPDATE xhs_tasks
               SET state = 'pending', attempts = 0, next_attempt_at = ?,
                   lease_until = NULL, last_error = NULL, result_json = NULL,
                   payload_json = COALESCE(?, payload_json),
                   revision = revision + 1, updated_at = ?
               WHERE id = ?""",
            (now, payload_json, now, row["id"]),
        )
        return self._xhs_task(conn, str(row["id"]))

    def xhs_task_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            counts = {state: 0 for state in sorted(XHS_TASK_STATES)}
            for row in conn.execute(
                "SELECT state, COUNT(*) AS count FROM xhs_tasks GROUP BY state"
            ):
                counts[str(row["state"])] = int(row["count"])
            return counts

    def get_xhs_task(self, task_id: str) -> dict[str, Any]:
        """The whole task, payload included. For the drain only; never a DTO."""

        with self._connect() as conn:
            return self._xhs_task(conn, task_id)

    def _xhs_running_task(
        self, conn: sqlite3.Connection, task_id: str, expected_revision: int, target: str
    ) -> dict[str, Any]:
        task = self._xhs_task(conn, task_id)
        self._expect_revision(task, expected_revision)
        if task["state"] != "running":
            raise InvalidTransition(str(task["state"]), target)
        return task

    def _xhs_task_transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expected_revision: int,
        *,
        state: str,
        next_attempt_at: str | None,
        last_error: str | None,
        result_json: str | None,
    ) -> dict[str, Any]:
        now = self._registry_now()
        cursor = conn.execute(
            """UPDATE xhs_tasks
               SET state = ?, lease_until = NULL,
                   next_attempt_at = COALESCE(?, next_attempt_at),
                   last_error = ?, result_json = ?,
                   revision = revision + 1, updated_at = ?
               WHERE id = ? AND revision = ? AND state = 'running'""",
            (state, next_attempt_at, last_error, result_json, now, task_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict(self._xhs_task(conn, task_id))
        return self._xhs_task(conn, task_id)

    def _xhs_task(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM xhs_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("xhs task", task_id)
        value = self._row(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        result = value.pop("result_json")
        value["result"] = json.loads(result) if result is not None else None
        return value

    # -- the drain's records ----------------------------------------------------

    def start_xhs_scans(
        self,
        *,
        max_pages: int,
        user_ids: Collection[str] | None = None,
        scan_id: str | None = None,
        full: bool = False,
    ) -> list[dict[str, Any]]:
        """Create page 1 of one scan per followed blogger, or per named one.

        A blogger whose previous scan still has a page to run gets no second
        scan, so a repeated pull never doubles the calls. A `full` scan is a
        backfill: it reads on past seen pages, up to `max_pages`.
        """

        _optional_int(max_pages, "max_pages", minimum=1, maximum=1_000)
        if max_pages is None or type(full) is not bool:
            raise ValueError("scan bounds are invalid")
        with self._transaction() as conn:
            scan_id = scan_id or self._utc_now().strftime("%Y%m%dT%H%M%SZ")
            if (
                not isinstance(scan_id, str)
                or re.fullmatch(r"[0-9A-Za-z_-]{1,40}", scan_id) is None
            ):
                raise ValueError("scan_id is invalid")
            if user_ids is None:
                selected = [
                    str(row["user_id"])
                    for row in conn.execute(
                        """SELECT user_id FROM xhs_bloggers WHERE followed = 1
                           ORDER BY created_at, user_id"""
                    )
                ]
            else:
                selected = [normalize_xhs_id(user_id, "user_id") for user_id in user_ids]
            created: list[dict[str, Any]] = []
            for user_id in selected:
                if not self._xhs_blogger(conn, user_id)["followed"]:
                    continue
                if conn.execute(
                    """SELECT 1 FROM xhs_tasks
                       WHERE kind = 'list_page' AND subject_key GLOB ?
                         AND state IN ('pending', 'running')""",
                    (f"scan:{user_id}:*",),
                ).fetchone() is not None:
                    continue
                task, fresh = self._xhs_create_task(
                    conn,
                    kind="list_page",
                    subject_key=f"scan:{user_id}:{scan_id}:1",
                    payload={
                        "user_id": user_id,
                        "scan": scan_id,
                        "page": 1,
                        "cursor": "",
                        "max_pages": max_pages,
                        "full": full,
                    },
                )
                if fresh:
                    created.append(task)
            return created

    def xhs_awaiting_refresh(self, note_id: str) -> list[int]:
        """Ordinals whose signed URL expired and wait for a fresh detail."""

        with self._connect() as conn:
            return [
                int(task["payload"]["ordinal"])
                for task in self._xhs_awaiting_refresh(conn, normalize_xhs_id(note_id, "note_id"))
            ]

    def record_xhs_task_result(
        self, task_id: str, *, expected_revision: int, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Apply one checked child answer and complete its task, atomically.

        The answer may hold signed URLs for the next tasks' private payloads;
        the task's own result keeps only a summary.
        """

        if not isinstance(result, Mapping):
            raise ValueError("xhs task result is invalid")
        with self._transaction() as conn:
            task = self._xhs_running_task(conn, task_id, expected_revision, "done")
            apply = {
                "list_page": self._xhs_apply_list_page,
                "detail": self._xhs_apply_detail,
                "download": self._xhs_apply_download,
                "ocr": self._xhs_apply_ocr,
                "identify": self._xhs_apply_identify,
                "resolve": self._xhs_apply_resolve,
                "save": self._xhs_apply_save,
                "blog_import": self._xhs_apply_blog_import,
                "capture_link": self._xhs_apply_capture_link,
            }.get(str(task["kind"]))
            if apply is None:
                raise ValueError("xhs task kind records no result")
            summary = apply(conn, task, result)
            return self._xhs_complete_task(
                conn, task_id, expected_revision=expected_revision, result=summary
            )

    def record_xhs_task_failure(
        self,
        task_id: str,
        *,
        expected_revision: int,
        category: str,
        refund: str | None = None,
        refund_day: str | None = None,
    ) -> dict[str, Any]:
        """Fail or back off one task, and tell the rows it was working on.

        `refund` names the provider whose reserved call was not billed, and
        `refund_day` the UTC day it was reserved on.
        """

        with self._transaction() as conn:
            task = self._xhs_fail_task(
                conn, task_id, expected_revision=expected_revision, category=category
            )
            self._xhs_apply_failure(conn, task, category)
            if refund is not None:
                self._xhs_refund_usage(conn, provider=refund, day=refund_day)
            return task

    def _xhs_apply_list_page(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Record unseen notes, the next page if the stop rule allows it, and
        the blogger's scan outcome.

        A page continues only when it is under the scan's page limit, the
        provider has more, and at least one unseen note on it is not sticky:
        a pinned note is old news on every page. A full scan drops the last
        condition.
        """

        payload = task["payload"]
        user_id = normalize_xhs_id(payload["user_id"], "user_id")
        page = int(payload["page"])
        unseen = unseen_regular = 0
        for note in result["notes"]:
            supported = note["note_type"] == "normal"
            inserted = self._xhs_insert_note(
                conn,
                note_id=note["note_id"],
                user_id=user_id,
                note_type=note["note_type"],
                title=note["title"],
                caption=note["caption"],
                caption_complete=False,
                published_at=note["published_at"],
                state="discovered" if supported else "unsupported",
            )
            if not inserted:
                continue
            unseen += 1
            if not note["sticky"]:
                unseen_regular += 1
            if supported:
                self._xhs_create_task(
                    conn,
                    kind="detail",
                    subject_key=f"detail:{note['note_id']}",
                    payload={"note_id": note["note_id"]},
                )
        next_cursor = result["next_cursor"]
        continues = bool(
            result["has_more"]
            and next_cursor
            and page < int(payload["max_pages"])
            and (unseen_regular or payload.get("full") is True)
        )
        if continues:
            self._xhs_create_task(
                conn,
                kind="list_page",
                subject_key=f"scan:{user_id}:{payload['scan']}:{page + 1}",
                payload={**payload, "page": page + 1, "cursor": next_cursor},
            )
        # Only a first page can say "nothing new": a later page exists because
        # the one before it found new notes.
        self._xhs_record_scan(
            conn,
            user_id=user_id,
            outcome="ok" if unseen or page > 1 else "no_new_notes",
            new_note_at=self._registry_now() if unseen else None,
        )
        return {
            "page": page,
            "notes": len(result["notes"]),
            "unseen": unseen,
            "next_page": continues,
        }

    def _xhs_apply_detail(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A first detail stores the whole note and queues its downloads; a
        later one only refreshes expired URLs, matching each image by fileid."""

        note_id = normalize_xhs_id(task["payload"]["note_id"], "note_id")
        detail = result["note"]
        if detail["note_id"] != note_id:
            raise ValueError("detail answers another note")
        note = self._xhs_note(conn, note_id)
        if note["state"] != "discovered":
            refreshed = missing = 0
            by_fileid = {image["fileid"]: image for image in result["images"] if image["url"]}
            for waiting in self._xhs_awaiting_refresh(conn, note_id):
                image = by_fileid.get(waiting["payload"]["fileid"])
                if image is None:
                    missing += 1
                    self._xhs_fail_image_download(
                        conn, note_id, int(waiting["payload"]["ordinal"]), "not_found"
                    )
                    continue
                refreshed += 1
                self._xhs_reset_task(
                    conn,
                    str(waiting["subject_key"]),
                    payload={**waiting["payload"], "url": image["url"], "refreshed": True},
                )
            self._xhs_advance_note(conn, note_id)
            return {"refreshed": refreshed, "missing": missing}
        fields: dict[str, Any] = {
            "title": detail["title"],
            "caption": detail["caption"],
            "caption_complete": True,
            "published_at": detail["published_at"] or note["published_at"],
        }
        if detail["note_type"] != "normal":
            self._xhs_update_note(
                conn, note_id, expected_revision=note["revision"], state="unsupported",
                note_type=detail["note_type"], **fields,
            )
            return {"images": 0, "unsupported": True}
        self._xhs_update_note(
            conn, note_id, expected_revision=note["revision"], state="detail_ok", **fields
        )
        blogger = self._xhs_blogger(conn, str(note["user_id"]))
        if (
            detail["user_name"]
            and detail["user_id"] == note["user_id"]
            and blogger["display_name"] is None
        ):
            conn.execute(
                """UPDATE xhs_bloggers
                   SET display_name = ?, revision = revision + 1, updated_at = ?
                   WHERE user_id = ?""",
                (
                    self._required_text(detail["user_name"], "display_name", maximum=200),
                    self._registry_now(),
                    note["user_id"],
                ),
            )
        for image in result["images"]:
            ordinal = image["ordinal"]
            self._xhs_upsert_image(
                conn,
                note_id=note_id,
                ordinal=ordinal,
                fileid=image["fileid"],
                upstream_width=image["width"],
                upstream_height=image["height"],
            )
            task, _ = self._xhs_create_task(
                conn,
                kind="download",
                subject_key=f"download:{note_id}:{ordinal}",
                payload={
                    "note_id": note_id,
                    "ordinal": ordinal,
                    "fileid": image["fileid"],
                    "url": image["url"] or None,
                    "refreshed": False,
                },
            )
            if not image["url"]:
                # Nothing to fetch: the image is shown failed rather than
                # silently dropped, and keeps its ordinal. Its download task
                # stays, failed, so a retry asks a fresh detail for its URL.
                conn.execute(
                    """UPDATE xhs_tasks
                       SET state = 'failed', last_error = 'invalid_response',
                           revision = revision + 1, updated_at = ?
                       WHERE id = ? AND state = 'pending'""",
                    (self._registry_now(), task["id"]),
                )
                self._xhs_fail_image_download(conn, note_id, ordinal, "invalid_response")
        self._xhs_advance_note(conn, note_id)
        return {"images": len(result["images"])}

    def _xhs_apply_download(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        note_id = normalize_xhs_id(task["payload"]["note_id"], "note_id")
        ordinal = int(task["payload"]["ordinal"])
        image = result["image"]
        current = self._xhs_image(conn, note_id, ordinal)
        fields: dict[str, Any] = {
            "download_state": "ok",
            "download_error": None,
            "sha256": image["sha256"],
            "byte_size": image["byte_size"],
            "media_type": image["media_type"],
            "width": image["width"],
            "height": image["height"],
            "asset_name": f"{ordinal}-{image['sha256'][:12]}.{image['extension']}",
        }
        if current["sha256"] != image["sha256"]:
            # Other bytes: an earlier transcription no longer describes them.
            fields.update(
                ocr_state="pending", ocr_error=None, ocr_engine=None,
                ocr_flags=(), ocr_text_sha256=None,
            )
        self._xhs_update_image(
            conn, note_id, ordinal, expected_revision=current["revision"], **fields
        )
        self._xhs_create_task(
            conn,
            kind="ocr",
            subject_key=f"ocr:{note_id}:{ordinal}:{image['sha256']}",
            payload={
                "note_id": note_id,
                "ordinal": ordinal,
                "sha256": image["sha256"],
                "name": f"{image['sha256']}.{image['extension']}",
            },
        )
        self._xhs_advance_note(conn, note_id)
        return {"ordinal": ordinal, "sha256": image["sha256"]}

    def _xhs_apply_ocr(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        note_id = normalize_xhs_id(task["payload"]["note_id"], "note_id")
        ordinal = int(task["payload"]["ordinal"])
        current = self._xhs_image(conn, note_id, ordinal)
        if current["sha256"] != task["payload"]["sha256"] or current["ocr_state"] != "pending":
            # The image was fetched again meanwhile; its own task transcribes it.
            return {"ordinal": ordinal, "stale": True}
        self._xhs_update_image(
            conn,
            note_id,
            ordinal,
            expected_revision=current["revision"],
            ocr_state="ok",
            ocr_error=None,
            ocr_engine=result["engine"],
            ocr_flags=tuple(result["flags"]),
            ocr_text_sha256=result["text_sha256"],
        )
        self._xhs_advance_note(conn, note_id)
        return {"ordinal": ordinal, "engine": result["engine"], "flags": sorted(result["flags"])}

    def _xhs_apply_failure(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], category: str
    ) -> None:
        """What a failed task means for its blogger, note or image.

        A list page failure is the scan's answer at once, even while a retry
        waits. Everything else changes only once the task is final, so a
        retry in flight is never shown as a failure. A task that names no
        subject has nothing to update.
        """

        payload = task["payload"]
        final = task["state"] == "failed"
        if task["kind"] == "list_page":
            user_id = payload.get("user_id")
            if user_id and conn.execute(
                "SELECT 1 FROM xhs_bloggers WHERE user_id = ?", (user_id,)
            ).fetchone() is not None:
                self._xhs_record_scan(conn, user_id=user_id, outcome="failed", error=category)
            return
        if task["kind"] in {"resolve", "blog_import"}:
            recommendation_id = payload.get("recommendation_id")
            if not final or not recommendation_id:
                return
            recommendation = self._xhs_recommendation(conn, str(recommendation_id))
            if task["kind"] == "resolve" and recommendation["url_state"] == "none":
                # Searched and failed: shown as such, and retryable.
                self._xhs_update_recommendation(
                    conn, recommendation["id"], expected_revision=recommendation["revision"],
                    url=None, url_state="failed",
                )
            elif task["kind"] == "blog_import" and recommendation["import_state"] == "importing":
                self._xhs_update_recommendation(
                    conn, recommendation["id"], expected_revision=recommendation["revision"],
                    import_state="failed",
                )
            return
        note_id = payload.get("note_id")
        if not final or not note_id or conn.execute(
            "SELECT 1 FROM xhs_notes WHERE note_id = ?", (note_id,)
        ).fetchone() is None:
            return
        if task["kind"] == "detail":
            note = self._xhs_note(conn, note_id)
            if note["state"] == "discovered":
                self._xhs_update_note(
                    conn, note_id, expected_revision=note["revision"],
                    state="failed", last_error=category,
                )
                return
            for waiting in self._xhs_awaiting_refresh(conn, note_id):
                self._xhs_fail_image_download(
                    conn, note_id, int(waiting["payload"]["ordinal"]), "url_expired"
                )
            self._xhs_advance_note(conn, note_id)
        elif task["kind"] == "download":
            ordinal = int(payload["ordinal"])
            if self._xhs_image(conn, note_id, ordinal)["download_state"] != "pending":
                return
            if (
                category == "url_expired"
                and not payload.get("refreshed")
                and self._xhs_request_refresh(conn, note_id)
            ):
                # The image waits, still pending, for one fresh detail.
                return
            self._xhs_fail_image_download(conn, note_id, ordinal, category)
            self._xhs_advance_note(conn, note_id)
        elif task["kind"] == "ocr":
            ordinal = int(payload["ordinal"])
            image = self._xhs_image(conn, note_id, ordinal)
            if image["sha256"] != payload.get("sha256") or image["ocr_state"] != "pending":
                return
            self._xhs_update_image(
                conn, note_id, ordinal, expected_revision=image["revision"],
                ocr_state="failed", ocr_error=category,
            )
            self._xhs_advance_note(conn, note_id)
        elif task["kind"] in {"identify", "save"}:
            # The note keeps its stage; identification never becomes an empty
            # list, and the last saved version stays readable.
            note = self._xhs_note(conn, note_id)
            self._xhs_update_note(
                conn, note_id, expected_revision=note["revision"], last_error=category
            )

    def _xhs_request_refresh(self, conn: sqlite3.Connection, note_id: str) -> bool:
        """Make the note's detail due once more, unless it cannot run again."""

        try:
            self._xhs_reset_task(conn, f"detail:{note_id}")
        except (NotFound, InvalidTransition):
            return False
        return True

    def _xhs_awaiting_refresh(
        self, conn: sqlite3.Connection, note_id: str
    ) -> list[dict[str, Any]]:
        """Download tasks that found their URL expired and have not yet had
        their one refresh, for images still pending."""

        rows = conn.execute(
            """SELECT id FROM xhs_tasks
               WHERE kind = 'download' AND subject_key GLOB ?
                 AND state = 'failed' AND last_error = 'url_expired'
               ORDER BY subject_key""",
            (f"download:{note_id}:*",),
        ).fetchall()
        waiting = []
        for row in rows:
            task = self._xhs_task(conn, str(row["id"]))
            if task["payload"].get("refreshed"):
                continue
            image = self._xhs_image(conn, note_id, int(task["payload"]["ordinal"]))
            if image["download_state"] == "pending":
                waiting.append(task)
        return waiting

    def _xhs_fail_image_download(
        self, conn: sqlite3.Connection, note_id: str, ordinal: int, category: str
    ) -> None:
        image = self._xhs_image(conn, note_id, ordinal)
        self._xhs_update_image(
            conn, note_id, ordinal, expected_revision=image["revision"],
            download_state="failed", download_error=category,
        )

    def _xhs_advance_note(self, conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
        """Move a note on once every image is final for the current stage.

        Downloads come first: `assets_done` needs every image downloaded or
        failed. Then `ocr_done` needs every downloaded image transcribed or
        failed; an image whose download failed has nothing to transcribe.
        """

        note = self._xhs_note(conn, note_id)
        images = self._xhs_images(conn, note_id)
        if note["state"] == "detail_ok" and all(
            image["download_state"] != "pending" for image in images
        ):
            note = self._xhs_update_note(
                conn, note_id, expected_revision=note["revision"], state="assets_done"
            )
        if note["state"] == "assets_done" and all(
            image["download_state"] == "failed" or image["ocr_state"] != "pending"
            for image in images
        ):
            note = self._xhs_update_note(
                conn, note_id, expected_revision=note["revision"], state="ocr_done"
            )
            note = self._xhs_queue_identify(conn, note, images)
        return note

    # -- identification, saves and imports ------------------------------------

    @staticmethod
    def _xhs_identify_input(
        note: Mapping[str, Any], images: Collection[Mapping[str, Any]]
    ) -> str:
        return xhs_identify_digest(
            str(note["caption"]),
            [
                (int(image["ordinal"]), str(image["ocr_text_sha256"]))
                for image in images
                if image["download_state"] == "ok" and image["ocr_state"] == "ok"
            ],
        )

    def _xhs_queue_identify(
        self,
        conn: sqlite3.Connection,
        note: Mapping[str, Any],
        images: Collection[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Queue identification for a note whose transcriptions are final.

        The task is keyed by its input. When that exact input was identified
        already, as after a retried image that failed again, the recorded
        answer stands and the note goes on to its save.
        """

        note_id = str(note["note_id"])
        digest = self._xhs_identify_input(note, images)
        task, fresh = self._xhs_create_task(
            conn,
            kind="identify",
            subject_key=f"identify:{note_id}:{digest}",
            payload={"note_id": note_id, "input_sha256": digest},
        )
        if fresh or task["state"] in {"pending", "running"}:
            return dict(note)
        result = task["result"] or {}
        if task["state"] == "done" and not result.get("stale") and not result.get("skipped"):
            note = self._xhs_update_note(
                conn, note_id, expected_revision=note["revision"], state="identified"
            )
            self._xhs_queue_save(conn, note)
            return note
        self._xhs_reset_task(conn, str(task["subject_key"]))
        return dict(note)

    def _xhs_apply_identify(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Upsert the checked recommendations, then queue link searches and the save.

        Each item is keyed by its note and item key, so identifying the same
        note again updates rows rather than adding them. An answer for input
        the note no longer has changes nothing.
        """

        note_id = normalize_xhs_id(task["payload"]["note_id"], "note_id")
        note = self._xhs_note(conn, note_id)
        if (
            result.get("stale")
            or note["state"] != "ocr_done"
            or task["payload"]["input_sha256"]
            != self._xhs_identify_input(note, self._xhs_images(conn, note_id))
        ):
            return {"stale": True}
        recommendation_ids: list[str] = []
        for item in result["items"]:
            recommendation = self._xhs_upsert_recommendation(
                conn,
                note_id=note_id,
                item_key=item["item_key"],
                image_ordinal=item["image"],
                kind=item["kind"],
                title=item["title"],
                quote=item["quote"],
                arxiv_id=item["arxiv_id"],
                url=item["url"],
                url_state=item["url_state"],
                origin=item["origin"],
                identify_run=str(task["id"]),
            )
            recommendation_ids.append(str(recommendation["id"]))
            if recommendation["kind"] == "blog" and recommendation["url_state"] == "none":
                self._xhs_create_task(
                    conn,
                    kind="resolve",
                    subject_key=f"resolve:{recommendation['id']}:1",
                    payload={"recommendation_id": recommendation["id"]},
                )
        note = self._xhs_update_note(
            conn, note_id, expected_revision=note["revision"], state="identified",
            last_error=None,
        )
        self._xhs_queue_save(conn, note)
        return {
            "prompt_version": result["prompt_version"],
            "input_sha256": result["input_sha256"],
            "response_id": result["response_id"],
            "model": result["model"],
            "model_items": result["model_items"],
            "dropped": result["dropped"],
            "rule_items": result["rule_items"],
            "recommendations": recommendation_ids,
        }

    def _xhs_apply_resolve(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Set a blog recommendation's link from the search and its check.

        A link that arrived meanwhile, from the operator or the text, wins.
        """

        recommendation = self._xhs_recommendation(
            conn, str(task["payload"]["recommendation_id"])
        )
        if (
            recommendation["kind"] != "blog"
            or recommendation["url_state"] not in _RESOLVABLE_URL_STATES
        ):
            return {"stale": True}
        self._xhs_update_recommendation(
            conn,
            str(recommendation["id"]),
            expected_revision=recommendation["revision"],
            url=result["url"],
            url_state=result["url_state"],
            url_checked_title=result["checked_title"],
        )
        # The saved note shows a found link too; a save not yet run includes it.
        note = self._xhs_note(conn, str(recommendation["note_id"]))
        if result["url"] is not None and note["state"] in {"identified", "saved"}:
            self._xhs_queue_save(conn, note)
        return {
            "url_state": result["url_state"],
            "prompt_version": result["prompt_version"],
            "response_id": result["response_id"],
            "verification_failure": result["verification_failure"],
        }

    def _xhs_apply_save(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Register the note's source on its first save and bind the version
        just written, in one transaction. The note becomes `saved`."""

        note_id = normalize_xhs_id(task["payload"]["note_id"], "note_id")
        note = self._xhs_note(conn, note_id)
        version = result["version"]
        if (
            note["state"] not in {"identified", "saved"}
            or int(note["content_version"]) + 1 != version
        ):
            return {"version": version, "stale": True}
        source_id = self._register_content_source(
            conn, source_kind="xhs_note", authority_id=note_id, official_title=result["title"]
        )
        self._append_content_binding(
            conn,
            source_id=source_id,
            version=version,
            tree_sha256=result["tree_sha256"],
            metadata=result["metadata"],
        )
        cursor = conn.execute(
            """UPDATE xhs_notes
               SET state = 'saved', source_id = ?, content_version = ?, last_error = NULL,
                   revision = revision + 1, updated_at = ?
               WHERE note_id = ? AND revision = ?""",
            (source_id, version, self._registry_now(), note_id, note["revision"]),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict(self._xhs_note(conn, note_id))
        # A change made while this version was written, such as a link edit
        # that found this save already running, goes into the next one.
        if result.get("inputs_sha256") != self._xhs_save_inputs_sha256(conn, note_id):
            self._xhs_queue_save(conn, self._xhs_note(conn, note_id))
        return {"version": version, "source_id": source_id, "tree_sha256": result["tree_sha256"]}

    def _xhs_apply_blog_import(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Register or reuse the blog by its URL identity, bind the version
        written, and link it to the recommending note, all together."""

        recommendation = self._xhs_recommendation(
            conn, str(task["payload"]["recommendation_id"])
        )
        if recommendation["import_state"] != "importing":
            return {"stale": True}
        source_id = self._register_content_source(
            conn,
            source_kind="blog",
            authority_id=result["authority_id"],
            official_title=result["title"],
        )
        self._append_content_binding(
            conn,
            source_id=source_id,
            version=result["version"],
            tree_sha256=result["tree_sha256"],
            metadata=result["metadata"],
        )
        self._xhs_link_recommendation(conn, recommendation, source_id)
        return {"source_id": source_id, "version": result["version"]}

    def _xhs_apply_capture_link(
        self, conn: sqlite3.Connection, task: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Link each paper recommendation staged as this Capture to the source
        it produced, or mark its import failed when the Capture did not."""

        capture = self._capture(conn, str(task["payload"]["capture_id"]))
        if capture["state"] not in _CAPTURE_TERMINAL:
            raise ValueError("capture is still open")
        consumed = [str(value) for value in capture["consumed_source_ids"] or []]
        rows = conn.execute(
            """SELECT id FROM xhs_recommendations
               WHERE capture_id = ? AND import_state IN ('staged', 'importing')
               ORDER BY created_at, id""",
            (capture["id"],),
        ).fetchall()
        linked = failed = 0
        for row in rows:
            recommendation = self._xhs_recommendation(conn, str(row["id"]))
            source_id = (
                self._xhs_capture_source(conn, recommendation, consumed)
                if capture["state"] == "consumed"
                else None
            )
            if source_id is None:
                self._xhs_update_recommendation(
                    conn, str(recommendation["id"]),
                    expected_revision=recommendation["revision"], import_state="failed",
                )
                failed += 1
                continue
            self._xhs_link_recommendation(conn, recommendation, source_id)
            linked += 1
        return {"capture_state": capture["state"], "linked": linked, "failed": failed}

    def _xhs_capture_source(
        self,
        conn: sqlite3.Connection,
        recommendation: Mapping[str, Any],
        consumed: list[str],
    ) -> str | None:
        """The paper a consumed Capture produced for this recommendation.

        A single source is the answer; among several, only the one with the
        recommended arXiv ID is.
        """

        if len(consumed) == 1:
            return consumed[0]
        for source_id in consumed:
            row = conn.execute(
                "SELECT authority_id FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is not None and recommendation["arxiv_id"] and (
                row["authority_id"] == recommendation["arxiv_id"]
            ):
                return source_id
        return None

    def _xhs_link_recommendation(
        self, conn: sqlite3.Connection, recommendation: Mapping[str, Any], source_id: str
    ) -> dict[str, Any]:
        note = self._xhs_note(conn, str(recommendation["note_id"]))
        if note["source_id"] is None:
            raise InvalidTransition("xhs_note_not_saved", "imported")
        self._insert_source_link(
            conn,
            from_source_id=str(note["source_id"]),
            to_source_id=source_id,
            recommendation_id=str(recommendation["id"]),
        )
        return self._xhs_update_recommendation(
            conn,
            str(recommendation["id"]),
            expected_revision=recommendation["revision"],
            import_state="imported",
            imported_source_id=source_id,
        )

    def _xhs_queue_save(self, conn: sqlite3.Connection, note: Mapping[str, Any]) -> dict[str, Any]:
        """Queue the note's next version: its first save, or a later change.

        Changes made before that save runs share it; a finished task for the
        same version is made due again.
        """

        note_id = str(note["note_id"])
        version = int(note["content_version"]) + 1
        task, fresh = self._xhs_create_task(
            conn,
            kind="save",
            subject_key=f"save:{note_id}:{version}",
            payload={"note_id": note_id, "version": version},
        )
        if not fresh and task["state"] in {"done", "failed", "canceled"}:
            task = self._xhs_reset_task(conn, str(task["subject_key"]))
        return task

    def _xhs_queue_blog_import(
        self, conn: sqlite3.Connection, recommendation: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Queue the import of one blog recommendation that has a link.

        For the import command, inside its transaction. An import already
        queued or running is returned; after a failed one, a new task follows.
        """

        recommendation_id = str(recommendation["id"])
        if recommendation["kind"] != "blog" or recommendation["url"] is None:
            raise InvalidTransition("xhs_recommendation_not_importable", "importing")
        if recommendation["import_state"] == "imported":
            raise InvalidTransition("imported", "importing")
        rows = conn.execute(
            """SELECT id, state FROM xhs_tasks
               WHERE kind = 'blog_import' AND subject_key GLOB ?""",
            (f"blog:{recommendation_id}:*",),
        ).fetchall()
        waiting = [row for row in rows if row["state"] in {"pending", "running"}]
        if waiting:
            task = self._xhs_task(conn, str(waiting[0]["id"]))
        else:
            task, _ = self._xhs_create_task(
                conn,
                kind="blog_import",
                subject_key=f"blog:{recommendation_id}:{len(rows) + 1}",
                payload={"recommendation_id": recommendation_id},
            )
        if recommendation["import_state"] != "importing":
            self._xhs_update_recommendation(
                conn, recommendation_id, expected_revision=recommendation["revision"],
                import_state="importing",
            )
        return task

    def _xhs_queue_capture_link(
        self, conn: sqlite3.Connection, recommendation: Mapping[str, Any], capture_id: str
    ) -> dict[str, Any]:
        """Record the Capture staged for a paper recommendation and queue its link.

        For the import command, inside its transaction. One Capture reused by
        several recommendations has one task, which links them all.
        """

        recommendation_id = str(recommendation["id"])
        if recommendation["kind"] != "paper":
            raise InvalidTransition("xhs_recommendation_not_importable", "staged")
        if recommendation["import_state"] == "imported":
            raise InvalidTransition("imported", "staged")
        capture = self._capture(conn, capture_id)
        self._xhs_update_recommendation(
            conn, recommendation_id, expected_revision=recommendation["revision"],
            capture_id=capture["id"], import_state="staged",
        )
        task, fresh = self._xhs_create_task(
            conn,
            kind="capture_link",
            subject_key=f"capture:{capture['id']}",
            payload={"capture_id": capture["id"]},
        )
        if not fresh and task["state"] in {"done", "failed", "canceled"}:
            task = self._xhs_reset_task(conn, str(task["subject_key"]))
        return task

    def content_source_id(self, source_kind: str, authority_id: str) -> str | None:
        """The registered source for one blog or note identity, if any."""

        kind = CONTENT_SOURCE_KINDS.get(source_kind)
        if kind is None:
            raise ValueError("source kind has no content bindings")
        _, _, canonical_id = self._canonical_source_identity(kind.authority, authority_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM sources WHERE canonical_id = ? AND source_kind = ?",
                (canonical_id, source_kind),
            ).fetchone()
            return None if row is None else str(row["id"])

    # -- operator import and links -----------------------------------------------

    def import_xhs_recommendations(
        self,
        *,
        note_source_id: str,
        recommendation_ids: Sequence[str],
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ):
        """Import recommendations of one saved note, each with its own disposition.

        A paper is staged as a pending Capture of its arXiv abstract page, and
        an open Capture of the same payload is reused; approval stays the
        Capture's own command. A blog with a link gets a `blog_import` task.
        Anything else is refused item by item while the rest proceed.
        `expected_revision` is the note's.
        """

        if (
            isinstance(recommendation_ids, (str, bytes))
            or not isinstance(recommendation_ids, Sequence)
            or not 1 <= len(recommendation_ids) <= XHS_IMPORT_BATCH_MAX
        ):
            raise ValueError("recommendation_ids is invalid")
        ids = [
            self._required_text(value, "recommendation_id", maximum=200)
            for value in recommendation_ids
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("recommendation_ids must be unique")
        request = {"recommendation_ids": ids, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/sources/{note_source_id}/recommendations/import"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            note = self._xhs_note_by_source(conn, note_source_id)
            self._expect_revision(note, expected_revision)
            items = [self._xhs_import_one(conn, note, value) for value in ids]
            dispositions: dict[str, int] = {}
            for item in items:
                dispositions[item["disposition"]] = dispositions.get(item["disposition"], 0) + 1
            self._audit(
                conn, "xhs_note", str(note["note_id"]), "xhs.recommendations.imported",
                {"dispositions": dict(sorted(dispositions.items()))},
            )
            value = {"note_source_id": note_source_id, "items": items}
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def _xhs_import_one(
        self, conn: sqlite3.Connection, note: Mapping[str, Any], recommendation_id: str
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "recommendation_id": recommendation_id,
            "disposition": "refused",
            "reason": None,
            "capture_id": None,
            "capture_revision": None,
            "capture_state": None,
            "recommendation": None,
        }
        row = conn.execute(
            "SELECT id FROM xhs_recommendations WHERE id = ? AND note_id = ?",
            (recommendation_id, note["note_id"]),
        ).fetchone()
        if row is None:
            return {**item, "reason": "not_found"}
        recommendation = self._xhs_recommendation(conn, recommendation_id)
        reason = self._xhs_import_refusal(recommendation)
        if reason is not None:
            return {
                **item,
                "reason": reason,
                "recommendation": self._xhs_recommendation_view(conn, recommendation_id),
            }
        if recommendation["kind"] == "blog":
            self._xhs_queue_blog_import(conn, recommendation)
            item["disposition"] = "blog_import_queued"
        else:
            capture, reused = self._stage_capture(
                conn,
                payload=f"https://arxiv.org/abs/{recommendation['arxiv_id']}",
                note=self._xhs_capture_note(note, recommendation),
            )
            self._xhs_queue_capture_link(conn, recommendation, str(capture["id"]))
            item.update(
                disposition="capture_reused" if reused else "capture_staged",
                capture_id=capture["id"],
                capture_revision=capture["revision"],
                capture_state=capture["state"],
            )
        item["recommendation"] = self._xhs_recommendation_view(conn, recommendation_id)
        return item

    @staticmethod
    def _xhs_import_refusal(recommendation: Mapping[str, Any]) -> str | None:
        """Why one recommendation cannot be imported, or None when it can."""

        if recommendation["import_state"] == "imported":
            return "already_imported"
        if recommendation["kind"] == "paper":
            # Only an arXiv ID makes a Capture payload; a title alone does not.
            return None if recommendation["arxiv_id"] else "no_arxiv_id"
        if recommendation["kind"] == "blog":
            return None if recommendation["url"] else "no_url"
        return "not_importable"

    @staticmethod
    def _xhs_capture_note(note: Mapping[str, Any], recommendation: Mapping[str, Any]) -> str:
        title = " ".join(str(note["title"] or "").split()) or f"XHS note {note['note_id']}"
        ordinal = recommendation["image_ordinal"]
        where = "caption" if ordinal is None else f"image {ordinal}"
        return f"Recommended in XHS note {title[:500]} · {where}"

    def set_xhs_recommendation_link(
        self,
        *,
        note_source_id: str,
        recommendation_id: str,
        url: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ):
        """Replace a blog recommendation's link with the operator's.

        The link becomes `operator_set`, which no later search or
        identification overwrites, and an identified or saved note queues its
        next version with it. A link is not changed while its blog imports or
        after it was imported. `expected_revision` is the recommendation's.
        """

        if not isinstance(url, str):
            raise ValueError("url is invalid")
        url = normalize_url(url)
        if urlsplit(url).port is not None:
            # Normalization keeps only a port the blog fetch would refuse.
            raise ValueError("url must use the default port")
        request = {"url": url, "expected_revision": expected_revision}
        operation = (
            f"POST:/api/v1/sources/{note_source_id}/recommendations/{recommendation_id}/link"
        )
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            note = self._xhs_note_by_source(conn, note_source_id)
            row = conn.execute(
                "SELECT id FROM xhs_recommendations WHERE id = ? AND note_id = ?",
                (recommendation_id, note["note_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("xhs recommendation", recommendation_id)
            recommendation = self._xhs_recommendation(conn, recommendation_id)
            self._expect_revision(recommendation, expected_revision)
            if recommendation["kind"] != "blog":
                raise InvalidTransition("xhs_recommendation_not_linkable", "operator_set")
            if recommendation["import_state"] in {"importing", "imported"}:
                raise InvalidTransition(str(recommendation["import_state"]), "operator_set")
            self._xhs_update_recommendation(
                conn, recommendation_id, expected_revision=expected_revision,
                url=url, url_state="operator_set", url_checked_title=None,
            )
            recommendation = self._xhs_recommendation_view(conn, recommendation_id)
            note = self._xhs_note(conn, str(note["note_id"]))
            if note["state"] in {"identified", "saved"}:
                # A note still on its way to `identified` saves the link anyway.
                self._xhs_queue_save(conn, note)
            self._audit(
                conn, "xhs_recommendation", recommendation_id, "xhs.recommendation.link_set",
                {"note_id": note["note_id"]},
            )
            value = {"recommendation": recommendation}
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    # -- retries ----------------------------------------------------------------

    def retry_xhs_image(
        self,
        *,
        note_id: str,
        ordinal: int,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ):
        """Reset one failed image, then its OCR; identification and the save
        follow, and the save writes a new version.

        `expected_revision` is the note's.
        """

        note_id = normalize_xhs_id(note_id, "note_id")
        _optional_int(ordinal, "ordinal", minimum=1, maximum=100)
        request = {"note_id": note_id, "ordinal": ordinal, "expected_revision": expected_revision}
        operation = f"INTERNAL:xhs/notes/{note_id}/images/{ordinal}/retry"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            self._expect_revision(self._xhs_note(conn, note_id), expected_revision)
            self._xhs_retry_image(conn, note_id, int(ordinal))
            self._audit(conn, "xhs_note", note_id, "xhs.image.retried", {"ordinal": ordinal})
            value = {
                "note": self._xhs_note(conn, note_id),
                "image": self._xhs_image(conn, note_id, int(ordinal)),
            }
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def _xhs_retry_image(self, conn: sqlite3.Connection, note_id: str, ordinal: int) -> None:
        note = self._xhs_note(conn, note_id)
        image = self._xhs_image(conn, note_id, ordinal)
        if note["state"] not in _NOTE_ORDER:
            raise InvalidTransition(str(note["state"]), "retry")
        cleared: dict[str, Any] = {
            "ocr_state": "pending",
            "ocr_error": None,
            "ocr_engine": None,
            "ocr_flags": (),
            "ocr_text_sha256": None,
        }
        if image["download_state"] == "failed":
            row = conn.execute(
                "SELECT id FROM xhs_tasks WHERE subject_key = ?",
                (f"download:{note_id}:{ordinal}",),
            ).fetchone()
            if row is None:
                raise InvalidTransition("xhs_image_not_retryable", "pending")
            task = self._xhs_task(conn, str(row["id"]))
            self._xhs_update_image(
                conn, note_id, ordinal, expected_revision=image["revision"],
                download_state="pending", download_error=None, **cleared,
            )
            # The signed URL has likely expired; its first expiry earns one
            # fresh detail, matched by fileid as before.
            self._xhs_reset_task(
                conn, str(task["subject_key"]), payload={**task["payload"], "refreshed": False}
            )
            stage = "detail_ok"
        elif image["ocr_state"] == "failed":
            self._xhs_update_image(
                conn, note_id, ordinal, expected_revision=image["revision"], **cleared
            )
            self._xhs_reset_task(conn, f"ocr:{note_id}:{ordinal}:{image['sha256']}")
            stage = "assets_done"
        else:
            raise InvalidTransition("xhs_image_not_failed", "pending")
        if _NOTE_ORDER.index(str(note["state"])) > _NOTE_ORDER.index(stage):
            self._xhs_update_note(
                conn, note_id, expected_revision=note["revision"], state=stage, last_error=None
            )

    def retry_failed_xhs_tasks(
        self,
        *,
        kinds: Collection[str] | None = None,
        actor_id: str,
        idempotency_key: str,
    ):
        """Make every failed task due again, with the rows it was working on.

        A failed image goes through the image retry; a note whose first detail
        failed goes back to `discovered`; a failed link search or blog import
        becomes searchable or importable again. A failed task that no longer
        matches its rows (a stale OCR, a refresh already retried through its
        images) is skipped.
        """

        selected = sorted(XHS_TASK_KINDS if kinds is None else set(kinds))
        if not selected or not set(selected) <= set(XHS_TASK_KINDS):
            raise ValueError("xhs task kinds are invalid")
        request = {"kinds": selected}
        operation = "INTERNAL:xhs/tasks/retry-failed"
        placeholders = ", ".join("?" for _ in selected)
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            rows = conn.execute(
                f"""SELECT id FROM xhs_tasks WHERE state = 'failed' AND kind IN ({placeholders})
                    ORDER BY created_at, id""",
                selected,
            ).fetchall()
            retried = {kind: 0 for kind in selected}
            skipped = 0
            for row in rows:
                task = self._xhs_task(conn, str(row["id"]))
                if task["state"] == "failed" and self._xhs_retry_task(conn, task):
                    retried[str(task["kind"])] += 1
                else:
                    skipped += 1
            value = {"retried": retried, "skipped": skipped}
            self._audit(conn, "xhs_tasks", "failed", "xhs.tasks.retried", value)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def _xhs_retry_task(self, conn: sqlite3.Connection, task: Mapping[str, Any]) -> bool:
        kind, payload = str(task["kind"]), task["payload"]
        if kind in {"download", "ocr"}:
            note_id, ordinal = str(payload["note_id"]), int(payload["ordinal"])
            image = self._xhs_image(conn, note_id, ordinal)
            if kind == "download" and image["download_state"] != "failed":
                return False
            if kind == "ocr" and (
                image["download_state"] != "ok"
                or image["ocr_state"] != "failed"
                or image["sha256"] != payload["sha256"]
            ):
                return False
            try:
                self._xhs_retry_image(conn, note_id, ordinal)
            except InvalidTransition:
                return False
            return True
        if kind in {"detail", "identify", "save"}:
            note = self._xhs_note(conn, str(payload["note_id"]))
            wanted = {
                "detail": {"failed"},
                "identify": {"ocr_done"},
                "save": {"identified", "saved"},
            }
            if note["state"] not in wanted[kind]:
                return False
            if kind == "save" and int(note["content_version"]) >= int(payload["version"]):
                return False
            fields: dict[str, Any] = {"last_error": None}
            if kind == "detail":
                fields["state"] = "discovered"
            self._xhs_update_note(
                conn, str(note["note_id"]), expected_revision=note["revision"], **fields
            )
        elif kind in {"resolve", "blog_import"}:
            recommendation = self._xhs_recommendation(conn, str(payload["recommendation_id"]))
            if kind == "resolve":
                if recommendation["url_state"] != "failed":
                    return False
                self._xhs_update_recommendation(
                    conn, str(recommendation["id"]),
                    expected_revision=recommendation["revision"], url=None, url_state="none",
                )
            else:
                if recommendation["import_state"] != "failed" or recommendation["url"] is None:
                    return False
                self._xhs_update_recommendation(
                    conn, str(recommendation["id"]),
                    expected_revision=recommendation["revision"], import_state="importing",
                )
        self._xhs_reset_task(conn, str(task["subject_key"]))
        return True

    def xhs_last_failures(self) -> dict[str, str | None]:
        """The latest recorded failure category per provider, for Status.

        A task that later succeeds clears its own, so this is the latest
        failure still standing.
        """

        with self._connect() as conn:
            failures: dict[str, str | None] = {}
            for provider, kinds in _XHS_PROVIDER_KINDS.items():
                placeholders = ", ".join("?" for _ in kinds)
                row = conn.execute(
                    f"""SELECT last_error FROM xhs_tasks
                        WHERE kind IN ({placeholders}) AND last_error IS NOT NULL
                        ORDER BY updated_at DESC, id DESC LIMIT 1""",
                    kinds,
                ).fetchone()
                failures[provider] = None if row is None else str(row["last_error"])
            return failures

    # -- usage ----------------------------------------------------------------

    def reserve_xhs_usage(self, provider: str, *, cap: int, calls: int = 1) -> bool:
        return self.reserve_xhs_usage_day(provider, cap=cap, calls=calls) is not None

    def reserve_xhs_usage_day(
        self, provider: str, *, cap: int, calls: int = 1
    ) -> str | None:
        """Reserve as `reserve_xhs_usage` does; answer the UTC day the calls
        count on, which a refund names, or None when refused."""

        with self._transaction() as conn:
            return self._xhs_reserve_usage(conn, provider=provider, cap=cap, calls=calls)

    def _xhs_reserve_usage(
        self, conn: sqlite3.Connection, *, provider: str, cap: int, calls: int = 1
    ) -> str | None:
        """Count `calls` against today's cap and answer the day, or refuse
        without counting."""

        if provider not in XHS_USAGE_PROVIDERS:
            raise ValueError("usage provider is unsupported")
        if type(cap) is not int or cap < 0 or type(calls) is not int or calls < 1:
            raise ValueError("usage cap or calls are invalid")
        day = self._utc_now().date().isoformat()
        row = conn.execute(
            "SELECT calls FROM xhs_usage WHERE day = ? AND provider = ?", (day, provider)
        ).fetchone()
        used = int(row["calls"]) if row is not None else 0
        if used + calls > cap:
            return None
        conn.execute(
            """INSERT INTO xhs_usage (day, provider, calls) VALUES (?, ?, ?)
               ON CONFLICT(day, provider) DO UPDATE SET calls = calls + excluded.calls""",
            (day, provider, calls),
        )
        return day

    def refund_xhs_usage(
        self, provider: str, *, calls: int = 1, day: str | None = None
    ) -> None:
        with self._transaction() as conn:
            self._xhs_refund_usage(conn, provider=provider, calls=calls, day=day)

    def _xhs_refund_usage(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        calls: int = 1,
        day: str | None = None,
    ) -> None:
        """Return reserved calls the provider did not bill, never below zero.

        `day` is the reservation's: a call reserved before UTC midnight and
        refused after it is returned to the day it counted on. Without it,
        today's count is used.
        """

        if provider not in XHS_USAGE_PROVIDERS:
            raise ValueError("usage provider is unsupported")
        if day is None:
            day = self._utc_now().date().isoformat()
        elif not isinstance(day, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day) is None:
            raise ValueError("usage day is invalid")
        conn.execute(
            """UPDATE xhs_usage SET calls = MAX(0, calls - ?)
               WHERE day = ? AND provider = ?""",
            (calls, day, provider),
        )

    def xhs_exhausted_providers(self, caps: Mapping[str, int]) -> frozenset[str]:
        """Providers whose calls today have reached their cap.

        Their tasks are not claimed until the UTC day changes.
        """

        usage = self.xhs_usage()
        return frozenset(
            provider
            for provider, used in usage.items()
            if used >= int(caps.get(provider, 0))
        )

    def xhs_usage(self, day: str | None = None) -> dict[str, int]:
        """Calls counted per provider on one UTC day, today by default."""

        with self._connect() as conn:
            day = day or self._utc_now().date().isoformat()
            usage = {provider: 0 for provider in sorted(XHS_USAGE_PROVIDERS)}
            for row in conn.execute(
                "SELECT provider, calls FROM xhs_usage WHERE day = ?", (day,)
            ):
                usage[str(row["provider"])] = int(row["calls"])
            return usage

    # -- content sources and bindings -----------------------------------------

    def xhs_roots_status(self) -> dict[str, str]:
        """`ready`, `disabled`, `missing` or `overlaps_corpus` for each plugin root.

        Both roots stay outside the paper corpus, so the paper indexer can
        never scan a note or a blog; a root nested either way, after `..` and
        links are resolved, is not ready.
        """

        with self._connect() as conn:
            corpus = conn.execute(
                "SELECT private_path FROM asset_roots WHERE root_id = 'research-corpus'"
            ).fetchone()
            status: dict[str, str] = {}
            for root_id in XHS_ROOT_IDS:
                row = conn.execute(
                    "SELECT private_path, enabled FROM asset_roots WHERE root_id = ?",
                    (root_id,),
                ).fetchone()
                if row is None:
                    status[root_id] = "missing"
                elif corpus is not None and _paths_overlap(
                    row["private_path"], corpus["private_path"]
                ):
                    status[root_id] = "overlaps_corpus"
                else:
                    status[root_id] = "ready" if row["enabled"] else "disabled"
            return status

    def next_content_version(self, source_kind: str, authority_id: str) -> int:
        """The version a writer stages next for this identity: 1 for a new one."""

        with self._connect() as conn:
            return self._next_content_version(conn, source_kind, authority_id)

    def _next_content_version(
        self, conn: sqlite3.Connection, source_kind: str, authority_id: str
    ) -> int:
        kind = CONTENT_SOURCE_KINDS.get(source_kind)
        if kind is None:
            raise ValueError("source kind has no content bindings")
        _, _, canonical_id = self._canonical_source_identity(kind.authority, authority_id)
        row = conn.execute(
            """SELECT MAX(b.version) AS version FROM sources s
               JOIN source_content_bindings b ON b.source_id = s.id
               WHERE s.canonical_id = ?""",
            (canonical_id,),
        ).fetchone()
        return 1 + int(row["version"] or 0)

    def bind_content_source_version(
        self,
        *,
        source_kind: str,
        authority_id: str,
        official_title: str,
        version: int,
        tree_sha256: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Register or reuse the source and record one written version, atomically."""

        with self._transaction() as conn:
            source_id = self._register_content_source(
                conn,
                source_kind=source_kind,
                authority_id=authority_id,
                official_title=official_title,
            )
            binding = self._append_content_binding(
                conn,
                source_id=source_id,
                version=version,
                tree_sha256=tree_sha256,
                metadata=metadata,
            )
            return {"source": self._source(conn, source_id), "binding": binding}

    def _register_content_source(
        self,
        conn: sqlite3.Connection,
        *,
        source_kind: str,
        authority_id: str,
        official_title: str,
    ) -> str:
        """The source for one blog or note identity, created on first save.

        A later save may retitle it; its identity and kind never change.
        """

        kind = CONTENT_SOURCE_KINDS.get(source_kind)
        if kind is None:
            raise ValueError("source kind has no content bindings")
        authority, authority_id, canonical_id = self._canonical_source_identity(
            kind.authority, authority_id
        )
        official_title = normalize_source_text(
            official_title, "official_title", maximum=2_000
        )
        row = conn.execute(
            "SELECT id, source_kind, official_title FROM sources WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        now = self._now()
        if row is not None:
            if row["source_kind"] != source_kind:
                raise InvalidTransition("canonical_source_collision", "registered")
            if row["official_title"] != official_title:
                conn.execute(
                    """UPDATE sources SET official_title = ?, revision = revision + 1,
                       updated_at = ? WHERE id = ?""",
                    (official_title, now, row["id"]),
                )
            return str(row["id"])
        source_id = self._insert_source(
            conn,
            authority=authority,
            authority_id=authority_id,
            source_kind=source_kind,
            official_title=official_title,
            engine_ref=kind.engine_ref(authority_id),
            import_state="existing",
            aliases=(),
            now=now,
        )
        self._audit(
            conn,
            "source",
            source_id,
            "source.registered",
            {"canonical_id": canonical_id, "import_state": "existing"},
        )
        return source_id

    def _append_content_binding(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        version: int,
        tree_sha256: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record version N of a source's retained tree, written before this call.

        Replaying the same version and tree returns the recorded binding, so a
        crash between the rename and this transaction is recoverable; any other
        version than the next one is refused.
        """

        source = self._source(conn, source_id)
        kind = CONTENT_SOURCE_KINDS.get(str(source["source_kind"]))
        if kind is None:
            raise ValueError("source kind has no content bindings")
        if type(version) is not int or version < 1:
            raise ValueError("content version is invalid")
        if not isinstance(tree_sha256, str) or _SHA256_RE.fullmatch(tree_sha256) is None:
            raise ValueError("tree_sha256 is invalid")
        if not isinstance(metadata, Mapping):
            raise ValueError("content metadata is invalid")
        metadata_json = _json_text(
            dict(metadata), "content metadata", maximum=_METADATA_MAX_BYTES
        )
        existing = conn.execute(
            """SELECT tree_sha256 FROM source_content_bindings
               WHERE source_id = ? AND version = ?""",
            (source_id, version),
        ).fetchone()
        if existing is not None:
            if existing["tree_sha256"] != tree_sha256:
                raise InvalidTransition("content_version_conflict", "bound")
            return self._content_binding(conn, source_id, version)
        latest = conn.execute(
            "SELECT MAX(version) AS version FROM source_content_bindings WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if version != 1 + int(latest["version"] or 0):
            raise InvalidTransition("content_version_out_of_order", "bound")
        root = conn.execute(
            "SELECT enabled FROM asset_roots WHERE root_id = ?", (kind.root_id,)
        ).fetchone()
        if root is None or not root["enabled"]:
            raise InvalidTransition("asset_root_unavailable", "bound")
        conn.execute(
            """INSERT INTO source_content_bindings
               (source_id, version, root_id, directory, tree_sha256,
                metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                version,
                kind.root_id,
                kind.directory(str(source["authority_id"]), version),
                tree_sha256,
                metadata_json,
                self._registry_now(),
            ),
        )
        return self._content_binding(conn, source_id, version)

    def latest_content_binding(self, source_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM source_content_bindings WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row["version"] is None:
                return None
            return self._content_binding(conn, source_id, int(row["version"]))

    def _content_binding(
        self, conn: sqlite3.Connection, source_id: str, version: int
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM source_content_bindings WHERE source_id = ? AND version = ?",
            (source_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("source content binding", f"{source_id}:{version}")
        value = self._row(row)
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    # -- source links ---------------------------------------------------------

    def _insert_source_link(
        self,
        conn: sqlite3.Connection,
        *,
        from_source_id: str,
        to_source_id: str,
        recommendation_id: str,
    ) -> dict[str, Any]:
        """Link a note to the source one of its recommendations imported.

        Idempotent per recommendation; the schema refuses a self-link and a
        from end that is not the saved note holding the recommendation.
        """

        existing = conn.execute(
            "SELECT * FROM source_links WHERE recommendation_id = ?", (recommendation_id,)
        ).fetchone()
        if existing is not None:
            if (existing["from_source_id"], existing["to_source_id"]) != (
                from_source_id,
                to_source_id,
            ):
                raise InvalidTransition("source_link_collision", "linked")
            return self._row(existing)
        if from_source_id == to_source_id:
            raise ValueError("a source cannot recommend itself")
        link_id = self._id_factory("source_link")
        conn.execute(
            """INSERT INTO source_links
               (id, from_source_id, to_source_id, relation, recommendation_id, created_at)
               VALUES (?, ?, ?, 'recommends', ?, ?)""",
            (link_id, from_source_id, to_source_id, recommendation_id, self._registry_now()),
        )
        return self._row(
            conn.execute("SELECT * FROM source_links WHERE id = ?", (link_id,)).fetchone()
        )

    def list_source_links(self, source_id: str) -> dict[str, list[dict[str, Any]]]:
        """Notes that recommend this source, and what this source recommends."""

        with self._connect() as conn:
            self._source(conn, source_id)
            columns = """l.id AS link_id, l.recommendation_id, r.image_ordinal,
                         l.created_at, s.official_title, s.source_kind"""
            recommended_in = conn.execute(
                f"""SELECT {columns}, l.from_source_id AS source_id
                    FROM source_links l
                    JOIN sources s ON s.id = l.from_source_id
                    JOIN xhs_recommendations r ON r.id = l.recommendation_id
                    WHERE l.to_source_id = ? ORDER BY l.created_at, l.id""",
                (source_id,),
            ).fetchall()
            recommends = conn.execute(
                f"""SELECT {columns}, l.to_source_id AS source_id
                    FROM source_links l
                    JOIN sources s ON s.id = l.to_source_id
                    JOIN xhs_recommendations r ON r.id = l.recommendation_id
                    WHERE l.from_source_id = ? ORDER BY r.image_ordinal, l.created_at, l.id""",
                (source_id,),
            ).fetchall()
            return {
                "recommended_in": [self._row(row) for row in recommended_in],
                "recommends": [self._row(row) for row in recommends],
            }

    # -- shared -----------------------------------------------------------------

    def _xhs_fenced_update(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        where: Mapping[str, Any],
        expected_revision: int,
        values: Mapping[str, Any],
        current,
    ) -> dict[str, Any]:
        """Update named columns only at the expected revision, or refuse."""

        resource = current()
        self._expect_revision(resource, expected_revision)
        assignments = ", ".join(f"{name} = ?" for name in values)
        conditions = " AND ".join(f"{name} = ?" for name in where)
        try:
            cursor = conn.execute(
                f"""UPDATE {table}
                    SET {assignments}, revision = revision + 1, updated_at = ?
                    WHERE {conditions} AND revision = ?""",
                (*values.values(), self._registry_now(), *where.values(), expected_revision),
            )
        except sqlite3.IntegrityError:
            # A cross-column CHECK or foreign key refused the combination.
            raise ValueError(f"{table} update is inconsistent") from None
        if cursor.rowcount != 1:
            raise RevisionConflict(current())
        return current()
