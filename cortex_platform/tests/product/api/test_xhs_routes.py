"""The XHS read routes and the import, link and retry commands of the Control API.

Notes are produced by the scripted pipeline from the drain tests, so every
identity, caption and image is synthetic. Paper imports stage Captures through
the store's own capture creation; approval stays the Capture route's.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.config import XhsSettings
from cortex_platform.product.control import ControlStore
from cortex_platform.tests.product.test_xhs_pipeline import (  # noqa: F401 - fixtures
    ACTOR,
    SIGNATURE,
    USER,
    PipelineSupervisor,
    _end_capture,
    _saved,
    _two_notes_recommending_one_blog,
    clock,
    note_id,
    pipeline,
    png,
    store,
    supervisor,
    tasks,
)

from .test_sources import TOKEN, _headers

PAPER_URL = "https://arxiv.org/abs/2601.00042"
RECOMMENDATION_FIELDS = {
    "id", "image_ordinal", "kind", "title", "quote", "arxiv_id", "url", "url_state",
    "url_checked_title", "origin", "identify_run", "capture_id", "capture_state",
    "capture_revision", "import_state", "imported_source_id", "imported_source_kind",
    "review", "revision", "created_at", "updated_at",
}
REVIEW_FIELDS = {"state", "method", "reason_code", "reason", "corrected_fields", "updated_at"}
NEEDS_OPERATOR_FIELDS = {
    "note_source_id", "note_title", "recommendation_id", "kind", "title", "reason_code",
    "reason", "updated_at",
}
NOTE_HEADER_FIELDS = {
    "source_id", "note_id", "title", "state", "last_error", "content_version", "revision",
}


def _api(store: ControlStore, **kwargs: Any) -> ControlAPI:
    return ControlAPI(store, access_token=TOKEN, **kwargs)


def get(api: ControlAPI, target: str, **kwargs: Any):
    return api.handle(method="GET", target=target, headers=kwargs.pop("headers", _headers()))


def post(api: ControlAPI, target: str, body: dict[str, Any], key: str, **kwargs: Any):
    return api.handle(
        method="POST",
        target=target,
        headers=kwargs.pop("headers", _headers(key=key)),
        body=json.dumps(body).encode(),
    )


@pytest.fixture
def saved(store: ControlStore, supervisor: PipelineSupervisor) -> dict[str, Any]:
    """One saved note: image 2 failed to download; a paper on image 1, a
    searched blog on image 3 and a blog with a written link in the caption."""

    return _saved(store, supervisor)


def _recommendations(store: ControlStore, n: int = 1) -> dict[str, dict[str, Any]]:
    return {r["title"]: r for r in store.list_xhs_recommendations(note_id(n))}


def _extra_recommendations(store: ControlStore) -> dict[str, str]:
    """Rows the import must refuse: an `other` item, a blog without a link and
    a paper named by title only."""

    rows = {}
    with store._transaction() as conn:
        for key, kind, title in (
            ("other:1", "other", "A synthetic talk"),
            ("blog:none", "blog", "A synthetic blog without a link"),
            ("paper:title", "paper", "A synthetic paper title"),
        ):
            rows[kind] = store._xhs_upsert_recommendation(
                conn, note_id=note_id(1), item_key=key, image_ordinal=1, kind=kind,
                title=title, quote=title, arxiv_id=None, url=None, url_state="none",
                origin="model", identify_run="synthetic-run",
            )["id"]
    return rows


def _paper_source(store: ControlStore) -> dict[str, Any]:
    return store.register_source(
        authority="arxiv", authority_id="2601.00042", source_kind="paper",
        official_title="Fast Inference from Transformers",
        engine_ref="paper:20261005-Fast_Inference", actor_id=ACTOR,
        idempotency_key="paper-source-api-01",
    ).value


# -- read routes -------------------------------------------------------------------


def test_note_route_projects_the_saved_note_without_private_data(
    store: ControlStore, saved: dict[str, Any], tmp_path: Path
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    response = get(api, f"/api/v1/sources/{sid}/note")
    assert response.status == 200
    assert ("Cache-Control", "no-store") in response.headers
    note = response.payload
    assert note["source_id"] == sid and note["note_id"] == note_id(1)
    assert (note["state"], note["content_version"], note["revision"]) == (
        "saved", 1, saved["revision"],
    )
    # The detail named the blogger.
    assert note["blogger"] == {"user_id": USER, "name": "合成博主 Synthetic", "role": "curator"}
    assert note["permalink"] == f"https://www.xiaohongshu.com/explore/{note_id(1)}"
    assert note["caption_complete"] is True and note["caption"] == saved["caption"]
    assert note["published_at"] == saved["published_at"]

    images = {image["ordinal"]: image for image in note["images"]}
    assert sorted(images) == [1, 2, 3]
    assert (images[2]["download_state"], images[2]["download_error"]) == ("failed", "not_found")
    assert images[2]["asset_path"] is None and images[2]["ocr_state"] == "pending"
    first = f"assets/1-{hashlib.sha256(png(1)).hexdigest()[:12]}.png"
    assert images[1]["asset_path"] == first and images[1]["media_type"] == "image/png"
    assert images[3]["ocr_flags"] == ["truncated"] and images[3]["ocr_state"] == "ok"
    # The listed path is the asset route's own.
    asset = api.handle(
        method="GET", target=f"/api/v1/sources/{sid}/asset?path={first}", headers=_headers()
    )
    assert (asset.status, asset.body) == (200, png(1))

    recommendations = {r["title"]: r for r in note["recommendations"]}
    assert all(set(r) == RECOMMENDATION_FIELDS for r in recommendations.values())
    assert [r["title"] for r in note["recommendations"]] == [
        "Fast Inference from Transformers", "Attention Sinks", "Efficient Streaming",
    ]
    sinks = recommendations["Attention Sinks"]
    assert (sinks["url"], sinks["url_state"], sinks["image_ordinal"]) == (
        "https://blog.example/attention-sinks", "auto_matched", 3,
    )
    paper = recommendations["Fast Inference from Transformers"]
    assert (paper["capture_id"], paper["capture_state"], paper["import_state"]) == (
        None, None, "none",
    )

    rendered = json.dumps(note, ensure_ascii=False)
    for private in (
        SIGNATURE, "cdn.example", str(tmp_path), "staging", "raw/", "ocr/",
        "lease", "payload", "fileid", "item_key", "sha256",
    ):
        assert private not in rendered


def test_note_route_redacts_sensitive_lines_and_links(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    with store._transaction() as conn:
        store._xhs_upsert_recommendation(
            conn, note_id=note_id(1), item_key="blog:token", image_ordinal=None,
            kind="blog", title="Leaky", quote="first line\napi_key=abcdef0123456789",
            arxiv_id=None, url="https://blog.example/post?token=abcdef0123456789",
            url_state="from_text", origin="model", identify_run="synthetic-run",
        )
    note = get(_api(store), f"/api/v1/sources/{saved['source_id']}/note").payload
    leaky = next(r for r in note["recommendations"] if r["title"] == "Leaky")
    assert leaky["quote"] == "first line\n[redacted]"
    assert (leaky["url"], leaky["url_state"]) == (None, "from_text")
    assert "abcdef0123456789" not in json.dumps(note)


def test_provider_text_keeps_reasoning_vocabulary(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    title = "GPT-6 Astra, Looped Transformers, and Hidden Reasoning"
    with store._transaction() as conn:
        conn.execute(
            "UPDATE sources SET official_title = ? WHERE id = ?", (title, saved["source_id"])
        )
        conn.execute(
            "UPDATE xhs_notes SET title = ?, caption = ? WHERE source_id = ?",
            (title, "chain of thought\napi_key=abcdef0123456789", saved["source_id"]),
        )
        store._xhs_upsert_recommendation(
            conn, note_id=note_id(1), item_key="blog:reasoning", image_ordinal=None,
            kind="blog", title="Monitoring chain-of-thought", quote="<thinking> tags",
            arxiv_id=None, url="https://blog.example/chain-of-thought-monitoring",
            url_state="from_text", origin="model", identify_run="synthetic-run",
        )
    api = _api(store)
    assert get(api, f"/api/v1/sources/{saved['source_id']}").payload["official_title"] == title
    note = get(api, f"/api/v1/sources/{saved['source_id']}/note").payload
    assert note["title"] == title
    assert note["caption"] == "chain of thought\n[redacted]"
    blog = next(r for r in note["recommendations"] if r["title"] == "Monitoring chain-of-thought")
    assert blog["quote"] == "<thinking> tags"
    assert blog["url"] == "https://blog.example/chain-of-thought-monitoring"
    links = ControlAPI._source_links_projection({
        "recommended_in": [],
        "recommends": [
            {"source_id": "source_a", "source_kind": "blog", "official_title": title,
             "image_ordinal": 1, "recommendation_id": "rec_a", "created_at": "2026-10-07T00:00:00Z"},
            {"source_id": "source_b", "source_kind": "blog", "official_title": "token=abcdef0123456789",
             "image_ordinal": 2, "recommendation_id": "rec_b", "created_at": "2026-10-07T00:00:00Z"},
        ],
    })
    assert [entry["title"] for entry in links["recommends"]] == [title, "Untitled source"]


def test_note_route_refuses_other_sources_and_queries(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    paper = _paper_source(store)
    assert get(api, f"/api/v1/sources/{paper['id']}/note").status == 404
    assert get(api, "/api/v1/sources/source_missing/note").status == 404
    response = get(api, f"/api/v1/sources/{saved['source_id']}/note?full=1")
    assert (response.status, response.payload["category"]) == (400, "invalid_request")


def test_sources_list_filters_by_kind(store: ControlStore, saved: dict[str, Any]) -> None:
    api = _api(store)
    paper = _paper_source(store)

    def ids(target: str) -> list[str]:
        response = get(api, target)
        assert response.status == 200
        return [item["id"] for item in response.payload["items"]]

    assert sorted(ids("/api/v1/sources")) == sorted([paper["id"], saved["source_id"]])
    assert ids("/api/v1/sources?kind=xhs_note") == [saved["source_id"]]
    assert ids("/api/v1/sources?kind=paper") == [paper["id"]]
    assert ids("/api/v1/sources?kind=blog") == []
    for query in ("kind=video", "kind=", "kind=paper&kind=blog"):
        assert get(api, f"/api/v1/sources?{query}").status == 400


def test_links_route_lists_two_notes_recommending_one_blog(
    store: ControlStore, supervisor: PipelineSupervisor
) -> None:
    api = _api(store)
    recommendations = _two_notes_recommending_one_blog(store, supervisor)
    notes = [store.get_xhs_note(note_id(n)) for n in (1, 2)]
    for index, (note, recommendation) in enumerate(zip(notes, recommendations)):
        response = post(
            api,
            f"/api/v1/sources/{note['source_id']}/recommendations/import",
            {"recommendation_ids": [recommendation["id"]], "expected_revision": note["revision"]},
            key=f"import-blog-00000{index}",
        )
        assert response.status == 200
        assert response.payload["items"][0]["disposition"] == "blog_import_queued"
        pipeline(store, supervisor).drain()
    blog_id = store.get_xhs_recommendation(recommendations[0]["id"])["imported_source_id"]
    assert store.get_xhs_recommendation(recommendations[1]["id"])["imported_source_id"] == blog_id

    links = get(api, f"/api/v1/sources/{blog_id}/links")
    assert links.status == 200 and ("Cache-Control", "no-store") in links.headers
    assert links.payload["recommends"] == []
    recommended_in = links.payload["recommended_in"]
    assert [entry["source_id"] for entry in recommended_in] == [n["source_id"] for n in notes]
    assert {entry["source_kind"] for entry in recommended_in} == {"xhs_note"}
    assert [entry["image_ordinal"] for entry in recommended_in] == [1, 1]
    assert set(recommended_in[0]) == {
        "source_id", "source_kind", "title", "image_ordinal", "recommendation_id", "created_at",
    }
    from_note = get(api, f"/api/v1/sources/{notes[0]['source_id']}/links").payload
    assert from_note["recommended_in"] == []
    assert [(e["source_id"], e["source_kind"], e["title"]) for e in from_note["recommends"]] == [
        (blog_id, "blog", "Attention Sinks")
    ]
    assert get(api, "/api/v1/sources/source_missing/links").status == 404
    assert get(api, f"/api/v1/sources/{blog_id}/links?x=1").status == 400


def test_xhs_status_route_reports_without_paths(
    store: ControlStore, saved: dict[str, Any], tmp_path: Path
) -> None:
    default = get(_api(store), "/api/v1/xhs/status")
    assert default.status == 200 and ("Cache-Control", "no-store") in default.headers
    assert (default.payload["enabled"], default.payload["refusal"]) == (
        False, "disabled_in_config",
    )
    settings = XhsSettings(enabled=True, daily_calls={"tikhub": 7, "ocr": 8, "gpt": 9})
    status = get(_api(store, xhs_settings=settings), "/api/v1/xhs/status").payload
    # Configured, roots ready, but neither schedule row is armed.
    assert (status["enabled_in_config"], status["roots_ready"], status["enabled"]) == (
        True, True, False,
    )
    assert status["refusal"] is None
    assert {row["enabled"] for row in status["schedules"].values()} == {False}
    assert [b["user_id"] for b in status["bloggers"]][0] == USER
    assert {b["last_scan_outcome"] for b in status["bloggers"]} == {"ok", "no_new_notes"}
    assert status["usage"]["gpt"] == {"calls": store.xhs_usage()["gpt"], "cap": 9}
    assert status["tasks"]["failed"] >= 1
    assert status["last_failures"]["cdn"] == "not_found"
    rendered = json.dumps(status)
    assert str(tmp_path) not in rendered and SIGNATURE not in rendered
    assert get(_api(store), "/api/v1/xhs/status?verbose=1").status == 400


def test_source_routes_and_status_redact_provider_text_as_the_note_route_does(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    leaked = "api_key=synthetic-only-review-value"
    with store._transaction() as conn:
        conn.execute(
            "UPDATE sources SET official_title = ? WHERE id = ?",
            (f"Notes {leaked}", saved["source_id"]),
        )
        conn.execute(
            "UPDATE xhs_notes SET title = ? WHERE source_id = ?",
            (f"Notes {leaked}", saved["source_id"]),
        )
        conn.execute(
            "UPDATE xhs_bloggers SET display_name = ? WHERE user_id = ?", (leaked, USER)
        )
    settings = XhsSettings(enabled=True)
    api = _api(store, xhs_settings=settings)
    listed = get(api, "/api/v1/sources?kind=xhs_note").payload["items"]
    assert [item["official_title"] for item in listed] == ["[redacted]"]
    detail = get(api, f"/api/v1/sources/{saved['source_id']}").payload
    assert detail["official_title"] == "[redacted]"
    note = get(api, f"/api/v1/sources/{saved['source_id']}/note").payload
    assert note["title"] == "[redacted]"
    status = get(api, "/api/v1/xhs/status").payload
    assert "[redacted]" in [b["display_name"] for b in status["bloggers"]]
    for payload in (listed, detail, note, status):
        assert "synthetic-only-review-value" not in json.dumps(payload)


def test_xhs_status_is_not_enabled_while_a_root_is_not_ready(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    from cortex_platform.product.engine.schedules import XHS_DRAIN_JOB, XHS_PULL_JOB

    for job_key in (XHS_PULL_JOB, XHS_DRAIN_JOB):
        row = store.get_research_schedule(job_key)
        store.set_research_schedule_enabled(
            job_key=job_key, enabled=True, expected_revision=row["revision"],
            actor_id=ACTOR, idempotency_key=f"arm-schedule-{job_key}",
        )
    api = _api(store, xhs_settings=XhsSettings(enabled=True))
    assert get(api, "/api/v1/xhs/status").payload["enabled"] is True
    root = store.get_asset_root("blogs")
    store.update_asset_root(
        root_id="blogs", private_path=root.private_path, max_bytes=root.max_bytes,
        enabled=False, expected_revision=root.revision, actor_id=ACTOR,
        idempotency_key="disable-blogs-root",
    )
    status = get(api, "/api/v1/xhs/status").payload
    assert (status["enabled"], status["roots_ready"], status["refusal"]) == (
        False, False, "roots_not_ready",
    )


# -- import ------------------------------------------------------------------------


def test_import_gives_each_item_its_disposition_and_reuses_open_captures(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    found = _recommendations(store)
    extra = _extra_recommendations(store)
    ids = [
        found["Fast Inference from Transformers"]["id"],
        found["Attention Sinks"]["id"],
        found["Efficient Streaming"]["id"],
        extra["other"], extra["blog"], extra["paper"], "xhs_rec_unknown",
    ]
    body = {"recommendation_ids": ids, "expected_revision": saved["revision"]}
    response = post(api, f"/api/v1/sources/{sid}/recommendations/import", body, "import-0000000001")
    assert response.status == 200
    items = response.payload["items"]
    assert [(i["disposition"], i["reason"]) for i in items] == [
        ("capture_staged", None),
        ("blog_import_queued", None),
        ("blog_import_queued", None),
        ("refused", "not_importable"),
        ("refused", "no_url"),
        ("refused", "no_arxiv_id"),
        ("refused", "not_found"),
    ]
    staged = items[0]
    capture = store.get_capture(staged["capture_id"])
    assert (capture["payload"], capture["state"]) == (PAPER_URL, "pending")
    assert capture["note"] == "Recommended in XHS note 👩‍💻 Weekly papers 本周论文 · image 1"
    assert (staged["capture_revision"], staged["capture_state"]) == (0, "pending")
    assert staged["recommendation"]["import_state"] == "staged"
    assert staged["recommendation"]["capture_state"] == "pending"
    assert {items[i]["recommendation"]["import_state"] for i in (1, 2)} == {"importing"}
    assert items[3]["recommendation"]["import_state"] == "none"
    assert items[6]["recommendation"] is None
    assert [t["subject_key"] for t in tasks(store, "capture_link")] == [
        f"capture:{capture['id']}"
    ]
    assert len(tasks(store, "blog_import")) == 2
    assert all(set(i["recommendation"]) == RECOMMENDATION_FIELDS for i in items[:6])

    replay = post(api, f"/api/v1/sources/{sid}/recommendations/import", body, "import-0000000001")
    assert ("Idempotency-Replayed", "true") in replay.headers
    assert replay.payload == response.payload
    changed = post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {**body, "recommendation_ids": ids[:1]}, "import-0000000001",
    )
    assert (changed.status, changed.payload["category"]) == (409, "idempotency_conflict")

    # Approval is the Capture's own route; a second import reuses the Capture.
    approved = post(
        api, f"/api/v1/captures/{capture['id']}/approve",
        {"expected_revision": staged["capture_revision"]}, "approve-000000001",
    )
    assert (approved.status, approved.payload["state"]) == (200, "approved")
    again = post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {"recommendation_ids": ids[:1], "expected_revision": saved["revision"]},
        "import-0000000002",
    ).payload["items"][0]
    assert (again["disposition"], again["capture_id"], again["capture_state"]) == (
        "capture_reused", capture["id"], "approved",
    )
    assert len(store.list_captures()) == 1


def test_import_reuses_a_capture_the_operator_opened(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    opened = post(
        api, "/api/v1/captures", {"payload": PAPER_URL, "note": ""}, "capture-0000000001"
    ).payload
    paper = _recommendations(store)["Fast Inference from Transformers"]
    item = post(
        api, f"/api/v1/sources/{saved['source_id']}/recommendations/import",
        {"recommendation_ids": [paper["id"]], "expected_revision": saved["revision"]},
        "import-0000000003",
    ).payload["items"][0]
    assert (item["disposition"], item["capture_id"]) == ("capture_reused", opened["id"])
    assert store.get_capture(opened["id"])["note"] == ""


def test_an_imported_paper_is_linked_and_refused_a_second_time(
    store: ControlStore, supervisor: PipelineSupervisor, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    paper = _recommendations(store)["Fast Inference from Transformers"]
    body = {"recommendation_ids": [paper["id"]], "expected_revision": saved["revision"]}
    staged = post(api, f"/api/v1/sources/{sid}/recommendations/import", body, "import-0000000004")
    source = _paper_source(store)
    _end_capture(store, staged.payload["items"][0]["capture_id"], "consumed", [source["id"]])
    pipeline(store, supervisor).drain()

    note = get(api, f"/api/v1/sources/{sid}/note").payload
    row = next(r for r in note["recommendations"] if r["id"] == paper["id"])
    assert (row["import_state"], row["imported_source_id"], row["imported_source_kind"]) == (
        "imported", source["id"], "paper",
    )
    assert row["capture_state"] == "consumed"
    links = get(api, f"/api/v1/sources/{source['id']}/links").payload
    assert [(e["source_id"], e["image_ordinal"]) for e in links["recommended_in"]] == [(sid, 1)]
    item = post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {**body, "expected_revision": note["revision"]}, "import-0000000005",
    ).payload["items"][0]
    assert (item["disposition"], item["reason"]) == ("refused", "already_imported")


def test_import_checks_the_note_revision_and_its_body(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    paper = _recommendations(store)["Fast Inference from Transformers"]
    target = f"/api/v1/sources/{sid}/recommendations/import"
    stale = post(
        api, target, {"recommendation_ids": [paper["id"]], "expected_revision": 0}, "import-stale-001"
    )
    assert (stale.status, stale.payload["category"]) == (409, "revision_conflict")
    assert set(stale.payload["current"]) == NOTE_HEADER_FIELDS
    assert stale.payload["current"]["revision"] == saved["revision"]
    assert store.list_captures() == []

    for index, body in enumerate((
        {"recommendation_ids": [paper["id"]]},
        {"recommendation_ids": [paper["id"]], "expected_revision": 1, "dry_run": True},
        {"recommendation_ids": paper["id"], "expected_revision": saved["revision"]},
        {"recommendation_ids": [], "expected_revision": saved["revision"]},
        {"recommendation_ids": [paper["id"]] * 2, "expected_revision": saved["revision"]},
        {"recommendation_ids": [f"r{i}" for i in range(101)], "expected_revision": 1},
        {"recommendation_ids": ["../x"], "expected_revision": saved["revision"]},
        {"recommendation_ids": [paper["id"]], "expected_revision": "1"},
    )):
        response = post(api, target, body, f"import-bad-0000{index:02d}")
        assert (response.status, response.payload["category"]) == (400, "invalid_request")
        assert "idempotency" not in response.payload["title"].lower()
    missing_key = api.handle(
        method="POST", target=target, headers=_headers(),
        body=json.dumps({"recommendation_ids": [paper["id"]], "expected_revision": 1}).encode(),
    )
    assert missing_key.status == 400
    other = _paper_source(store)
    for source_id in (other["id"], "source_missing"):
        response = post(
            api, f"/api/v1/sources/{source_id}/recommendations/import",
            {"recommendation_ids": [paper["id"]], "expected_revision": 0}, "import-404-000001",
        )
        assert response.status == 404
    assert store.list_captures() == []


def test_an_import_chosen_before_a_link_edit_is_refused(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    streaming = _recommendations(store)["Efficient Streaming"]
    read = get(api, f"/api/v1/sources/{sid}/note").payload
    edited = post(
        api, f"/api/v1/sources/{sid}/recommendations/{streaming['id']}/link",
        {"url": "https://other.example/changed", "expected_revision": streaming["revision"]},
        "link-edit-elsewhere-01",
    )
    assert edited.status == 200
    stale = post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {"recommendation_ids": [streaming["id"]], "expected_revision": read["revision"]},
        "import-after-edit-0001",
    )
    assert (stale.status, stale.payload["category"]) == (409, "revision_conflict")
    assert tasks(store, "blog_import") == []


# -- links -------------------------------------------------------------------------


def test_link_route_sets_the_operator_link_and_saves_the_next_version(
    store: ControlStore, supervisor: PipelineSupervisor, saved: dict[str, Any], tmp_path: Path
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    sinks = _recommendations(store)["Attention Sinks"]
    target = f"/api/v1/sources/{sid}/recommendations/{sinks['id']}/link"
    body = {"url": "HTTPS://Blog.Example/Sinks?ref=xhs#top", "expected_revision": sinks["revision"]}
    response = post(api, target, body, "link-00000000001")
    assert response.status == 200
    linked = response.payload["recommendation"]
    assert set(linked) == RECOMMENDATION_FIELDS
    assert (linked["url"], linked["url_state"], linked["url_checked_title"]) == (
        "https://blog.example/Sinks?ref=xhs", "operator_set", None,
    )
    assert linked["revision"] == sinks["revision"] + 1
    replay = post(api, target, body, "link-00000000001")
    assert ("Idempotency-Replayed", "true") in replay.headers
    assert [t["state"] for t in tasks(store, "save") if t["subject_key"].endswith(":2")] == [
        "pending"
    ]
    pipeline(store, supervisor).drain()
    assert store.get_xhs_note(note_id(1))["content_version"] == 2
    note_md = tmp_path / "xhs-notes" / note_id(1) / "v2" / "note.md"
    assert "https://blog.example/Sinks?ref=xhs" in note_md.read_text()

    stale = post(api, target, body, "link-00000000002")
    assert (stale.status, stale.payload["category"]) == (409, "revision_conflict")
    assert set(stale.payload["current"]) == RECOMMENDATION_FIELDS
    assert stale.payload["current"]["url_state"] == "operator_set"


def test_link_route_refuses_bad_links_and_unlinkable_items(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    found = _recommendations(store)
    sinks, paper = found["Attention Sinks"], found["Fast Inference from Transformers"]
    target = f"/api/v1/sources/{sid}/recommendations/{sinks['id']}/link"
    for index, url in enumerate((
        "ftp://blog.example/x", "https://user:pw@blog.example/x", "https://blog.example:8443/x",
        "https:///nohost", "not a url",
    )):
        response = post(
            api, target, {"url": url, "expected_revision": sinks["revision"]},
            f"link-bad-000000{index}",
        )
        assert (response.status, response.payload["category"]) == (400, "invalid_request")
        assert "url" in response.payload["title"].lower()
    extra = post(
        api, target, {"url": "https://blog.example/x", "expected_revision": 0, "x": 1},
        "link-bad-extra-0001",
    )
    assert (extra.status, extra.payload["title"]) == (400, "JSON body fields are invalid")
    refused = post(
        api, f"/api/v1/sources/{sid}/recommendations/{paper['id']}/link",
        {"url": "https://blog.example/x", "expected_revision": paper["revision"]}, "link-paper-000001",
    )
    assert (refused.status, refused.payload["category"]) == (409, "invalid_transition")
    missing = post(
        api, f"/api/v1/sources/{sid}/recommendations/xhs_rec_unknown/link",
        {"url": "https://blog.example/x", "expected_revision": 0}, "link-missing-001",
    )
    assert missing.status == 404
    # Once its import is queued, the link it imports from stays.
    post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {"recommendation_ids": [sinks["id"]], "expected_revision": saved["revision"]},
        "import-0000000006",
    )
    importing = store.get_xhs_recommendation(sinks["id"])
    busy = post(
        api, target, {"url": "https://blog.example/x", "expected_revision": importing["revision"]},
        "link-busy-000001",
    )
    assert (busy.status, busy.payload["category"]) == (409, "invalid_transition")
    assert store.get_xhs_recommendation(sinks["id"])["url_state"] == "auto_matched"


# -- exclusion and the weekly fallback ----------------------------------------------


def _left_to_operator(store: ControlStore, reasons: dict[str, str]) -> None:
    """Reviews the weekly run would leave for the operator, by title."""

    found = _recommendations(store)
    with store._transaction() as conn:
        for title, reason in reasons.items():
            store._xhs_set_review(
                conn, found[title]["id"], state="needs_operator", method="model",
                reason_code="conflicting_evidence", reason=reason,
            )


def test_exclude_and_restore_routes_follow_the_receipt_and_the_fence(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    streaming = _recommendations(store)["Efficient Streaming"]
    target = f"/api/v1/sources/{sid}/recommendations/{streaming['id']}"
    body = {"reason": "A course, not a blog", "expected_revision": streaming["revision"]}

    response = post(api, f"{target}/exclude", body, "exclude-route-00001")
    assert response.status == 200
    excluded = response.payload["recommendation"]
    assert set(excluded) == RECOMMENDATION_FIELDS and set(excluded["review"]) == REVIEW_FIELDS
    assert excluded["review"] | {"updated_at": None} == {
        "state": "excluded", "method": "operator", "reason_code": "operator",
        "reason": "A course, not a blog", "corrected_fields": [], "updated_at": None,
    }
    assert excluded["revision"] == streaming["revision"] + 1
    replay = post(api, f"{target}/exclude", body, "exclude-route-00001")
    assert ("Idempotency-Replayed", "true") in replay.headers
    assert replay.payload == response.payload
    changed = post(api, f"{target}/exclude", {**body, "reason": "Other"}, "exclude-route-00001")
    assert (changed.status, changed.payload["category"]) == (409, "idempotency_conflict")
    stale = post(
        api, f"{target}/restore", {"expected_revision": streaming["revision"]},
        "restore-route-00001",
    )
    assert (stale.status, stale.payload["category"]) == (409, "revision_conflict")
    assert set(stale.payload["current"]) == RECOMMENDATION_FIELDS
    assert stale.payload["current"]["review"]["state"] == "excluded"
    twice = post(
        api, f"{target}/exclude", {**body, "expected_revision": excluded["revision"]},
        "exclude-route-00002",
    )
    assert (twice.status, twice.payload["category"]) == (409, "invalid_transition")

    # The note shows the review, and an import refuses the row.
    note = get(api, f"/api/v1/sources/{sid}/note").payload
    shown = next(r for r in note["recommendations"] if r["id"] == streaming["id"])
    assert shown["review"] == excluded["review"]
    assert all(
        r["review"] is None for r in note["recommendations"] if r["id"] != streaming["id"]
    )
    refused = post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {"recommendation_ids": [streaming["id"]], "expected_revision": note["revision"]},
        "import-excluded-001",
    ).payload["items"][0]
    assert (refused["disposition"], refused["reason"]) == ("refused", "excluded")
    assert tasks(store, "blog_import") == []

    restore = {"expected_revision": excluded["revision"]}
    response = post(api, f"{target}/restore", restore, "restore-route-00002")
    assert response.status == 200
    restored = response.payload["recommendation"]
    assert set(restored) == RECOMMENDATION_FIELDS
    assert (
        restored["review"]["state"], restored["review"]["method"],
        restored["review"]["reason_code"], restored["review"]["reason"],
    ) == ("operator_owned", "operator", None, None)
    replay = post(api, f"{target}/restore", restore, "restore-route-00002")
    assert ("Idempotency-Replayed", "true") in replay.headers
    assert replay.payload == response.payload
    again = post(
        api, f"{target}/restore", {"expected_revision": restored["revision"]},
        "restore-route-00003",
    )
    assert (again.status, again.payload["category"]) == (409, "invalid_transition")


def test_exclude_and_restore_routes_refuse_bad_requests(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    api = _api(store)
    sid = saved["source_id"]
    found = _recommendations(store)
    sinks, streaming = found["Attention Sinks"], found["Efficient Streaming"]
    target = f"/api/v1/sources/{sid}/recommendations/{streaming['id']}"
    revision = streaming["revision"]
    for index, (action, body) in enumerate((
        ("exclude", {"expected_revision": revision}),
        ("exclude", {"reason": "   ", "expected_revision": revision}),
        ("exclude", {"reason": "x" * 501, "expected_revision": revision}),
        ("exclude", {"reason": 5, "expected_revision": revision}),
        ("exclude", {"reason": "A course", "expected_revision": revision, "x": 1}),
        ("exclude", {"reason": "A course", "expected_revision": "1"}),
        ("restore", {"expected_revision": revision, "reason": "A course"}),
        ("restore", {}),
    )):
        response = post(api, f"{target}/{action}", body, f"review-bad-000{index:02d}")
        assert (response.status, response.payload["category"]) == (400, "invalid_request")
    missing_key = api.handle(
        method="POST", target=f"{target}/exclude", headers=_headers(),
        body=json.dumps({"reason": "A course", "expected_revision": revision}).encode(),
    )
    assert missing_key.status == 400
    other = _paper_source(store)
    for path in (
        f"/api/v1/sources/{sid}/recommendations/xhs_rec_unknown/exclude",
        f"/api/v1/sources/{other['id']}/recommendations/{streaming['id']}/exclude",
        "/api/v1/sources/source_missing/recommendations/xhs_rec_unknown/exclude",
    ):
        response = post(
            api, path, {"reason": "A course", "expected_revision": 0}, "review-404-000001"
        )
        assert response.status == 404
    # A row whose import is queued is neither excluded nor restored.
    post(
        api, f"/api/v1/sources/{sid}/recommendations/import",
        {"recommendation_ids": [sinks["id"]], "expected_revision": saved["revision"]},
        "import-0000000007",
    )
    importing = store.get_xhs_recommendation(sinks["id"])
    busy = post(
        api, f"/api/v1/sources/{sid}/recommendations/{sinks['id']}/exclude",
        {"reason": "A course", "expected_revision": importing["revision"]},
        "review-busy-000001",
    )
    assert (busy.status, busy.payload["category"]) == (409, "invalid_transition")
    assert store.get_xhs_recommendation(streaming["id"])["revision"] == revision


def test_needs_operator_route_lists_what_waits_for_the_operator(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    _left_to_operator(store, {
        "Attention Sinks": "Two pages match the title.",
        "Efficient Streaming": "api_key=abcdef0123456789",
    })
    api = _api(store)
    response = get(api, "/api/v1/xhs/recommendations?review=needs_operator")
    assert response.status == 200 and ("Cache-Control", "no-store") in response.headers
    assert response.payload["total"] == 2
    items = {item["title"]: item for item in response.payload["items"]}
    assert all(set(item) == NEEDS_OPERATOR_FIELDS for item in items.values())
    sinks = items["Attention Sinks"]
    assert sinks | {"updated_at": None} == {
        "note_source_id": saved["source_id"], "note_title": saved["title"],
        "recommendation_id": _recommendations(store)["Attention Sinks"]["id"],
        "kind": "blog", "title": "Attention Sinks", "reason_code": "conflicting_evidence",
        "reason": "Two pages match the title.", "updated_at": None,
    }
    # The reason is redacted as provider text is.
    assert items["Efficient Streaming"]["reason"] == "[redacted]"
    assert "abcdef0123456789" not in json.dumps(response.payload)
    limited = get(api, "/api/v1/xhs/recommendations?review=needs_operator&limit=1").payload
    assert (len(limited["items"]), limited["total"]) == (1, 2)
    for query in (
        "", "?review=excluded", "?review=needs_operator&limit=0",
        "?review=needs_operator&limit=101", "?review=needs_operator&limit=x",
        "?review=needs_operator&x=1", "?review=needs_operator&review=needs_operator",
    ):
        response = get(api, f"/api/v1/xhs/recommendations{query}")
        assert (response.status, response.payload["category"]) == (400, "invalid_request")


def test_xhs_status_route_reports_the_weekly_fallback(
    store: ControlStore, saved: dict[str, Any]
) -> None:
    run = store.start_xhs_fallback_run(
        trigger="operator", item_cap=100, model="gpt-6.1-sol", effort="xhigh"
    )["run"]
    while (item := store.claim_xhs_fallback_item(lease_seconds=900)) is not None:
        started, day = store.begin_xhs_fallback_call(
            item["id"], expected_revision=item["revision"], cap=300
        )
        assert day is not None
        store.apply_xhs_fallback_result(
            started["id"], expected_revision=started["revision"],
            decision={"action": "needs_operator", "reason_code": "insufficient_evidence"},
        )
    store.set_xhs_fallback_digest(run["id"], state="pending", reason="transport_disabled")
    settings = XhsSettings(enabled=True, fallback_enabled=True)

    fallback = get(_api(store, xhs_settings=settings), "/api/v1/xhs/status").payload["fallback"]

    assert set(fallback) == {
        "enabled", "running", "last", "next_start_at", "backlog", "needs_operator",
    }
    assert (fallback["enabled"], fallback["running"], fallback["backlog"]) == (True, None, 0)
    last = fallback["last"]
    assert (last["id"], last["state"], last["trigger"], last["model"]) == (
        run["id"], "completed", "operator", "gpt-6.1-sol",
    )
    assert (last["digest_state"], last["digest_reason"]) == ("pending", "transport_disabled")
    assert last["summary"]["needs_operator"] == fallback["needs_operator"] > 0
    assert fallback["next_start_at"] > last["started_at"]
    listed = get(_api(store), "/api/v1/xhs/recommendations?review=needs_operator").payload
    assert listed["total"] == fallback["needs_operator"]
    assert get(_api(store), "/api/v1/xhs/status").payload["fallback"]["enabled"] is False


# -- image retry -------------------------------------------------------------------


def test_retry_route_resets_a_failed_image(store: ControlStore, saved: dict[str, Any]) -> None:
    api = _api(store)
    sid = saved["source_id"]
    target = f"/api/v1/sources/{sid}/images/2/retry"
    body = {"expected_revision": saved["revision"]}
    response = post(api, target, body, "retry-0000000001")
    assert response.status == 200
    assert set(response.payload["note"]) == NOTE_HEADER_FIELDS
    assert (response.payload["note"]["state"], response.payload["image"]["download_state"]) == (
        "detail_ok", "pending",
    )
    assert response.payload["image"]["download_error"] is None
    download = next(
        t for t in tasks(store, "download") if t["subject_key"] == f"download:{note_id(1)}:2"
    )
    assert download["state"] == "pending"
    assert SIGNATURE not in json.dumps(response.payload)
    replay = post(api, target, body, "retry-0000000001")
    assert ("Idempotency-Replayed", "true") in replay.headers and replay.payload == response.payload

    current = store.get_xhs_note(note_id(1))["revision"]
    stale = post(api, target, body, "retry-0000000002")
    assert (stale.status, stale.payload["category"]) == (409, "revision_conflict")
    healthy = post(
        api, f"/api/v1/sources/{sid}/images/1/retry", {"expected_revision": current},
        "retry-0000000003",
    )
    assert (healthy.status, healthy.payload["category"]) == (409, "invalid_transition")
    assert post(
        api, f"/api/v1/sources/{sid}/images/101/retry", {"expected_revision": current},
        "retry-0000000004",
    ).status == 400
    assert post(
        api, f"/api/v1/sources/{sid}/images/0/retry", {"expected_revision": current},
        "retry-0000000005",
    ).status == 404
    assert post(
        api, "/api/v1/sources/source_missing/images/2/retry", {"expected_revision": 0},
        "retry-0000000006",
    ).status == 404


# -- authorization -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("GET", "/api/v1/sources?kind=blog"),
        ("GET", "/api/v1/sources/{sid}/note"),
        ("GET", "/api/v1/sources/{sid}/links"),
        ("GET", "/api/v1/xhs/status"),
        ("GET", "/api/v1/xhs/recommendations?review=needs_operator"),
        ("POST", "/api/v1/sources/{sid}/recommendations/import"),
        ("POST", "/api/v1/sources/{sid}/recommendations/xhs_rec_1/link"),
        ("POST", "/api/v1/sources/{sid}/recommendations/xhs_rec_1/exclude"),
        ("POST", "/api/v1/sources/{sid}/recommendations/xhs_rec_1/restore"),
        ("POST", "/api/v1/sources/{sid}/images/2/retry"),
    ],
)
def test_xhs_routes_authenticate_before_reading(
    store: ControlStore, saved: dict[str, Any], monkeypatch, method: str, route: str
) -> None:
    api = _api(store)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("read before authentication")

    for name in (
        "list_sources", "xhs_note_view", "list_source_links", "xhs_usage",
        "import_xhs_recommendations", "set_xhs_recommendation_link", "xhs_note_id_for_source",
        "list_xhs_needs_operator", "exclude_xhs_recommendation", "restore_xhs_recommendation",
        "xhs_fallback_state",
    ):
        monkeypatch.setattr(store, name, refuse)
    response = api.handle(
        method=method,
        target=route.format(sid=saved["source_id"]),
        headers={"Idempotency-Key": "auth-000000000001"},
        body=b"{}" if method == "POST" else b"",
    )
    assert (response.status, response.payload["category"]) == (403, "authentication_required")
