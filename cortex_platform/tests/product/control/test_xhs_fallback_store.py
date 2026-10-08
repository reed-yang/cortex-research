"""The weekly recommendation fallback in Control: reviews, the free rules, run
selection, item stages, applying a decision, and the operator's exclusion.

Synthetic identities only. No child runs here: each stage's answer is what the
drain would record after its child.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cortex_platform.product.control import (
    ControlStore,
    IdempotencyConflict,
    InvalidTransition,
    RevisionConflict,
)
from cortex_platform.tests.product.control.test_fragments import (
    DeterministicIds,
    MovableClock,
)
from cortex_platform.tests.product.control.test_xhs_store import (
    ACTOR,
    NOTE,
    SECOND_NOTE,
    _follow,
    _note,
)

LEASE = 900
WEEK = 7 * 86_400
REVIEW_FIELDS = {"state", "method", "reason_code", "reason", "corrected_fields", "updated_at"}


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MovableClock) -> ControlStore:
    value = ControlStore(tmp_path / "control.db", clock=clock, id_factory=DeterministicIds())
    value.initialize()
    _follow(value)
    return value


def _saved(store: ControlStore, note_id: str = NOTE) -> str:
    """A saved note at content version 1; answers its source ID."""

    _note(store, note_id, caption="Caption naming Attention Sinks and a paper.")
    with store._transaction() as conn:
        store._xhs_upsert_image(conn, note_id=note_id, ordinal=1, fileid="file-1")
        source_id = store._register_content_source(
            conn, source_kind="xhs_note", authority_id=note_id, official_title="A note"
        )
        conn.execute(
            """UPDATE xhs_notes SET state = 'saved', source_id = ?, content_version = 1
               WHERE note_id = ?""",
            (source_id, note_id),
        )
    return source_id


def _rec(
    store: ControlStore,
    key: str,
    kind: str,
    title: str,
    *,
    note_id: str = NOTE,
    url: str | None = None,
    arxiv_id: str | None = None,
    image: int | None = None,
    clock: MovableClock | None = None,
) -> dict[str, Any]:
    if clock is not None:
        clock.advance(1)
    with store._transaction() as conn:
        return store._xhs_upsert_recommendation(
            conn, note_id=note_id, item_key=key, image_ordinal=image, kind=kind,
            title=title, quote=title, arxiv_id=arxiv_id, url=url,
            url_state="from_text" if url else "none", origin="model",
            identify_run="identify:run-1",
        )


def _start(store: ControlStore, cap: int = 100, trigger: str = "schedule") -> dict[str, Any]:
    return store.start_xhs_fallback_run(
        trigger=trigger, item_cap=cap, model="gpt-6.1-sol", effort="xhigh"
    )


def _count(store: ControlStore, sql: str, *args: Any) -> int:
    with sqlite3.connect(store.path) as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def _no_capture(store: ControlStore) -> None:
    """No paper was imported: no Capture and no Capture link."""

    assert _count(store, "SELECT COUNT(*) FROM captures") == 0
    assert _count(store, "SELECT COUNT(*) FROM xhs_tasks WHERE kind = 'capture_link'") == 0
    assert _count(
        store, "SELECT COUNT(*) FROM xhs_recommendations WHERE capture_id IS NOT NULL"
    ) == 0


def _events(store: ControlStore, prefix: str) -> list[str]:
    with sqlite3.connect(store.path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT type FROM control_audit WHERE type GLOB ? ORDER BY rowid", (prefix + "*",)
            )
        ]


def _decide(store: ControlStore) -> dict[str, Any]:
    """Claim the next decide stage and start its call."""

    item = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    assert item is not None and item["stage"] == "decide"
    started, day = store.begin_xhs_fallback_call(
        item["id"], expected_revision=item["revision"], cap=300
    )
    assert day is not None and started["call_state"] == "may_have_started"
    return started


# -- views ------------------------------------------------------------------------


def test_the_note_view_carries_each_recommendation_review(store: ControlStore) -> None:
    source_id = _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog", url="https://arxiv.org/abs/2509.00001")
    other = _rec(store, "blog:b", "blog", "Another blog")
    assert store.apply_xhs_fallback_rules() == {"converted": 1, "duplicates": 0}
    views = {r["id"]: r for r in store.xhs_note_view(source_id)["recommendations"]}
    assert views[other["id"]]["review"] is None
    review = views[blog["id"]]["review"]
    assert set(review) == REVIEW_FIELDS
    assert review | {"updated_at": None} == {
        "state": "resolved_paper", "method": "rule", "reason_code": "arxiv_link",
        "reason": "The link is the arXiv page of 2509.00001.",
        "corrected_fields": ["kind", "arxiv_id"], "updated_at": None,
    }


# -- rules ----------------------------------------------------------------------------


def test_an_arxiv_link_makes_a_blog_the_paper_and_imports_nothing(
    store: ControlStore,
) -> None:
    _saved(store)
    note_before = store.get_xhs_note(NOTE)
    blog = _rec(store, "blog:a", "blog", "A blog", url="https://arxiv.org/pdf/2509.00001v2")
    plain = _rec(store, "blog:b", "blog", "Plain", url="https://blog.example/post")
    assert store.apply_xhs_fallback_rules() == {"converted": 1, "duplicates": 0}
    paper = store.get_xhs_recommendation(blog["id"])
    assert (paper["kind"], paper["arxiv_id"], paper["url"]) == (
        "paper", "2509.00001", "https://arxiv.org/pdf/2509.00001v2",
    )
    # Identity, evidence and identification stay; the revisions move.
    for name in ("item_key", "title", "quote", "image_ordinal", "origin", "identify_run"):
        assert paper[name] == blog[name]
    assert paper["revision"] > blog["revision"] and paper["import_state"] == "none"
    assert store.get_xhs_note(NOTE)["revision"] > note_before["revision"]
    assert store.get_xhs_recommendation(plain["id"]) == plain
    # The note's next version is queued as for a link edit.
    assert _count(store, "SELECT COUNT(*) FROM xhs_tasks WHERE subject_key = ?",
                  f"save:{NOTE}:2") == 1
    assert _events(store, "xhs.recommendation.") == ["xhs.recommendation.corrected"]
    _no_capture(store)
    # A second application finds nothing left to do.
    assert store.apply_xhs_fallback_rules() == {"converted": 0, "duplicates": 0}


def test_a_second_link_to_a_paper_in_the_same_note_is_a_duplicate(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    _saved(store, SECOND_NOTE)
    paper = _rec(store, "arxiv:2509.00002", "paper", "Known", arxiv_id="2509.00002",
                 clock=clock)
    same = _rec(store, "blog:a", "blog", "Same paper",
                url="https://arxiv.org/abs/2509.00002", clock=clock)
    first = _rec(store, "blog:b", "blog", "First", url="https://arxiv.org/abs/2509.00003",
                 clock=clock)
    second = _rec(store, "blog:c", "blog", "Second", url="https://arxiv.org/html/2509.00003v1",
                  clock=clock)
    elsewhere = _rec(store, "blog:d", "blog", "Elsewhere", note_id=SECOND_NOTE,
                     url="https://arxiv.org/abs/2509.00002", clock=clock)
    assert store.apply_xhs_fallback_rules() == {"converted": 2, "duplicates": 2}
    with store._connect() as conn:
        reviews = {rid: store._xhs_review(conn, rid)
                   for rid in (same["id"], first["id"], second["id"], elsewhere["id"])}
    assert (reviews[same["id"]]["state"], reviews[same["id"]]["duplicate_of"]) == (
        "excluded", paper["id"],
    )
    assert reviews[same["id"]]["reason_code"] == "duplicate"
    assert store.get_xhs_recommendation(same["id"])["kind"] == "blog"
    assert reviews[first["id"]]["state"] == "resolved_paper"
    assert (reviews[second["id"]]["state"], reviews[second["id"]]["duplicate_of"]) == (
        "excluded", first["id"],
    )
    # Never across notes.
    assert reviews[elsewhere["id"]]["state"] == "resolved_paper"
    assert store.get_xhs_recommendation(elsewhere["id"])["arxiv_id"] == "2509.00002"
    _no_capture(store)


def test_the_rules_leave_importing_and_reviewed_rows_alone(store: ControlStore) -> None:
    source_id = _saved(store)
    importing = _rec(store, "blog:a", "blog", "Importing", url="https://arxiv.org/abs/2509.00004")
    excluded = _rec(store, "blog:b", "blog", "Excluded", url="https://arxiv.org/abs/2509.00005")
    resolved = _rec(store, "blog:c", "blog", "Resolved", url="https://arxiv.org/abs/2509.00006")
    with store._transaction() as conn:
        store._xhs_update_recommendation(
            conn, importing["id"], expected_revision=importing["revision"],
            import_state="importing",
        )
        store._xhs_set_review(conn, resolved["id"], state="resolved_blog", method="model",
                              corrected=("url",))
    store.exclude_xhs_recommendation(
        note_source_id=source_id, recommendation_id=excluded["id"], reason="Not wanted",
        expected_revision=excluded["revision"], actor_id=ACTOR,
        idempotency_key="exclude-00000000001",
    )
    assert store.apply_xhs_fallback_rules() == {"converted": 1, "duplicates": 0}
    assert store.get_xhs_recommendation(importing["id"])["kind"] == "blog"
    assert store.get_xhs_recommendation(excluded["id"])["kind"] == "blog"
    converted = store.get_xhs_recommendation(resolved["id"])
    assert (converted["kind"], converted["arxiv_id"]) == ("paper", "2509.00006")
    with store._connect() as conn:
        # The earlier correction stays listed beside the rule's.
        assert store._xhs_review(conn, resolved["id"])["corrected_fields"] == [
            "kind", "arxiv_id", "url",
        ]
    with pytest.raises(ValueError):
        store.apply_xhs_fallback_rules(limit=101)


def test_reidentification_keeps_corrected_fields(store: ControlStore) -> None:
    _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog", url="https://arxiv.org/abs/2509.00001")
    store.apply_xhs_fallback_rules()
    again = _rec(store, "blog:a", "blog", "A blog, retitled",
                 url="https://arxiv.org/abs/2509.00001")
    assert (again["kind"], again["arxiv_id"], again["title"]) == (
        "paper", "2509.00001", "A blog, retitled",
    )
    # A corrected link survives a link found in the text.
    linked = _rec(store, "blog:b", "blog", "Linked", url="https://blog.example/old")
    with store._transaction() as conn:
        store._xhs_correct_recommendation(
            conn, linked, url="https://blog.example/new", url_state="auto_matched",
            url_checked_title="Linked",
        )
        store._xhs_set_review(conn, linked["id"], state="resolved_blog", method="model",
                              corrected=("url",), touch=False)
    kept = _rec(store, "blog:b", "blog", "Linked", url="https://blog.example/other")
    assert (kept["url"], kept["url_state"]) == ("https://blog.example/new", "auto_matched")
    with store._connect() as conn:
        assert store._xhs_review(conn, blog["id"])["state"] == "resolved_paper"


# -- selection ---------------------------------------------------------------------------


def test_a_run_takes_blogs_then_papers_oldest_first_up_to_the_cap(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    _note(store, SECOND_NOTE)  # never saved
    paper_old = _rec(store, "paper:old", "paper", "Old paper", clock=clock)
    blog_old = _rec(store, "blog:old", "blog", "Old blog", clock=clock)
    failed = _rec(store, "blog:failed", "blog", "Failed blog", url="https://b.example/f",
                  clock=clock)
    paper_new = _rec(store, "paper:new", "paper", "New paper", clock=clock)
    blog_new = _rec(store, "blog:new", "blog", "New blog", clock=clock)
    # Not taken: other items, papers with an ID, imports in progress, waiting
    # blog imports, unsaved notes and reviewed rows.
    _rec(store, "other:a", "other", "A course", clock=clock)
    _rec(store, "arxiv:2509.00009", "paper", "With ID", arxiv_id="2509.00009", clock=clock)
    importing = _rec(store, "blog:importing", "blog", "Importing", url="https://b.example/i",
                     clock=clock)
    waiting = _rec(store, "blog:waiting", "blog", "Waiting", url="https://b.example/w",
                   clock=clock)
    _rec(store, "blog:unsaved", "blog", "Unsaved", note_id=SECOND_NOTE, clock=clock)
    reviewed = _rec(store, "blog:reviewed", "blog", "Reviewed", clock=clock)
    with store._transaction() as conn:
        store._xhs_update_recommendation(conn, failed["id"], expected_revision=0,
                                         import_state="failed")
        store._xhs_update_recommendation(conn, importing["id"], expected_revision=0,
                                         import_state="importing")
        store._xhs_create_task(conn, kind="blog_import", subject_key=f"blog:{waiting['id']}:1",
                               payload={"recommendation_id": waiting["id"]})
        store._xhs_set_review(conn, reviewed["id"], state="needs_operator", method="model",
                              reason_code="insufficient_evidence")
    preview = store.preview_xhs_fallback_run(item_cap=4)
    assert [item["recommendation_id"] for item in preview["selected"]] == [
        blog_old["id"], failed["id"], blog_new["id"], paper_old["id"],
    ]
    assert preview["selection"] == {
        "eligible": 5, "already_reviewed": 0, "selected": 4, "waiting": 1,
    }
    started = _start(store, cap=4)
    run = started["run"]
    assert started["refusal"] is None and started["rules"] == {"converted": 0, "duplicates": 0}
    assert (run["state"], run["trigger"], run["item_cap"], run["digest_state"]) == (
        "running", "schedule", 4, "none",
    )
    assert (run["model"], run["effort"], run["prompt_version"]) == (
        "gpt-6.1-sol", "xhigh", "xhs-fallback-1",
    )
    items = store.list_xhs_fallback_items(run["id"])
    assert [(i["ordinal"], i["recommendation_id"]) for i in items] == [
        (1, blog_old["id"]), (2, failed["id"]), (3, blog_new["id"]), (4, paper_old["id"]),
    ]
    assert [i["input_sha256"] for i in items] == [
        i["input_sha256"] for i in preview["selected"]
    ]
    assert all(
        (i["state"], i["call_state"], i["attempts"], i["expected_revision"])
        == ("pending", "not_started", 0, store.get_xhs_recommendation(i["recommendation_id"])[
            "revision"])
        for i in items
    )
    assert paper_new["id"] not in {i["recommendation_id"] for i in items}
    assert _events(store, "xhs.fallback.") == ["xhs.fallback.started"]
    # The configured cap never exceeds 100, and a cap below 1 is refused.
    with pytest.raises(ValueError):
        store.preview_xhs_fallback_run(item_cap=0)
    assert store.preview_xhs_fallback_run(item_cap=500)["refusal"] == "running"


def test_a_start_applies_the_rules_and_the_preview_writes_nothing(
    store: ControlStore,
) -> None:
    _saved(store)
    arxiv = _rec(store, "blog:a", "blog", "On arXiv", url="https://arxiv.org/abs/2509.00001")
    blog = _rec(store, "blog:b", "blog", "A blog")
    with sqlite3.connect(store.path) as conn:
        before = conn.execute("SELECT * FROM xhs_recommendations ORDER BY id").fetchall()
    preview = store.preview_xhs_fallback_run(item_cap=100)
    assert preview["rules"] == [{"recommendation_id": arxiv["id"], "note_id": NOTE,
                                 "arxiv_id": "2509.00001", "duplicate_of": None}]
    # The row a rule changes is not selected.
    assert [item["recommendation_id"] for item in preview["selected"]] == [blog["id"]]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT * FROM xhs_recommendations ORDER BY id").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM xhs_fallback_runs").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM xhs_recommendation_reviews").fetchone() == (0,)
    started = _start(store, trigger="operator")
    assert started["rules"] == {"converted": 1, "duplicates": 0}
    assert [i["recommendation_id"] for i in store.list_xhs_fallback_items(
        started["run"]["id"])] == [blog["id"]]


def test_runs_start_one_at_a_time_and_at_most_weekly(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    _rec(store, "blog:a", "blog", "A blog")
    assert _start(store)["refusal"] is None
    first = store.xhs_fallback_state()["running"]
    assert first is not None
    assert _start(store)["refusal"] == "running"
    item = _decide(store)
    store.apply_xhs_fallback_result(
        item["id"], expected_revision=item["revision"],
        decision={"action": "exclude", "reason_code": "not_a_blog", "reason": "A course."},
    )
    state = store.xhs_fallback_state()
    assert state["running"] is None and state["last"]["id"] == first["id"]
    clock.advance(WEEK - 1)
    another = _rec(store, "blog:b", "blog", "Another blog")
    refused = _start(store)
    assert refused["refusal"] == "too_soon" and refused["run"] is None
    assert refused["next_start_at"] == state["next_start_at"]
    clock.advance(1)
    second = _start(store)
    assert second["refusal"] is None
    # The excluded row is reviewed; only the new one is taken.
    assert [i["recommendation_id"] for i in store.list_xhs_fallback_items(
        second["run"]["id"])] == [another["id"]]


def test_an_input_reviewed_once_is_not_taken_again(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog")
    run = _start(store)["run"]
    # A change that leaves the model input as it was makes the item stale.
    with store._transaction() as conn:
        store._xhs_update_recommendation(conn, blog["id"], expected_revision=blog["revision"],
                                         import_state="failed")
    assert store.claim_xhs_fallback_item(lease_seconds=LEASE) is None
    done = store.get_xhs_fallback_run(run["id"])
    assert done["state"] == "completed" and done["summary"]["stale"] == 1
    assert done["digest_state"] == "suppressed"
    clock.advance(WEEK)
    again = _start(store)
    assert again["refusal"] == "nothing_selected"
    assert again["selection"]["already_reviewed"] == 1
    # A changed input is taken.
    _rec(store, "blog:a", "blog", "A blog, retitled")
    assert _start(store)["run"] is not None


# -- stages and applying -----------------------------------------------------------------


def test_a_verified_blog_is_queued_for_import_and_nothing_else(store: ControlStore) -> None:
    _saved(store)
    blog = _rec(store, "blog:a", "blog", "Attention Sinks", image=1)
    run = _start(store)["run"]
    note_revision = store.get_xhs_note(NOTE)["revision"]
    claimed = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    assert claimed["stage"] == "decide" and claimed["lease_until"] is not None
    assert claimed["context"]["note"] == {
        "note_id": NOTE, "title": "本周论文 weekly papers",
        "caption": "Caption naming Attention Sinks and a paper.",
    }
    assert claimed["context"]["image"] == {"ordinal": 1, "ocr_state": "pending",
                                           "ocr_text_sha256": None}
    assert claimed["context"]["recommendation"]["id"] == blog["id"]
    item, day = store.begin_xhs_fallback_call(
        claimed["id"], expected_revision=claimed["revision"], cap=300
    )
    assert store.xhs_usage(day)["gpt"] == 1
    item = store.record_xhs_fallback_proposal(
        item["id"], expected_revision=item["revision"],
        proposal={"outcome": "corrected_url", "url": "https://blog.example/sinks"},
        usage={"input_tokens": 900, "output_tokens": 40, "web_search_calls": 2},
    )
    assert (item["state"], item["call_state"], item["lease_until"]) == (
        "verifying", "finished", None,
    )
    verify = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    assert (verify["stage"], verify["attempts"]) == ("verify", 1)
    done = store.apply_xhs_fallback_result(
        verify["id"], expected_revision=verify["revision"],
        decision={"action": "blog", "url": "https://blog.example/sinks",
                  "checked_title": "Attention Sinks | Blog"},
        verification={"final_url": "https://blog.example/sinks", "title_match": True},
    )
    assert (done["state"], done["applied"]) == ("done", "blog_queued")
    assert done["usage"] == {"input_tokens": 900, "output_tokens": 40, "web_search_calls": 2}
    assert done["verification"]["title_match"] is True
    corrected = store.get_xhs_recommendation(blog["id"])
    assert (corrected["url"], corrected["url_state"], corrected["url_checked_title"]) == (
        "https://blog.example/sinks", "auto_matched", "Attention Sinks | Blog",
    )
    assert corrected["import_state"] == "importing"
    assert store.get_xhs_note(NOTE)["revision"] > note_revision
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT subject_key, state FROM xhs_tasks WHERE kind = 'blog_import'"
        ).fetchall() == [(f"blog:{blog['id']}:1", "pending")]
    with store._connect() as conn:
        review = store._xhs_review(conn, blog["id"])
    assert (review["state"], review["method"], review["corrected_fields"], review["run_id"]) == (
        "resolved_blog", "model", ["url"], run["id"],
    )
    finished = store.get_xhs_fallback_run(run["id"])
    assert (finished["state"], finished["digest_state"]) == ("completed", "suppressed")
    assert finished["finished_at"] is not None and finished["summary"]["blog_queued"] == 1
    assert [e for e in _events(store, "xhs.") if e.startswith(("xhs.fallback.",
            "xhs.recommendation."))] == [
        "xhs.fallback.started", "xhs.recommendation.corrected", "xhs.fallback.completed",
    ]
    _no_capture(store)


def test_paper_corrections_never_stage_a_capture(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    blog_id = _rec(store, "blog:a", "blog", "Blog paper", clock=clock)
    blog_none = _rec(store, "blog:b", "blog", "Blog off arXiv", clock=clock)
    paper_id = _rec(store, "paper:a", "paper", "Paper gains ID", clock=clock)
    paper_none = _rec(store, "paper:b", "paper", "Paper off arXiv", clock=clock)
    _start(store)
    decisions = {
        blog_id["id"]: {"action": "paper", "arxiv_id": "2509.00011"},
        blog_none["id"]: {"action": "paper", "arxiv_id": None, "reason": "A workshop paper."},
        paper_id["id"]: {"action": "paper", "arxiv_id": "2509.00012v2"},
        paper_none["id"]: {"action": "paper", "arxiv_id": None},
    }
    applied = {}
    for _ in decisions:
        item = _decide(store)
        result = store.apply_xhs_fallback_result(
            item["id"], expected_revision=item["revision"],
            decision=decisions[item["recommendation_id"]],
            proposal={"outcome": "reclassify_paper"},
        )
        applied[item["recommendation_id"]] = result["applied"]
        assert result["call_state"] == "finished"
    assert applied == {
        blog_id["id"]: "paper_corrected", blog_none["id"]: "paper_corrected",
        paper_id["id"]: "paper_corrected", paper_none["id"]: "paper_kept",
    }
    rows = {rid: store.get_xhs_recommendation(rid) for rid in decisions}
    assert (rows[blog_id["id"]]["kind"], rows[blog_id["id"]]["arxiv_id"]) == (
        "paper", "2509.00011",
    )
    assert (rows[blog_none["id"]]["kind"], rows[blog_none["id"]]["arxiv_id"]) == ("paper", None)
    assert rows[paper_id["id"]]["arxiv_id"] == "2509.00012"
    assert rows[paper_none["id"]] == {**paper_none, "revision": paper_none["revision"] + 1,
                                      "updated_at": rows[paper_none["id"]]["updated_at"]}
    assert all(row["import_state"] == "none" for row in rows.values())
    with store._connect() as conn:
        reviews = {rid: store._xhs_review(conn, rid) for rid in decisions}
    assert {rid: (r["state"], r["reason_code"], r["corrected_fields"])
            for rid, r in reviews.items()} == {
        blog_id["id"]: ("resolved_paper", None, ["kind", "arxiv_id"]),
        blog_none["id"]: ("resolved_paper", "not_on_arxiv", ["kind"]),
        paper_id["id"]: ("resolved_paper", None, ["arxiv_id"]),
        paper_none["id"]: ("resolved_paper", "not_on_arxiv", []),
    }
    _no_capture(store)


def test_a_model_paper_already_in_the_note_is_a_duplicate(store: ControlStore) -> None:
    _saved(store)
    known = _rec(store, "arxiv:2509.00021", "paper", "Known", arxiv_id="2509.00021")
    blog = _rec(store, "blog:a", "blog", "Same paper")
    _start(store)
    item = _decide(store)
    done = store.apply_xhs_fallback_result(
        item["id"], expected_revision=item["revision"],
        decision={"action": "paper", "arxiv_id": "2509.00021"},
    )
    assert done["applied"] == "excluded"
    with store._connect() as conn:
        review = store._xhs_review(conn, blog["id"])
    assert (review["state"], review["reason_code"], review["duplicate_of"]) == (
        "excluded", "duplicate", known["id"],
    )
    assert store.get_xhs_recommendation(blog["id"])["kind"] == "blog"


def test_exclusions_and_undecided_items_make_a_pending_digest(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    course = _rec(store, "blog:a", "blog", "A course", clock=clock)
    unclear = _rec(store, "blog:b", "blog", "Unclear", clock=clock)
    run = _start(store)["run"]
    for decision in (
        {"action": "exclude", "reason_code": "not_a_blog", "reason": "It is a course."},
        {"action": "needs_operator", "reason_code": "conflicting_evidence"},
    ):
        item = _decide(store)
        store.apply_xhs_fallback_result(item["id"], expected_revision=item["revision"],
                                        decision=decision)
    with store._connect() as conn:
        excluded = store._xhs_review(conn, course["id"])
        undecided = store._xhs_review(conn, unclear["id"])
    assert (excluded["state"], excluded["method"], excluded["reason"]) == (
        "excluded", "model", "It is a course.",
    )
    assert (undecided["state"], undecided["reason_code"]) == (
        "needs_operator", "conflicting_evidence",
    )
    finished = store.get_xhs_fallback_run(run["id"])
    assert finished["digest_state"] == "pending"
    assert (finished["summary"]["excluded"], finished["summary"]["needs_operator"]) == (1, 1)
    assert "xhs.recommendation.excluded" in _events(store, "xhs.recommendation.")


def test_apply_finds_a_changed_recommendation_stale_and_changes_nothing(
    store: ControlStore,
) -> None:
    source_id = _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog")
    run = _start(store)["run"]
    item = _decide(store)
    store.set_xhs_recommendation_link(
        note_source_id=source_id, recommendation_id=blog["id"],
        url="https://blog.example/operator", expected_revision=blog["revision"],
        actor_id=ACTOR, idempotency_key="link-000000000001",
    )
    edited = store.get_xhs_recommendation(blog["id"])
    stale = store.apply_xhs_fallback_result(
        item["id"], expected_revision=item["revision"],
        decision={"action": "blog", "url": "https://blog.example/model",
                  "checked_title": "A blog"},
        proposal={"outcome": "corrected_url"},
    )
    assert (stale["state"], stale["applied"], stale["call_state"]) == ("stale", None, "finished")
    assert stale["proposal"] == {"outcome": "corrected_url"}
    assert store.get_xhs_recommendation(blog["id"]) == edited
    with store._connect() as conn:
        assert store._xhs_review(conn, blog["id"]) is None
    assert _count(store, "SELECT COUNT(*) FROM xhs_tasks WHERE kind = 'blog_import'") == 0
    finished = store.get_xhs_fallback_run(run["id"])
    assert finished["summary"]["stale"] == 1 and finished["digest_state"] == "suppressed"


def test_an_expired_call_is_never_made_again(store: ControlStore, clock: MovableClock) -> None:
    _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog")
    run = _start(store)["run"]
    item = _decide(store)
    clock.advance(LEASE + 1)
    assert store.claim_xhs_fallback_item(lease_seconds=LEASE) is None
    unknown = store.get_xhs_fallback_item(item["id"])
    assert (unknown["state"], unknown["applied"], unknown["call_state"]) == (
        "done", "needs_operator", "may_have_started",
    )
    with store._connect() as conn:
        assert store._xhs_review(conn, blog["id"])["reason_code"] == "outcome_unknown"
    assert store.xhs_usage()["gpt"] == 1
    assert store.get_xhs_fallback_run(run["id"])["digest_state"] == "pending"
    with pytest.raises(RevisionConflict):
        store.apply_xhs_fallback_result(
            item["id"], expected_revision=item["revision"],
            decision={"action": "exclude", "reason_code": "not_a_blog"},
        )


def test_a_lease_lost_before_the_call_is_claimed_again(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    _rec(store, "blog:a", "blog", "A blog")
    _start(store)
    first = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    assert store.claim_xhs_fallback_item(lease_seconds=LEASE) is None
    clock.advance(LEASE + 1)
    again = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    assert again["id"] == first["id"] and again["stage"] == "decide"
    with pytest.raises(InvalidTransition):
        # A call is recorded only after it may have started.
        store.apply_xhs_fallback_result(
            again["id"], expected_revision=again["revision"],
            decision={"action": "exclude", "reason_code": "not_a_blog"},
        )


def test_the_daily_cap_and_a_refused_dispatch_leave_the_item_pending(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    _rec(store, "blog:a", "blog", "A blog")
    _start(store)
    claimed = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    waiting, day = store.begin_xhs_fallback_call(
        claimed["id"], expected_revision=claimed["revision"], cap=0
    )
    assert day is None and (waiting["state"], waiting["lease_until"]) == ("pending", None)
    assert waiting["next_attempt_at"] == "2026-09-02T00:00:00.000000Z"
    assert store.claim_xhs_fallback_item(lease_seconds=LEASE) is None
    clock.advance(12 * 3_600)
    item = _decide(store)
    assert store.xhs_usage()["gpt"] == 1
    # Credentials missing: proven before dispatch, so the call is refunded.
    released = store.release_xhs_fallback_item(
        item["id"], expected_revision=item["revision"], refund_day="2026-09-02"
    )
    assert (released["state"], released["call_state"]) == ("pending", "not_started")
    assert store.xhs_usage()["gpt"] == 0


def test_verification_retries_twice_then_leaves_the_item_to_the_operator(
    store: ControlStore, clock: MovableClock
) -> None:
    _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog")
    _start(store)
    item = _decide(store)
    store.record_xhs_fallback_proposal(
        item["id"], expected_revision=item["revision"],
        proposal={"outcome": "corrected_url", "url": "https://blog.example/a"},
    )
    for wait in (600, 3_600):
        verify = store.claim_xhs_fallback_item(lease_seconds=LEASE)
        assert verify["stage"] == "verify"
        waiting = store.fail_xhs_fallback_verification(
            verify["id"], expected_revision=verify["revision"], category="transient"
        )
        assert (waiting["state"], waiting["last_error"]) == ("verifying", "transient")
        assert store.claim_xhs_fallback_item(lease_seconds=LEASE) is None
        clock.advance(wait)
    last = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    assert last["attempts"] == 3
    final = store.fail_xhs_fallback_verification(
        last["id"], expected_revision=last["revision"], category="transient",
        verification={"error": "transient"},
    )
    assert (final["state"], final["applied"]) == ("done", "needs_operator")
    with store._connect() as conn:
        review = store._xhs_review(conn, blog["id"])
    assert (review["state"], review["reason_code"]) == ("needs_operator", "fetch_failed")
    # One model call in all.
    assert store.xhs_usage()["gpt"] == 1


def test_a_refused_fetch_is_never_retried_or_an_exclusion(store: ControlStore) -> None:
    _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog")
    _start(store)
    item = _decide(store)
    store.record_xhs_fallback_proposal(
        item["id"], expected_revision=item["revision"], proposal={"outcome": "corrected_url"},
    )
    verify = store.claim_xhs_fallback_item(lease_seconds=LEASE)
    with pytest.raises(ValueError):
        # A paper host is never imported as a blog.
        store.apply_xhs_fallback_result(
            verify["id"], expected_revision=verify["revision"],
            decision={"action": "blog", "url": "https://openreview.net/forum?id=abc",
                      "checked_title": "A blog"},
        )
    final = store.fail_xhs_fallback_verification(
        verify["id"], expected_revision=verify["revision"], category="upstream_error"
    )
    assert final["applied"] == "needs_operator"
    with store._connect() as conn:
        assert store._xhs_review(conn, blog["id"])["state"] == "needs_operator"


# -- the operator's exclusion -------------------------------------------------------------


def test_exclude_and_restore_follow_the_receipt_and_revision_fence(
    store: ControlStore,
) -> None:
    source_id = _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog", url="https://blog.example/a")

    def exclude(key: str, revision: int, reason: str = "Not a recommendation"):
        return store.exclude_xhs_recommendation(
            note_source_id=source_id, recommendation_id=blog["id"], reason=reason,
            expected_revision=revision, actor_id=ACTOR, idempotency_key=key,
        )

    excluded = exclude("exclude-00000000001", blog["revision"])
    view = excluded.value["recommendation"]
    assert view["revision"] == blog["revision"] + 1
    assert view["review"] | {"updated_at": None} == {
        "state": "excluded", "method": "operator", "reason_code": "operator",
        "reason": "Not a recommendation", "corrected_fields": [], "updated_at": None,
    }
    assert exclude("exclude-00000000001", blog["revision"]).replayed is True
    with pytest.raises(IdempotencyConflict):
        exclude("exclude-00000000001", blog["revision"], reason="Another reason")
    with pytest.raises(RevisionConflict):
        exclude("exclude-00000000002", blog["revision"])
    with pytest.raises(InvalidTransition):
        exclude("exclude-00000000003", view["revision"])
    with pytest.raises(ValueError):
        exclude("exclude-00000000004", view["revision"], reason="   ")
    # The import refuses it, and no run takes it.
    note = store.get_xhs_note(NOTE)
    refused = store.import_xhs_recommendations(
        note_source_id=source_id, recommendation_ids=[blog["id"]],
        expected_revision=note["revision"], actor_id=ACTOR,
        idempotency_key="import-000000000001",
    ).value["items"][0]
    assert (refused["disposition"], refused["reason"]) == ("refused", "excluded")
    assert refused["recommendation"]["review"]["state"] == "excluded"
    assert store.preview_xhs_fallback_run(item_cap=100)["selected"] == []

    restored = store.restore_xhs_recommendation(
        note_source_id=source_id, recommendation_id=blog["id"],
        expected_revision=view["revision"], actor_id=ACTOR,
        idempotency_key="restore-0000000001",
    ).value["recommendation"]
    assert restored["review"]["state"] == "operator_owned"
    assert (restored["review"]["reason"], restored["review"]["reason_code"]) == (None, None)
    with pytest.raises(InvalidTransition):
        store.restore_xhs_recommendation(
            note_source_id=source_id, recommendation_id=blog["id"],
            expected_revision=restored["revision"], actor_id=ACTOR,
            idempotency_key="restore-0000000002",
        )
    # Automatic review leaves it alone; the operator may import it again.
    assert store.preview_xhs_fallback_run(item_cap=100)["selected"] == []
    imported = store.import_xhs_recommendations(
        note_source_id=source_id, recommendation_ids=[blog["id"]],
        expected_revision=store.get_xhs_note(NOTE)["revision"], actor_id=ACTOR,
        idempotency_key="import-000000000002",
    ).value["items"][0]
    assert imported["disposition"] == "blog_import_queued"
    # An importing row is neither excluded nor restored.
    with pytest.raises(InvalidTransition):
        exclude("exclude-00000000005", store.get_xhs_recommendation(blog["id"])["revision"])
    assert _events(store, "xhs.recommendation.") == [
        "xhs.recommendation.excluded", "xhs.recommendation.restored",
    ]


def test_an_exclusion_keeps_the_corrections_a_rule_made(store: ControlStore) -> None:
    source_id = _saved(store)
    blog = _rec(store, "blog:a", "blog", "A blog", url="https://arxiv.org/abs/2509.00001")
    store.apply_xhs_fallback_rules()
    paper = store.get_xhs_recommendation(blog["id"])
    view = store.exclude_xhs_recommendation(
        note_source_id=source_id, recommendation_id=paper["id"], reason="Already read",
        expected_revision=paper["revision"], actor_id=ACTOR,
        idempotency_key="exclude-00000000001",
    ).value["recommendation"]
    assert view["review"]["corrected_fields"] == ["kind", "arxiv_id"]
    again = _rec(store, "blog:a", "blog", "A blog")
    assert (again["kind"], again["arxiv_id"]) == ("paper", "2509.00001")
