"""The weekly recommendation fallback in the drain: rules, the run's start, its
model calls and page checks, and what they may apply.

The scripted supervisor from the pipeline tests answers each child; for the
fallback it answers by recommendation title or page URL. Nothing reaches a
provider. Every note, link and answer is synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.workflows.coordinator import EffectPermanentlyRejected
from cortex_platform.product.xhs import fallback
from cortex_platform.product.xhs.status import xhs_fallback_status, xhs_status
from cortex_platform.tests.product.test_xhs_drain import (  # noqa: F401 - fixtures
    SIGNATURE,
    Execution,
    clock,
    note_id,
    settings,
    staging,
    store,
    tasks,
)
from cortex_platform.tests.product.test_xhs_pipeline import (
    BLOG_TEXT,
    CAPTION_BLOG,
    PAPER_TEXT,
    PipelineSupervisor,
    _three_images,
    model_items,
    pipeline,
)

WEEK = 7 * 86_400
SINKS, STREAMING, SPECULATIVE = "Attention Sinks", "Efficient Streaming", "Speculative decoding"
BLOGS = (SINKS, STREAMING)
FALLBACK_OPERATIONS = frozenset({"xhs_fallback_decide", "xhs_fallback_verify"})


@dataclass
class FallbackSupervisor(PipelineSupervisor):
    """Adds the weekly fallback's two child operations.

    `decide` maps a recommendation title to a queue of answers: an answer
    object, None for an answer that was not JSON, a failure category, or an
    exception the dispatch raises before any call. `pages` maps a requested
    URL to a page (`title`, optional `og_title` and `final_url`) or a failure
    category, or to a queue of them.
    """

    decide: dict[str, list[Any]] = field(default_factory=dict)
    pages: dict[str, Any] = field(default_factory=dict)

    def run(self, operation: str, payload, *, write_roots=None) -> Execution:
        if operation not in FALLBACK_OPERATIONS:
            return super().run(operation, payload, write_roots=write_roots)
        if not self.store.runtime_dispatch_enabled():
            raise EffectPermanentlyRejected("runtime_activation_disabled")
        assert write_roots is None
        if operation == "xhs_fallback_decide":
            title = json.loads(payload["input"])["recommendation"]["title"]
            answer = self.decide[title].pop(0)
            if isinstance(answer, Exception):
                raise answer
            self.calls.append((operation, dict(payload), write_roots))
            marker = f"marker-{len(self.calls)}"
            if isinstance(answer, str):
                return Execution(False, None, answer, marker)
            # A callable rewrites the child's whole document.
            rewrite, answer = (answer, UNDECIDED) if callable(answer) else (None, answer)
            engine = {
                "prompt_version": fallback.PROMPT_VERSION,
                "input_text_sha256": fallback.input_text_sha256(payload["input"]),
                "response_id": "resp-fallback", "model": payload["gpt_model"],
                "usage": {"input_tokens": 1_200, "output_tokens": 80},
                "answer": answer,
                "answer_error": None if answer is not None else "responses: not JSON",
            }
            return Execution(True, rewrite(engine) if rewrite else engine, None, marker)
        self.calls.append((operation, dict(payload), write_roots))
        marker = f"marker-{len(self.calls)}"
        url = (
            payload["url"]
            if payload["check"] == "blog"
            else fallback.arxiv_abs_url(payload["arxiv_id"])
        )
        page = self.pages[url]
        if isinstance(page, list):
            page = page.pop(0)
        if isinstance(page, str):
            return Execution(False, None, page, marker)
        return Execution(True, {
            "check": payload["check"], "requested_url": url,
            "final_url": page.get("final_url", url), "title": page.get("title"),
            "og_title": page.get("og_title"), "paper_host": False,
        }, None, marker)

    def fallback_calls(self, operation: str = "xhs_fallback_decide") -> list[dict[str, Any]]:
        return [payload for name, payload, _ in self.calls if name == operation]

    def decided(self) -> list[str]:
        return [
            json.loads(payload["input"])["recommendation"]["title"]
            for payload in self.fallback_calls()
        ]


@pytest.fixture
def supervisor(store: ControlStore) -> FallbackSupervisor:
    return FallbackSupervisor(store=store)


def answer(outcome: str, **fields: Any) -> dict[str, Any]:
    value = {"outcome": outcome, "url": None, "arxiv_id": None, "reason_code": "found",
             "reason": "A short factual sentence."}
    value.update(fields)
    return value


UNDECIDED = answer("undecided", reason_code="insufficient_evidence")


def _saved(store: ControlStore, supervisor: FallbackSupervisor) -> dict[str, dict[str, Any]]:
    """A saved note whose two blogs are unimported and whose paper
    `Speculative decoding` has no arXiv ID; its recommendations by title."""

    _three_images(supervisor)
    supervisor.identify[PAPER_TEXT] = [model_items(extra=[
        {"kind": "paper", "title": SPECULATIVE, "image": 1, "quote": "# Speculative decoding",
         "arxiv_id": None, "url": None},
    ])]
    drain = pipeline(store, supervisor)
    drain.pull()
    drain.drain()
    assert store.get_xhs_note(note_id(1))["state"] == "saved"
    return recommendations(store)


def recommendations(store: ControlStore) -> dict[str, dict[str, Any]]:
    return {r["title"]: r for r in store.list_xhs_recommendations(note_id(1))}


def fallback_drain(store, supervisor, **overrides):
    values: dict[str, Any] = {"fallback_enabled": True, "gpt_base": "https://gpt.example/v1"}
    values.update(overrides)
    return pipeline(store, supervisor, **values)


def review(store: ControlStore, recommendation_id: str) -> dict[str, Any] | None:
    with store._connect() as conn:
        return store._xhs_review(conn, recommendation_id)


def items(store: ControlStore, run_id: str | None = None) -> dict[str, dict[str, Any]]:
    run_id = run_id or store.xhs_fallback_state()["last"]["id"]
    titles = {r["id"]: r["title"] for r in store.list_xhs_recommendations(note_id(1))}
    return {titles[item["recommendation_id"]]: item for item in store.list_xhs_fallback_items(run_id)}


def count(store: ControlStore, sql: str, *args: Any) -> int:
    with sqlite3.connect(store.path) as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def no_capture(store: ControlStore) -> None:
    assert count(store, "SELECT COUNT(*) FROM captures") == 0
    assert tasks(store, "capture_link") == []
    assert count(
        store, "SELECT COUNT(*) FROM xhs_recommendations WHERE capture_id IS NOT NULL"
    ) == 0


# -- the switch and the rules --------------------------------------------------------


def test_nothing_happens_while_the_fallback_is_off_and_rules_run_once_it_is_on(
    store: ControlStore, supervisor: FallbackSupervisor
) -> None:
    recs = _saved(store, supervisor)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE xhs_recommendations SET url = ? WHERE id = ?",
            ("https://arxiv.org/abs/2601.00099v2", recs[STREAMING]["id"]),
        )
    pipeline(store, supervisor).drain()
    assert recommendations(store)[STREAMING]["kind"] == "blog"
    assert store.xhs_fallback_state()["last"] is None
    assert supervisor.fallback_calls() == []

    supervisor.decide.update({SINKS: [UNDECIDED], SPECULATIVE: [UNDECIDED]})
    fallback_drain(store, supervisor).drain()
    streaming = recommendations(store)[STREAMING]
    assert (streaming["kind"], streaming["arxiv_id"], streaming["import_state"]) == (
        "paper", "2601.00099", "none",
    )
    assert review(store, streaming["id"])["reason_code"] == "arxiv_link"
    # The converted paper is no longer one a run takes; the others are.
    assert set(items(store)) == {SINKS, SPECULATIVE}
    assert store.xhs_fallback_state()["last"]["trigger"] == "schedule"
    no_capture(store)


def test_fallback_stages_come_first_and_count_within_the_tick(
    store: ControlStore, supervisor: FallbackSupervisor, clock
) -> None:
    _saved(store, supervisor)
    supervisor.decide.update({title: [UNDECIDED] for title in (*BLOGS, SPECULATIVE)})
    report = fallback_drain(store, supervisor, drain_units_per_tick=1).drain()
    assert [unit.kind for unit in report.units] == ["fallback_decide"]
    drain = fallback_drain(store, supervisor, drain_units_per_tick=3)
    # A new scan: its ID is the time it starts.
    clock.advance(1)
    drain.pull()
    report = drain.drain()
    # Two fallback stages, then the remaining unit for an ordinary task.
    assert [unit.kind for unit in report.units] == [
        "fallback_decide", "fallback_decide", "list_page",
    ]
    assert len(supervisor.fallback_calls()) == 3


# -- what a run applies -------------------------------------------------------------


def test_a_run_corrects_and_imports_a_verified_blog_and_never_imports_a_paper(
    store: ControlStore, supervisor: FallbackSupervisor, tmp_path: Path, clock
) -> None:
    _saved(store, supervisor)
    gpt_before = store.xhs_usage()["gpt"]
    supervisor.decide.update({
        SINKS: [answer("corrected_url", url="https://blog.example/sinks-post",
                       reason="The lab's blog has this post.")],
        STREAMING: [answer("exclude", reason_code="not_a_recommendation",
                           reason="The note only mentions it.")],
        SPECULATIVE: [answer("reclassify_paper", arxiv_id="arXiv:2601.00077v1")],
    })
    supervisor.pages.update({
        "https://blog.example/sinks-post": {"title": "Attention Sinks | Example Lab"},
        "https://arxiv.org/abs/2601.00077": {"title": "[2601.00077] Speculative Decoding",
                                             "og_title": "Speculative Decoding"},
    })
    supervisor.blogs["https://blog.example/sinks-post"] = ("Attention Sinks", "Body text. " * 40)
    drain = fallback_drain(store, supervisor)
    for _ in range(3):
        drain.drain()

    run = store.xhs_fallback_state()["last"]
    assert run["state"] == "completed" and run["digest_state"] == "suppressed"
    assert run["summary"] == {
        "blog_queued": 1, "paper_corrected": 1, "paper_kept": 0, "excluded": 1,
        "needs_operator": 0, "stale": 0,
    }
    # One call per item, with the run's model and effort; two page checks.
    decided = supervisor.fallback_calls()
    assert sorted(supervisor.decided()) == sorted([SINKS, STREAMING, SPECULATIVE])
    assert {(p["gpt_model"], p["gpt_effort"], p["gpt_base"]) for p in decided} == {
        ("gpt-6.1-sol", "xhigh", "https://gpt.example/v1"),
    }
    assert len(supervisor.fallback_calls("xhs_fallback_verify")) == 2
    assert store.xhs_usage()["gpt"] == gpt_before + 3
    inputs = {json.loads(p["input"])["recommendation"]["title"]: p["input"] for p in decided}
    sinks_input = json.loads(inputs[SINKS])
    assert (sinks_input["note"]["cited"], sinks_input["note"]["text"]) == ("image 3", BLOG_TEXT)
    assert json.loads(inputs[STREAMING])["note"]["text"] == CAPTION_BLOG
    for text in inputs.values():
        assert str(tmp_path) not in text and SIGNATURE not in text and "staging" not in text

    recs = recommendations(store)
    sinks, streaming, paper = recs[SINKS], recs[STREAMING], recs[SPECULATIVE]
    assert (sinks["url"], sinks["url_state"], sinks["url_checked_title"]) == (
        "https://blog.example/sinks-post", "auto_matched", "Attention Sinks | Example Lab",
    )
    assert sinks["import_state"] == "imported"
    assert [t["subject_key"].split(":")[1] for t in tasks(store, "blog_import")] == [sinks["id"]]
    assert review(store, sinks["id"])["state"] == "resolved_blog"
    assert review(store, streaming["id"])["reason"] == "The note only mentions it."
    assert (paper["kind"], paper["arxiv_id"], paper["import_state"]) == (
        "paper", "2601.00077", "none",
    )
    assert review(store, paper["id"])["state"] == "resolved_paper"
    no_capture(store)
    for item in items(store).values():
        assert item["usage"] == {"input_tokens": 1_200, "output_tokens": 80}
        assert (item["state"], item["call_state"]) == ("done", "finished")
    # Reviewed rows are never taken again, a week later or not.
    clock.advance(WEEK + 1)
    drain.drain()
    assert len(supervisor.fallback_calls()) == 3


def test_an_unusable_or_injected_answer_is_left_to_the_operator_and_never_asked_again(
    store: ControlStore, supervisor: FallbackSupervisor, clock
) -> None:
    _saved(store, supervisor)
    supervisor.decide.update({
        # As if a quote had told the model to call a paper page a blog.
        SINKS: [answer("corrected_url", url="https://arxiv.org/abs/2601.00042")],
        STREAMING: [None],
        SPECULATIVE: [answer("import_paper", arxiv_id="2601.00042")],
    })
    drain = fallback_drain(store, supervisor)
    drain.drain()
    drain.drain()
    recs = recommendations(store)
    assert {title: review(store, recs[title]["id"])["reason_code"] for title in recs
            if review(store, recs[title]["id"]) is not None} == {
        SINKS: "not_a_blog", STREAMING: "insufficient_evidence",
        SPECULATIVE: "insufficient_evidence",
    }
    assert supervisor.fallback_calls("xhs_fallback_verify") == []
    assert tasks(store, "blog_import") == []
    no_capture(store)
    run_items = items(store)
    assert run_items[STREAMING]["proposal"] == {"error": "responses: not JSON"}
    assert {item["call_state"] for item in run_items.values()} == {"finished"}
    run = store.xhs_fallback_state()["last"]
    assert (run["digest_state"], run["summary"]["needs_operator"]) == ("pending", 3)
    status = xhs_fallback_status(store, settings(fallback_enabled=True))
    assert status["needs_operator"] == 3 and status["running"] is None
    assert status["last"]["digest_state"] == "pending" and status["backlog"] == 0
    assert xhs_status(store, settings())["fallback"]["needs_operator"] == 3

    clock.advance(WEEK + 1)
    drain.drain()
    assert len(supervisor.fallback_calls()) == 3


def test_a_failure_after_dispatch_is_never_retried_and_a_refusal_before_it_is(
    store: ControlStore, supervisor: FallbackSupervisor, clock
) -> None:
    _saved(store, supervisor)
    gpt_before = store.xhs_usage()["gpt"]
    supervisor.decide.update({
        SINKS: ["transient"],
        # A result that answers another input is as unknown as no result.
        STREAMING: [lambda engine: {**engine, "input_text_sha256": "0" * 64}],
        SPECULATIVE: [
            "auth",
            EffectPermanentlyRejected("adapter_unavailable"),
            answer("exclude", reason_code="not_a_recommendation"),
        ],
    })
    drain = fallback_drain(store, supervisor)
    drain.drain()
    report = drain.drain()
    # `auth` proves the model never ran: the item and its call go back.
    assert report.stopped == "auth"
    assert store.xhs_usage()["gpt"] == gpt_before + 2
    paper_item = items(store)[SPECULATIVE]
    assert (paper_item["state"], paper_item["call_state"]) == ("pending", "not_started")
    drain.drain()
    assert len(supervisor.fallback_calls()) == 3

    clock.advance(3_601)
    report = drain.drain()
    # A credential that does not resolve is refused before any call.
    assert report.stopped == "auth" and len(supervisor.fallback_calls()) == 3
    clock.advance(3_601)
    drain.drain()
    run_items = items(store)
    for title in BLOGS:
        assert run_items[title]["call_state"] == "may_have_started"
        assert review(store, recommendations(store)[title]["id"])["reason_code"] == (
            "outcome_unknown"
        )
    assert run_items[SPECULATIVE]["applied"] == "excluded"
    assert store.xhs_usage()["gpt"] == gpt_before + 3
    assert store.xhs_fallback_state()["last"]["state"] == "completed"
    clock.advance(WEEK + 1)
    drain.drain()
    assert len(supervisor.fallback_calls()) == 4


def test_an_unreadable_input_waits_without_a_call(
    store: ControlStore, supervisor: FallbackSupervisor, tmp_path: Path
) -> None:
    _saved(store, supervisor)
    (staging(tmp_path, 1) / "ocr" / "3.md").unlink()
    supervisor.decide.update({STREAMING: [UNDECIDED], SPECULATIVE: [UNDECIDED]})
    gpt_before = store.xhs_usage()["gpt"]
    drain = fallback_drain(store, supervisor)
    for _ in range(3):
        drain.drain()
    assert sorted(supervisor.decided()) == [STREAMING, SPECULATIVE]
    assert store.xhs_usage()["gpt"] == gpt_before + 2
    sinks = items(store)[SINKS]
    assert (sinks["state"], sinks["call_state"]) == ("pending", "not_started")
    assert store.xhs_fallback_state()["running"] is not None


# -- verification -------------------------------------------------------------------


def test_verification_retries_without_calling_the_model_again(
    store: ControlStore, supervisor: FallbackSupervisor, clock
) -> None:
    _saved(store, supervisor)
    for title in BLOGS:
        url = f"https://blog.example/{title.split()[1].lower()}"
        supervisor.decide[title] = [answer("corrected_url", url=url)]
        supervisor.pages[url] = ["transient", "rate_limited", "transient"]
    drain = fallback_drain(store, supervisor, fallback_weekly_cap=1)
    drain.drain()
    (title,) = supervisor.decided()
    verified = supervisor.fallback_calls("xhs_fallback_verify")
    assert len(verified) == 1
    drain.drain()
    assert len(supervisor.fallback_calls("xhs_fallback_verify")) == 1
    clock.advance(600)
    drain.drain()
    assert len(supervisor.fallback_calls("xhs_fallback_verify")) == 2
    clock.advance(600)
    drain.drain()
    assert len(supervisor.fallback_calls("xhs_fallback_verify")) == 2
    clock.advance(3_000)
    drain.drain()
    assert len(supervisor.fallback_calls("xhs_fallback_verify")) == 3
    assert supervisor.decided() == [title]
    item = items(store)[title]
    assert (item["state"], item["applied"], item["attempts"]) == ("done", "needs_operator", 3)
    assert review(store, recommendations(store)[title]["id"])["reason_code"] == "fetch_failed"
    assert tasks(store, "blog_import") == []


def test_a_mismatch_or_a_paper_page_is_never_imported_and_never_excluded(
    store: ControlStore, supervisor: FallbackSupervisor
) -> None:
    _saved(store, supervisor)
    supervisor.decide.update({
        SINKS: [answer("corrected_url", url="https://blog.example/other")],
        STREAMING: [answer("corrected_url", url="https://blog.example/moved")],
        SPECULATIVE: [answer("reclassify_paper", arxiv_id="2601.00077")],
    })
    supervisor.pages.update({
        "https://blog.example/other": {"title": "Something Else Entirely"},
        # Redirected to a paper page whose title matches: still not a blog.
        "https://blog.example/moved": {"title": "Efficient Streaming",
                                       "final_url": "https://openreview.net/forum?id=x"},
        "https://arxiv.org/abs/2601.00077": {"title": "[2601.00077] Graph Kernels Revisited"},
    })
    drain = fallback_drain(store, supervisor)
    for _ in range(3):
        drain.drain()
    recs = recommendations(store)
    codes = {title: review(store, recs[title]["id"])["reason_code"]
             for title in (SINKS, STREAMING, SPECULATIVE)}
    assert codes == {SINKS: "title_mismatch", STREAMING: "not_a_blog",
                     SPECULATIVE: "title_mismatch"}
    assert {review(store, recs[title]["id"])["state"] for title in codes} == {"needs_operator"}
    assert recs[SINKS]["url"] == "https://blog.example/attention-sinks"
    assert (recs[SPECULATIVE]["kind"], recs[SPECULATIVE]["arxiv_id"]) == ("paper", None)
    assert tasks(store, "blog_import") == []
    no_capture(store)


def test_a_refused_fetch_ends_verification_at_once(
    store: ControlStore, supervisor: FallbackSupervisor
) -> None:
    _saved(store, supervisor)
    for title in BLOGS:
        url = f"https://blog.example/{title.split()[1].lower()}"
        supervisor.decide[title] = [answer("corrected_url", url=url)]
        supervisor.pages[url] = "not_found"
    drain = fallback_drain(store, supervisor, fallback_weekly_cap=1)
    drain.drain()
    (title,) = supervisor.decided()
    item = items(store)[title]
    assert (item["applied"], item["attempts"]) == ("needs_operator", 1)
    assert review(store, recommendations(store)[title]["id"])["state"] == "needs_operator"


# -- caps and spacing -----------------------------------------------------------------


def test_the_daily_gpt_cap_holds_the_rest_of_the_run_until_the_next_day(
    store: ControlStore, supervisor: FallbackSupervisor, clock
) -> None:
    _saved(store, supervisor)
    used = store.xhs_usage()["gpt"]
    supervisor.decide.update({title: [UNDECIDED] for title in (*BLOGS, SPECULATIVE)})
    drain = fallback_drain(
        store, supervisor, daily_calls={"tikhub": 100, "ocr": 1000, "gpt": used + 1}
    )
    report = drain.drain()
    assert [unit.outcome for unit in report.units] == ["done", "capped"]
    drain.drain()
    assert len(supervisor.fallback_calls()) == 1
    assert store.xhs_usage()["gpt"] == used + 1
    clock.advance(86_400)
    drain.drain()
    drain.drain()
    assert len(supervisor.fallback_calls()) == 3
    assert store.xhs_fallback_state()["last"]["state"] == "completed"


def test_one_run_takes_at_most_100_and_the_rest_waits(
    store: ControlStore, supervisor: FallbackSupervisor
) -> None:
    _saved(store, supervisor)
    with store._transaction() as conn:
        for index in range(98):
            store._xhs_upsert_recommendation(
                conn, note_id=note_id(1), item_key=f"blog:extra-{index:03d}",
                image_ordinal=None, kind="blog", title=f"Extra blog {index:03d}",
                quote=f"Extra blog {index:03d}", arxiv_id=None, url=None,
                url_state="none", origin="model", identify_run="identify:extra",
            )
    supervisor.decide.update(
        {title: [UNDECIDED] for title in recommendations(store)}
    )
    report = fallback_drain(store, supervisor, fallback_weekly_cap=100).drain()
    assert [unit.kind for unit in report.units][:2] == ["fallback_decide"] * 2
    run = store.xhs_fallback_state()["running"]
    taken = items(store, run["id"])
    assert run["item_cap"] == 100 and len(taken) == 100
    # Blogs before papers: the one paper is the one left for next week.
    assert SPECULATIVE not in taken
    status = xhs_fallback_status(store, settings(fallback_enabled=True))
    assert (status["backlog"], status["running"]["items"]) == (1, 100)
    assert status["running"]["remaining"] == 98


def test_runs_are_weekly_capped_and_retake_an_input_never_asked(
    store: ControlStore, supervisor: FallbackSupervisor, clock
) -> None:
    _saved(store, supervisor)
    supervisor.decide.update({title: [UNDECIDED] for title in (*BLOGS, SPECULATIVE)})
    started = store.start_xhs_fallback_run(
        trigger="operator", item_cap=1, model="gpt-6.1-sol", effort="xhigh"
    )
    (first,) = items(store, started["run"]["id"])
    first_id = recommendations(store)[first]["id"]
    # Taken by an import before its call: the item goes stale, nothing is asked.
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """UPDATE xhs_recommendations SET import_state = 'importing',
               revision = revision + 1 WHERE id = ?""",
            (first_id,),
        )
    drain = fallback_drain(store, supervisor, fallback_weekly_cap=1)
    drain.drain()
    run = store.get_xhs_fallback_run(started["run"]["id"])
    assert (run["state"], run["summary"]["stale"]) == ("completed", 1)
    assert supervisor.fallback_calls() == []
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """UPDATE xhs_recommendations SET import_state = 'failed',
               revision = revision + 1 WHERE id = ?""",
            (first_id,),
        )
    clock.advance(WEEK - 60)
    drain.drain()
    assert store.xhs_fallback_state()["last"]["id"] == run["id"]
    clock.advance(61)
    drain.drain()
    # The model never saw the first input, so the next run takes it again,
    # and the cap leaves the other two waiting. (An input the model saw is
    # never taken again: `test_an_input_reviewed_once_is_not_taken_again`.)
    state = store.xhs_fallback_state()
    assert state["last"]["id"] != run["id"]
    taken = items(store, state["last"]["id"])
    assert list(taken) == [first]
    assert supervisor.decided() == [first]
    status = xhs_fallback_status(store, settings(fallback_enabled=True, fallback_weekly_cap=1))
    assert status["backlog"] == 2
