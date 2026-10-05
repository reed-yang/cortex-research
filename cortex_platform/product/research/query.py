"""The lexical search text for a fresh research selection, derived from the question.

Pure and deterministic: no I/O and no model call. The packet keeps the verbatim
question as `query`; the derived text is recorded as its `retrieval_query`.
"""

import re
import unicodedata
from collections.abc import Iterable

from ..sources.search import MAX_QUERY_BYTES, MAX_QUERY_TERMS, query_terms
from .context import _INLINE_LINK, _LABEL_BRACKET, _LINK_TEXT

#: English words that describe the requested answer format rather than its topic.
FORMAT_TERMS = frozenset(
    "cite cites cited citing citation citations word words character characters chars "
    "sentence sentences paragraph paragraphs bullet bullets concise concisely briefly "
    "markdown latex please summarize summarise".split()
)
_LINK_TEXT_PREFIX = re.compile(_LINK_TEXT)
#: The answer's citation scan (inline link, then label bracket) first, so labels
#: are read exactly as answers cite; then any other link text, which is a link
#: only when a destination of any depth follows it (_destination_ends).
_QUERY_SCAN = re.compile(rf"(?P<link>{_INLINE_LINK})|(?P<label>{_LABEL_BRACKET})|{_LINK_TEXT}")
#: After a destination's balanced text: an optional "...", '...' or (...) title, then ")".
_TITLE_TAIL = re.compile(r"""(?:\s+(?:"[^"]*"|'[^']*'|\([^()]*\)))?\s*\)""")
_RUN = re.compile(r"\S+")
_PARENTHESIS = re.compile(r"[()]")
# Explicit answer-length requirements only; other numbers, years and versions stay.
# A count may be a range (800-1000, 300到500, 2 to 3). After a CJK unit the span
# must end at a length suffix, 的, punctuation, a space, the end of the text or an
# answer verb (300字总结, 300字内回答). So a count inside a compound term such as
# 1024字节, 2段式, 8字符串 or 500字内存 is kept, and so is an ordinal such as 第2段
# or 第 2段. A verb that starts a compound noun (分析法, 解释器, 写作) is not an
# answer verb, so 2段分析法 and 3段写作技巧 keep their counts after 用 as well.
# After a strong prefix the suffix 内 always ends the span, and 字数 may be
# followed by a bare count (字数控制在500以内). 在, 用 and 字数 start a requirement
# only at the start of a word or after 请, 把 or 将 (请把字数控制在500以内), so 现在,
# 使用 and 汉字数 keep their characters. A hyphenated English size is a
# requirement only for words, sentences, paragraphs and bullets (a 300-word
# summary), not for model or scale sizes such as a 128-token context.
_NUMBER = r"(?<![0-9A-Za-z.第])(?<!第 )\d{1,5}(?:\s*(?:[-–~～到至]|to(?=\s))\s*\d{1,5})?"
_COUNT = rf"{_NUMBER}\s*(?:个\s*)?"
_CJK_UNIT = r"(?:段落|句话|字符(?!串)|字|词|句|段|条)"
_ANSWER_VERB = r"(?:(?:总结|概括|概述|回答|介绍|说明|描述|解释|阐述|分析|论述|讲解|写)(?![法器作]))"
_CJK_CLOSE = rf"(?:以内|之内|左右|的|内?(?!\w)|内?(?={_ANSWER_VERB}))"
_CJK_PREFIX = r"(?:控制在|不超过|不多于|少于)"
_WORD_START = r"(?:(?<![^\W\d_])|(?<=[请把将]))"
_ENGLISH_PREFIX = r"(?:\b(?:within|under|in|at most|no more than)\s+)?"
_LENGTH = re.compile(
    rf"{_CJK_PREFIX}\s*{_COUNT}{_CJK_UNIT}(?:内|{_CJK_CLOSE})"
    rf"|{_WORD_START}字数\s*[:：]?\s*(?:{_CJK_PREFIX}|在)?\s*{_COUNT}{_CJK_UNIT}?{_CJK_CLOSE}"
    rf"|{_WORD_START}[在用]\s*{_COUNT}{_CJK_UNIT}{_CJK_CLOSE}"
    rf"|{_ENGLISH_PREFIX}{_COUNT}{_CJK_UNIT}{_CJK_CLOSE}"
    rf"|{_ENGLISH_PREFIX}{_NUMBER}"
    r"(?:\s*(?:个\s*)?(?:words?|characters?|chars?|sentences?|paragraphs?|bullets?|points?|tokens?)"
    r"|-(?:words?|sentences?|paragraphs?|bullets?))"
    r"(?![A-Za-z])(?:以内|之内|内|左右)?",
    re.IGNORECASE,
)
_PIECE = re.compile(r"[^\W_]+(?:\.[0-9]+)*")


def _normalized(text):
    """Controls and every kind of whitespace become single spaces."""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return " ".join(text.split())


def _destination_ends(text):
    """Where the link destination opened by each "(" ends, at any nesting depth.

    As in the answer grammar, a destination is optional whitespace, text
    without whitespace whose parentheses balance, an optional "...", '...' or
    (...) title and ")", but its parentheses may nest to any depth. Maps each
    "(" that opens one to the index just past its closing ")". One pass.
    """
    ends, before_space = {}, None
    for run in _RUN.finditer(text):
        # None is a "(" before this run, whose destination starts after whitespace.
        stack, after_space = [None], None
        for parenthesis in _PARENTHESIS.finditer(text, run.start(), run.end()):
            if parenthesis[0] == "(":
                stack.append(parenthesis.start())
            elif stack:
                opener = stack.pop()
                if opener is None:
                    after_space = parenthesis.end()
                else:
                    ends[opener] = parenthesis.end()
        if stack and (tail := _TITLE_TAIL.match(text, run.end())):
            if stack[-1] is None:
                after_space = tail.end()
            else:
                ends[stack[-1]] = tail.end()
        if before_space is not None and after_space is not None:
            ends[before_space] = after_space
        before_space = run.end() - 1 if text[run.end() - 1] == "(" else None
    return ends


def _without_citations(text):
    """Read with the answer's citation scan: label-led brackets go, links keep their text.

    A bracket that cited_labels would read as a label group, malformed ones
    included, is removed, together with the destination of a link it starts
    (its text may hold one nested bracket). An inline link, including one whose
    destination nests parentheses to any depth, is replaced by its text,
    cleaned the same way, so destinations and titles never become search terms.
    Other bracketed text stays and is scanned inside.
    """
    ends = _destination_ends(text)
    kept, position, scan = [], 0, 0
    while (match := _QUERY_SCAN.search(text, scan)) is not None:
        link_text = _LINK_TEXT_PREFIX.match(text, match.start())
        destination = ends.get(link_text.end()) if link_text else None
        if match["link"] is not None:
            end, words = match.end(), link_text[0][1:-1]
        elif match["label"] is not None:
            end, words = destination or ends.get(match.end(), match.end()), ""
        elif destination is not None:
            end, words = destination, link_text[0][1:-1]
        else:
            scan = match.start() + 1
            continue
        kept += [text[position:match.start()], f" {_without_citations(words)} "]
        position = scan = end
    return "".join(kept) + text[position:]


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
