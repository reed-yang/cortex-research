"""The XHS plugin's two schedule jobs: `xhs_pull` and `xhs_drain`.

`xhs_pull` creates one scan per followed blogger, as a `list_page` task, and
does nothing else. `xhs_drain` claims at most `drain_units_per_tick` due tasks,
oldest first. Each unit runs one child operation through the engine supervisor
outside any SQLite transaction, then records the answer in one Control
transaction fenced by the task revision (`control/xhs_store.py`).

Each task kind has one handler. A handler builds the child payload from
Control state and, after the child, checks the answer and writes the note's
private staging files; the store applies the rows and queues what comes next.
A kind without a handler is never claimed. `save` and `capture_link` make no
child call: a save writes the note's next version (`layout.py`) and the store
registers it; a Capture link waits, unclaimed, until its Capture ends.

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
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from cortex_platform.product.config import XhsSettings
from cortex_platform.product.control.errors import (
    InvalidTransition,
    NotFound,
    RevisionConflict,
)
from cortex_platform.product.control.xhs_store import (
    XHS_FAILURE_CATEGORIES,
    xhs_identify_digest,
)
from cortex_platform.product.engine.protocol import PROVIDER_WRITE_ROOTS
from cortex_platform.product.sources.identity import blog_url_identity
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected

from . import identify
from .layout import (
    note_title,
    render_blog_notes,
    render_note,
    render_transcription,
    source_title,
    write_version,
)
from .results import validate_engine

NOTES_ROOT_ID = "xhs-notes"
BLOGS_ROOT_ID = "blogs"
STAGING_DIRECTORY = "staging"
# A staged Capture is looked at again this often until it ends.
CAPTURE_WAIT_SECONDS = 600
CAPTURE_TERMINAL_STATES = frozenset({"consumed", "dismissed", "failed"})
# A blog recommendation whose link may still be searched for.
RESOLVABLE_URL_STATES = frozenset({"none", "not_found", "failed"})
# What the store accepts for a recommendation's title and quote.
_MAX_TITLE = 1_000
_MAX_QUOTE = 4_000
_BLOG_FILES = frozenset({"article.md", "raw/page.html", "raw/jina.md"})
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


def _short(value: Any, maximum: int = 500) -> str | None:
    """A provider-supplied label, trimmed and bounded, or None."""

    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]


class Deferred(Exception):
    """A task waiting on something outside the plugin: it comes back later,
    without spending an attempt."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"deferred for {seconds} s")
        self.seconds = seconds


@dataclass(frozen=True)
class UnitOutcome:
    """What one claimed task came to.

    `outcome` is `done`, `skipped` (nothing left to do), `retry` (backed off),
    `failed`, `capped`, `released` or `deferred` (returned unrun), or `lost`
    (the lease passed to another holder before the record).
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


class IdentifyHandler(TaskHandler):
    kind = "identify"
    operation = "xhs_identify"
    provider = "gpt"

    def prepare(self, drain, task):
        inputs = drain.identify_inputs(task["payload"])
        if inputs is None:
            return None
        caption, transcriptions = inputs
        return {
            "caption": caption,
            "transcriptions": [{"image": image, "text": text} for image, text in transcriptions],
            **drain.gpt_settings(),
        }

    def finish(self, drain, task, engine):
        inputs = drain.identify_inputs(task["payload"])
        if inputs is None:
            # A retry moved the note on meanwhile; its own identification follows.
            return {"stale": True}
        caption, transcriptions = inputs
        if (
            engine["prompt_version"] != identify.PROMPT_VERSION
            or engine["input_sha256"] != identify.input_sha256(caption, transcriptions)
        ):
            raise ValueError("identification answers another input")
        # The child's items are not taken on trust: cortexd re-runs the rules,
        # the verbatim filter and the merge on its own copy of the input.
        model_items = identify.parse_model_items(json.dumps({"items": engine["model_items"]}))
        outcome = identify.identify(caption, transcriptions, model_items)
        items = [
            dict(item)
            for item in outcome.items
            if 1 <= len(item["title"]) <= _MAX_TITLE and 1 <= len(item["quote"]) <= _MAX_QUOTE
        ]
        return {
            "prompt_version": engine["prompt_version"],
            "input_sha256": engine["input_sha256"],
            "response_id": _short(engine.get("response_id")),
            "model": _short(engine.get("model")),
            "model_items": outcome.model_items,
            "dropped": outcome.dropped + len(outcome.items) - len(items),
            "rule_items": outcome.rule_items,
            "items": items,
        }


class ResolveHandler(TaskHandler):
    kind = "resolve"
    operation = "xhs_resolve_link"
    provider = "gpt"

    def prepare(self, drain, task):
        recommendation = drain.store.get_xhs_recommendation(task["payload"]["recommendation_id"])
        if (
            recommendation["kind"] != "blog"
            or recommendation["url_state"] not in RESOLVABLE_URL_STATES
        ):
            return None
        return {"title": recommendation["title"], **drain.gpt_settings()}

    def finish(self, drain, task, engine):
        if engine["prompt_version"] != identify.LINK_PROMPT_VERSION:
            raise ValueError("link search answers another prompt")
        failure = engine["verification_failure"] or {}
        category = failure.get("category")
        return {
            "url": engine["url"],
            "url_state": engine["url_state"],
            "checked_title": _short(engine["checked_title"], _MAX_TITLE),
            "prompt_version": engine["prompt_version"],
            "response_id": _short(engine.get("response_id")),
            "verification_failure": category if category in XHS_FAILURE_CATEGORIES else None,
        }


class SaveHandler(TaskHandler):
    """Write the note's next version from Control state and its staging files."""

    kind = "save"
    operation = None
    provider = None

    def prepare(self, drain, task):
        payload = task["payload"]
        note = drain.store.get_xhs_note(payload["note_id"])
        if note["state"] not in {"identified", "saved"} or int(note["content_version"]) >= int(
            payload["version"]
        ):
            return None
        return {"note_id": note["note_id"], "version": int(payload["version"])}

    def finish(self, drain, task, engine):
        return drain.write_note_version(engine["note_id"], engine["version"])


class BlogImportHandler(TaskHandler):
    kind = "blog_import"
    operation = "blog_fetch"
    provider = None

    def prepare(self, drain, task):
        recommendation = drain.store.get_xhs_recommendation(task["payload"]["recommendation_id"])
        if recommendation["import_state"] != "importing":
            return None
        if recommendation["kind"] != "blog" or recommendation["url"] is None:
            raise ValueError("recommendation has no link to import")
        normalized, authority_id = blog_url_identity(recommendation["url"])
        staging = drain.blog_staging_dir(authority_id)
        # A fetch starts clean: no file of an earlier attempt may be claimed.
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        return {"url": normalized, "staging_dir": str(staging)}

    def finish(self, drain, task, engine):
        recommendation = drain.store.get_xhs_recommendation(task["payload"]["recommendation_id"])
        if recommendation["url"] is None:
            raise ValueError("recommendation lost its link")
        normalized, authority_id = blog_url_identity(recommendation["url"])
        if (engine["normalized_url"], engine["authority_id"]) != (normalized, authority_id):
            raise ValueError("blog fetch answers another link")
        return drain.write_blog_version(recommendation, engine)


class CaptureLinkHandler(TaskHandler):
    """Wait for a staged paper Capture to end, then let the store link it."""

    kind = "capture_link"
    operation = None
    provider = None

    def prepare(self, drain, task):
        capture = drain.store.get_capture(task["payload"]["capture_id"])
        if capture["state"] not in CAPTURE_TERMINAL_STATES:
            raise Deferred(CAPTURE_WAIT_SECONDS)
        return {"capture_id": capture["id"]}

    def finish(self, drain, task, engine):
        return dict(engine)


PIPELINE_HANDLERS: tuple[TaskHandler, ...] = ACQUISITION_HANDLERS + (
    IdentifyHandler(),
    ResolveHandler(),
    SaveHandler(),
    BlogImportHandler(),
    CaptureLinkHandler(),
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
        """Run at most `drain_units_per_tick` due tasks, oldest first.

        A deferred task (a Capture still open) is not a unit: it only reads
        Control state and comes back later, so staged Captures waiting for
        approval never use up a tick's budget ahead of newer work.
        """

        units: list[UnitOutcome] = []
        capped: set[str] = set()
        counted = 0
        while counted < self.settings.drain_units_per_tick:
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
            if unit.outcome != "deferred":
                counted += 1
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
        except Deferred as deferred:
            return self._defer(task, deferred.seconds)
        except (NotFound, ValueError, KeyError, TypeError):
            return self._fail(task, "invalid_response")
        if payload is None:
            return self._skip(task)
        if handler.operation is None:
            try:
                result = handler.finish(self, task, payload)
            except (NotFound, ValueError, KeyError, TypeError):
                return self._fail(task, "invalid_response")
            except OSError:
                # A local file write that failed may well succeed later.
                return self._fail(task, "transient")
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
            except (NotFound, ValueError, KeyError, TypeError):
                return self._fail(task, "invalid_response")
            except OSError:
                return self._fail(task, "transient")
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

    def _defer(self, task: Mapping[str, Any], seconds: int) -> UnitOutcome:
        try:
            self.store.release_xhs_task(
                task["id"], expected_revision=task["revision"], delay_seconds=seconds
            )
        except RevisionConflict:
            return UnitOutcome(str(task["id"]), str(task["kind"]), "lost")
        return UnitOutcome(str(task["id"]), str(task["kind"]), "deferred")

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

    def gpt_settings(self) -> dict[str, str]:
        return {
            "gpt_base": self.settings.gpt_base,
            "gpt_model": self.settings.gpt_model,
            "gpt_effort": self.settings.gpt_effort,
        }

    def transcription(self, note_id: str, image: Mapping[str, Any]) -> str:
        """An image's verbatim transcription, checked against its recorded hash."""

        path = self.staging_dir(note_id) / "ocr" / f"{int(image['ordinal'])}.md"
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError("a transcription is missing") from None
        if hashlib.sha256(data).hexdigest() != image["ocr_text_sha256"]:
            raise ValueError("a transcription does not match its hash")
        return data.decode("utf-8")

    def staged_image(self, note_id: str, image: Mapping[str, Any]) -> bytes:
        """A downloaded image's bytes, checked against its recorded hash."""

        extension = str(image["asset_name"]).rsplit(".", 1)[-1]
        path = self.staging_dir(note_id) / f"{image['sha256']}.{extension}"
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError("a downloaded image is missing") from None
        if hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise ValueError("a downloaded image does not match its hash")
        return data

    def identify_inputs(
        self, payload: Mapping[str, Any]
    ) -> tuple[str, list[tuple[int, str]]] | None:
        """The caption and each transcription under its original ordinal, or
        None when the note no longer has the input the task was keyed by.

        Images whose download or OCR failed are left out.
        """

        note_id = payload["note_id"]
        note = self.store.get_xhs_note(note_id)
        if note["state"] != "ocr_done":
            return None
        images = [
            image
            for image in self.store.list_xhs_note_images(note_id)
            if image["download_state"] == "ok" and image["ocr_state"] == "ok"
        ]
        digest = xhs_identify_digest(
            note["caption"], [(image["ordinal"], image["ocr_text_sha256"]) for image in images]
        )
        if digest != payload["input_sha256"]:
            return None
        return note["caption"], [
            (int(image["ordinal"]), self.transcription(note_id, image)) for image in images
        ]

    def write_note_version(self, note_id: str, version: int) -> dict[str, Any]:
        """Write `<note_id>/v<version>/` and describe it for the save record."""

        note = self.store.get_xhs_note(note_id)
        blogger = self.store.get_xhs_blogger(note["user_id"])
        images = self.store.list_xhs_note_images(note_id)
        recommendations = self.store.list_xhs_recommendations(note_id)
        staging = self.staging_dir(note_id)
        files: dict[str, bytes] = {}
        texts: dict[int, str] = {}
        for image in images:
            ordinal = int(image["ordinal"])
            if image["download_state"] != "ok":
                continue
            files[f"assets/{image['asset_name']}"] = self.staged_image(note_id, image)
            if image["ocr_state"] == "ok":
                texts[ordinal] = self.transcription(note_id, image)
                raw = staging / "ocr" / f"{ordinal}.json"
                if raw.is_file():
                    files[f"ocr/{ordinal}.json"] = raw.read_bytes()
        for name in ("raw/list.json", "raw/detail.json"):
            if (staging / name).is_file():
                files[name] = (staging / name).read_bytes()
        files["note.md"] = render_note(note, blogger, images, recommendations).encode("utf-8")
        files["transcription.md"] = render_transcription(images, texts).encode("utf-8")
        digest = write_version(self.root(NOTES_ROOT_ID) / note_id, version, files)
        return {
            "version": version,
            "tree_sha256": digest,
            "title": source_title(note_title(note), f"XHS note {note_id}"),
            "metadata": {
                "note_id": note_id,
                "user_id": note["user_id"],
                "published_at": note["published_at"],
                "images": len(images),
                "failed_images": sum(
                    1
                    for image in images
                    if image["download_state"] == "failed" or image["ocr_state"] == "failed"
                ),
                "recommendations": len(recommendations),
            },
        }

    def blog_staging_dir(self, authority_id: str) -> Path:
        if _SHA256_RE.fullmatch(str(authority_id)) is None:
            raise ValueError("blog identity is invalid")
        return self.root(BLOGS_ROOT_ID) / authority_id[:16] / STAGING_DIRECTORY

    def write_blog_version(
        self, recommendation: Mapping[str, Any], engine: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Write the blog's next version: the fetched article and private raw
        files, and `notes.md` naming every note that recommends it, each with
        its screenshot copied from the note."""

        authority_id = str(engine["authority_id"])
        staging = self.blog_staging_dir(authority_id)
        files: dict[str, bytes] = {}
        for name, described in engine["files"].items():
            if name not in _BLOG_FILES:
                raise ValueError("blog fetch wrote an unexpected file")
            try:
                data = (staging / name).read_bytes()
            except OSError:
                raise ValueError("a fetched blog file is missing") from None
            if (hashlib.sha256(data).hexdigest(), len(data)) != (
                described.get("sha256"),
                described.get("bytes"),
            ):
                raise ValueError("a fetched blog file does not match its hash")
            files[name] = data
        metadata = engine["metadata"]
        # Never claim raw HTML that was not fetched, nor hide Jina's text.
        if bool(metadata.get("raw_html")) != ("raw/page.html" in files) or bool(
            metadata.get("raw_jina")
        ) != ("raw/jina.md" in files):
            raise ValueError("blog fetch describes its raw files inconsistently")
        source_id = self.store.content_source_id("blog", authority_id)
        linked = (
            [
                str(link["recommendation_id"])
                for link in self.store.list_source_links(source_id)["recommended_in"]
            ]
            if source_id is not None
            else []
        )
        entries = []
        for recommendation_id in [*linked, str(recommendation["id"])]:
            if any(entry["recommendation_id"] == recommendation_id for entry in entries):
                continue
            entry = self._recommended_in(recommendation_id, files)
            entries.append(entry)
        title = source_title(
            str(metadata.get("title") or ""), source_title(str(recommendation["title"]), "Blog")
        )
        files["notes.md"] = render_blog_notes(
            title=title,
            normalized_url=str(engine["normalized_url"]),
            final_url=metadata.get("final_url"),
            content_source=str(metadata.get("content_source")),
            recommended_in=entries,
        ).encode("utf-8")
        version = self.store.next_content_version("blog", authority_id)
        digest = write_version(
            self.root(BLOGS_ROOT_ID) / authority_id[:16], version, files
        )
        origin_failure = metadata.get("origin_failure") or {}
        return {
            "authority_id": authority_id,
            "version": version,
            "tree_sha256": digest,
            "title": title,
            "metadata": {
                "normalized_url": engine["normalized_url"],
                "final_url": _short(metadata.get("final_url"), 2_000),
                "content_source": metadata.get("content_source"),
                "author": _short(metadata.get("author")),
                "date": _short(metadata.get("date")),
                "characters": metadata.get("characters"),
                "raw_html": "raw/page.html" in files,
                "raw_jina": "raw/jina.md" in files,
                "origin_failure": origin_failure.get("category"),
            },
        }

    def _recommended_in(self, recommendation_id: str, files: dict[str, bytes]) -> dict[str, Any]:
        recommendation = self.store.get_xhs_recommendation(recommendation_id)
        note = self.store.get_xhs_note(recommendation["note_id"])
        entry: dict[str, Any] = {
            "recommendation_id": recommendation_id,
            "note_title": note_title(note),
            "image_ordinal": recommendation["image_ordinal"],
            "quote": recommendation["quote"],
            "asset_name": None,
        }
        if recommendation["image_ordinal"] is not None:
            image = self.image(note["note_id"], recommendation["image_ordinal"])
            if image["download_state"] == "ok":
                files[f"assets/{image['asset_name']}"] = self.staged_image(note["note_id"], image)
                entry["asset_name"] = image["asset_name"]
        return entry
