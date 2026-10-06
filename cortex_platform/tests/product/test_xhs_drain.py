"""The XHS acquisition pipeline: scans, list pages, details, downloads and OCR.

A scripted supervisor stands in for the engine child, so nothing here reaches
a provider. Every identity, caption and image is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from cortex_platform.product.config import XhsSettings
from cortex_platform.product.control import ControlStore
from cortex_platform.product.engine.schedules import (
    XHS_DRAIN_JOB,
    XHS_PULL_JOB,
    JobsFileReading,
    ResearchScheduleTick,
    seed_schedules,
)
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected
from cortex_platform.product.xhs.drain import DetailHandler, ListPageHandler, XhsDrain
from cortex_platform.tests.product.control.test_fragments import (
    DeterministicIds,
    MovableClock,
)

ACTOR = "local-operator"
USER = "5f0e1d2c3b4a59687766554a"
OTHER_USER = "5f0e1d2c3b4a59687766554b"
QUIET_USER = "5f0e1d2c3b4a59687766554c"
SIGNATURE = "sign=private-token"


def note_id(n: int) -> str:
    return f"66f1a2b3c4d5e6f70819{n:04x}"


def png(seed: int) -> bytes:
    """A small synthetic PNG: signature, header chunk and distinct filler."""

    header = struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + header
        + bytes([seed % 256]) * 16
    )


def listed(n: int, *, sticky: bool = False, note_type: str = "normal") -> dict[str, Any]:
    return {
        "note_id": note_id(n),
        "user_id": USER,
        "note_type": note_type,
        "sticky": sticky,
        "title": f"Weekly papers 本周论文 {n}",
        "caption": "Three papers on speculative decoding 投机解码…",
        "caption_complete": False,
        "published_at": "2026-08-20T08:00:00Z",
        "cursor": f"cursor-{n}",
        "images": [
            {
                "ordinal": 1,
                "fileid": f"list-file-{n}",
                "width": 1080,
                "height": 1440,
                "variant": "original",
                "url": f"https://cdn.example/{n}/1.webp?{SIGNATURE}",
            }
        ],
    }


def page(user: str, cursor: str, notes: list[dict[str, Any]], has_more: bool) -> dict[str, Any]:
    return {
        "user_id": user,
        "cursor": cursor,
        "has_more": has_more,
        "next_cursor": notes[-1]["cursor"] if notes else None,
        "notes": notes,
        "raw": {
            "data": {
                "success": True,
                "data": {
                    "notes": [
                        {
                            "id": note["note_id"],
                            "desc": note["caption"],
                            # A signed URL the parent must strip again.
                            "images_list": [
                                {"original": f"https://cdn.example/a.webp?{SIGNATURE}"}
                            ],
                        }
                        for note in notes
                    ]
                },
            }
        },
    }


def detail(n: int, fileids: list[str], *, tag: str = "v1") -> dict[str, Any]:
    caption = (
        "Three papers on speculative decoding 投机解码, with the full list of links "
        "and a long explanation that the list page cut at one hundred characters."
    )
    return {
        "note": {
            "note_id": note_id(n),
            "note_type": "normal",
            "title": f"Weekly papers 本周论文 {n}",
            "caption": caption,
            "caption_complete": True,
            "published_at": "2026-08-20T08:00:00Z",
            "user_id": USER,
            "user_name": "合成博主 Synthetic",
            "images": [
                {
                    "ordinal": ordinal,
                    "fileid": fileid,
                    "width": 1080,
                    "height": 1440,
                    "variant": "original",
                    "url": f"https://cdn.example/{fileid}.webp?{SIGNATURE}&v={tag}",
                }
                for ordinal, fileid in enumerate(fileids, start=1)
            ],
        },
        "raw": {
            "data": {
                "success": True,
                "data": [
                    {
                        "note_list": [
                            {
                                "id": note_id(n),
                                "desc": caption,
                                "images_list": [
                                    {"original": f"https://cdn.example/{f}.webp?{SIGNATURE}"}
                                    for f in fileids
                                ],
                            }
                        ]
                    }
                ],
            }
        },
    }


@dataclass
class Execution:
    ok: bool
    engine: dict[str, Any] | None
    failure_category: str | None
    marker: str


@dataclass
class ScriptedSupervisor:
    """Answers each child operation from a script; makes no network call.

    `lists` maps (user, cursor), `details` a note ID to a queue of answers,
    `downloads` a URL to image bytes or a failure category, and `ocr` an
    image hash to a transcription or a failure category.
    """

    store: ControlStore
    lists: dict[tuple[str, str], Any] = field(default_factory=dict)
    details: dict[str, list[Any]] = field(default_factory=dict)
    downloads: dict[str, Any] = field(default_factory=dict)
    ocr: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    timeout_seconds: int = 600
    stopping: bool = False

    def run(self, operation: str, payload, *, write_roots=None) -> Execution:
        if not self.store.runtime_dispatch_enabled():
            raise EffectPermanentlyRejected("runtime_activation_disabled")
        self.calls.append((operation, dict(payload), write_roots))
        marker = f"marker-{len(self.calls)}"
        if operation == "xhs_list_page":
            answer = self.lists[(payload["user_id"], payload["cursor"])]
        elif operation == "xhs_note_detail":
            answer = self.details[payload["note_id"]].pop(0)
        elif operation == "xhs_download_image":
            assert write_roots and Path(payload["staging_dir"]).is_relative_to(write_roots[0])
            answer = self.downloads[payload["url"]]
            if isinstance(answer, bytes):
                digest = hashlib.sha256(answer).hexdigest()
                staging = Path(payload["staging_dir"])
                (staging / f"{digest}.png").write_bytes(answer)
                answer = {
                    "note_id": payload["note_id"],
                    "ordinal": payload["ordinal"],
                    "image": {
                        "name": f"{digest}.png", "sha256": digest, "byte_size": len(answer),
                        "media_type": "image/png", "extension": "png",
                        "width": 4, "height": 3,
                    },
                }
        elif operation == "xhs_ocr_image":
            answer = self.ocr[payload["sha256"]]
            if isinstance(answer, tuple):
                markdown, flags = answer
                answer = {
                    "sha256": payload["sha256"], "engine": "deepseek-ocr-2",
                    "markdown": markdown,
                    "text_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                    "flags": flags, "finish_reason": "length" if flags else "stop",
                    "usage": {"completion_tokens": 12}, "attempts": [],
                    "raw": {"deepseek-ocr-2": {"choices": [{"text": markdown}]}},
                }
        else:  # pragma: no cover - the drain asks for nothing else here
            raise AssertionError(operation)
        if isinstance(answer, str):
            return Execution(False, None, answer, marker)
        return Execution(True, answer, None, marker)

    def discard(self, marker: str) -> None:
        self.discarded.append(marker)

    def operations(self) -> list[str]:
        return [operation for operation, _, _ in self.calls]


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MovableClock) -> ControlStore:
    value = ControlStore(tmp_path / "control.db", clock=clock, id_factory=DeterministicIds())
    value.initialize()
    for root_id in ("xhs-notes", "blogs"):
        value.register_asset_root(
            root_id=root_id, private_path=tmp_path / root_id, max_bytes=1 << 30,
            enabled=True, actor_id=ACTOR, idempotency_key=f"root-{root_id}-00001",
        )
    value.enable_runtime_activation(
        mode="permanent", actor_id=ACTOR, idempotency_key="activation-0000001"
    )
    for index, user in enumerate((USER, OTHER_USER)):
        value.follow_xhs_blogger(
            user_id=user, role="curator", actor_id=ACTOR,
            idempotency_key=f"follow-000000000{index}",
        )
    return value


@pytest.fixture
def supervisor(store: ControlStore) -> ScriptedSupervisor:
    return ScriptedSupervisor(store=store)


def settings(**overrides) -> XhsSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "max_list_pages": 3,
        "drain_units_per_tick": 100,
        "daily_calls": {"tikhub": 100, "ocr": 1000, "gpt": 300},
    }
    values.update(overrides)
    return XhsSettings(**values)


def drain_for(store, supervisor, **overrides) -> XhsDrain:
    handlers = overrides.pop("handlers", None)
    if handlers is not None:
        return XhsDrain(store=store, supervisor=supervisor, settings=settings(**overrides),
                        handlers=handlers)
    return XhsDrain(store=store, supervisor=supervisor, settings=settings(**overrides))


def staging(tmp_path: Path, n: int) -> Path:
    return tmp_path / "xhs-notes" / note_id(n) / "staging"


def tasks(store: ControlStore, kind: str | None = None) -> list[dict[str, Any]]:
    with store._connect() as conn:
        rows = conn.execute("SELECT id FROM xhs_tasks ORDER BY created_at, id").fetchall()
        found = [store._xhs_task(conn, str(row["id"])) for row in rows]
    return [task for task in found if kind is None or task["kind"] == kind]


# -- default-off, roots and the gate -----------------------------------------------


def _due_now(clock: MovableClock) -> None:
    # Migration 21 seeds both rows due at the wall-clock time it ran.
    clock.now = datetime.now(UTC) + timedelta(seconds=1)


def test_the_plugin_is_off_by_default_even_when_its_rows_are_armed(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    _due_now(clock)
    seed_schedules(store, reading=JobsFileReading(path="absent", present=False, jobs=()))
    assert not store.get_research_schedule(XHS_PULL_JOB)["enabled"]
    assert not store.get_research_schedule(XHS_DRAIN_JOB)["enabled"]
    drain = XhsDrain(store=store, supervisor=supervisor, settings=XhsSettings())
    tick = ResearchScheduleTick(store=store, consumer=_NoCaptures(), xhs=drain)
    report = tick.run()
    assert XHS_PULL_JOB not in report.due and XHS_DRAIN_JOB not in report.due
    # Armed rows still refuse while `[xhs] enabled` is false.
    for key in (XHS_PULL_JOB, XHS_DRAIN_JOB):
        schedule = store.get_research_schedule(key)
        store.set_research_schedule_enabled(
            job_key=key, enabled=True, expected_revision=schedule["revision"],
            actor_id=ACTOR, idempotency_key=f"arm-{key}-000001",
        )
    report = tick.run()
    assert report.outcomes[XHS_PULL_JOB] == report.outcomes[XHS_DRAIN_JOB] == "refused"
    assert drain.refusal() == "disabled_in_config"
    assert tasks(store) == [] and supervisor.calls == []


def test_the_tick_runs_both_jobs_once_enabled_and_armed(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    _due_now(clock)
    seed_schedules(store, reading=JobsFileReading(path="absent", present=False, jobs=()))
    for key in (XHS_PULL_JOB, XHS_DRAIN_JOB):
        schedule = store.get_research_schedule(key)
        store.set_research_schedule_enabled(
            job_key=key, enabled=True, expected_revision=schedule["revision"],
            actor_id=ACTOR, idempotency_key=f"arm-{key}-000001",
        )
    tick = ResearchScheduleTick(
        store=store, consumer=_NoCaptures(), xhs=drain_for(store, supervisor)
    )
    report = tick.run()
    # The drain sorts first and finds nothing; the pull creates two scans.
    assert report.outcomes[XHS_DRAIN_JOB] == "skipped"
    assert report.outcomes[XHS_PULL_JOB] == "ran"
    assert sorted(task["subject_key"].split(":")[1] for task in tasks(store)) == [USER, OTHER_USER]


def test_without_both_roots_ready_the_plugin_refuses_and_says_why(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    root = store.get_asset_root("blogs")
    store.update_asset_root(
        root_id="blogs", private_path=root.private_path, max_bytes=root.max_bytes,
        enabled=False, expected_revision=root.revision,
        actor_id=ACTOR, idempotency_key="disable-blogs-0001",
    )
    drain = drain_for(store, supervisor)
    assert drain.refusal() == "roots_not_ready"
    assert drain.run_job("xhs_pull") == ("refused", 0)
    assert tasks(store) == []


def test_a_closed_dispatch_gate_claims_nothing(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    drain = drain_for(store, supervisor)
    drain.pull()
    store.disable_runtime_activation(actor_id=ACTOR, idempotency_key="deactivate-000001")
    assert drain.run_job("xhs_drain") == ("refused", 0)
    assert supervisor.calls == []
    assert {task["state"] for task in tasks(store)} == {"pending"}
    tick = ResearchScheduleTick(store=store, consumer=_NoCaptures(), xhs=drain)
    assert tick.run().gate_enabled is False


def test_a_daemon_shutdown_launches_no_further_child(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    drain = drain_for(store, supervisor)
    drain.pull()
    supervisor.stopping = True
    assert drain.drain().stopped == "stopping"
    assert drain.run_job("xhs_drain") == ("skipped", 0)
    assert supervisor.calls == []
    assert {task["state"] for task in tasks(store)} == {"pending"}


class _NoCaptures:
    def run_once(self):
        return None


# -- scans and pagination ----------------------------------------------------------


def test_pull_creates_one_scan_per_followed_blogger_and_never_two(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    store.follow_xhs_blogger(user_id=QUIET_USER, role="author", actor_id=ACTOR,
                             idempotency_key="follow-0000000009")
    store.unfollow_xhs_blogger(user_id=QUIET_USER, actor_id=ACTOR,
                               idempotency_key="unfollow-00000009")
    drain = drain_for(store, supervisor)
    created = drain.pull()
    assert [task["payload"]["user_id"] for task in created] == [USER, OTHER_USER]
    assert created[0]["payload"]["max_pages"] == 3
    # The previous scan has not run yet: a second pull adds nothing.
    assert drain.pull() == []
    assert len(tasks(store, "list_page")) == 2


def test_pagination_stops_on_a_seen_page_and_ignores_sticky_notes(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock, tmp_path: Path
) -> None:
    drain = drain_for(store, supervisor, handlers=(ListPageHandler(),))
    supervisor.lists.update({
        (USER, ""): page(USER, "", [listed(1, sticky=True), listed(2), listed(3)], True),
        (USER, "cursor-3"): page(USER, "cursor-3", [listed(4), listed(5, note_type="video")], True),
        (USER, "cursor-5"): page(USER, "cursor-5", [listed(6)], True),
        (OTHER_USER, ""): page(OTHER_USER, "", [], False),
    })
    drain.pull(scan_id="first")
    drain.drain()
    # Page three is the limit even though the provider has more.
    pages = sorted(task["subject_key"] for task in tasks(store, "list_page"))
    assert pages == [f"scan:{USER}:first:1", f"scan:{USER}:first:2",
                     f"scan:{USER}:first:3", f"scan:{OTHER_USER}:first:1"]
    assert store.get_xhs_note(note_id(5))["state"] == "unsupported"
    assert store.get_xhs_note(note_id(2))["state"] == "discovered"
    assert len(tasks(store, "detail")) == 5  # every new image note, the sticky one too
    assert store.get_xhs_blogger(USER)["last_scan_outcome"] == "ok"
    assert store.get_xhs_blogger(OTHER_USER)["last_scan_outcome"] == "no_new_notes"

    # Next day: a newly pinned note is new, but sticky, and the rest is seen.
    clock.advance(86_400)
    supervisor.lists[(USER, "")] = page(
        USER, "", [listed(7, sticky=True), listed(2), listed(3)], True
    )
    drain.pull(user_ids=[USER], scan_id="second")
    drain.drain()
    assert not any(task["subject_key"] == f"scan:{USER}:second:2" for task in tasks(store))
    assert store.get_xhs_blogger(USER)["last_scan_outcome"] == "ok"

    # A wholly seen first page is "no new notes", and the scan stops there.
    clock.advance(86_400)
    supervisor.lists[(USER, "")] = page(USER, "", [listed(7, sticky=True), listed(2)], True)
    drain.pull(user_ids=[USER], scan_id="third")
    drain.drain()
    assert not any(task["subject_key"] == f"scan:{USER}:third:2" for task in tasks(store))
    blogger = store.get_xhs_blogger(USER)
    assert (blogger["last_scan_outcome"], blogger["last_scan_error"]) == ("no_new_notes", None)
    raw = (staging(tmp_path, 2) / "raw" / "list.json").read_text(encoding="utf-8")
    assert SIGNATURE not in raw and json.loads(raw)["id"] == note_id(2)


def test_a_full_scan_reads_past_seen_pages_up_to_its_limit(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    drain = drain_for(store, supervisor, handlers=(ListPageHandler(),))
    supervisor.lists[(USER, "")] = page(USER, "", [listed(1)], True)
    drain.pull(user_ids=[USER], scan_id="recent", max_pages=1)
    drain.drain()
    # Page 1 is now wholly seen; only a full scan reads on to page 2.
    supervisor.lists[(USER, "cursor-1")] = page(USER, "cursor-1", [listed(2)], True)
    drain.pull(user_ids=[USER], scan_id="backfill", max_pages=2, full=True)
    drain.drain()
    pages = sorted(
        task["subject_key"] for task in tasks(store, "list_page")
        if ":backfill:" in task["subject_key"]
    )
    assert pages == [f"scan:{USER}:backfill:1", f"scan:{USER}:backfill:2"]
    assert store.get_xhs_note(note_id(2))["state"] == "discovered"
    assert not store.list_xhs_note_images(note_id(2))


def test_a_provider_failure_is_never_no_new_notes(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    drain = drain_for(store, supervisor, handlers=(ListPageHandler(),))
    supervisor.lists.update({(USER, ""): "transient", (OTHER_USER, ""): "upstream_error"})
    drain.pull(scan_id="outage")
    report = drain.drain()
    assert [unit.outcome for unit in report.units] == ["retry", "failed"]
    for user, category in ((USER, "transient"), (OTHER_USER, "upstream_error")):
        blogger = store.get_xhs_blogger(user)
        assert (blogger["last_scan_outcome"], blogger["last_scan_error"]) == ("failed", category)
    # The retry waits ten minutes, then succeeds with nothing new.
    supervisor.lists[(USER, "")] = page(USER, "", [], False)
    assert drain.drain().units == ()
    clock.advance(600)
    drain.drain()
    assert store.get_xhs_blogger(USER)["last_scan_outcome"] == "no_new_notes"
    assert store.get_xhs_blogger(OTHER_USER)["last_scan_outcome"] == "failed"


# -- detail, download and OCR ------------------------------------------------------


def _one_note(supervisor: ScriptedSupervisor, fileids: list[str]) -> None:
    supervisor.lists.update({
        (USER, ""): page(USER, "", [listed(1)], False),
        (OTHER_USER, ""): page(OTHER_USER, "", [], False),
    })
    supervisor.details[note_id(1)] = [detail(1, fileids)]


def _url(fileid: str, tag: str = "v1") -> str:
    return f"https://cdn.example/{fileid}.webp?{SIGNATURE}&v={tag}"


def test_a_failed_image_keeps_its_ordinal_while_later_images_proceed(
    store: ControlStore, supervisor: ScriptedSupervisor, tmp_path: Path
) -> None:
    _one_note(supervisor, ["file-a", "file-b", "file-c"])
    first, third = png(1), png(3)
    supervisor.downloads.update({
        _url("file-a"): first, _url("file-b"): "not_found", _url("file-c"): third,
    })
    supervisor.ocr.update({
        hashlib.sha256(first).hexdigest(): ("# Speculative decoding\narXiv:2601.00042", []),
        hashlib.sha256(third).hexdigest(): ("推荐阅读 Attention sinks", ["truncated"]),
    })
    drain = drain_for(store, supervisor)
    drain.pull()
    drain.drain()
    note = store.get_xhs_note(note_id(1))
    assert note["state"] == "ocr_done"
    assert note["caption_complete"] is True and "one hundred characters" in note["caption"]
    images = store.list_xhs_note_images(note_id(1))
    assert [(i["ordinal"], i["fileid"], i["download_state"], i["ocr_state"]) for i in images] == [
        (1, "file-a", "ok", "ok"), (2, "file-b", "failed", "pending"), (3, "file-c", "ok", "ok"),
    ]
    assert images[1]["download_error"] == "not_found"
    third_hash = hashlib.sha256(third).hexdigest()
    assert images[2]["asset_name"] == f"3-{third_hash[:12]}.png"
    assert images[2]["ocr_flags"] == ["truncated"] and images[2]["ocr_engine"] == "deepseek-ocr-2"
    assert store.get_xhs_blogger(USER)["display_name"] == "合成博主 Synthetic"
    folder = staging(tmp_path, 1)
    assert (folder / f"{third_hash}.png").read_bytes() == third
    assert (folder / "ocr" / "3.md").read_text(encoding="utf-8") == "推荐阅读 Attention sinks"
    assert json.loads((folder / "ocr" / "3.json").read_text())["raw"]
    assert not (folder / "ocr" / "2.md").exists()
    # Signed URLs reach no file and no task result; only private payloads hold them.
    for path in folder.rglob("*.json"):
        assert SIGNATURE not in path.read_text(encoding="utf-8")
    assert all(SIGNATURE not in json.dumps(task["result"]) for task in tasks(store))
    assert supervisor.operations().count("xhs_ocr_image") == 2
    assert store.xhs_usage() == {"gpt": 0, "ocr": 2, "tikhub": 3}
    assert len(supervisor.discarded) == len(supervisor.calls)


def test_an_expired_url_refreshes_the_detail_once_and_matches_by_fileid(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    _one_note(supervisor, ["file-a", "file-b"])
    # The refreshed detail lists the carousel in another order: fileid decides.
    refreshed = detail(1, ["file-b", "file-a"], tag="v2")
    supervisor.details[note_id(1)].append(refreshed)
    supervisor.downloads.update({
        _url("file-a"): png(1), _url("file-b"): "url_expired", _url("file-b", "v2"): png(2),
    })
    supervisor.ocr.update({
        hashlib.sha256(png(1)).hexdigest(): ("first", []),
        hashlib.sha256(png(2)).hexdigest(): ("second", []),
    })
    drain = drain_for(store, supervisor)
    drain.pull()
    drain.drain()
    images = store.list_xhs_note_images(note_id(1))
    assert [(i["ordinal"], i["fileid"], i["download_state"]) for i in images] == [
        (1, "file-a", "ok"), (2, "file-b", "ok"),
    ]
    assert images[1]["sha256"] == hashlib.sha256(png(2)).hexdigest()
    assert supervisor.operations().count("xhs_note_detail") == 2
    assert store.get_xhs_note(note_id(1))["state"] == "ocr_done"


def test_a_second_expiry_after_the_refresh_fails_the_image(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    _one_note(supervisor, ["file-a", "file-b"])
    supervisor.details[note_id(1)].append(detail(1, ["file-a", "file-b"], tag="v2"))
    supervisor.downloads.update({
        _url("file-a"): png(1), _url("file-b"): "url_expired",
        _url("file-b", "v2"): "url_expired",
    })
    supervisor.ocr[hashlib.sha256(png(1)).hexdigest()] = ("first", [])
    drain = drain_for(store, supervisor)
    drain.pull()
    drain.drain()
    image = store.list_xhs_note_images(note_id(1))[1]
    assert (image["download_state"], image["download_error"]) == ("failed", "url_expired")
    assert supervisor.operations().count("xhs_note_detail") == 2
    assert store.get_xhs_note(note_id(1))["state"] == "ocr_done"


def test_an_image_without_a_url_fails_and_its_retry_asks_a_fresh_detail(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    _one_note(supervisor, ["file-a", "file-b"])
    supervisor.details[note_id(1)][0]["note"]["images"][1]["url"] = None
    supervisor.details[note_id(1)].append(detail(1, ["file-a", "file-b"], tag="v2"))
    supervisor.downloads.update({_url("file-a"): png(1), _url("file-b", "v2"): png(2)})
    supervisor.ocr.update({
        hashlib.sha256(png(1)).hexdigest(): ("first", []),
        hashlib.sha256(png(2)).hexdigest(): ("second", []),
    })
    drain = drain_for(store, supervisor)
    drain.pull()
    drain.drain()
    image = store.list_xhs_note_images(note_id(1))[1]
    assert (image["download_state"], image["download_error"]) == ("failed", "invalid_response")
    assert store.get_xhs_note(note_id(1))["state"] == "ocr_done"
    assert supervisor.operations().count("xhs_download_image") == 1
    retried = store.retry_failed_xhs_tasks(
        kinds=["download"], actor_id=ACTOR, idempotency_key="retry-download-00001"
    ).value
    assert retried == {"retried": {"download": 1}, "skipped": 0}
    drain.drain()
    image = store.list_xhs_note_images(note_id(1))[1]
    assert (image["download_state"], image["sha256"]) == (
        "ok", hashlib.sha256(png(2)).hexdigest(),
    )
    assert supervisor.operations().count("xhs_note_detail") == 2


def test_an_inconsistent_answer_is_an_invalid_response(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    _one_note(supervisor, ["file-a"])
    broken = detail(1, ["file-a"])
    broken["note"]["note_id"] = note_id(9)
    supervisor.details[note_id(1)] = [broken]
    drain = drain_for(store, supervisor)
    drain.pull()
    drain.drain()
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["last_error"]) == ("failed", "invalid_response")
    assert store.list_xhs_note_images(note_id(1)) == []


# -- caps, stops, leases and re-runs -------------------------------------------------


def test_a_capped_provider_waits_for_the_next_day(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    supervisor.lists.update({
        (USER, ""): page(USER, "", [], False), (OTHER_USER, ""): page(OTHER_USER, "", [], False),
    })
    drain = drain_for(store, supervisor, daily_calls={"tikhub": 1, "ocr": 1000, "gpt": 300})
    drain.pull()
    report = drain.drain()
    assert [unit.outcome for unit in report.units] == ["done"]
    assert store.xhs_usage()["tikhub"] == 1
    assert len(supervisor.calls) == 1
    assert drain.drain().units == ()
    clock.advance(86_400)
    assert [unit.outcome for unit in drain.drain().units] == ["done"]
    assert store.get_xhs_blogger(OTHER_USER)["last_scan_outcome"] == "no_new_notes"


def test_auth_stops_the_tick_fails_the_task_and_is_not_billed(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    supervisor.lists.update({(USER, ""): "auth", (OTHER_USER, ""): page(OTHER_USER, "", [], False)})
    drain = drain_for(store, supervisor)
    drain.pull()
    assert drain.run_job("xhs_drain") == ("failed", 1)
    first, second = tasks(store, "list_page")
    assert (first["state"], first["last_error"]) == ("failed", "auth")
    assert second["state"] == "pending"
    assert store.xhs_usage()["tikhub"] == 0
    assert store.get_xhs_blogger(USER)["last_scan_error"] == "auth"


def test_a_refund_after_utc_midnight_goes_back_to_the_day_it_was_reserved_on(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor.lists.update({(USER, ""): "auth", (OTHER_USER, ""): page(OTHER_USER, "", [], False)})
    drain = drain_for(store, supervisor, daily_calls={"tikhub": 1, "ocr": 1000, "gpt": 300})
    drain.pull()
    clock.advance(12 * 3600 - 1)
    run = supervisor.run

    def across_midnight(operation, payload, *, write_roots=None):
        clock.advance(2)
        # Another drain's billed call lands on the new day meanwhile.
        assert store.reserve_xhs_usage("tikhub", cap=1) is True
        return run(operation, payload, write_roots=write_roots)

    monkeypatch.setattr(supervisor, "run", across_midnight)
    drain.drain()
    assert store.xhs_usage("2026-09-01")["tikhub"] == 0
    assert store.xhs_usage("2026-09-02")["tikhub"] == 1
    assert store.reserve_xhs_usage("tikhub", cap=1) is False


def test_an_unresolvable_credential_is_handled_like_auth(
    store: ControlStore, supervisor: ScriptedSupervisor
) -> None:
    def refuse(*args, **kwargs):
        raise EffectPermanentlyRejected("adapter_unavailable")

    supervisor.run = refuse  # type: ignore[method-assign]
    drain = drain_for(store, supervisor)
    drain.pull()
    report = drain.drain()
    assert len(report.units) == 1 and report.stopped == "auth"
    assert store.xhs_usage()["tikhub"] == 0


def _detail_done(store: ControlStore, supervisor: ScriptedSupervisor) -> None:
    _one_note(supervisor, ["file-a"])
    supervisor.downloads[_url("file-a")] = png(1)
    supervisor.ocr[hashlib.sha256(png(1)).hexdigest()] = ("first", [])
    drain = drain_for(store, supervisor, handlers=(ListPageHandler(), DetailHandler()))
    drain.pull()
    drain.drain()


def test_an_expired_lease_is_recovered_by_the_next_drain(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    _detail_done(store, supervisor)
    # A holder crashes mid-download and never records; its lease lapses.
    crashed = store.claim_xhs_task(lease_seconds=60, kinds=["download"])
    # While the lease holds, nobody else runs the task.
    assert drain_for(store, supervisor).drain().units == ()
    clock.advance(61)
    drain_for(store, supervisor).drain()
    task = store.get_xhs_task(crashed["id"])
    assert (task["state"], task["attempts"]) == ("done", 2)
    assert store.get_xhs_note(note_id(1))["state"] == "ocr_done"


def test_a_lease_that_keeps_expiring_fails_the_image_and_the_note_moves_on(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    _detail_done(store, supervisor)
    for _ in range(4):
        assert store.claim_xhs_task(lease_seconds=60, kinds=["download"]) is not None
        clock.advance(61)
    assert drain_for(store, supervisor).drain().units == ()
    image = store.list_xhs_note_images(note_id(1))[0]
    assert (image["download_state"], image["download_error"]) == ("failed", "outcome_unknown")
    assert store.get_xhs_note(note_id(1))["state"] == "ocr_done"
    assert "xhs_download_image" not in supervisor.operations()


def test_a_re_run_changes_nothing_that_is_already_done(
    store: ControlStore, supervisor: ScriptedSupervisor, clock: MovableClock
) -> None:
    _one_note(supervisor, ["file-a"])
    supervisor.downloads[_url("file-a")] = png(1)
    supervisor.ocr[hashlib.sha256(png(1)).hexdigest()] = ("first", [])
    drain = drain_for(store, supervisor)
    drain.pull()
    drain.drain()
    before = {task["subject_key"]: task["revision"] for task in tasks(store)}
    images = store.list_xhs_note_images(note_id(1))
    calls = len(supervisor.calls)
    assert drain.drain().units == ()
    clock.advance(86_400)
    drain.pull()
    drain.drain()
    after = {task["subject_key"]: task["revision"] for task in tasks(store)}
    new = set(after) - set(before)
    assert all(key.startswith("scan:") for key in new) and len(new) == 2
    assert {key: after[key] for key in before} == before
    assert store.list_xhs_note_images(note_id(1)) == images
    assert supervisor.operations()[calls:] == ["xhs_list_page", "xhs_list_page"]
    assert store.get_xhs_blogger(USER)["last_scan_outcome"] == "no_new_notes"
