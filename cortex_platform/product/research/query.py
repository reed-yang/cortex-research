"""The lexical search text for a fresh research selection, derived from the question.

Pure and deterministic: no I/O and no model call. The packet keeps the verbatim
question as `query`; the derived text is recorded as its `retrieval_query`.
"""

import re
import unicodedata
from collections.abc import Iterable

from ..sources.search import MAX_QUERY_BYTES, MAX_QUERY_TERMS, query_terms
from .context import _CITATION_SCAN, _LINK_TEXT

#: English words that describe the requested answer format rather than its topic.
FORMAT_TERMS = frozenset(
    "cite cites cited citing citation citations word words character characters chars "
    "sentence sentences paragraph paragraphs bullet bullets concise concisely briefly "
    "markdown latex please summarize summarise".split()
)
_LINK_TEXT_PREFIX = re.compile(_LINK_TEXT)
# Explicit answer-length requirements only; other numbers, years and versions stay.
# A count may be a range (800-1000, 300到500, 2 to 3). After a CJK unit the span
# must end at a length suffix, 的, punctuation, a space or the end of the text, or
# before an answer verb such as 总结 or 回答 (optionally after 内). So a count
# inside a compound term such as 1024字节, 2段式, 8字符串 or 500字内存 is kept, and
# so is an ordinal such as 第2段. After a strong CJK length prefix the suffix 内
# always ends it, and 字数 may be followed by a bare count (字数控制在500以内).
_NUMBER = r"(?<![0-9A-Za-z.第])\d{1,5}(?:\s*(?:[-–~～到至]|to(?=\s))\s*\d{1,5})?"
_COUNT = rf"{_NUMBER}\s*(?:个\s*)?"
_CJK_UNIT = r"(?:段落|句话|字符(?!串)|字|词|句|段|条)"
_ANSWER_VERB = r"(?:总结|概括|概述|回答|介绍|说明|描述|解释|阐述|分析|论述|讲解|写)"
_CJK_CLOSE = rf"(?:以内|之内|左右|的|内?(?!\w)|内?(?={_ANSWER_VERB}))"
_CJK_PREFIX = r"(?:控制在|不超过|不多于|少于)"
_ENGLISH_PREFIX = r"(?:\b(?:within|under|in|at most|no more than)\s+)?"
_LENGTH = re.compile(
    rf"{_CJK_PREFIX}\s*{_COUNT}{_CJK_UNIT}(?:内|{_CJK_CLOSE})"
    rf"|字数\s*[:：]?\s*(?:{_CJK_PREFIX}|在)?\s*{_COUNT}{_CJK_UNIT}?{_CJK_CLOSE}"
    rf"|(?:在\s*|{_ENGLISH_PREFIX}){_COUNT}{_CJK_UNIT}{_CJK_CLOSE}"
    rf"|{_ENGLISH_PREFIX}{_NUMBER}(?:\s*|-)"
    r"(?:words?|characters?|chars?|sentences?|paragraphs?|bullets?|points?|tokens?)"
    r"(?![A-Za-z])(?:以内|之内|内|左右)?",
    re.IGNORECASE,
)
_PIECE = re.compile(r"[^\W_]+(?:\.[0-9]+)*")


def _normalized(text):
    """Controls and every kind of whitespace become single spaces."""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return " ".join(text.split())


def _without_citations(text):
    """Read with the answer's citation scan: label-led brackets go, links keep their text.

    A bracket that cited_labels would read as a label group, malformed ones
    included, is removed; an inline link is replaced by its text, cleaned the
    same way, so destinations and titles never become search terms.
    """
    def replace(match):
        if match[1] is not None:
            return " "
        return f" {_without_citations(_LINK_TEXT_PREFIX.match(match[0])[0][1:-1])} "
    return _CITATION_SCAN.sub(replace, text)


def _pieces(text):
    text = _LENGTH.sub(" ", _without_citations(_normalized(text)))
    return [piece for piece in _PIECE.findall(text)
            if piece.casefold() not in FORMAT_TERMS and any(query_terms(piece))]


def _within_limits(text):
    english, unicode = query_terms(text)
    return (len(english) + len(unicode) <= MAX_QUERY_TERMS
            and len(text.encode("utf-8")) <= MAX_QUERY_BYTES)


def _longest_prefix(pieces):
    """Drop trailing pieces until the joined text fits search's term and byte caps."""
    kept = []
    for piece in pieces:
        if not _within_limits(" ".join([*kept, piece])):
            break
        kept.append(piece)
    return " ".join(kept)


def retrieval_query(question: str, context: Iterable[str] = ()) -> str:
    """Search text: context pieces (the selected item's title) first, then the question.

    Citation label groups, link destinations and titles, explicit length
    requirements and FORMAT_TERMS are removed, pieces without searchable terms
    are dropped and case-insensitive duplicates keep their first occurrence. If nothing
    searchable remains, the normalized question is searched as before, under the
    same caps; an empty result is refused by search rather than sent unbounded.
    """
    pieces = {}
    for text in (*context, question):
        for piece in _pieces(text):
            pieces.setdefault(piece.casefold(), piece)
    derived = _longest_prefix(pieces.values())
    if derived:
        return derived
    return _longest_prefix(_normalized(question).split())
