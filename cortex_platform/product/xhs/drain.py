"""The XHS plugin's two schedule jobs: `xhs_pull` and `xhs_drain`.

`xhs_pull` creates one scan per followed blogger, as a `list_page` task, and
does nothing else. `xhs_drain` claims at most `drain_units_per_tick` due tasks,
oldest first. Each unit runs one child operation through the engine supervisor
outside any SQLite transaction, then records the answer in one Control
transaction fenced by the task revision (`control/xhs_store.py`).

Each task kind has one handler. A handler builds the child payload from
Control state and, after the child, checks the answer and writes the note's
private staging files; the store applies the rows and queues what comes next.
A kind without a handler is never claimed.

The staging directory is `<xhs-notes root>/<note_id>/staging/`: downloaded
images under their hash, `raw/list.json` and `raw/detail.json` with signed
URLs removed, and `ocr/<ordinal>.json` (the provider's raw answers) beside
`ocr/<ordinal>.md` (the verbatim transcription). No route serves it; a saved
version is written from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from cortex_platform.product.config import XhsSettings
from cortex_platform.product.control.errors import (
    InvalidTransition,
    NotFound,
    RevisionConflict,
)
from cortex_platform.product.engine.protocol import PROVIDER_WRITE_ROOTS
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected

from .results import validate_engine

NOTES_ROOT_ID = "xhs-notes"
STAGING_DIRECTORY = "staging"
# The TikHub note type of an image note; every other type is unsupported.
IMAGE_NOTE_TYPE = "normal"
# Failures after which every later call this tick would fail the same way.
_STOPPING = frozenset({"auth", "payment"})
# The provider does not bill these, so the daily cap does not count them.
_UNBILLED = frozenset({"auth", "payment"})
_NOTE_TYPE_RE = re.compile(r"[a-z_]{1,32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ENGINE_RE = re.compile(r"[0-9a-z.-]{1,64}\Z")
_EXTENSIONS = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}
# Keys whose values are user text, kept verbatim even when they look like a URL.
_TEXT_KEYS = frozenset({"desc", "title", "display_title", "nickname", "name"})
_LEASE_MARGIN_SECONDS = 300


def strip_signed_urls(value: Any, key: str | None = None) -> Any:
    """Drop the query and fragment of every URL value, recursively.

    The child already does this; cortexd does it again before any raw answer
    reaches a file, because a signed CDN URL must never be written.
    """

    if isinstance(value, dict):
        return {name: strip_signed_urls(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [strip_signed_urls(item, key) for item in value]
    if (
        isinstance(value, str)
        and key not in _TEXT_KEYS
        and value.lower().startswith(("http://", "https://"))
    ):
        return value.split("?", 1)[0].split("#", 1)[0]
    return value


def _write_private(path: Path, text: str) -> None:
    """Replace one staging file whole: a reader sees the old file or the new."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


@dataclass(frozen=True)
class UnitOutcome:
    """What one claimed task came to.

    `outcome` is `done`, `skipped` (nothing left to do), `retry` (backed off),
    `failed`, `capped` or `released` (returned unrun), or `lost` (the lease
    passed to another holder before the record).
    """

    task_id: str
    kind: str
    outcome: str
    category: str | None = None
    stop: bool = False


@dataclass(frozen=True)
class DrainReport:
    units: tuple[UnitOutcome, ...]
    stopped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": [
                {
                    "task_id": unit.task_id,
                    "kind": unit.kind,
                    "outcome": unit.outcome,
                    "category": unit.category,
                }
                for unit in self.units
            ],
            "stopped": self.stopped,
        }


class TaskHandler:
    """One task kind: the child call it makes and how its answer is kept.

    `operation` is None for a kind that makes no child call; its `finish`
    then receives the prepared payload instead of a child answer.
    `provider` names the daily cap the call counts against, if any.
    """

    kind = ""
    operation: str | None = None
    provider: str | None = None

    def prepare(self, drain: XhsDrain, task: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The child payload, or None when the task has nothing left to do."""

        raise NotImplementedError

    def finish(
        self, drain: XhsDrain, task: Mapping[str, Any], engine: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Check the answer beyond its shape, write private files, and return
        what the store records. A ValueError makes it `invalid_response`."""

        raise NotImplementedError


class ListPageHandler(TaskHandler):
    kind = "list_page"
    operation = "xhs_list_page"
    provider = "tikhub"

    def prepare(self, drain, task):
        payload = task["payload"]
        if not drain.store.get_xhs_blogger(payload["user_id"])["followed"]:
            # Unfollowed since the scan began: its remaining pages are dropped.
            return None
        return {
            "user_id": payload["user_id"],
            "cursor": payload.get("cursor") or "",
            "tikhub_base": drain.settings.tikhub_base,
        }

    def finish(self, drain, task, engine):
        payload = task["payload"]
        if engine["user_id"] != payload["user_id"] or engine["cursor"] != (
            payload.get("cursor") or ""
        ):
            raise ValueError("list page answers another request")
        raw_notes = (((engine["raw"].get("data") or {}).get("data") or {}).get("notes")) or []
        raw_by_id = {
            entry.get("id"): entry for entry in raw_notes if isinstance(entry, dict)
        }
        notes = []
        for note in engine["notes"]:
            note_type = note["note_type"]
            if _NOTE_TYPE_RE.fullmatch(note_type) is None:
                note_type = "unknown"
            notes.append(
                {
                    "note_id": note["note_id"],
                    "note_type": note_type,
                    "sticky": note["sticky"],
                    "title": note["title"],
                    "caption": note["caption"],
                    "published_at": note["published_at"],
                }
            )
            if note_type == IMAGE_NOTE_TYPE and not drain.note_known(note["note_id"]):
                drain.write_staging(
                    note["note_id"],
                    "raw/list.json",
                    _json(strip_signed_urls(raw_by_id.get(note["note_id"]) or {})),
                )
        return {
            "notes": notes,
            "has_more": engine["has_more"],
            "next_cursor": engine["next_cursor"],
        }


class DetailHandler(TaskHandler):
    kind = "detail"
    operation = "xhs_note_detail"
    provider = "tikhub"

    def prepare(self, drain, task):
        note_id = task["payload"]["note_id"]
        state = drain.store.get_xhs_note(note_id)["state"]
        # A first detail, or a refresh for images whose signed URL expired.
        if state != "discovered" and not drain.store.xhs_awaiting_refresh(note_id):
            return None
        return {"note_id": note_id, "tikhub_base": drain.settings.tikhub_base}

    def finish(self, drain, task, engine):
        note_id = task["payload"]["note_id"]
        note = engine["note"]
        if note["note_id"] != note_id:
            raise ValueError("detail answers another note")
        note_type = note["note_type"]
        if _NOTE_TYPE_RE.fullmatch(note_type) is None:
            note_type = "unknown"
        if drain.store.get_xhs_note(note_id)["state"] == "discovered":
            # The first detail is the one kept; a refresh only serves URLs.
            drain.write_staging(note_id, "raw/detail.json", _json(strip_signed_urls(engine["raw"])))
        return {
            "note": {
                "note_id": note_id,
                "note_type": note_type,
                "title": note["title"],
                "caption": note["caption"],
                "published_at": note["published_at"],
                "user_id": note["user_id"],
                "user_name": note["user_name"],
            },
            "images": [
                {
                    "ordinal": image["ordinal"],
                    "fileid": image["fileid"],
                    "width": image["width"],
                    "height": image["height"],
                    "url": image["url"],
                }
                for image in note["images"]
            ],
        }


class DownloadHandler(TaskHandler):
    kind = "download"
    operation = "xhs_download_image"
    provider = None

    def prepare(self, drain, task):
        payload = task["payload"]
        if drain.image(payload["note_id"], payload["ordinal"])["download_state"] != "pending":
            return None
        staging = drain.staging_dir(payload["note_id"])
        staging.mkdir(parents=True, exist_ok=True)
        return {
            "note_id": payload["note_id"],
            "ordinal": payload["ordinal"],
            "url": payload["url"],
            "staging_dir": str(staging),
        }

    def finish(self, drain, task, engine):
        payload = task["payload"]
        image = engine["image"]
        if (engine.get("note_id"), engine.get("ordinal")) != (
            payload["note_id"],
            payload["ordinal"],
        ):
            raise ValueError("download answers another image")
        sha256, extension = image["sha256"], image["extension"]
        if (
            _SHA256_RE.fullmatch(sha256) is None
            or _EXTENSIONS.get(extension) != image["media_type"]
            or image["name"] != f"{sha256}.{extension}"
        ):
            raise ValueError("downloaded image is described inconsistently")
        # The bytes on disk are what the record vouches for: check them here.
        path = drain.staging_dir(payload["note_id"]) / image["name"]
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError("downloaded image is missing") from None
        if len(data) != image["byte_size"] or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("downloaded image does not match its hash")
        return {
            "image": {
                "sha256": sha256,
                "extension": extension,
                "byte_size": image["byte_size"],
                "media_type": image["media_type"],
                "width": image["width"],
                "height": image["height"],
            }
        }


class OcrHandler(TaskHandler):
    kind = "ocr"
    operation = "xhs_ocr_image"
    provider = "ocr"

    def prepare(self, drain, task):
        payload = task["payload"]
        image = drain.image(payload["note_id"], payload["ordinal"])
        if (
            image["download_state"] != "ok"
            or image["sha256"] != payload["sha256"]
            or image["ocr_state"] != "pending"
        ):
            return None
        path = drain.staging_dir(payload["note_id"]) / payload["name"]
        if not path.is_file():
            raise ValueError("staged image is missing")
        return {"image_path": str(path), "sha256": payload["sha256"]}

    def finish(self, drain, task, engine):
        payload = task["payload"]
        markdown = engine["markdown"]
        if (
            engine["sha256"] != payload["sha256"]
            or _ENGINE_RE.fullmatch(engine["engine"]) is None
            or hashlib.sha256(markdown.encode("utf-8")).hexdigest() != engine["text_sha256"]
        ):
            raise ValueError("transcription is described inconsistently")
        ordinal = int(payload["ordinal"])
        # Verbatim, as the provider answered: the transcription is evidence.
        drain.write_staging(payload["note_id"], f"ocr/{ordinal}.md", markdown)
        drain.write_staging(
            payload["note_id"],
            f"ocr/{ordinal}.json",
            _json(
                {
                    "sha256": engine["sha256"],
                    "engine": engine["engine"],
                    "text_sha256": engine["text_sha256"],
                    "flags": engine["flags"],
                    "finish_reason": engine["finish_reason"],
                    "usage": engine.get("usage"),
                    "attempts": engine["attempts"],
                    "raw": engine["raw"],
                }
            ),
        )
        return {
            "engine": engine["engine"],
            "flags": list(engine["flags"]),
            "text_sha256": engine["text_sha256"],
        }


ACQUISITION_HANDLERS: tuple[TaskHandler, ...] = (
    ListPageHandler(),
    DetailHandler(),
    DownloadHandler(),
    OcrHandler(),
)


class XhsDrain:
    """Run the plugin's scan and drain jobs; cortexd's tick calls `run_job`."""

    def __init__(
        self,
        *,
        store: Any,
        supervisor: Any,
        settings: XhsSettings,
        handlers: Sequence[TaskHandler] = ACQUISITION_HANDLERS,
    ) -> None:
        self.store = store
        self.settings = settings
        self._supervisor = supervisor
        self._handlers = {handler.kind: handler for handler in handlers}
        # A lease outlives the child's hard timeout, so only a crashed holder
        # ever loses one.
        self._lease_seconds = min(
            86_400, int(supervisor.timeout_seconds) + _LEASE_MARGIN_SECONDS
        )

    # -- jobs -----------------------------------------------------------------

    def refusal(self) -> str | None:
        """Why the plugin will not run, or None when it may."""

        if not self.settings.enabled:
            return "disabled_in_config"
        if any(state != "ready" for state in self.store.xhs_roots_status().values()):
            return "roots_not_ready"
        return None

    def run_job(self, operation: str) -> tuple[str, int]:
        """One schedule job, as the tick records it: an outcome and a count."""

        if self.refusal() is not None:
            return "refused", 0
        if operation == "xhs_pull":
            created = self.pull()
            return ("ran" if created else "skipped"), len(created)
        if operation == "xhs_drain":
            report = self.drain()
            if report.stopped == "runtime_activation_disabled":
                return "refused", len(report.units)
            if report.stopped is not None:
                return "failed", len(report.units)
            return ("ran" if report.units else "skipped"), len(report.units)
        return "skipped", 0

    def pull(
        self,
        *,
        user_ids: Sequence[str] | None = None,
        max_pages: int | None = None,
        scan_id: str | None = None,
        full: bool = False,
    ) -> list[dict[str, Any]]:
        """Create one scan per followed blogger. Makes no call itself."""

        return self.store.start_xhs_scans(
            max_pages=max_pages or self.settings.max_list_pages,
            user_ids=user_ids,
            scan_id=scan_id,
            full=full,
        )

    def drain(self) -> DrainReport:
        """Run at most `drain_units_per_tick` due tasks, oldest first."""

        units: list[UnitOutcome] = []
        capped: set[str] = set()
        for _ in range(self.settings.drain_units_per_tick):
            # Asked again before every claim: a window can lapse mid-tick.
            if not self.store.runtime_dispatch_enabled():
                return DrainReport(tuple(units), "runtime_activation_disabled")
            # A provider at its cap keeps its tasks pending, unclaimed, until
            # the UTC day changes.
            exhausted = capped | self.store.xhs_exhausted_providers(self.settings.daily_calls)
            kinds = [
                kind
                for kind, handler in self._handlers.items()
                if handler.provider not in exhausted
            ]
            if not kinds:
                break
            task = self.store.claim_xhs_task(lease_seconds=self._lease_seconds, kinds=kinds)
            if task is None:
                break
            unit = self._unit(task)
            units.append(unit)
            if unit.outcome == "capped":
                capped.add(str(self._handlers[unit.kind].provider))
            if unit.stop:
                return DrainReport(tuple(units), unit.category)
        return DrainReport(tuple(units))

    # -- one unit ---------------------------------------------------------------

    def _unit(self, task: Mapping[str, Any]) -> UnitOutcome:
        handler = self._handlers[str(task["kind"])]
        try:
            payload = handler.prepare(self, task)
        except (NotFound, ValueError, KeyError, TypeError):
            return self._fail(task, "invalid_response")
        if payload is None:
            return self._skip(task)
        if handler.operation is None:
            try:
                result = handler.finish(self, task, payload)
            except (ValueError, KeyError, TypeError):
                return self._fail(task, "invalid_response")
            return self._record(task, result)
        provider = handler.provider
        if provider is not None and not self.store.reserve_xhs_usage(
            provider, cap=int(self.settings.daily_calls[provider])
        ):
            self.store.release_xhs_task(task["id"], expected_revision=task["revision"])
            return UnitOutcome(str(task["id"]), str(task["kind"]), "capped", provider)
        root_id = PROVIDER_WRITE_ROOTS.get(handler.operation)
        write_roots = (self.root(root_id),) if root_id else None
        try:
            execution = self._supervisor.run(
                handler.operation, payload, write_roots=write_roots
            )
        except EffectPermanentlyRejected as rejection:
            if rejection.category == "runtime_activation_disabled":
                # Never started: the task and its reserved call go back.
                self.store.release_xhs_task(task["id"], expected_revision=task["revision"])
                if provider is not None:
                    self.store.refund_xhs_usage(provider)
                return UnitOutcome(
                    str(task["id"]), str(task["kind"]), "released",
                    "runtime_activation_disabled", stop=True,
                )
            # A credential that does not resolve: as with `auth`, the task
            # fails and the tick stops.
            return self._fail(task, "auth", refund=provider, stop=True)
        try:
            if not execution.ok:
                category = execution.failure_category or "outcome_unknown"
                return self._fail(
                    task,
                    category,
                    refund=provider if category in _UNBILLED else None,
                    stop=category in _STOPPING,
                )
            try:
                engine = validate_engine(handler.operation, execution.engine)
                result = handler.finish(self, task, engine)
            except (ValueError, KeyError, TypeError):
                return self._fail(task, "invalid_response")
            return self._record(task, result)
        finally:
            self._supervisor.discard(execution.marker)

    def _record(self, task: Mapping[str, Any], result: Mapping[str, Any]) -> UnitOutcome:
        try:
            self.store.record_xhs_task_result(
                task["id"], expected_revision=task["revision"], result=result
            )
        except RevisionConflict:
            return UnitOutcome(str(task["id"]), str(task["kind"]), "lost")
        except (ValueError, InvalidTransition, NotFound, KeyError, TypeError):
            # The store refused what the answer claims; nothing was applied.
            return self._fail(task, "invalid_response")
        return UnitOutcome(str(task["id"]), str(task["kind"]), "done")

    def _skip(self, task: Mapping[str, Any]) -> UnitOutcome:
        try:
            self.store.complete_xhs_task(
                task["id"], expected_revision=task["revision"], result={"skipped": True}
            )
        except RevisionConflict:
            return UnitOutcome(str(task["id"]), str(task["kind"]), "lost")
        return UnitOutcome(str(task["id"]), str(task["kind"]), "skipped")

    def _fail(
        self,
        task: Mapping[str, Any],
        category: str,
        *,
        refund: str | None = None,
        stop: bool = False,
    ) -> UnitOutcome:
        try:
            after = self.store.record_xhs_task_failure(
                task["id"],
                expected_revision=task["revision"],
                category=category,
                refund=refund,
            )
        except RevisionConflict:
            return UnitOutcome(str(task["id"]), str(task["kind"]), "lost", category)
        outcome = "retry" if after["state"] == "pending" else "failed"
        return UnitOutcome(str(task["id"]), str(task["kind"]), outcome, category, stop)

    # -- files and lookups --------------------------------------------------------

    def root(self, root_id: str) -> Path:
        return Path(self.store.get_asset_root(root_id).private_path)

    def staging_dir(self, note_id: str) -> Path:
        # A note ID is 24 hex characters (checked by the store), never a path.
        if re.fullmatch(r"[0-9a-f]{24}", str(note_id)) is None:
            raise ValueError("note_id is invalid")
        return self.root(NOTES_ROOT_ID) / note_id / STAGING_DIRECTORY

    def write_staging(self, note_id: str, name: str, text: str) -> None:
        _write_private(self.staging_dir(note_id) / name, text)

    def note_known(self, note_id: str) -> bool:
        try:
            self.store.get_xhs_note(note_id)
        except NotFound:
            return False
        return True

    def image(self, note_id: str, ordinal: int) -> Mapping[str, Any]:
        for image in self.store.list_xhs_note_images(note_id):
            if image["ordinal"] == ordinal:
                return image
        raise NotFound("xhs note image", f"{note_id}:{ordinal}")
