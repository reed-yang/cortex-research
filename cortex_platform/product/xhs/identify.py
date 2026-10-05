"""Recommendation identification: rules, the model prompt, and its checks.

Identification reads a note's caption and each image's verbatim transcription
and returns the papers, blogs and other items the note recommends. Rules find
arXiv IDs and URLs in the text. The model proposes items with a verbatim
quote; an item survives only when its quote and title both occur, after
whitespace and case normalization, in the text it cites. Model output is
untrusted until it passes these checks, and a model failure never becomes an
empty list: the caller fails the task instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from cortex_platform.product.sources.identity import canonicalize_arxiv_id, normalize_url

PROMPT_VERSION = "xhs-identify-1"
LINK_PROMPT_VERSION = "xhs-link-1"
RECOMMENDATION_KINDS = ("paper", "blog", "other")
MAX_ITEMS = 200
TITLE_OVERLAP = 0.6

IDENTIFY_INSTRUCTIONS = """\
You read one Xiaohongshu note: its caption and the verbatim OCR transcription
of each of its images. List every paper, blog post or other resource the note
recommends or discusses.

Answer with one JSON object and nothing else:
{"items":[{"kind":"paper|blog|other","title":str,"image":int|null,"quote":str,"arxiv_id":str|null,"url":str|null}]}

Rules:
- "image" is the number of the image whose transcription shows the item, or
  null when the item appears only in the caption.
- "title" is the item's title exactly as written in that text.
- "quote" is a short passage copied character for character from that text
  that names the item. Never paraphrase, translate or complete it.
- "arxiv_id" and "url" only when they are written in that text; otherwise null.
  Never guess a URL or an identifier.
- Use "blog" for articles and blog posts, "paper" for research papers, and
  "other" for anything else (courses, tools, books, videos).
- When the note recommends nothing, answer {"items":[]}.
"""

LINK_INSTRUCTIONS = """\
Find the canonical web page of the article with exactly the given title. Search
the web; never guess. Answer with one JSON object and nothing else:
{"url":str|null,"page_title":str|null}
Use null for url when no page with that exact title is found.
"""

# A new-style arXiv identifier with a real month. The canonicalizer validates
# the rest; this pattern only keeps dates and prices out.
_ARXIV_RE = re.compile(
    r"(?<![0-9A-Za-z.])(?:arxiv\s*[:：]?\s*)?([0-9]{2}(?:0[1-9]|1[0-2])\.[0-9]{4,5})(v[0-9]+)?(?![0-9])",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_URL_TRAILING = ".,;:!?)]}'\""
# Words, except that each CJK character is its own token: those scripts are
# written without spaces.
_CJK = "\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af"
_TOKEN_RE = re.compile(f"[{_CJK}]|[^\\W_{_CJK}]+")


class IdentifyAnswerError(ValueError):
    """The model's answer is not the JSON the prompt asked for."""


@dataclass(frozen=True)
class IdentifyOutcome:
    """The recommendations to upsert, and how many model items were dropped."""

    items: tuple[Mapping[str, Any], ...]
    dropped: int
    rule_items: int
    model_items: int


def normalize_match_text(value: str) -> str:
    """NFKC, case folding and single spaces: the verbatim comparison form."""

    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _sorted_transcriptions(transcriptions: Iterable[tuple[int, str]]) -> list[tuple[int, str]]:
    seen: dict[int, str] = {}
    for image, text in transcriptions:
        if isinstance(image, bool) or not isinstance(image, int) or image < 1:
            raise ValueError("an image number must be a positive integer")
        if image in seen:
            raise ValueError("an image number repeats")
        seen[image] = text or ""
    return sorted(seen.items())


def build_identify_input(caption: str, transcriptions: Iterable[tuple[int, str]]) -> str:
    """The caption, then each transcription under its original image number."""

    parts = ["## Caption", "", (caption or "").strip() or "(empty)", ""]
    for image, text in _sorted_transcriptions(transcriptions):
        parts.extend([f"## Image {image}", "", text.strip() or "(no text)", ""])
    return "\n".join(parts)


def build_link_input(title: str) -> str:
    return f"Title: {title.strip()}"


def input_sha256(caption: str, transcriptions: Iterable[tuple[int, str]]) -> str:
    """The identification input's digest, with the prompt version in it."""

    text = PROMPT_VERSION + "\n" + build_identify_input(caption, transcriptions)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_json_object(text: str) -> dict[str, Any]:
    """The one JSON object in an answer, allowing a fenced code block."""

    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        raise IdentifyAnswerError("answer has no JSON object")
    try:
        value = json.loads(stripped[start : end + 1])
    except ValueError as error:
        raise IdentifyAnswerError("answer is not valid JSON") from error
    if not isinstance(value, dict):
        raise IdentifyAnswerError("answer is not a JSON object")
    return value


def _optional_text(item: Mapping[str, Any], name: str) -> str | None:
    value = item.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise IdentifyAnswerError(f"item {name} is not a string or null")
    value = value.strip()
    return value or None


def parse_model_items(text: str) -> list[dict[str, Any]]:
    """Schema-check the model's `{"items": [...]}` answer."""

    answer = extract_json_object(text)
    items = answer.get("items")
    if not isinstance(items, list):
        raise IdentifyAnswerError("answer has no items list")
    if len(items) > MAX_ITEMS:
        raise IdentifyAnswerError("answer lists too many items")
    parsed: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise IdentifyAnswerError("an item is not an object")
        kind = item.get("kind")
        if kind not in RECOMMENDATION_KINDS:
            raise IdentifyAnswerError("an item kind is unsupported")
        image = item.get("image")
        if image is not None and (isinstance(image, bool) or not isinstance(image, int)):
            raise IdentifyAnswerError("an item image is not an integer or null")
        title = item.get("title")
        quote = item.get("quote")
        if not isinstance(title, str) or not isinstance(quote, str):
            raise IdentifyAnswerError("an item title or quote is not a string")
        parsed.append(
            {
                "kind": kind,
                "title": title.strip(),
                "image": image,
                "quote": quote.strip(),
                "arxiv_id": _optional_text(item, "arxiv_id"),
                "url": _optional_text(item, "url"),
            }
        )
    return parsed


def parse_link_answer(text: str) -> tuple[str | None, str | None]:
    """`(url, page_title)`; a `null` URL is a valid "not found"."""

    answer = extract_json_object(text)
    url = answer.get("url")
    page_title = answer.get("page_title")
    if url is not None and not isinstance(url, str):
        raise IdentifyAnswerError("url is not a string or null")
    if page_title is not None and not isinstance(page_title, str):
        raise IdentifyAnswerError("page_title is not a string or null")
    url = (url or "").strip() or None
    if url is not None:
        try:
            url = normalize_url(url)
        except ValueError as error:
            raise IdentifyAnswerError("url is not a usable http(s) URL") from error
    return url, (page_title or "").strip() or None


def _arxiv(value: str | None) -> str | None:
    if not value:
        return None
    match = _ARXIV_RE.search(value)
    if match is None:
        return None
    try:
        return canonicalize_arxiv_id(match.group(1)).authority_id
    except ValueError:
        return None


def urls_in(text: str) -> list[str]:
    """Every http(s) URL written in `text`, normalized, in order."""

    found: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(_URL_TRAILING)
        try:
            normalized = normalize_url(candidate)
        except ValueError:
            continue
        if normalized not in found:
            found.append(normalized)
    return found


def _item_key(kind: str, image: int | None, title: str, arxiv_id: str | None) -> str:
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    digest = hashlib.sha256(normalize_match_text(title).encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{image if image is not None else 'caption'}:{digest}"


def rule_items(caption: str, transcriptions: Iterable[tuple[int, str]]) -> list[dict[str, Any]]:
    """One `paper` item per arXiv ID written in an image or the caption.

    An image occurrence is preferred over the caption, so the item can show
    its screenshot. The quote is the matched text as written.
    """

    sources: list[tuple[int | None, str]] = [
        (image, text) for image, text in _sorted_transcriptions(transcriptions)
    ]
    sources.append((None, caption or ""))
    items: dict[str, dict[str, Any]] = {}
    for image, text in sources:
        for match in _ARXIV_RE.finditer(text):
            try:
                arxiv_id = canonicalize_arxiv_id(match.group(1)).authority_id
            except ValueError:
                continue
            if arxiv_id in items:
                continue
            items[arxiv_id] = {
                "item_key": f"arxiv:{arxiv_id}",
                "kind": "paper",
                "title": f"arXiv:{arxiv_id}",
                "image": image,
                "quote": match.group(0).strip(),
                "arxiv_id": arxiv_id,
                "url": None,
                "url_state": "none",
                "origin": "rule",
            }
    return list(items.values())


def verbatim_filter(
    items: Sequence[Mapping[str, Any]],
    caption: str,
    transcriptions: Iterable[tuple[int, str]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep model items whose quote and title occur in the text they cite."""

    texts: dict[int | None, str] = {
        image: normalize_match_text(text) for image, text in _sorted_transcriptions(transcriptions)
    }
    texts[None] = normalize_match_text(caption)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in items:
        image = item.get("image")
        cited = texts.get(image) if image in texts else None
        quote = normalize_match_text(str(item.get("quote") or ""))
        title = normalize_match_text(str(item.get("title") or ""))
        if cited is None or not quote or not title or quote not in cited or title not in cited:
            dropped += 1
            continue
        kept.append(dict(item))
    return kept, dropped


def _model_item(
    item: Mapping[str, Any], raw_texts: Mapping[int | None, str]
) -> dict[str, Any]:
    kind = str(item["kind"])
    image = item.get("image")
    title = str(item["title"])
    arxiv_id = _arxiv(item.get("arxiv_id")) if kind == "paper" else None
    url: str | None = None
    url_state = "none"
    if kind != "paper":
        written = urls_in(raw_texts.get(image, ""))
        quoted = urls_in(str(item.get("quote") or ""))
        proposed = None
        if item.get("url"):
            try:
                proposed = normalize_url(str(item["url"]))
            except ValueError:
                proposed = None
        # A URL counts only when it is written in the cited text; a model URL
        # that is not there is a guess, and the item goes to link resolution.
        if proposed is not None and proposed in written:
            url, url_state = proposed, "from_text"
        elif len(quoted) == 1 and quoted[0] in written:
            url, url_state = quoted[0], "from_text"
    return {
        "item_key": _item_key(kind, image, title, arxiv_id),
        "kind": kind,
        "title": title,
        "image": image,
        "quote": str(item["quote"]),
        "arxiv_id": arxiv_id,
        "url": url,
        "url_state": url_state,
        "origin": "model",
    }


def merge_items(
    rules: Sequence[Mapping[str, Any]],
    model: Sequence[Mapping[str, Any]],
    caption: str,
    transcriptions: Iterable[tuple[int, str]],
) -> list[dict[str, Any]]:
    """Merge rule and filtered model items by arXiv ID, or by title and image."""

    raw_texts: dict[int | None, str] = dict(_sorted_transcriptions(transcriptions))
    raw_texts[None] = caption or ""
    by_arxiv = {str(item["arxiv_id"]): dict(item) for item in rules}
    merged: list[dict[str, Any]] = []
    seen_titles: set[tuple[str, int | None]] = set()
    for item in model:
        candidate = _model_item(item, raw_texts)
        arxiv_id = candidate["arxiv_id"]
        if arxiv_id and arxiv_id in by_arxiv:
            rule = by_arxiv.pop(arxiv_id)
            candidate["origin"] = "rule+model"
            if candidate["image"] is None:
                candidate["image"] = rule["image"]
            merged.append(candidate)
            continue
        key = (normalize_match_text(candidate["title"]), candidate["image"])
        if key in seen_titles:
            continue
        seen_titles.add(key)
        merged.append(candidate)
    merged.extend(by_arxiv.values())
    unique: dict[str, dict[str, Any]] = {}
    for item in merged:
        unique.setdefault(item["item_key"], item)
    return list(unique.values())


def identify(
    caption: str,
    transcriptions: Iterable[tuple[int, str]],
    model_items: Sequence[Mapping[str, Any]],
) -> IdentifyOutcome:
    """Rules, then the verbatim filter on model items, then the merge."""

    transcriptions = _sorted_transcriptions(transcriptions)
    rules = rule_items(caption, transcriptions)
    kept, dropped = verbatim_filter(model_items, caption, transcriptions)
    items = merge_items(rules, kept, caption, transcriptions)
    return IdentifyOutcome(
        items=tuple(items),
        dropped=dropped,
        rule_items=len(rules),
        model_items=len(model_items),
    )


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_match_text(value))


def title_matches(expected: str, observed: Iterable[str | None]) -> bool:
    """Whether a fetched page title names the expected item.

    The item title contained in the page title (which often appends a site
    name), the page title of at least three tokens contained in the item
    title, or token overlap of at least 0.6 of the item title's tokens.
    """

    wanted = " ".join(_tokens(expected))
    wanted_tokens = set(wanted.split())
    if not wanted_tokens:
        return False
    for title in observed:
        if not title:
            continue
        found_tokens = _tokens(title)
        found = " ".join(found_tokens)
        if not found:
            continue
        if wanted in found or (len(found_tokens) >= 3 and found in wanted):
            return True
        overlap = len(wanted_tokens & set(found_tokens)) / len(wanted_tokens)
        if overlap >= TITLE_OVERLAP:
            return True
    return False
