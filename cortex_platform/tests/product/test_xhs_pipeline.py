"""The XHS pipeline after OCR: identification, link search, saves, imports and retries.

The scripted supervisor from the acquisition tests answers each child; for
identification it runs the real rules, verbatim filter and merge over a
scripted model answer, as the child does. Every identity, caption, image and
page is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.control.errors import RevisionConflict
from cortex_platform.product.sources.identity import blog_url_identity
from cortex_platform.product.xhs import identify
from cortex_platform.product.xhs.drain import PIPELINE_HANDLERS, XhsDrain
from cortex_platform.product.xhs.layout import read_tree, tree_sha256, write_version
from cortex_platform.tests.product.test_xhs_drain import (  # noqa: F401 - fixtures
    ACTOR,
    OTHER_USER,
    SIGNATURE,
    USER,
    Execution,
    ScriptedSupervisor,
    _url,
    clock,
    detail,
    listed,
    note_id,
    page,
    png,
    settings,
    staging,
    store,
    tasks,
)

PAPER_TEXT = "# Speculative decoding\narXiv:2601.00042 Fast Inference from Transformers"
BLOG_TEXT = "推荐阅读 Attention Sinks 博客, by Example Lab"
CAPTION_BLOG = "Blog: Efficient Streaming https://blog.example/streaming"


@dataclass
class PipelineSupervisor(ScriptedSupervisor):
    """Adds identification, link search and blog fetch to the scripted child.

    `identify` maps the first transcription's text (or the caption, when no
    image was transcribed) to a queue of model answers or failure categories;
    `links` maps a title to a link answer; `blogs` maps a URL to an article.
    """

    identify: dict[str, list[Any]] = field(default_factory=dict)
    links: dict[str, Any] = field(default_factory=dict)
    blogs: dict[str, Any] = field(default_factory=dict)

    def run(self, operation: str, payload, *, write_roots=None) -> Execution:
        if operation not in {"xhs_identify", "xhs_resolve_link", "blog_fetch"}:
            return super().run(operation, payload, write_roots=write_roots)
        self.calls.append((operation, dict(payload), write_roots))
        marker = f"marker-{len(self.calls)}"
        if operation == "xhs_identify":
            assert write_roots is None and payload["gpt_model"] == "gpt-6-luna"
            pairs = [(entry["image"], entry["text"]) for entry in payload["transcriptions"]]
            key = pairs[0][1] if pairs else payload["caption"]
            answer = self.identify[key].pop(0)
            if not isinstance(answer, str):
                outcome = identify.identify(payload["caption"], pairs, answer)
                answer = {
                    "prompt_version": identify.PROMPT_VERSION,
                    "input_sha256": identify.input_sha256(payload["caption"], pairs),
                    "response_id": "resp-synthetic", "model": "gpt-6-luna", "usage": None,
                    "model_items": answer, "items": [dict(item) for item in outcome.items],
                    "dropped": outcome.dropped, "rule_items": outcome.rule_items,
                }
        elif operation == "xhs_resolve_link":
            answer = self.links[payload["title"]]
        else:
            staging_dir = Path(payload["staging_dir"])
            assert write_roots and staging_dir.is_relative_to(write_roots[0])
            assert list(staging_dir.iterdir()) == []
            answer = self.blogs[payload["url"]]
            if isinstance(answer, tuple):
                title, body = answer
                normalized, authority_id = blog_url_identity(payload["url"])
                article = f"# {title}\n\n{body}\n".encode()
                html = f"<html><title>{title}</title><p>{body}</p></html>".encode()
                files = {}
                for name, data in (("article.md", article), ("raw/page.html", html)):
                    (staging_dir / name).parent.mkdir(parents=True, exist_ok=True)
                    (staging_dir / name).write_bytes(data)
                    files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                answer = {
                    "normalized_url": normalized, "authority_id": authority_id,
                    "metadata": {
                        "requested_url": payload["url"], "final_url": normalized,
                        "content_source": "origin", "title": title, "author": None,
                        "date": None, "characters": len(article), "raw_html": True,
                        "raw_jina": False, "origin_failure": None,
                    },
                    "files": files,
                }
        if isinstance(answer, str):
            return Execution(False, None, answer, marker)
        return Execution(True, answer, None, marker)


@pytest.fixture
def supervisor(store: ControlStore) -> PipelineSupervisor:
    return PipelineSupervisor(store=store)


def pipeline(store, supervisor, **overrides) -> XhsDrain:
    return XhsDrain(
        store=store, supervisor=supervisor, settings=settings(**overrides),
        handlers=PIPELINE_HANDLERS,
    )


def model_items(*, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [
        {"kind": "paper", "title": "Fast Inference from Transformers", "image": 1,
         "quote": "arXiv:2601.00042 Fast Inference from Transformers",
         "arxiv_id": "2601.00042", "url": None},
        {"kind": "blog", "title": "Attention Sinks", "image": 3,
         "quote": "推荐阅读 Attention Sinks 博客", "arxiv_id": None, "url": None},
        {"kind": "blog", "title": "Efficient Streaming", "image": None,
         "quote": CAPTION_BLOG, "arxiv_id": None,
         "url": "https://blog.example/streaming"},
        # Not written anywhere: a guess the verbatim filter drops.
        {"kind": "paper", "title": "Imaginary Results", "image": 3,
         "quote": "a paper this note never names", "arxiv_id": None, "url": None},
        # Cites the image whose download failed: there is no text to check.
        {"kind": "other", "title": "Attention Sinks", "image": 2,
         "quote": "Attention Sinks", "arxiv_id": None, "url": None},
        *(extra or []),
    ]


def with_caption(answer: dict[str, Any], caption: str) -> dict[str, Any]:
    answer["note"]["caption"] = caption
    return answer


def _three_images(supervisor: PipelineSupervisor, n: int = 1) -> None:
    supervisor.lists.update({
        (USER, ""): page(USER, "", [listed(n)], False),
        (OTHER_USER, ""): page(OTHER_USER, "", [], False),
    })
    supervisor.details[note_id(n)] = [
        with_caption(detail(n, ["file-a", "file-b", "file-c"]), CAPTION_BLOG)
    ]
    supervisor.downloads.update({
        _url("file-a"): png(1), _url("file-b"): "not_found", _url("file-c"): png(3),
    })
    supervisor.ocr.update({
        hashlib.sha256(png(1)).hexdigest(): (PAPER_TEXT, []),
        hashlib.sha256(png(3)).hexdigest(): (BLOG_TEXT, ["truncated"]),
    })
    supervisor.links["Attention Sinks"] = {
        "prompt_version": identify.LINK_PROMPT_VERSION, "response_id": "resp-link",
        "usage": None, "url": "https://blog.example/attention-sinks",
        "page_title": "Attention Sinks | Example Lab", "url_state": "auto_matched",
        "final_url": "https://blog.example/attention-sinks",
        "checked_title": "Attention Sinks | Example Lab", "verification_failure": None,
    }


def _saved(store: ControlStore, supervisor: PipelineSupervisor) -> dict[str, Any]:
    _three_images(supervisor)
    supervisor.identify[PAPER_TEXT] = [model_items()]
    drain = pipeline(store, supervisor)
    drain.pull()
    drain.drain()
    return store.get_xhs_note(note_id(1))


def _version_dir(tmp_path: Path, n: int, version: int) -> Path:
    return tmp_path / "xhs-notes" / note_id(n) / f"v{version}"


# -- identification ----------------------------------------------------------------


def test_identification_keeps_verbatim_items_merges_rules_and_saves_version_one(
    store: ControlStore, supervisor: PipelineSupervisor, tmp_path: Path
) -> None:
    note = _saved(store, supervisor)
    assert (note["state"], note["content_version"]) == ("saved", 1)
    recommendations = {r["title"]: r for r in store.list_xhs_recommendations(note_id(1))}
    assert sorted(recommendations) == [
        "Attention Sinks", "Efficient Streaming", "Fast Inference from Transformers",
    ]
    paper = recommendations["Fast Inference from Transformers"]
    assert (paper["origin"], paper["arxiv_id"], paper["image_ordinal"]) == (
        "rule+model", "2601.00042", 1,
    )
    streaming = recommendations["Efficient Streaming"]
    assert (streaming["url_state"], streaming["url"], streaming["image_ordinal"]) == (
        "from_text", "https://blog.example/streaming", None,
    )
    # A blog without a written link was searched for and its page title checked.
    sinks = recommendations["Attention Sinks"]
    assert (sinks["url_state"], sinks["url"], sinks["url_checked_title"]) == (
        "auto_matched", "https://blog.example/attention-sinks", "Attention Sinks | Example Lab",
    )
    run = tasks(store, "identify")[0]
    assert run["result"]["dropped"] == 2 and run["result"]["model_items"] == 5
    assert run["result"]["prompt_version"] == identify.PROMPT_VERSION
    assert all(r["identify_run"] == run["id"] for r in recommendations.values())
    assert [t["subject_key"] for t in tasks(store, "resolve")] == [f"resolve:{sinks['id']}:1"]
    assert store.xhs_usage()["gpt"] == 2
    # The failed image's text was never sent: only images 1 and 3.
    sent = [payload for op, payload, _ in supervisor.calls if op == "xhs_identify"][0]
    assert [entry["image"] for entry in sent["transcriptions"]] == [1, 3]


def test_the_saved_version_has_the_layout_and_its_digest_is_recorded(
    store: ControlStore, supervisor: PipelineSupervisor, tmp_path: Path
) -> None:
    note = _saved(store, supervisor)
    binding = store.latest_content_binding(note["source_id"])
    assert (binding["version"], binding["root_id"], binding["directory"]) == (
        1, "xhs-notes", f"{note_id(1)}/v1",
    )
    source = store.get_source(note["source_id"])
    assert (source["source_kind"], source["authority_id"]) == ("xhs_note", note_id(1))
    folder = _version_dir(tmp_path, 1, 1)
    tree = read_tree(folder)
    assert binding["tree_sha256"] == tree_sha256(tree)
    first, third = (hashlib.sha256(png(seed)).hexdigest() for seed in (1, 3))
    assert sorted(tree) == sorted([
        "note.md", "transcription.md", f"assets/1-{first[:12]}.png",
        f"assets/3-{third[:12]}.png", "ocr/1.json", "ocr/3.json",
        "raw/list.json", "raw/detail.json",
    ])
    assert tree[f"assets/3-{third[:12]}.png"] == png(3)
    transcription = tree["transcription.md"].decode()
    assert transcription.index("## Image 1") < transcription.index("## Image 2") < (
        transcription.index("## Image 3")
    )
    assert "Download failed (not_found)." in transcription
    assert BLOG_TEXT in transcription and "Flags: truncated" in transcription
    note_md = tree["note.md"].decode()
    assert "合成博主 Synthetic (curator)" in note_md and CAPTION_BLOG in note_md
    assert f"https://www.xiaohongshu.com/explore/{note_id(1)}" in note_md
    assert "Link: <https://blog.example/attention-sinks> (auto_matched)" in note_md
    assert all(SIGNATURE.encode() not in data for data in tree.values())
    assert not list(folder.parent.glob(".v*"))


def test_a_model_failure_never_yields_an_empty_list_and_retry_recovers(
    store: ControlStore, supervisor: PipelineSupervisor
) -> None:
    _three_images(supervisor)
    supervisor.identify[PAPER_TEXT] = ["invalid_response", model_items()]
    drain = pipeline(store, supervisor)
    drain.pull()
    drain.drain()
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["last_error"], note["source_id"]) == (
        "ocr_done", "invalid_response", None,
    )
    assert store.list_xhs_recommendations(note_id(1)) == []
    assert tasks(store, "save") == [] and tasks(store, "identify")[0]["state"] == "failed"

    retried = store.retry_failed_xhs_tasks(
        kinds=["identify"], actor_id=ACTOR, idempotency_key="retry-identify-0001"
    ).value
    assert retried == {"retried": {"identify": 1}, "skipped": 0}
    drain.drain()
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["last_error"]) == ("saved", None)
    assert len(store.list_xhs_recommendations(note_id(1))) == 3


def test_a_link_search_that_finds_nothing_is_not_found_and_a_failure_is_shown(
    store: ControlStore, supervisor: PipelineSupervisor
) -> None:
    _three_images(supervisor)
    supervisor.identify[PAPER_TEXT] = [model_items()]
    supervisor.links["Attention Sinks"] = "payment"
    drain = pipeline(store, supervisor)
    drain.pull()
    report = drain.drain()
    assert report.stopped == "payment"
    sinks = [r for r in store.list_xhs_recommendations(note_id(1)) if r["kind"] == "blog"]
    assert {r["url_state"] for r in sinks} == {"failed", "from_text"}
    # The save is not held up by a link search.
    drain.drain()
    assert store.get_xhs_note(note_id(1))["state"] == "saved"
    supervisor.links["Attention Sinks"] = {
        "prompt_version": identify.LINK_PROMPT_VERSION, "response_id": "resp-link",
        "usage": None, "url": None, "page_title": None, "url_state": "not_found",
        "final_url": None, "checked_title": None, "verification_failure": None,
    }
    store.retry_failed_xhs_tasks(
        kinds=["resolve"], actor_id=ACTOR, idempotency_key="retry-resolve-00001"
    )
    drain.drain()
    sinks = store.get_xhs_recommendation(tasks(store, "resolve")[0]["payload"]["recommendation_id"])
    assert (sinks["url_state"], sinks["url"]) == ("not_found", None)


# -- versions, retries and crashes ---------------------------------------------------


def test_retrying_a_failed_image_writes_version_two_and_keeps_version_one(
    store: ControlStore, supervisor: PipelineSupervisor, tmp_path: Path
) -> None:
    note = _saved(store, supervisor)
    v1 = read_tree(_version_dir(tmp_path, 1, 1))
    with pytest.raises(RevisionConflict):
        store.retry_xhs_image(
            note_id=note_id(1), ordinal=2, expected_revision=note["revision"] - 1,
            actor_id=ACTOR, idempotency_key="retry-image-000001",
        )
    # The old signed URL has expired: one fresh detail serves a new one.
    supervisor.downloads[_url("file-b")] = "url_expired"
    supervisor.details[note_id(1)].append(
        with_caption(detail(1, ["file-a", "file-b", "file-c"], tag="v2"), CAPTION_BLOG)
    )
    supervisor.downloads[_url("file-b", "v2")] = png(2)
    supervisor.ocr[hashlib.sha256(png(2)).hexdigest()] = ("第二张 image two", [])
    supervisor.identify[PAPER_TEXT] = [model_items()]
    retried = store.retry_xhs_image(
        note_id=note_id(1), ordinal=2, expected_revision=note["revision"],
        actor_id=ACTOR, idempotency_key="retry-image-000001",
    )
    assert retried.value["note"]["state"] == "detail_ok"
    assert retried.value["image"]["download_state"] == "pending"
    replay = store.retry_xhs_image(
        note_id=note_id(1), ordinal=2, expected_revision=note["revision"],
        actor_id=ACTOR, idempotency_key="retry-image-000001",
    )
    assert replay.value == retried.value
    pipeline(store, supervisor).drain()
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["content_version"]) == ("saved", 2)
    image = store.list_xhs_note_images(note_id(1))[1]
    assert (image["download_state"], image["ocr_state"]) == ("ok", "ok")
    assert read_tree(_version_dir(tmp_path, 1, 1)) == v1
    v2 = read_tree(_version_dir(tmp_path, 1, 2))
    assert "第二张 image two" in v2["transcription.md"].decode()
    assert store.latest_content_binding(note["source_id"])["tree_sha256"] == tree_sha256(v2)
    # A new transcription is a new input: identification ran once more.
    assert len(tasks(store, "identify")) == 2
    assert len(store.list_xhs_recommendations(note_id(1))) == 3


def test_an_image_that_fails_again_after_a_retry_reuses_its_identification(
    store: ControlStore, supervisor: PipelineSupervisor, tmp_path: Path
) -> None:
    note = _saved(store, supervisor)
    calls = supervisor.operations().count("xhs_identify")
    store.retry_failed_xhs_tasks(
        kinds=["download"], actor_id=ACTOR, idempotency_key="retry-download-001"
    )
    assert store.get_xhs_note(note_id(1))["state"] == "detail_ok"
    pipeline(store, supervisor).drain()
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["content_version"]) == ("saved", 2)
    assert supervisor.operations().count("xhs_identify") == calls


def test_a_crash_between_the_file_write_and_the_registration_reuses_the_version(
    store: ControlStore, supervisor: PipelineSupervisor, clock, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _three_images(supervisor)
    supervisor.identify[PAPER_TEXT] = [model_items()]
    record = store.record_xhs_task_result

    def crash_on_save(task_id, **arguments):
        if store.get_xhs_task(task_id)["kind"] == "save":
            raise RuntimeError("cortexd stopped")
        return record(task_id, **arguments)

    monkeypatch.setattr(store, "record_xhs_task_result", crash_on_save)
    drain = pipeline(store, supervisor)
    drain.pull()
    with pytest.raises(RuntimeError):
        drain.drain()
    folder = _version_dir(tmp_path, 1, 1)
    written = read_tree(folder)
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["source_id"]) == ("identified", None)
    monkeypatch.setattr(store, "record_xhs_task_result", record)
    clock.advance(901)
    drain.drain()
    note = store.get_xhs_note(note_id(1))
    assert (note["state"], note["content_version"]) == ("saved", 1)
    assert store.latest_content_binding(note["source_id"])["tree_sha256"] == tree_sha256(written)
    assert read_tree(folder) == written
    assert sorted(path.name for path in folder.parent.iterdir()) == ["staging", "v1"]
    assert tasks(store, "save")[0]["attempts"] == 2


def test_an_unregistered_version_with_other_content_is_replaced(tmp_path: Path) -> None:
    write_version(tmp_path, 1, {"note.md": b"left by a crash\n"})
    digest = write_version(tmp_path, 1, {"note.md": b"current\n", "assets/1-x.png": b"\x89PNG"})
    assert read_tree(tmp_path / "v1") == {"note.md": b"current\n", "assets/1-x.png": b"\x89PNG"}
    assert digest == tree_sha256(read_tree(tmp_path / "v1"))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["v1"]
    with pytest.raises(ValueError):
        write_version(tmp_path, 2, {"../escape.md": b""})


# -- imports ---------------------------------------------------------------------------


def _two_notes_recommending_one_blog(
    store: ControlStore, supervisor: PipelineSupervisor
) -> list[dict[str, Any]]:
    supervisor.lists.update({
        (USER, ""): page(USER, "", [listed(1), listed(2)], False),
        (OTHER_USER, ""): page(OTHER_USER, "", [], False),
    })
    url = "https://Blog.Example:443/attention-sinks#top"
    for n, fileid in ((1, "file-a"), (2, "file-z")):
        text = f"笔记 {n}: Attention Sinks {url}"
        supervisor.details[note_id(n)] = [detail(n, [fileid])]
        supervisor.downloads[_url(fileid)] = png(10 + n)
        supervisor.ocr[hashlib.sha256(png(10 + n)).hexdigest()] = (text, [])
        supervisor.identify[text] = [[
            {"kind": "blog", "title": "Attention Sinks", "image": 1,
             "quote": text, "arxiv_id": None, "url": url},
        ]]
    supervisor.blogs["https://blog.example/attention-sinks"] = (
        "Attention Sinks", "Streaming language models keep the first tokens.",
    )
    drain = pipeline(store, supervisor)
    drain.pull()
    drain.drain()
    return [store.list_xhs_recommendations(note_id(n))[0] for n in (1, 2)]


def test_two_notes_importing_one_blog_share_one_source_and_get_two_links(
    store: ControlStore, supervisor: PipelineSupervisor, tmp_path: Path
) -> None:
    recommendations = _two_notes_recommending_one_blog(store, supervisor)
    assert [r["url_state"] for r in recommendations] == ["from_text", "from_text"]
    with store._transaction() as conn:
        store._xhs_queue_blog_import(conn, recommendations[0])
    pipeline(store, supervisor).drain()
    first = store.get_xhs_recommendation(recommendations[0]["id"])
    assert first["import_state"] == "imported"
    with store._transaction() as conn:
        store._xhs_queue_blog_import(
            conn, store._xhs_recommendation(conn, recommendations[1]["id"])
        )
    pipeline(store, supervisor).drain()
    second = store.get_xhs_recommendation(recommendations[1]["id"])
    assert second["imported_source_id"] == first["imported_source_id"]
    blog_id = first["imported_source_id"]
    source = store.get_source(blog_id)
    _, authority_id = blog_url_identity("https://blog.example/attention-sinks")
    assert (source["source_kind"], source["authority_id"]) == ("blog", authority_id)
    assert source["official_title"] == "Attention Sinks"
    links = store.list_source_links(blog_id)["recommended_in"]
    assert sorted(link["recommendation_id"] for link in links) == sorted(
        r["id"] for r in recommendations
    )
    # The second import wrote the blog's next version, naming both notes.
    binding = store.latest_content_binding(blog_id)
    assert (binding["version"], binding["directory"]) == (2, f"{authority_id[:16]}/v2")
    assert binding["metadata"]["raw_html"] is True
    assert binding["metadata"]["normalized_url"] == "https://blog.example/attention-sinks"
    tree = read_tree(tmp_path / "blogs" / authority_id[:16] / "v2")
    assert binding["tree_sha256"] == tree_sha256(tree)
    shots = [f"assets/1-{hashlib.sha256(png(10 + n)).hexdigest()[:12]}.png" for n in (1, 2)]
    assert sorted(tree) == sorted(["article.md", "notes.md", "raw/page.html", *shots])
    notes = tree["notes.md"].decode()
    assert "笔记 1: Attention Sinks" in notes and "笔记 2: Attention Sinks" in notes
    assert "Not peer-reviewed" in notes
    assert len(list((tmp_path / "blogs").iterdir())) == 1


def test_a_failed_blog_fetch_marks_the_import_failed_and_retry_imports_it(
    store: ControlStore, supervisor: PipelineSupervisor
) -> None:
    recommendation = _two_notes_recommending_one_blog(store, supervisor)[0]
    supervisor.blogs["https://blog.example/attention-sinks"] = "not_found"
    with store._transaction() as conn:
        task = store._xhs_queue_blog_import(conn, recommendation)
    assert task["subject_key"] == f"blog:{recommendation['id']}:1"
    pipeline(store, supervisor).drain()
    assert store.get_xhs_recommendation(recommendation["id"])["import_state"] == "failed"
    supervisor.blogs["https://blog.example/attention-sinks"] = ("Attention Sinks", "Body.")
    store.retry_failed_xhs_tasks(
        kinds=["blog_import"], actor_id=ACTOR, idempotency_key="retry-blog-0000001"
    )
    pipeline(store, supervisor).drain()
    assert store.get_xhs_recommendation(recommendation["id"])["import_state"] == "imported"


def _paper_capture(store: ControlStore, supervisor: PipelineSupervisor) -> tuple[dict, str]:
    note = _saved(store, supervisor)
    paper = next(
        r for r in store.list_xhs_recommendations(note_id(1)) if r["kind"] == "paper"
    )
    capture = store.create_capture(
        payload="https://arxiv.org/abs/2601.00042",
        note=f"Recommended in XHS note {note['title']} · image 1",
        actor_id=ACTOR, idempotency_key="capture-xhs-000001",
    ).value
    with store._transaction() as conn:
        task = store._xhs_queue_capture_link(conn, paper, capture["id"])
    assert task["subject_key"] == f"capture:{capture['id']}"
    return paper, capture["id"]


def _end_capture(store: ControlStore, capture_id: str, state: str, sources: list[str]) -> None:
    # The Capture consumer's own path is tested with it; here only its end state matters.
    with store._transaction() as conn:
        conn.execute(
            "UPDATE captures SET state = ?, consumed_source_ids = ? WHERE id = ?",
            (state, json.dumps(sources) if sources else None, capture_id),
        )


def test_capture_link_waits_for_the_capture_then_links_its_paper(
    store: ControlStore, supervisor: PipelineSupervisor, clock
) -> None:
    paper, capture_id = _paper_capture(store, supervisor)
    report = pipeline(store, supervisor).drain()
    assert [unit.outcome for unit in report.units] == ["deferred"]
    task = tasks(store, "capture_link")[0]
    assert (task["state"], task["attempts"]) == ("pending", 0)
    assert pipeline(store, supervisor).drain().units == ()
    source = store.register_source(
        authority="arxiv", authority_id="2601.00042", source_kind="paper",
        official_title="Fast Inference from Transformers",
        engine_ref="paper:20261005-Fast_Inference", actor_id=ACTOR,
        idempotency_key="paper-source-000001",
    ).value
    _end_capture(store, capture_id, "consumed", [source["id"]])
    clock.advance(601)
    pipeline(store, supervisor).drain()
    linked = store.get_xhs_recommendation(paper["id"])
    assert (linked["import_state"], linked["imported_source_id"]) == ("imported", source["id"])
    note_source = store.get_xhs_note(note_id(1))["source_id"]
    assert [link["source_id"] for link in store.list_source_links(source["id"])[
        "recommended_in"
    ]] == [note_source]
    assert tasks(store, "capture_link")[0]["result"] == {
        "capture_state": "consumed", "linked": 1, "failed": 0,
    }


def test_a_failed_capture_marks_the_import_failed(
    store: ControlStore, supervisor: PipelineSupervisor
) -> None:
    paper, capture_id = _paper_capture(store, supervisor)
    _end_capture(store, capture_id, "failed", [])
    pipeline(store, supervisor).drain()
    failed = store.get_xhs_recommendation(paper["id"])
    assert (failed["import_state"], failed["imported_source_id"]) == ("failed", None)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as conn:
            conn.execute(
                """INSERT INTO source_links
                   (id, from_source_id, to_source_id, relation, recommendation_id, created_at)
                   VALUES ('x', 'a', 'b', 'recommends', ?, 'now')""",
                (paper["id"],),
            )
