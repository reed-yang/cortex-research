"""XHS Control commands: bloggers, notes, images, recommendations, tasks, usage,
content bindings and links. Synthetic identities only; nothing leaves the store."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
    RevisionConflict,
)
from cortex_platform.product.sources.identity import blog_url_identity
from cortex_platform.tests.product.control.test_fragments import (
    DeterministicIds,
    MovableClock,
)

USER = "5f0e1d2c3b4a59687766554a"
NOTE = "66f1a2b3c4d5e6f708192a3b"
SECOND_NOTE = "66f1a2b3c4d5e6f708192a3c"
ACTOR = "local-operator"


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MovableClock) -> ControlStore:
    value = ControlStore(tmp_path / "control.db", clock=clock, id_factory=DeterministicIds())
    value.initialize()
    return value


def _follow(store: ControlStore, key: str = "follow-0000000001", **overrides):
    request = {"user_id": USER, "role": "curator", "display_name": "研究笔记 Lab"}
    request.update(overrides)
    return store.follow_xhs_blogger(actor_id=ACTOR, idempotency_key=key, **request)


def _note(store: ControlStore, note_id: str = NOTE, **overrides) -> None:
    values = {
        "note_id": note_id,
        "user_id": USER,
        "note_type": "normal",
        "title": "本周论文 weekly papers",
        "caption": "Three papers on speculative decoding 投机解码…",
        "caption_complete": False,
        "published_at": "2026-09-20T08:00:00Z",
    }
    values.update(overrides)
    with store._transaction() as conn:
        return store._xhs_insert_note(conn, **values)


def _roots(store: ControlStore, tmp_path: Path, enabled: bool = True) -> None:
    for root_id in ("xhs-notes", "blogs"):
        store.register_asset_root(
            root_id=root_id, private_path=tmp_path / root_id, max_bytes=1 << 20,
            enabled=enabled, actor_id=ACTOR, idempotency_key=f"root-{root_id}-00001",
        )


# -- bloggers -------------------------------------------------------------------


def test_follow_replays_and_refollow_updates_the_role(store: ControlStore) -> None:
    first = _follow(store)
    assert first.value["followed"] is True and first.value["role"] == "curator"
    assert _follow(store).replayed is True
    with pytest.raises(IdempotencyConflict):
        _follow(store, role="author")
    store.unfollow_xhs_blogger(user_id=USER, actor_id=ACTOR, idempotency_key="unfollow-00000001")
    assert store.get_xhs_blogger(USER)["followed"] is False
    assert store.list_xhs_bloggers(followed_only=True) == []
    again = _follow(store, key="follow-0000000002", role="author", display_name=None)
    assert again.value["followed"] is True and again.value["role"] == "author"
    assert again.value["display_name"] == "研究笔记 Lab"
    store.set_xhs_blogger_role(user_id=USER, role="curator", actor_id=ACTOR,
                               idempotency_key="set-role-0000000001")
    assert store.get_xhs_blogger(USER)["role"] == "curator"


@pytest.mark.parametrize("user_id", ["5f0e1d2c", "5F0E1D2C3B4A59687766554", "x" * 24, None])
def test_follow_refuses_anything_but_a_full_user_id(store: ControlStore, user_id) -> None:
    with pytest.raises(ValueError):
        _follow(store, user_id=user_id)
    with pytest.raises(ValueError):
        _follow(store, key="follow-0000000003", role="reader")


def test_a_failed_scan_is_never_recorded_as_no_new_notes(store: ControlStore) -> None:
    _follow(store)
    with store._transaction() as conn:
        with pytest.raises(ValueError):
            store._xhs_record_scan(conn, user_id=USER, outcome="failed")
        with pytest.raises(ValueError):
            store._xhs_record_scan(conn, user_id=USER, outcome="no_new_notes", error="transient")
        with pytest.raises(ValueError):
            store._xhs_record_scan(conn, user_id=USER, outcome="failed", error="it broke")
        failed = store._xhs_record_scan(conn, user_id=USER, outcome="failed", error="auth")
    assert (failed["last_scan_outcome"], failed["last_scan_error"]) == ("failed", "auth")
    with store._transaction() as conn:
        quiet = store._xhs_record_scan(conn, user_id=USER, outcome="no_new_notes")
    assert (quiet["last_scan_outcome"], quiet["last_scan_error"]) == ("no_new_notes", None)


# -- notes, images, recommendations ---------------------------------------------


def test_a_seen_note_is_not_inserted_twice_and_updates_are_fenced(store: ControlStore) -> None:
    _follow(store)
    assert _note(store) is True
    assert _note(store, caption="a different list caption") is False
    note = store.get_xhs_note(NOTE)
    assert note["state"] == "discovered" and note["caption_complete"] is False
    assert note["caption"] == "Three papers on speculative decoding 投机解码…"
    with store._transaction() as conn:
        updated = store._xhs_update_note(
            conn, NOTE, expected_revision=0, state="detail_ok",
            caption="  whole caption, kept verbatim  \n", caption_complete=True,
        )
    assert updated["caption"] == "  whole caption, kept verbatim  \n"
    assert updated["revision"] == 1
    with store._transaction() as conn:
        with pytest.raises(RevisionConflict):
            store._xhs_update_note(conn, NOTE, expected_revision=0, state="assets_done")
        with pytest.raises(ValueError):
            store._xhs_update_note(conn, NOTE, expected_revision=1, state="done")
        with pytest.raises(ValueError):
            store._xhs_update_note(conn, NOTE, expected_revision=1, source_id="x")


def test_a_failed_image_keeps_its_ordinal_while_later_ones_proceed(store: ControlStore) -> None:
    _follow(store)
    _note(store)
    with store._transaction() as conn:
        for ordinal in (1, 2, 3):
            store._xhs_upsert_image(conn, note_id=NOTE, ordinal=ordinal,
                                    fileid=f"file-{ordinal}", upstream_width=1080)
        store._xhs_update_image(conn, NOTE, 2, expected_revision=0,
                                download_state="failed", download_error="url_expired")
        store._xhs_update_image(
            conn, NOTE, 3, expected_revision=0, download_state="ok",
            sha256="c" * 64, byte_size=2048, media_type="image/webp",
            asset_name="3-cccccccccccc.webp", width=1080, height=1440,
        )
        store._xhs_update_image(
            conn, NOTE, 3, expected_revision=1, ocr_state="ok", ocr_engine="deepseek-ocr-2",
            ocr_text_sha256="d" * 64, ocr_flags=["truncated"],
        )
    images = store.list_xhs_note_images(NOTE)
    assert [(i["ordinal"], i["fileid"], i["download_state"]) for i in images] == [
        (1, "file-1", "pending"), (2, "file-2", "failed"), (3, "file-3", "ok"),
    ]
    assert images[2]["ocr_flags"] == ["truncated"]
    with store._transaction() as conn:
        # A refreshed detail brings the same images at the same ordinals.
        store._xhs_upsert_image(conn, note_id=NOTE, ordinal=2, fileid="file-2",
                                upstream_width=1080)
        with pytest.raises(InvalidTransition):
            store._xhs_upsert_image(conn, note_id=NOTE, ordinal=2, fileid="file-9")
        with pytest.raises(ValueError):
            # An OK download names its bytes; the schema refuses half of one.
            store._xhs_update_image(conn, NOTE, 1, expected_revision=0, download_state="ok")
        with pytest.raises(ValueError):
            store._xhs_update_image(conn, NOTE, 1, expected_revision=0,
                                    asset_name="../1-cccccccccccc.webp")


def test_recommendations_merge_by_key_and_keep_an_operator_link(store: ControlStore) -> None:
    _follow(store)
    _note(store)
    with store._transaction() as conn:
        store._xhs_upsert_image(conn, note_id=NOTE, ordinal=2, fileid="file-2")
        blog = store._xhs_upsert_recommendation(
            conn, note_id=NOTE, item_key="title:a-blog:2", image_ordinal=2, kind="blog",
            title="Scaling Laws, Revisited", quote="Scaling Laws, Revisited — a blog post",
            arxiv_id=None, url="HTTPS://Example.COM:443/Post/#top", url_state="from_text",
            origin="rule", identify_run="identify:run-1",
        )
        assert blog["url"] == "https://example.com/Post/"
        store._xhs_update_recommendation(
            conn, blog["id"], expected_revision=0,
            url="https://example.org/real", url_state="operator_set",
        )
        again = store._xhs_upsert_recommendation(
            conn, note_id=NOTE, item_key="title:a-blog:2", image_ordinal=2, kind="blog",
            title="Scaling Laws, Revisited", quote="Scaling Laws, Revisited",
            arxiv_id=None, url=None, url_state="not_found",
            origin="rule+model", identify_run="identify:run-2",
        )
        paper = store._xhs_upsert_recommendation(
            conn, note_id=NOTE, item_key="arxiv:2609.01234", image_ordinal=None,
            kind="paper", title="A paper", quote="arXiv:2609.01234v2", arxiv_id="2609.01234v2",
            url=None, url_state="none", origin="rule", identify_run="identify:run-2",
        )
        for url, state in (("https:///nohost", "from_text"), (None, "auto_matched"),
                           ("https://example.com/", "none"), ("ftp://example.com/", "unverified")):
            with pytest.raises(ValueError):
                store._xhs_update_recommendation(conn, again["id"], expected_revision=2,
                                                 url=url, url_state=state)
        with pytest.raises(ValueError):
            store._xhs_upsert_recommendation(
                conn, note_id=NOTE, item_key="arxiv:x", image_ordinal=None, kind="blog",
                title="t", quote="q", arxiv_id="2609.01234", url=None, url_state="none",
                origin="rule", identify_run="r",
            )
    assert (again["id"], again["url"], again["url_state"]) == (
        blog["id"], "https://example.org/real", "operator_set",
    )
    assert again["origin"] == "rule+model" and again["quote"] == "Scaling Laws, Revisited"
    assert paper["arxiv_id"] == "2609.01234"
    listed = store.list_xhs_recommendations(NOTE)
    assert [item["id"] for item in listed] == [blog["id"], paper["id"]]


# -- tasks ------------------------------------------------------------------------


def test_task_creation_is_idempotent_by_subject_key(store: ControlStore) -> None:
    first, created = store.create_xhs_task(
        kind="download", subject_key=f"download:{NOTE}:1",
        payload={"url": "https://cdn.example/signed?token=private"},
    )
    again, created_again = store.create_xhs_task(
        kind="download", subject_key=f"download:{NOTE}:1", payload={"url": "other"},
    )
    assert created is True and created_again is False
    assert again["id"] == first["id"] and again["payload"] == first["payload"]
    with pytest.raises(ValueError):
        # A subject key names its kind, so another kind cannot claim it.
        store.create_xhs_task(kind="ocr", subject_key=f"download:{NOTE}:1", payload={})
    for kind, key in (("detail", f"download:{NOTE}"), ("detail", "detail:"),
                      ("detail", "detail:with space"), ("crawl", "crawl:1")):
        with pytest.raises(ValueError):
            store.create_xhs_task(kind=kind, subject_key=key, payload={})


def test_claim_leases_the_oldest_due_task_and_completion_is_fenced(
    store: ControlStore, clock: MovableClock
) -> None:
    older, _ = store.create_xhs_task(kind="detail", subject_key=f"detail:{NOTE}", payload={})
    clock.advance(1)
    store.create_xhs_task(kind="detail", subject_key=f"detail:{SECOND_NOTE}", payload={})
    claimed = store.claim_xhs_task(lease_seconds=600)
    assert claimed["id"] == older["id"]
    assert (claimed["state"], claimed["attempts"]) == ("running", 1)
    assert store.claim_xhs_task(lease_seconds=600, kinds=["ocr"]) is None
    with pytest.raises(RevisionConflict):
        store.complete_xhs_task(claimed["id"], expected_revision=older["revision"], result={})
    done = store.complete_xhs_task(
        claimed["id"], expected_revision=claimed["revision"], result={"images": 3},
    )
    assert (done["state"], done["result"], done["lease_until"]) == ("done", {"images": 3}, None)
    with pytest.raises(InvalidTransition):
        store.fail_xhs_task(done["id"], expected_revision=done["revision"], category="transient")
    assert store.xhs_task_counts() == {
        "canceled": 0, "done": 1, "failed": 0, "pending": 1, "running": 0,
    }


def test_retryable_failures_back_off_then_fail(store: ControlStore, clock: MovableClock) -> None:
    store.create_xhs_task(kind="list_page", subject_key=f"scan:{USER}:1:1", payload={})
    for delay in (600, 3_600, 21_600):
        task = store.claim_xhs_task(lease_seconds=600)
        task = store.fail_xhs_task(task["id"], expected_revision=task["revision"],
                                   category="rate_limited")
        assert (task["state"], task["last_error"]) == ("pending", "rate_limited")
        clock.advance(delay - 1)
        assert store.claim_xhs_task(lease_seconds=600) is None
        clock.advance(1)
    task = store.claim_xhs_task(lease_seconds=600)
    assert task["attempts"] == 4
    task = store.fail_xhs_task(task["id"], expected_revision=task["revision"], category="transient")
    assert task["state"] == "failed"
    store.create_xhs_task(kind="detail", subject_key=f"detail:{NOTE}", payload={})
    task = store.claim_xhs_task(lease_seconds=600)
    with pytest.raises(ValueError):
        store.fail_xhs_task(task["id"], expected_revision=task["revision"], category="oops")
    final = store.fail_xhs_task(task["id"], expected_revision=task["revision"],
                                category="upstream_error")
    assert (final["state"], final["attempts"]) == ("failed", 1)


def test_an_expired_lease_is_reclaimed_until_the_budget_is_spent(
    store: ControlStore, clock: MovableClock
) -> None:
    created, _ = store.create_xhs_task(kind="ocr", subject_key=f"ocr:{NOTE}:1:{'a' * 64}",
                                       payload={})
    first = store.claim_xhs_task(lease_seconds=60)
    assert store.claim_xhs_task(lease_seconds=60) is None
    for attempt in (2, 3, 4):
        clock.advance(61)
        task = store.claim_xhs_task(lease_seconds=60)
        assert (task["id"], task["attempts"]) == (created["id"], attempt)
    # The crashed holder's late result is fenced out.
    with pytest.raises(RevisionConflict):
        store.complete_xhs_task(first["id"], expected_revision=first["revision"], result={})
    clock.advance(61)
    assert store.claim_xhs_task(lease_seconds=60) is None
    failed = store.get_xhs_task(created["id"])
    assert (failed["state"], failed["last_error"]) == ("failed", "outcome_unknown")


def test_release_returns_the_attempt_and_reset_restores_the_budget(
    store: ControlStore, clock: MovableClock
) -> None:
    store.create_xhs_task(kind="identify", subject_key=f"identify:{NOTE}:{'b' * 64}",
                          payload={})
    task = store.claim_xhs_task(lease_seconds=60)
    released = store.release_xhs_task(task["id"], expected_revision=task["revision"],
                                      not_before="2026-09-02T00:00:00.000000Z")
    assert (released["state"], released["attempts"]) == ("pending", 0)
    assert store.claim_xhs_task(lease_seconds=60) is None
    clock.advance(12 * 3600)
    task = store.claim_xhs_task(lease_seconds=60)
    failed = store.fail_xhs_task(task["id"], expected_revision=task["revision"], category="auth")
    with store._transaction() as conn:
        reset = store._xhs_reset_task(conn, failed["subject_key"])
    assert (reset["state"], reset["attempts"], reset["last_error"]) == ("pending", 0, None)
    task = store.claim_xhs_task(lease_seconds=60)
    with store._transaction() as conn:
        with pytest.raises(InvalidTransition):
            store._xhs_reset_task(conn, task["subject_key"])
        with pytest.raises(NotFound):
            store._xhs_reset_task(conn, "save:missing:1")


# -- usage ------------------------------------------------------------------------


def test_usage_is_capped_per_provider_and_utc_day(store: ControlStore, clock: MovableClock) -> None:
    assert store.reserve_xhs_usage("tikhub", cap=2) is True
    assert store.reserve_xhs_usage("tikhub", cap=2) is True
    assert store.reserve_xhs_usage("tikhub", cap=2) is False
    assert store.reserve_xhs_usage("gpt", cap=2, calls=2) is True
    assert store.xhs_usage() == {"gpt": 2, "ocr": 0, "tikhub": 2}
    with pytest.raises(ValueError):
        store.reserve_xhs_usage("jina", cap=10)
    clock.advance(24 * 3600)
    assert store.reserve_xhs_usage("tikhub", cap=2) is True
    assert store.xhs_usage() == {"gpt": 0, "ocr": 0, "tikhub": 1}
    assert store.xhs_usage("2026-09-01")["tikhub"] == 2


# -- content bindings and links ---------------------------------------------------


def test_roots_report_missing_disabled_overlapping_and_ready(
    store: ControlStore, tmp_path: Path
) -> None:
    assert store.xhs_roots_status() == {"xhs-notes": "missing", "blogs": "missing"}
    store.register_asset_root(
        root_id="research-corpus", private_path=tmp_path / "corpus", max_bytes=1 << 20,
        enabled=True, actor_id=ACTOR, idempotency_key="root-corpus-00001",
    )
    store.register_asset_root(
        root_id="xhs-notes", private_path=tmp_path / "corpus" / "notes", max_bytes=1 << 20,
        enabled=True, actor_id=ACTOR, idempotency_key="root-notes-000001",
    )
    store.register_asset_root(
        root_id="blogs", private_path=tmp_path / "blogs", max_bytes=1 << 20,
        enabled=False, actor_id=ACTOR, idempotency_key="root-blogs-000001",
    )
    assert store.xhs_roots_status() == {"xhs-notes": "overlaps_corpus", "blogs": "disabled"}


def test_content_versions_bind_once_in_order_and_replay(
    store: ControlStore, tmp_path: Path
) -> None:
    normalized, digest = blog_url_identity("https://Blog.Example/posts/Scaling")
    assert store.next_content_version("blog", digest) == 1
    with pytest.raises(InvalidTransition):
        # No enabled root: nothing is bound.
        store.bind_content_source_version(
            source_kind="blog", authority_id=digest, official_title="Scaling",
            version=1, tree_sha256="a" * 64, metadata={"normalized_url": normalized},
        )
    assert store.list_sources() == []
    _roots(store, tmp_path)
    first = store.bind_content_source_version(
        source_kind="blog", authority_id=digest, official_title="Scaling",
        version=1, tree_sha256="a" * 64, metadata={"normalized_url": normalized},
    )
    source = first["source"]
    assert (source["source_kind"], source["canonical_id"]) == ("blog", f"url:{digest}")
    assert source["engine_ref"] == f"blog:{digest}" and source["import_state"] == "existing"
    assert first["binding"]["directory"] == f"{digest[:16]}/v1"
    assert first["binding"]["root_id"] == "blogs"
    replay = store.bind_content_source_version(
        source_kind="blog", authority_id=digest, official_title="Scaling",
        version=1, tree_sha256="a" * 64, metadata={"normalized_url": normalized},
    )
    assert replay["binding"] == first["binding"]
    for version, tree in ((1, "b" * 64), (3, "b" * 64)):
        with pytest.raises(InvalidTransition):
            store.bind_content_source_version(
                source_kind="blog", authority_id=digest, official_title="Scaling",
                version=version, tree_sha256=tree, metadata={},
            )
    second = store.bind_content_source_version(
        source_kind="blog", authority_id=digest, official_title="Scaling, revised",
        version=2, tree_sha256="b" * 64, metadata={"content_source": "jina"},
    )
    assert second["source"]["id"] == source["id"]
    assert second["source"]["official_title"] == "Scaling, revised"
    assert store.latest_content_binding(source["id"])["version"] == 2
    assert store.next_content_version("blog", digest) == 3
    with pytest.raises(ValueError):
        store.bind_content_source_version(
            source_kind="paper", authority_id="2609.00001", official_title="x",
            version=1, tree_sha256="a" * 64, metadata={},
        )


def test_two_notes_link_to_one_blog(store: ControlStore, tmp_path: Path) -> None:
    _roots(store, tmp_path)
    _follow(store)
    _, digest = blog_url_identity("https://blog.example/post")
    links = []
    for index, note_id in enumerate((NOTE, SECOND_NOTE), 1):
        _note(store, note_id)
        note = store.bind_content_source_version(
            source_kind="xhs_note", authority_id=note_id, official_title=f"Note {index}",
            version=1, tree_sha256="c" * 64, metadata={},
        )["source"]
        blog = store.bind_content_source_version(
            source_kind="blog", authority_id=digest, official_title="A blog",
            version=1, tree_sha256="d" * 64, metadata={},
        )["source"]
        with store._transaction() as conn:
            conn.execute(
                """UPDATE xhs_notes SET source_id = ?, content_version = 1, state = 'saved'
                   WHERE note_id = ?""",
                (note["id"], note_id),
            )
            store._xhs_upsert_image(conn, note_id=note_id, ordinal=4, fileid="f4")
            rec = store._xhs_upsert_recommendation(
                conn, note_id=note_id, item_key="title:a-blog:4", image_ordinal=4,
                kind="blog", title="A blog", quote="A blog", arxiv_id=None,
                url="https://blog.example/post", url_state="from_text", origin="rule",
                identify_run="identify:1",
            )
            link = store._insert_source_link(
                conn, from_source_id=note["id"], to_source_id=blog["id"],
                recommendation_id=rec["id"],
            )
            assert store._insert_source_link(
                conn, from_source_id=note["id"], to_source_id=blog["id"],
                recommendation_id=rec["id"],
            ) == link
            with pytest.raises(InvalidTransition):
                store._insert_source_link(
                    conn, from_source_id=note["id"], to_source_id=note["id"],
                    recommendation_id=rec["id"],
                )
        links.append((note["id"], rec["id"]))
    blog_links = store.list_source_links(blog["id"])
    assert blog_links["recommends"] == []
    assert [(item["source_id"], item["recommendation_id"], item["image_ordinal"],
             item["source_kind"]) for item in blog_links["recommended_in"]] == [
        (note_id, rec_id, 4, "xhs_note") for note_id, rec_id in links
    ]
    note_links = store.list_source_links(links[0][0])
    assert [item["source_id"] for item in note_links["recommends"]] == [blog["id"]]
    assert len([s for s in store.list_sources() if s["source_kind"] == "blog"]) == 1
