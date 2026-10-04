"""The lexical search text for a fresh research selection, derived from the question.

Pure and deterministic: no I/O and no model call. The packet keeps the verbatim
question as `query`; the derived text is recorded as its `retrieval_query`.
"""

import re
import unicodedata
from collections.abc import Iterable

from ..sources.search import MAX_QUERY_BYTES, MAX_QUERY_TERMS, query_terms

#: English words that describe the requested answer format rather than its topic.
FORMAT_TERMS = frozenset(
    "cite cites cited citing citation citations word words character characters chars "
    "sentence sentences paragraph paragraphs bullet bullets concise concisely briefly "
    "markdown latex please summarize summarise".split()
)
# An inline Markdown link keeps its text; its destination never becomes search terms.
_LINK = re.compile(r'\[([^\[\]]*)\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+"[^"]*")?\)')
# Bracketed source and document label groups: [S1], [S1, S2], [S1 S2], [S1-S3], [D1].
_LABELS = re.compile(
    r"\[\s*[SsDd]\s*[0-9]{1,3}(?:(?:\s*[-–,;，、]\s*|\s+)(?:[SsDd]\s*)?[0-9]{1,3})*\s*\]")
# Explicit answer-length requirements only; other numbers, years and versions stay.
_LENGTH = re.compile(
    r"(?:(?:控制在|不超过|少于)\s*|\b(?:within|under|in|at most|no more than)\s+)?"
    r"(?<![0-9A-Za-z.])\d{1,5}\s*(?:个\s*)?"
    r"(?:字|词|句|段|条|(?:words?|characters?|chars?|sentences?|paragraphs?|bullets?"
    r"|points?|tokens?)(?![A-Za-z]))(?:以内|之内|内|左右)?",
    re.IGNORECASE,
)
_PIECE = re.compile(r"[^\W_]+(?:\.[0-9]+)*")


def _normalized(text):
    """Controls and every kind of whitespace become single spaces."""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return " ".join(text.split())


def _pieces(text):
    text = _LINK.sub(lambda match: f" {match[1]} ", _normalized(text))
    text = _LENGTH.sub(" ", _LABELS.sub(" ", text))
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

    Citation label groups, link destinations, explicit length requirements and
    FORMAT_TERMS are removed, pieces without searchable terms are dropped and
    case-insensitive duplicates keep their first occurrence. If nothing
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
