"""Audit asset references in a research corpus without changing any file.

Pass --corpus as the absolute real corpus root (no symlink component) and
repeat --paper-dir to restrict the audit to immediate paper directories. Every
top-level *.md file of each selected paper is read through the no-follow
readings helpers and one deterministic JSON report is printed, grouped by file
basename. The command writes nothing, opens no database, imports no indexer and
makes no network call. Corpus defects are report data. Exit status 1 means at
least one input could not be read; 2 means invalid arguments or selection.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import html
from itertools import accumulate
import json
import os
from pathlib import Path
import re
import stat
import string
import sys
from urllib.parse import unquote

from cortex_platform.product.readings.files import (
    MAX_FILE_BYTES,
    PublicationConflict,
    directory,
    parts,
    read_file,
)

FORMAT_VERSION = 1
CLASSES = ("present", "missing", "prefix_candidate", "unsafe", "unknown",
           "placeholder", "external", "non_asset", "unsupported")
FINDING_CLASSES = frozenset({"missing", "prefix_candidate", "unsafe", "unknown",
                             "placeholder", "unsupported"})
# Only this basename is a candidate for a later staged prefix repair.
REPAIR_FILE_TYPE = "full_text.md"
IMAGE_SYNTAX = frozenset({"markdown_image", "markdown_reference_image", "html_img"})

COVERAGE = {
    "scope": "top-level *.md files of immediate corpus subdirectories",
    "supported_syntax": [
        "markdown_link", "markdown_image", "markdown_reference_link",
        "markdown_reference_image", "html_img", "html_a",
    ],
    "excluded_regions": ["fenced_code", "indented_code", "inline_code", "html_comment"],
    "reported_as_unsupported": [
        "html_srcset", "css_url", "html_other_asset_attribute", "unparsed_html_tag",
        "unparsed_markdown_destination", "unparsed_reference_definition",
        "unrecognized_pseudo_destination", "unclosed_html_comment",
    ],
    "not_detected": [
        "autolinks and bare URLs",
        "Markdown inside raw HTML blocks, including after a closed block comment, "
        "is scanned as live Markdown",
        "code, comments and HTML blocks nested in block quotes are scanned as live text",
        "list nesting is approximated from indentation",
        "a definition-shaped line inside a paragraph still resolves otherwise undefined uses",
    ],
    "asset_rule": ("image destinations, and link destinations with an assets path "
                   "segment, resolved inside the paper directory"),
    "existence_test": "regular non-symlink file; images are not decoded or verified",
}

_PUNCTUATION = frozenset(string.punctuation)
# cmark refuses link destinations nested deeper than this; it also bounds work.
_MAX_DESTINATION_PARENS = 32
_MAX_LABEL = 999
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_LIST_MARKER = re.compile(r"([-+*]|\d{1,9}[.)])([ \t]+|$)")
_QUOTE_MARKERS = re.compile(r"(?: {0,3}>[ \t]?)*")
_COMMENT_BLOCK = re.compile(r" {0,3}<!--")
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    "details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|"
    "h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|"
    "optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|"
    "tr|track|ul")
_RAW_TEXT_TAGS = "pre|script|style|textarea"
_BLANK_LINE = re.compile(r"^[ \t]*$")
# HTML block start conditions 1 and 3-6 with the end condition each line is
# tested against. Condition 2, comments, is handled separately.
_HTML_BLOCKS = (
    (re.compile(r" {0,3}<(?:" + _RAW_TEXT_TAGS + r")(?:[ \t>]|$)", re.I),
     re.compile(r"</(?:" + _RAW_TEXT_TAGS + r")>", re.I)),
    (re.compile(r" {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r" {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r" {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
    (re.compile(r" {0,3}</?(?:" + _HTML_BLOCK_TAGS + r")(?:[ \t]|/?>|$)", re.I), _BLANK_LINE),
)
_CLOSING_TAG = re.compile(r"</[A-Za-z][A-Za-z0-9-]*[ \t]*>")
# Lines that may interrupt a paragraph (headings, list items, fences, thematic
# breaks, setext underlines, HTML blocks of types 1-6). Matching too many lines
# only splits paragraphs, which can over-report but never hides a reference.
_INTERRUPT = re.compile(
    r"[ \t]*(?:#{1,6}(?:[ \t]|$)|(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)|`{3,}[^`]*$|~{3,}|<[?!]"
    r"|</?(?:" + _HTML_BLOCK_TAGS + "|" + _RAW_TEXT_TAGS + r")(?:[ \t/>]|$)"
    r"|([-*_=])(?:[ \t]*\1)*[ \t]*$)", re.I)
_SINGLE_LINE_BLOCK = re.compile(
    r"[ \t]*(?:#{1,6}(?:[ \t].*)?|`{3,}[^`]*|~{3,}.*|([-*_=])(?:[ \t]*\1)*[ \t]*)")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t\r]*\n")
_BACKTICKS = re.compile(r"`+")
_BRACKET_TOKEN = re.compile(r"\\.|\[|\]", re.S)
_DEFINITION = re.compile(r"^ {0,3}\[((?:[^\[\]\\\x00]|\\.){1,999})\]:", re.M)
_TOKEN_END = re.compile(r"[ \t\r\n]|$")
_MARKUP_ESCAPE = re.compile(
    r"\\([!-/:-@\[-`{-~])|&(?:#[0-9]{1,7};|#[xX][0-9a-fA-F]{1,6};|[A-Za-z][A-Za-z0-9]{1,31};)")
_PLACEHOLDER_TAIL = re.compile(r"[ \t]*(page=\d+[ \t]*,[ \t]*bbox=\[[0-9., \t-]*\])[ \t]*\)")
_PLACEHOLDER = re.compile(
    r"page=\d+[ \t]*,[ \t]*bbox=\[[ \t]*-?\d+(?:\.\d+)?(?:[ \t]*,[ \t]*-?\d+(?:\.\d+)?)*[ \t]*\]")
_PSEUDO = re.compile(r"[ \t]*(?:page|bbox)[ \t]*=", re.I)
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]+):")
_ASSET_LOOKING = re.compile(
    r"(?<![A-Za-z0-9_-])assets(?:/|\\|%2f|%5c)"
    r"|\.(?:png|jpe?g|gif|svg|webp|bmp|tiff?|pdf|mp4|webm|mov)(?![A-Za-z0-9])", re.I)
_TAG = re.compile(
    r"<([A-Za-z][A-Za-z0-9-]*)((?:\s+[^\s\"'<>/=\x00]+"
    r"(?:\s*=\s*(?:\"[^\"\x00]*\"|'[^'\x00]*'|[^\s\"'=<>`\x00]+))?)*)\s*/?>")
_ATTRIBUTE = re.compile(
    r"\s+([^\s\"'<>/=\x00]+)(?:\s*=\s*(?:\"([^\"\x00]*)\"|'([^'\x00]*)'|([^\s\"'=<>`\x00]+)))?")
_TAG_OPEN = re.compile(r"<[A-Za-z][A-Za-z0-9-]*(?=[\s/>]|$)")
_LOOSE_ATTRIBUTE = re.compile(r"\s[^\s\"'<>/=\x00]+[ \t]*=")
_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"\x00\n]*)\1\s*\)", re.I)


class AuditInputError(ValueError):
    """Invalid arguments or paper selection; no report is produced."""


@dataclass(frozen=True)
class AssetReference:
    """One supported reference occurrence.

    ``line``/``column`` (1-based, columns in characters) locate the construct
    at its use site; ``byte_start``/``byte_end`` locate the destination bytes,
    which for a reference-style use are those of its definition.
    """

    syntax: str
    line: int
    column: int
    byte_start: int
    byte_end: int
    raw_target: str
    decoded_target: str
    definition_line: int | None = None


@dataclass(frozen=True)
class UnsupportedForm:
    """An asset-looking construct the scanner does not resolve."""

    form: str
    line: int
    column: int
    byte_start: int
    byte_end: int
    snippet: str


class _Source:
    """Map character indexes of the decoded text to lines, columns and bytes.

    A leading byte order mark is not part of ``text``, but byte offsets still
    count its three bytes so spans address the file as stored.
    """

    def __init__(self, data: bytes):
        text = data.decode("utf-8")
        self._lead = 3 if text.startswith("﻿") else 0
        self.text = text[1:] if self._lead else text
        self.newlines = [match.start() for match in re.finditer("\n", self.text)]
        self._bytes = (None if len(data) - self._lead == len(self.text) else
                       list(accumulate((len(ch.encode()) for ch in self.text), initial=0)))

    def byte(self, index: int) -> int:
        return self._lead + (index if self._bytes is None else self._bytes[index])

    def position(self, index: int) -> tuple[int, int]:
        line = bisect_left(self.newlines, index)
        start = self.newlines[line - 1] + 1 if line else 0
        return line + 1, index - start + 1

    def line_end(self, index: int) -> int:
        position = bisect_left(self.newlines, index)
        return self.newlines[position] if position < len(self.newlines) else len(self.text)


def _indent(line: str) -> int:
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _dedent(line: str, columns: int) -> str:
    """Remove up to ``columns`` columns of leading indentation."""
    width = 0
    for offset, ch in enumerate(line):
        if width >= columns or ch not in " \t":
            return " " * (width - columns) + line[offset:]
        width += 1 if ch == " " else 4 - width % 4
    return ""


def _closes(fence: tuple[str, int, int], rest: str) -> bool:
    """Whether ``rest`` is a closing line for ``fence``."""
    close = _FENCE.fullmatch(rest)
    return bool(close and close.group(1)[0] == fence[0] and len(close.group(1)) >= fence[1]
                and not close.group(2).strip(" \t"))


@dataclass
class _Blocks:
    """Block-level structure that bounds inline parsing."""

    excluded: list[tuple[int, int]]   # code lines and HTML block comments
    raw: list[tuple[int, int]]        # HTML block lines; no code spans there
    breaks: list[int]                 # offsets where a new block starts
    paragraph_starts: set[int]        # line starts that are not continuations
    unclosed_comments: list[int]      # block comments that run to the end


def _html_block(relative: str, paragraph: bool) -> re.Pattern | None:
    """Return the end condition when the line opens an HTML block."""
    for start, end in _HTML_BLOCKS:
        if start.match(relative):
            return end
    stripped = relative.lstrip(" ")
    if paragraph or len(relative) - len(stripped) > 3:
        return None   # condition 7 cannot interrupt a paragraph
    tag = _TAG.match(stripped)
    if tag is not None and tag.group(1).lower() not in _RAW_TEXT_TAGS.split("|"):
        rest = stripped[tag.end():]
    else:
        closing = _CLOSING_TAG.match(stripped)
        rest = stripped[closing.end():] if closing else None
    return _BLANK_LINE if rest is not None and not rest.strip(" \t") else None


def _blocks(text: str) -> _Blocks:
    """Find code, HTML blocks and comments, and block boundaries line by line.

    List indentation approximates nesting: a fence, comment or HTML block
    opens relative to the list item holding it, including on the item's
    marker line, and a less indented line ends that item and the block.
    Block quotes only contribute boundaries; code, comments and HTML blocks
    inside them stay live text, so they can only over-report.
    """
    blocks = _Blocks([], [], [], set(), [])
    fence: tuple[str, int, int] | None = None     # marker, run length, item indent
    comment: tuple[int, int] | None = None        # opening offset, item indent
    html: tuple[re.Pattern, int] | None = None    # end condition, item indent
    lists: list[int] = []
    may_start_code, in_code, after_block, paragraph, depth = True, False, False, False, 0

    def open_block(rest: str, base: int, rest_at: int, end: int, line: str,
                   in_paragraph: bool) -> bool:
        """Open a fence, block comment or HTML block at ``rest`` if it starts one."""
        nonlocal fence, comment, html, after_block
        opened = _FENCE.fullmatch(rest)
        if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence = (opened.group(1)[0], len(opened.group(1)), base)
            blocks.excluded.append((rest_at, end))
            return True
        if _COMMENT_BLOCK.match(rest):
            at = text.find("<!--", rest_at, end)
            close = text.find("-->", at + 2, end)
            if close >= 0:
                blocks.excluded.append((at, close + 3))
                after_block = True
            else:
                comment = (at, base)
            return True
        ends = _html_block(rest, in_paragraph)
        if ends is None:
            return False
        blocks.raw.append((rest_at, end))
        if ends.search(line):
            after_block = True
        else:
            html = (ends, base)
        return True

    position = 0
    for segment in text.split("\n"):
        start, end = position, position + len(segment)
        position = end + 1
        line = segment[:-1] if segment.endswith("\r") else segment
        blank = not line.strip(" \t")
        indent = _indent(line)
        holder = fence or comment or html
        if holder is not None and not blank and indent < holder[-1]:
            # A less indented line ends the list item and the block it holds.
            # Strict CommonMark then reads a closing fence line at the outer
            # level, where it opens a new fence that hides every later
            # reference. Consuming it as the intended closer can only
            # over-report; any other fence line opens at the outer level.
            if comment is not None:
                blocks.excluded.append((comment[0], start))
            del lists[bisect_right(lists, indent):]
            closed = fence is not None and _closes(fence, _dedent(line, lists[-1] if lists else 0))
            fence = comment = html = None
            after_block = True
            if closed:
                blocks.excluded.append((start, end))
                may_start_code = True
                continue
        if comment is not None:
            close = line.find("-->")
            if close >= 0:
                blocks.excluded.append((comment[0], start + close + 3))
                comment, after_block, may_start_code = None, True, True
            continue
        if html is not None:
            blocks.raw.append((start, end))
            if html[0].search(line):
                html, after_block, may_start_code = None, True, True
            continue
        if fence is not None:
            blocks.excluded.append((start, end))
            if _closes(fence, _dedent(line, fence[2])):
                fence, may_start_code, after_block = None, True, True
            continue
        if not paragraph or after_block:
            blocks.paragraph_starts.add(start)
        if not blank and indent >= (lists[-1] if lists else 0) + 4 and (may_start_code or in_code):
            blocks.excluded.append((start, end))
            blocks.breaks.append(start)
            in_code, may_start_code, after_block, paragraph = True, False, True, False
            continue
        base = next((width for width in reversed(lists) if width <= indent), 0)
        # cmark lets condition 7 start when the line falls outside the list
        # item holding the paragraph, even though it cannot interrupt one.
        held = paragraph and not (lists and indent < lists[-1])
        if not blank and open_block(_dedent(line, base), base, start, end, line, held):
            del lists[bisect_right(lists, indent):]
            blocks.breaks.append(start)
            in_code = paragraph = False
            may_start_code = after_block   # a block closed on its own line
            continue
        if blank:
            blocks.breaks.append(start)
            may_start_code, after_block, paragraph, depth = True, False, False, 0
            continue
        quote = _QUOTE_MARKERS.match(line)
        level, content = quote.group().count(">"), line[quote.end():]
        if (after_block or level > depth or not content.strip(" \t")
                or _INTERRUPT.match(content)):
            blocks.breaks.append(start)
        single = bool(_SINGLE_LINE_BLOCK.fullmatch(content))
        after_block, paragraph, depth = single, not single, level
        in_code = False
        content = line.lstrip(" \t")
        marker = _LIST_MARKER.match(content)
        if marker:
            while lists and indent < lists[-1]:
                lists.pop()
            spaces = len(marker.group(2))
            lists.append(indent + len(marker.group(1)) + (spaces if 1 <= spaces <= 4 else 1))
            rest_at = start + len(line) - len(content) + marker.end()
            if open_block(content[marker.end():], lists[-1], rest_at, end, line, False):
                paragraph = False
        elif may_start_code:
            while lists and indent < lists[-1]:
                lists.pop()
        # Indented code cannot interrupt a paragraph but may follow a heading.
        may_start_code = after_block
    if comment is not None:
        blocks.excluded.append((comment[0], len(text)))
        blocks.unclosed_comments.append(comment[0])
    return blocks


def _limit(breaks: list[int], index: int, size: int) -> int:
    """End of the block containing ``index``: the next boundary after it."""
    position = bisect_right(breaks, index)
    return breaks[position] if position < len(breaks) else size


def _closing_backticks(text: str, start: int, length: int, limit: int) -> int | None:
    for run in _BACKTICKS.finditer(text, start, limit):
        if len(run.group()) == length:
            return run.start()
    return None


def _inline_code_and_comment_ranges(text: str, blocks: _Blocks) -> list[tuple[int, int]]:
    """Code spans and inline HTML comments, each confined to one block.

    Constructs bind left to right: a raw HTML tag that starts first keeps its
    attribute values intact, and an opener without a closer in its block is
    plain text. HTML block lines hold comments but no code spans. A failed
    search is remembered for later openers that share its block, which keeps
    the scan linear.
    """
    breaks = blocks.breaks
    raw_starts = [start for start, _ in blocks.raw]
    ranges: list[tuple[int, int]] = []
    unclosed: dict[str | int, tuple[int, int]] = {}   # closer -> (block end, searched from)

    def find_closer(key, start: int, limit: int, search) -> int | None:
        known = unclosed.get(key)
        if known is not None and known[0] == limit and known[1] <= start:
            return None
        found = search(start, limit)
        if found is None:
            unclosed[key] = (limit, start)
        return found

    def comment_close(start: int, limit: int) -> int | None:
        found = text.find("-->", start, limit)
        return None if found < 0 else found

    index, size = 0, len(text)
    while index < size:
        ch = text[index]
        if ch == "\\":
            index += 2
        elif ch == "<" and text.startswith("<!--", index):
            if text.startswith("<!-->", index) or text.startswith("<!--->", index):
                end = text.index(">", index) + 1
            else:
                close = find_closer("-->", index + 4, _limit(breaks, index, size), comment_close)
                end = None if close is None else close + 3
            if end is None:
                index += 4
            else:
                ranges.append((index, end))
                index = end
        elif ch == "<":
            tag = _TAG.match(text, index)
            index = tag.end() if tag and tag.end() <= _limit(breaks, index, size) else index + 1
        elif ch == "`":
            run_end = index
            while run_end < size and text[run_end] == "`":
                run_end += 1
            line = bisect_right(raw_starts, index) - 1
            if line >= 0 and index < blocks.raw[line][1]:
                index = run_end
                continue
            length = run_end - index
            close = find_closer(length, run_end, _limit(breaks, index, size),
                                lambda start, limit: _closing_backticks(text, start, length, limit))
            if close is None:
                index = run_end
            else:
                ranges.append((index, close + length))
                index = close + length
        else:
            index += 1
    return ranges


def _mask(text: str, ranges: list[tuple[int, int]]) -> str:
    """Blank excluded regions with NUL while keeping offsets and line endings."""
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = "\x00"
    return "".join(chars)


def _escaped(text: str, index: int) -> bool:
    count = 0
    while index - count - 1 >= 0 and text[index - count - 1] == "\\":
        count += 1
    return count % 2 == 1


def _skip_space(text: str, index: int) -> int:
    """Skip spaces/tabs and at most one line ending."""
    size = len(text)
    while index < size and text[index] in " \t":
        index += 1
    if index < size and text[index] == "\r":
        index += 1
    if index < size and text[index] == "\n" and not _PARAGRAPH_BREAK.match(text, index):
        index += 1
    while index < size and text[index] in " \t":
        index += 1
    return index


def _destination(text: str, index: int) -> tuple[int, int, int] | None:
    """Return (start, end, after) of a link destination, or None."""
    size = len(text)
    if index < size and text[index] == "<":
        cursor = index + 1
        while cursor < size:
            ch = text[cursor]
            if ch == "\\" and cursor + 1 < size and text[cursor + 1] not in "\r\n":
                cursor += 2
                continue
            if ch == ">":
                return index + 1, cursor, cursor + 1
            if ch in "<\r\n\x00":
                return None
            cursor += 1
        return None
    cursor, depth = index, 0
    while cursor < size:
        ch = text[cursor]
        if ch == "\\" and cursor + 1 < size and text[cursor + 1] in _PUNCTUATION:
            cursor += 2
            continue
        if ch == "(":
            depth += 1
            if depth > _MAX_DESTINATION_PARENS:
                return None
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch <= " " or ch == "\x7f":
            break
        cursor += 1
    return None if depth else (index, cursor, cursor)


def _title_end(text: str, index: int) -> int | None:
    closing = {'"': '"', "'": "'", "(": ")"}.get(text[index] if index < len(text) else "")
    if closing is None:
        return None
    cursor = index + 1
    while cursor < len(text):
        ch = text[cursor]
        if ch == "\\":
            cursor += 2
            continue
        if ch == closing:
            return cursor + 1
        if ch == "\x00" or (closing == ")" and ch == "(") or (
                ch == "\n" and _PARAGRAPH_BREAK.match(text, cursor)):
            return None
        cursor += 1
    return None


def _inline_tail(text: str, index: int) -> tuple[int, int, int] | None:
    """Parse ``destination [title])`` after an opening parenthesis."""
    size = len(text)
    start = _skip_space(text, index)
    if start < size and text[start] == ")":
        return start, start, start + 1
    parsed = _destination(text, start)
    if parsed is None:
        return None
    dest_start, dest_end, after = parsed
    cursor = _skip_space(text, after)
    if cursor < size and text[cursor] == ")":
        return dest_start, dest_end, cursor + 1
    if cursor == after:
        return None
    title = _title_end(text, cursor)
    if title is None:
        return None
    cursor = _skip_space(text, title)
    if cursor < size and text[cursor] == ")":
        return dest_start, dest_end, cursor + 1
    return None


def _bracket_pairs(text: str, breaks: list[int]) -> dict[int, int]:
    """Match unescaped brackets in one pass; a block boundary drops open brackets."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    pending = 0
    for token in _BRACKET_TOKEN.finditer(text):
        if pending < len(breaks) and breaks[pending] <= token.start():
            stack.clear()
            pending = bisect_right(breaks, token.start(), pending)
        value = token.group()
        if value == "[":
            stack.append(token.start())
        elif value == "]":
            if stack:
                pairs[stack.pop()] = token.start()
    return pairs


def _normalize_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _markup_unescape(raw: str) -> str:
    """Apply Markdown backslash escapes and entity references once."""
    return _MARKUP_ESCAPE.sub(
        lambda m: m.group(1) if m.group(1) is not None else html.unescape(m.group(0)), raw)


def scan_markdown(data: bytes) -> tuple[tuple[AssetReference, ...], tuple[UnsupportedForm, ...]]:
    """Scan strict UTF-8 Markdown bytes; raises UnicodeDecodeError otherwise."""
    source = _Source(data)
    size = len(source.text)
    blocks = _blocks(source.text)
    breaks = blocks.breaks
    text = _mask(source.text, blocks.excluded)
    text = _mask(text, _inline_code_and_comment_ranges(text, blocks))
    consumed = bytearray(size + 1)
    closers = [match.start() for match in re.finditer(r"\)", text)]
    found: list[tuple[int, AssetReference]] = []
    unsupported: list[UnsupportedForm] = []

    def mark(start: int, end: int) -> None:
        consumed[start:end] = b"\x01" * (end - start)

    def reference(syntax, at, start, end, decoded, definition_line=None):
        line, column = source.position(at)
        raw = source.text[start:end]
        found.append((at, AssetReference(
            syntax, line, column, source.byte(start), source.byte(end), raw,
            decoded if decoded is not None else _markup_unescape(raw), definition_line)))

    def unsupported_form(form, start, end):
        line, column = source.position(start)
        unsupported.append(UnsupportedForm(form, line, column, source.byte(start),
                                           source.byte(end), source.text[start:end]))

    def tail_end(after: int) -> int:
        """End of the text after '(' that a failed destination is judged by."""
        position = bisect_left(closers, after)
        return min(source.line_end(after), closers[position] if position < len(closers) else size)

    for comment in blocks.unclosed_comments:
        if _ASSET_LOOKING.search(source.text, comment + 4):
            unsupported_form("unclosed_html_comment", comment, source.line_end(comment))

    definitions: dict[str, tuple[int, int, int]] = {}
    # A definition cannot interrupt a paragraph, so CommonMark reads such a
    # line as text. The block model is approximate, so it still resolves uses
    # that no real definition matches: an error over-reports, never hides.
    fallback: dict[str, tuple[int, int, int, int]] = {}
    previous_end = -2   # line end of the last valid definition
    for match in _DEFINITION.finditer(text):
        if match.group(1).startswith("^"):
            continue   # a footnote definition; its text is scanned inline
        continuation = (match.start() not in blocks.paragraph_starts
                        and match.start() != previous_end + 1)
        start = _skip_space(text, match.end())
        line_end = source.line_end(start)
        parsed = _destination(text, start)
        valid = False
        if parsed is not None and parsed[1] > parsed[0]:
            dest_start, dest_end, after = parsed
            rest = source.line_end(after)
            if not text[after:rest].strip(" \t\r"):
                valid, line_end = True, rest
            elif text[after] in " \t":
                title = _title_end(text, _skip_space(text, after))
                rest = source.line_end(title) if title is not None else rest
                valid = title is not None and not text[title:rest].strip(" \t\r")
                line_end = rest
        label = _normalize_label(match.group(1))
        if valid and continuation:
            if label:
                fallback.setdefault(label, (dest_start, dest_end, source.position(dest_start)[0],
                                            match.start(1) - 1))
        elif valid:
            mark(match.start(), line_end)
            previous_end = line_end
            if label and label not in definitions:
                definitions[label] = (dest_start, dest_end, source.position(dest_start)[0])
        elif _ASSET_LOOKING.search(text, start, _TOKEN_END.search(text, start).start()):
            # Only an asset-looking destination token is a failed definition;
            # the rest of the line is ordinary text and is scanned below.
            unsupported_form("unparsed_reference_definition", start, line_end)

    clean: tuple[int, int] | None = None   # (end, start) of a search without assets
    brackets = _bracket_pairs(text, breaks)
    for index in sorted(brackets):
        if consumed[index]:
            continue
        close = brackets[index]
        image = index > 0 and text[index - 1] == "!" and not _escaped(text, index - 1)
        at, kind = (index - 1, "image") if image else (index, "link")
        after = close + 1
        if after < size and text[after] == "(":
            tail = _inline_tail(text, after + 1)
            if tail is not None:
                reference(f"markdown_{kind}", at, tail[0], tail[1], None)
                mark(after, tail[2])
                continue
            placeholder = _PLACEHOLDER_TAIL.match(text, after + 1)
            if placeholder is not None:
                reference(f"markdown_{kind}", at, placeholder.start(1), placeholder.end(1), None)
                mark(after, placeholder.end())
                continue
            end = tail_end(after)
            if _PSEUDO.match(text, after + 1, end):
                unsupported_form("unrecognized_pseudo_destination", after + 1, end)
                mark(after, end)
            elif not (clean and clean[0] == end and clean[1] <= after + 1):
                if _ASSET_LOOKING.search(text, after + 1, end):
                    unsupported_form("unparsed_markdown_destination", after + 1, end)
                    mark(after, end)
                else:
                    clean = (end, after + 1)
            # A failed inline tail still leaves [label] as a shortcut reference.
        label_start, label_end = index + 1, close
        # A second bracket without a partner is text, leaving a shortcut.
        label_close = brackets.get(after) if after < size and text[after] == "[" else None
        if label_close is not None and (label_close - after - 1 > _MAX_LABEL
                                        or text[after + 1:label_close].strip()):
            label_start, label_end = after + 1, label_close
        # A label longer than CommonMark's limit never matches; checking the
        # length first also keeps nested brackets from being copied repeatedly.
        definition = None
        if label_end - label_start <= _MAX_LABEL:
            key = _normalize_label(text[label_start:label_end])
            definition = definitions.get(key)
            if definition is None and key in fallback and fallback[key][3] != index:
                definition = fallback[key][:3]
        if definition is not None and label_close is not None:
            # An undefined second label stays text that later brackets can use.
            mark(after, label_close + 1)
        if definition is not None:
            reference(f"markdown_reference_{kind}", at, definition[0], definition[1], None,
                      definition[2])

    # A ']' that pairs inside the label, such as an interval "(0, 1]" in an
    # ingested figure caption, leaves the real "](" without an opening bracket,
    # and CommonMark renders the construct as text. Every "](" that no parsed
    # construct consumed is judged like a failed inline destination, including
    # one after an escaped ']', so such a reference is reported, never hidden.
    clean = None
    for match in re.finditer(r"\]\(", text):
        after = match.end() - 1
        if consumed[after]:
            continue
        end = tail_end(after)
        if clean and clean[0] == end and clean[1] <= after + 1:
            continue
        if _PSEUDO.match(text, after + 1, end) or _ASSET_LOOKING.search(text, after + 1, end):
            unsupported_form("unparsed_markdown_destination", after + 1, end)
            mark(after, end)
        else:
            clean = (end, after + 1)

    tags: list[tuple[int, int]] = []
    for tag in _TAG.finditer(text):
        tags.append(tag.span())
        if consumed[tag.start()]:
            continue
        name = tag.group(1).lower()
        for attribute in _ATTRIBUTE.finditer(text, tag.start(2), tag.end(2)):
            group = next((g for g in (2, 3, 4) if attribute.group(g) is not None), None)
            if group is None:
                continue
            key, value = attribute.group(1).lower(), attribute.group(group)
            start, end = attribute.start(group), attribute.end(group)
            if (name, key) in (("img", "src"), ("a", "href")):
                reference(f"html_{name}", tag.start(), start, end, html.unescape(value))
            elif key == "srcset" and value.strip():
                unsupported_form("html_srcset", start, end)
            elif key in ("alt", "title", "style") or not value.strip():
                continue
            elif key in ("src", "href", "poster", "data") or _ASSET_LOOKING.search(value):
                unsupported_form("html_other_asset_attribute", start, end)

    inside = 0
    for opening in _TAG_OPEN.finditer(text):
        at = opening.start()
        while inside < len(tags) and tags[inside][1] <= at:
            inside += 1
        if consumed[at] or (inside < len(tags) and tags[inside][0] <= at):
            continue
        # An opening tag that never completes (cut by truncation, or a quote
        # left open) keeps its asset-looking attribute visible as unsupported.
        stop = _limit(breaks, at, size)
        for marker in ("<", ">"):
            found_at = text.find(marker, opening.end(), stop)
            stop = stop if found_at < 0 else found_at
        attribute = _LOOSE_ATTRIBUTE.search(text, opening.end(), stop)
        if attribute and _ASSET_LOOKING.search(text, attribute.end(), stop):
            while text[stop - 1] in " \t\r\n":
                stop -= 1
            unsupported_form("unparsed_html_tag", at, stop)

    for css in _CSS_URL.finditer(text):
        if not consumed[css.start()] and _ASSET_LOOKING.search(css.group(2)):
            unsupported_form("css_url", css.start(2), css.end(2))

    found.sort(key=lambda item: (item[0], item[1].byte_start))
    unsupported.sort(key=lambda item: (item.byte_start, item.form))
    return tuple(ref for _, ref in found), tuple(unsupported)


def _errno_reason(error: OSError) -> str:
    return os.strerror(error.errno).lower().replace(" ", "_") if error.errno else "os_error"


def _probe(paper_root: Path, components: tuple[str, ...]) -> tuple[str, str | None]:
    """Classify a paper-relative path without following any link."""
    opened: list[int] = []
    try:
        with directory(paper_root) as root_fd:
            fd = root_fd
            try:
                for name in components[:-1]:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        return "unsafe", "symlink"
                    if not stat.S_ISDIR(info.st_mode):
                        return "missing", None
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    opened.append(fd)
                info = os.stat(components[-1], dir_fd=fd, follow_symlinks=False)
            finally:
                for item in opened:
                    os.close(item)
    except (FileNotFoundError, NotADirectoryError):
        return "missing", None
    except PublicationConflict as error:
        return "unknown", str(error)
    except OSError as error:
        return "unknown", _errno_reason(error)
    if stat.S_ISLNK(info.st_mode):
        return "unsafe", "symlink"
    if stat.S_ISREG(info.st_mode):
        return "present", None
    return "unsafe", "not_regular_file"


def _unsafe_reason(path: str, decoded: str | None) -> str | None:
    if decoded is None:
        return "invalid_percent_encoding"
    if "\x00" in decoded:
        return "nul"
    if "\\" in decoded:
        return "backslash"
    if re.search(r"%2[fF]|%5[cC]", path):
        return "encoded_separator"
    if decoded.startswith("/") or re.match(r"[A-Za-z]:", decoded):
        return "absolute_path"
    relative = decoded[2:] if decoded.startswith("./") else decoded
    if ".." in relative.split("/"):
        return "traversal"
    try:
        parts(relative)
    except PublicationConflict:
        return "unsafe_component"
    return None


def _decode_path(value: str) -> str | None:
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        return None


class _PaperProbe:
    def __init__(self, paper_root: Path):
        self.paper_root = paper_root
        self._cache: dict[tuple[str, ...], tuple[str, str | None]] = {}

    def __call__(self, components: tuple[str, ...]) -> tuple[str, str | None]:
        if components not in self._cache:
            self._cache[components] = _probe(self.paper_root, components)
        return self._cache[components]


def _exists(probe: _PaperProbe, components: tuple[str, ...],
            literal: tuple[str, ...] | None) -> tuple[str, str | None]:
    """Probe a path; an ingest file name may keep a literal '#...' or '?...' suffix."""
    status, why = probe(components)
    if status != "present" and literal is not None and probe(literal)[0] == "present":
        return "present", None
    return status, why


def _prefix_detail(ref: AssetReference, components: tuple[str, ...],
                   literal: tuple[str, ...] | None, paper_dir: str, file_type: str,
                   probe: _PaperProbe) -> dict:
    """Decide whether removing ``papers/<paper>/`` is a proven same-paper repair.

    The original and the replacement use the same existence rule as an
    ordinary reference, including a literal '#...' or '?...' file name.
    """
    original, _ = _exists(probe, components, literal)
    replacement, _ = _exists(probe, ("assets", *components[3:]),
                             ("assets", *literal[3:]) if literal else None)
    prefix = next((p for p in (f"papers/{paper_dir}/", f"./papers/{paper_dir}/")
                   if ref.raw_target.startswith(p)), None)
    if components[1] != paper_dir:
        reason = "different_paper"
    elif original == "present":
        reason = "original_resolves"
    elif original != "missing":
        reason = "original_unsafe"
    elif replacement == "missing":
        reason = "replacement_missing"
    elif replacement == "unknown":
        reason = "replacement_unknown"
    elif replacement != "present":
        reason = "replacement_unsafe"
    elif prefix is None:
        reason = "encoded_prefix"
    elif file_type != REPAIR_FILE_TYPE:
        reason = "protected_file_type"
    else:
        reason = None
    return {"prefix_paper_dir": components[1], "repairable": reason is None, "reason": reason,
            "replacement": ref.raw_target[len(prefix):] if reason is None else None}


def _classify(ref: AssetReference, paper_dir: str, file_type: str,
              probe: _PaperProbe) -> tuple[str, str, dict]:
    """Return (class, unique-target key, extra finding fields)."""
    target = ref.decoded_target
    if _PLACEHOLDER.fullmatch(target):
        return "placeholder", target, {}
    if _PSEUDO.match(target):
        return "unsupported", target, {"reason": "unrecognized_pseudo_destination"}
    if not target or target.startswith("#"):
        return "non_asset", target, {}
    scheme = _SCHEME.match(target)
    if scheme or target.startswith("//"):
        if scheme and scheme.group(1).lower() == "file":
            return "unsafe", target, {"reason": "file_url"}
        return "external", target, {}
    path = re.split(r"[?#]", target, maxsplit=1)[0]
    decoded = _decode_path(path)
    segments = (decoded if decoded is not None else path).replace("\\", "/").split("/")
    if ref.syntax not in IMAGE_SYNTAX and "assets" not in segments:
        return "non_asset", target, {}
    reason = _unsafe_reason(path, decoded)
    if reason is not None:
        return "unsafe", target, {"reason": reason}
    relative = decoded[2:] if decoded.startswith("./") else decoded
    components = tuple(relative.split("/"))
    literal = None
    if path != target:
        whole = _decode_path(target[2:] if target.startswith("./") else target)
        if whole is not None and _unsafe_reason(whole, whole) is None:
            literal = tuple(whole.split("/"))
    if len(components) >= 4 and components[0] == "papers" and components[2] == "assets":
        return "prefix_candidate", relative, _prefix_detail(ref, components, literal, paper_dir,
                                                            file_type, probe)
    status, why = probe(components)
    if status != "present" and literal is not None and probe(literal)[0] == "present":
        return "present", "/".join(literal), {}
    return status, relative, ({"reason": why} if why else {})


def _audit_file(paper_root: Path, paper_dir: str, name: str, data: bytes) -> dict:
    references, unsupported = scan_markdown(data)
    probe = _PaperProbe(paper_root)
    occurrences = dict.fromkeys(CLASSES, 0)
    unique: dict[str, set[str]] = {name_: set() for name_ in CLASSES}
    repairable: set[int] = set()
    findings = []
    for ref in references:
        kind, key, extra = _classify(ref, paper_dir, name, probe)
        occurrences[kind] += 1
        unique[kind].add(key)
        if extra.get("repairable"):
            repairable.add(ref.byte_start)
        if kind in FINDING_CLASSES:
            finding = {"kind": kind, "syntax": ref.syntax, "line": ref.line, "column": ref.column,
                       "byte_start": ref.byte_start, "byte_end": ref.byte_end,
                       "raw_target": ref.raw_target, "decoded_target": ref.decoded_target,
                       "reason": None}
            if ref.definition_line is not None:
                finding["definition_line"] = ref.definition_line
            finding.update(extra)
            findings.append(finding)
    for form in unsupported:
        occurrences["unsupported"] += 1
        unique["unsupported"].add(form.snippet)
        findings.append({"kind": "unsupported", "syntax": form.form, "line": form.line,
                         "column": form.column, "byte_start": form.byte_start,
                         "byte_end": form.byte_end, "raw_target": form.snippet,
                         "decoded_target": None, "reason": "unsupported_syntax"})
    findings.sort(key=lambda f: (f["line"], f["column"], f["byte_start"], f["kind"]))
    return {"path": f"{paper_dir}/{name}", "paper_dir": paper_dir, "file_type": name,
            "sha256": hashlib.sha256(data).hexdigest(), "occurrences": occurrences,
            "unique_targets": {k: len(v) for k, v in unique.items()},
            "repairable_destinations": len(repairable), "findings": findings}


def _markdown_reason(info: os.stat_result) -> str | None:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "not_regular_file"
    if info.st_nlink != 1:
        return "hardlink"
    if info.st_size > MAX_FILE_BYTES:
        return "oversized"
    return None


def _audit_paper(corpus: Path, paper_dir: str, files: list, unreadable: list) -> None:
    paper_root = corpus / paper_dir
    try:
        with directory(paper_root) as fd:
            entries = [(name, os.stat(name, dir_fd=fd, follow_symlinks=False))
                       for name in sorted(os.listdir(fd)) if name.endswith(".md")]
    except PublicationConflict as error:
        unreadable.append({"path": paper_dir, "reason": str(error)})
        return
    except OSError as error:
        unreadable.append({"path": paper_dir, "reason": _errno_reason(error)})
        return
    for name, info in entries:
        relative = f"{paper_dir}/{name}"
        reason = _markdown_reason(info)
        if reason is None:
            try:
                files.append(_audit_file(paper_root, paper_dir, name, read_file(paper_root, name)))
                continue
            except UnicodeDecodeError:
                reason = "invalid_utf8"
            except PublicationConflict as error:
                reason = str(error)
            except OSError as error:
                reason = _errno_reason(error)
        unreadable.append({"path": relative, "reason": reason, "file_type": name})


def _require_corpus(corpus: Path) -> None:
    if not corpus.is_absolute() or ".." in corpus.parts:
        raise AuditInputError(f"--corpus must be an absolute normalized path: {corpus}")
    try:
        with directory(corpus):
            pass
    except (OSError, PublicationConflict):
        raise AuditInputError(
            f"--corpus must be an existing directory without symlink components: {corpus}"
        ) from None


def _select(corpus: Path, paper_dirs: tuple[str, ...], unreadable: list) -> list[str]:
    with directory(corpus) as fd:
        if paper_dirs:
            # A case- or normalization-insensitive file system would also
            # resolve a variant spelling, so require the exact entry name.
            entries = set(os.listdir(fd))
            selected = sorted(set(paper_dirs))
            for name in selected:
                try:
                    if len(parts(name)) != 1 or name not in entries:
                        raise PublicationConflict("not an exact entry")
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except (OSError, PublicationConflict):
                    info = None
                if info is None or not stat.S_ISDIR(info.st_mode):
                    raise AuditInputError(
                        f"--paper-dir must name an existing immediate paper directory "
                        f"exactly as listed, without symlinks: {name!r}")
            return selected
        selected = []
        for name in sorted(os.listdir(fd)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                selected.append(name)
            elif stat.S_ISLNK(info.st_mode):
                unreadable.append({"path": name, "reason": "symlink"})
        return selected


def _aggregate(files: list[dict], unreadable: list[dict]) -> dict:
    groups: dict[str, dict] = {}

    def group(file_type: str) -> dict:
        return groups.setdefault(file_type, {
            "files": 0, "files_unreadable": 0, "files_affected": 0,
            "occurrences": dict.fromkeys(CLASSES, 0),
            "unique_targets": dict.fromkeys(CLASSES, 0), "repairable_destinations": 0})

    for record in files:
        totals = group(record["file_type"])
        totals["files"] += 1
        totals["files_affected"] += bool(record["findings"])
        totals["repairable_destinations"] += record["repairable_destinations"]
        for kind in CLASSES:
            totals["occurrences"][kind] += record["occurrences"][kind]
            totals["unique_targets"][kind] += record["unique_targets"][kind]
    for item in unreadable:
        if "file_type" in item:
            group(item["file_type"])["files_unreadable"] += 1
    return groups


def audit_corpus(corpus: Path, *, paper_dirs: tuple[str, ...] = ()) -> dict:
    """Read-only audit of the selected (default: every) immediate paper directory."""
    _require_corpus(corpus)
    unreadable: list[dict] = []
    selected = _select(corpus, paper_dirs, unreadable)
    files: list[dict] = []
    for paper_dir in selected:
        _audit_paper(corpus, paper_dir, files, unreadable)
    unreadable.sort(key=lambda item: item["path"])
    return {
        "format_version": FORMAT_VERSION,
        "corpus": str(corpus),
        "paper_dirs": selected,
        "coverage": COVERAGE,
        "files_scanned": len(files),
        "unreadable_inputs": [{"path": i["path"], "reason": i["reason"]} for i in unreadable],
        "files": files,
        "by_file_type": _aggregate(files, unreadable),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.audit_asset_refs", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True,
                        help="absolute real corpus root; never defaulted")
    parser.add_argument("--paper-dir", action="append", default=[],
                        help="immediate paper directory to audit; repeatable")
    args = parser.parse_args(argv)
    try:
        report = audit_corpus(args.corpus, paper_dirs=tuple(args.paper_dir))
    except AuditInputError as error:
        print(f"audit_asset_refs: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["unreadable_inputs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
