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
# An inline Markdown link opener; _link_end decides whether a link really follows.
_LINK_OPEN = re.compile(r"\[([^\[\]]*)\]\(")
# After a link destination: an optional "...", '...' or (...) title, then ")".
_LINK_TAIL = re.compile(r"""(?: (?:"[^"]*"|'[^']*'|\([^()]*\)))? ?\)""")
# Bracketed source and document label groups: [S1], [S1, S2], [S1 S2], [S1-S3], [D1],
# and label-shaped malformed groups such as [S9a].
_LABELS = re.compile(
    r"\[\s*[SsDd]\s*[0-9]{1,3}[a-z]?"
    r"(?:(?:\s*[-–,;，、]\s*|\s+)(?:[SsDd]\s*)?[0-9]{1,3}[a-z]?)*\s*\]")
# Explicit answer-length requirements only; other numbers, years and versions stay.
# A CJK unit must close the span (a length suffix, punctuation, a space or the end),
# so a count inside a compound term such as 1024字节 or 2段式 is kept, and so is an
# ordinal such as 第2段. After a CJK length prefix, the suffix 内 also closes it.
_COUNT = r"(?<![0-9A-Za-z.第])\d{1,5}\s*(?:个\s*)?"
_CJK_UNIT = r"(?:段落|句话|字|词|句|段|条)"
_ENGLISH_PREFIX = r"(?:\b(?:within|under|in|at most|no more than)\s+)?"
_LENGTH = re.compile(
    rf"(?:控制在|不超过|少于)\s*{_COUNT}{_CJK_UNIT}(?:以内|之内|左右|内|(?!\w))"
    rf"|{_ENGLISH_PREFIX}{_COUNT}{_CJK_UNIT}(?:以内|之内|左右|内?(?!\w))"
    rf"|{_ENGLISH_PREFIX}{_COUNT}"
    r"(?:words?|characters?|chars?|sentences?|paragraphs?|bullets?|points?|tokens?)"
    r"(?![A-Za-z])(?:以内|之内|内|左右)?",
    re.IGNORECASE,
)
_PIECE = re.compile(r"[^\W_]+(?:\.[0-9]+)*")


def _normalized(text):
    """Controls and every kind of whitespace become single spaces."""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return " ".join(text.split())


def _link_end(text, index):
    """Where an inline link whose destination starts at index ends, or None if none does.

    The destination is <...> or non-space text whose parentheses balance at any
    depth; an optional "...", '...' or (...) title may follow it.
    """
    if text.startswith("<", index):
        close = text.find(">", index)
        if close < 0:
            return None
        index = close + 1
    else:
        depth = 0
        while index < len(text) and text[index] != " ":
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                if not depth:
                    return index + 1
                depth -= 1
            index += 1
        if depth:
            return None
    tail = _LINK_TAIL.match(text, index)
    return None if tail is None else tail.end()


def _link_texts(text):
    """Inline links keep their text; destinations and titles never become search terms."""
    kept, position = [], 0
    for match in _LINK_OPEN.finditer(text):
        if match.start() < position:
            continue
        end = _link_end(text, match.end())
        if end is not None:
            kept += [text[position:match.start()], f" {match[1]} "]
            position = end
    return "".join(kept) + text[position:]


def _pieces(text):
    text = _link_texts(_normalized(text))
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
