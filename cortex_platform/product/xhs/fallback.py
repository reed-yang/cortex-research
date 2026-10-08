"""The weekly recommendation fallback's pure parts: the free rules, the digest
of a model input, and the shape of what may be applied.

Nothing here reads Control state, a file or the network. `control/xhs_store.py`
selects the rows, plans with these functions and applies the plan in one
transaction (`docs/plans/xhs-recommendation-fallback.md`).

The rules need no model. A blog whose link is an arXiv page is the paper that
page names; when another paper of the same note already has that ID, the blog
is a duplicate of it instead. Neither rule imports anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from cortex_platform.product.sources.identity import (
    arxiv_id_from_url,
    canonicalize_arxiv_id,
    normalize_url,
)

PROMPT_VERSION = "xhs-fallback-1"
#: The most recommendations one weekly run takes, whatever the configured cap.
MAX_RUN_ITEMS = 100
#: A run starts at most once per this many seconds.
RUN_SPACING_SECONDS = 7 * 86_400
#: The most rows one application of the rules changes.
RULE_BATCH_MAX = 100
#: Verification attempts of one proposal; the model is never called again.
VERIFY_MAX_ATTEMPTS = 3
#: The wait after the first and the second failed verification attempt.
VERIFY_RETRY_SECONDS = (600, 3_600)
REASON_MAX = 500

REVIEW_STATES = frozenset(
    {"resolved_blog", "resolved_paper", "excluded", "needs_operator", "operator_owned"}
)
REVIEW_METHODS = frozenset({"rule", "model", "operator"})
REASON_CODES = frozenset(
    {
        "arxiv_link",
        "duplicate",
        "not_on_arxiv",
        "not_a_blog",
        "not_a_recommendation",
        "insufficient_evidence",
        "conflicting_evidence",
        "title_mismatch",
        "fetch_failed",
        "outcome_unknown",
        "operator",
    }
)
#: The fields a correction may set, in the order a review lists them.
CORRECTABLE_FIELDS = ("kind", "arxiv_id", "url")
APPLIED_ACTIONS = ("blog_queued", "paper_corrected", "paper_kept", "excluded", "needs_operator")
#: Why the model may exclude an item; never because nothing was found.
EXCLUDE_REASONS = frozenset({"not_a_blog", "not_a_recommendation", "duplicate"})
#: Why an item may be left to the operator.
NEEDS_OPERATOR_REASONS = frozenset(
    {
        "insufficient_evidence",
        "conflicting_evidence",
        "title_mismatch",
        "not_a_blog",
        "fetch_failed",
        "outcome_unknown",
    }
)
DECISION_ACTIONS = frozenset({"blog", "paper", "exclude", "needs_operator"})
#: Hosts whose pages are papers, with their subdomains; never imported as a blog.
PAPER_HOSTS = frozenset({"arxiv.org", "openreview.net", "doi.org", "aclanthology.org"})


def is_paper_url(url: str) -> bool:
    """Whether a URL is a paper host's page or a PDF, which no blog import takes."""

    split = urlsplit(url)
    host = (split.hostname or "").lower()
    return split.path.lower().endswith(".pdf") or any(
        host == name or host.endswith("." + name) for name in PAPER_HOSTS
    )


def merge_corrected_fields(*groups: Iterable[str]) -> list[str]:
    """Every corrected field named in any group, in the review's order."""

    names = {name for group in groups for name in group}
    if not names <= set(CORRECTABLE_FIELDS):
        raise ValueError("corrected fields are unsupported")
    return [name for name in CORRECTABLE_FIELDS if name in names]


def public_reason(value: Any) -> str | None:
    """A shown reason: None, or 1..500 characters without control characters
    other than a newline or a tab. Surrounding whitespace is dropped."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reason is invalid")
    text = value.strip()
    if not text:
        return None
    if len(text) > REASON_MAX or any(
        ord(character) < 32 and character not in "\n\t" for character in text
    ):
        raise ValueError("reason is invalid")
    return text


# -- rules ----------------------------------------------------------------------


@dataclass(frozen=True)
class RuleAction:
    """One rule's change to one blog recommendation.

    Without `duplicate_of` the blog becomes the paper `arxiv_id` names; with it,
    the blog is excluded as a duplicate of that paper in the same note.
    """

    recommendation_id: str
    note_id: str
    arxiv_id: str
    duplicate_of: str | None = None


def plan_rules(
    blogs: Sequence[Mapping[str, Any]],
    papers: Iterable[Mapping[str, Any]],
    *,
    limit: int = RULE_BATCH_MAX,
) -> list[RuleAction]:
    """The rule actions for candidate blogs, in the order given, at most `limit`.

    `blogs` are the rows the arXiv-link rule may change (the store selects
    them); `papers` are the papers with an arXiv ID in the same notes. A blog
    converted earlier in the same plan counts as such a paper, so two blogs
    linking one arXiv page in one note give one paper and one duplicate.
    Another note's paper never makes a duplicate.
    """

    known: dict[tuple[str, str], str] = {}
    for paper in papers:
        if paper["arxiv_id"]:
            known.setdefault((str(paper["note_id"]), str(paper["arxiv_id"])), str(paper["id"]))
    actions: list[RuleAction] = []
    for blog in blogs:
        if len(actions) >= limit:
            break
        arxiv_id = arxiv_id_from_url(blog["url"]) if blog["url"] else None
        if arxiv_id is None:
            continue
        key = (str(blog["note_id"]), arxiv_id)
        duplicate_of = known.get(key)
        if duplicate_of is None:
            known[key] = str(blog["id"])
        actions.append(RuleAction(str(blog["id"]), key[0], arxiv_id, duplicate_of))
    return actions


# -- the model input -------------------------------------------------------------


def input_sha256(
    recommendation: Mapping[str, Any],
    *,
    note_title: str,
    caption: str,
    image_text_sha256: str | None,
) -> str:
    """The digest of one item's model input, with the prompt version in it.

    The cited evidence is the caption, or the image's transcription by its text
    hash, which commits to the text; equal digests mean equal input. A run
    skips an item whose recommendation was reviewed with an equal digest.
    """

    ordinal = recommendation["image_ordinal"]
    cited: dict[str, Any] = (
        {"caption": caption}
        if ordinal is None
        else {"image": int(ordinal), "text_sha256": image_text_sha256}
    )
    value = {
        "prompt_version": PROMPT_VERSION,
        "kind": recommendation["kind"],
        "title": recommendation["title"],
        "quote": recommendation["quote"],
        "url": recommendation["url"],
        "url_state": recommendation["url_state"],
        "url_checked_title": recommendation["url_checked_title"],
        "note_title": note_title,
        "cited": cited,
    }
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- what may be applied -----------------------------------------------------------


def check_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
    """One item's final decision, checked, in the form the store applies.

    `blog`: a verified page (`url`, `checked_title`). `paper`: the item is a
    paper, with a canonical `arxiv_id` or none. `exclude` and
    `needs_operator`: a `reason_code` from their own sets. Every action may
    carry a public `reason`.
    """

    if not isinstance(decision, Mapping) or decision.get("action") not in DECISION_ACTIONS:
        raise ValueError("fallback decision is invalid")
    action = str(decision["action"])
    allowed = {
        "blog": {"action", "url", "checked_title", "reason"},
        "paper": {"action", "arxiv_id", "reason"},
        "exclude": {"action", "reason_code", "reason"},
        "needs_operator": {"action", "reason_code", "reason"},
    }[action]
    if not set(decision) <= allowed:
        raise ValueError("fallback decision has unsupported fields")
    checked: dict[str, Any] = {"action": action, "reason": public_reason(decision.get("reason"))}
    if action == "blog":
        title = decision.get("checked_title")
        if not isinstance(title, str) or not 1 <= len(title) <= 1_000 or "\x00" in title:
            raise ValueError("checked_title is invalid")
        url = normalize_url(decision.get("url"))
        if is_paper_url(url):
            raise ValueError("a paper page is not a blog")
        checked.update(url=url, checked_title=title)
    elif action == "paper":
        arxiv_id = decision.get("arxiv_id")
        checked["arxiv_id"] = (
            None if arxiv_id is None else canonicalize_arxiv_id(arxiv_id).authority_id
        )
    else:
        codes = EXCLUDE_REASONS if action == "exclude" else NEEDS_OPERATOR_REASONS
        if decision.get("reason_code") not in codes:
            raise ValueError("reason_code is unsupported for this action")
        checked["reason_code"] = decision["reason_code"]
    return checked


def run_summary(applied: Iterable[str | None]) -> dict[str, int]:
    """Counts by applied action; an item that went stale applied nothing."""

    counts = {name: 0 for name in APPLIED_ACTIONS} | {"stale": 0}
    for value in applied:
        counts["stale" if value is None else value] += 1
    return counts
