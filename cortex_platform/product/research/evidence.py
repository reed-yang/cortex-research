"""Section-aware paper evidence: bounded windows of retained files, in a fixed slot order.

Each file entry is one byte window of one retained file version. Its locator gives
the window's line range and byte offset in that version, and its content hash is
the whole file's sha256. A window is either the file's prefix or starts at a
section heading; it is never a complete deep read. Only a completely read file is
outlined: a longer file contributes its prefix, because a scan that stopped early
is not evidence about the sections it did not reach.
"""

import hashlib
import itertools
import re

from ..sources.reader import MAX_PAGE_BYTES, SourceContentUnavailable
from .context import MAX_EXCERPT_BYTES, MAX_SOURCE_EVIDENCE, digest

#: Reader pages scanned per file (8 x 20,000 bytes); a longer file contributes its prefix.
SCAN_PAGES = 8
_FENCE = re.compile(rb" {0,3}(`{3,}|~{3,})(.*)")
_ATX = re.compile(rb" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*")
#: Exact heading titles whose span forms the notes and grounding windows.
_TITLES = {"notes": ("key results", "limitations"), "grounding": ("key_results", "open_threads")}
#: Whole words, so a title such as "Learned Preferences" is not a reference list.
_REFERENCES = re.compile(r"\b(?:references|bibliography)\b")
_WORDS = re.compile(r"[^\W_]+")


def _read_file(reader, source_id, kind):
    """Exact bytes of one file version: (raw, content_sha256, complete) or None.

    Pages come from cursors bound to one file digest. A changed version, revoked
    authorization or missing file omits this kind; invalid queries propagate.
    """
    raw, sha, cursor = b"", None, None
    try:
        for _ in range(SCAN_PAGES):
            page = reader.read(source_id, kind=kind, cursor=cursor, limit=MAX_PAGE_BYTES)
            sha = page["content_sha256"] if sha is None else sha
            if page["content_sha256"] != sha or (
                    page["text"] and page["start_line"] != raw.count(b"\n") + 1):
                return None
            raw += page["text"].encode("utf-8")
            cursor = page["next_cursor"]
            if cursor is None:
                # Only unprojected page text reproduces the reader's file digest.
                return (raw, sha, True) if hashlib.sha256(raw).hexdigest() == sha else None
    except SourceContentUnavailable:
        return None
    return raw, sha, False


def _headings(raw):
    """ATX headings as (level, casefolded title, line byte offset).

    Lines inside leading YAML front matter or a fenced block are not headings.
    A fence closes only on its own character with at least its opening length;
    front matter without a closing line yields no headings.
    """
    heads, position, fence, front = [], 0, None, False
    while position < len(raw):
        offset, stop = position, raw.find(b"\n", position)
        position = len(raw) if stop < 0 else stop + 1
        line = raw[offset:position].rstrip(b"\r\n")
        if offset == 0 and line.rstrip() == b"---":
            front = True
            continue
        if front:
            front = line.rstrip() not in (b"---", b"...")
            continue
        marker = _FENCE.fullmatch(line)
        if fence is not None:
            if (marker and marker[1][:1] == fence[:1] and len(marker[1]) >= len(fence)
                    and not marker[2].strip()):
                fence = None
            continue
        if marker and not (marker[1][:1] == b"`" and b"`" in marker[2]):
            fence = marker[1]
            continue
        heading = _ATX.fullmatch(line)
        if heading:
            title = (heading[2] or b"").decode("utf-8").strip().casefold()
            heads.append((len(heading[1]), title, offset))
    return heads


def _bounded_end(raw, start, end):
    """Cap a window at MAX_EXCERPT_BYTES on a line boundary, else a UTF-8 boundary.

    A line cut must keep text after the window's first line; a heading followed
    by one oversized paragraph line is cut inside that line instead.
    """
    if end - start <= MAX_EXCERPT_BYTES:
        return end
    end = start + MAX_EXCERPT_BYTES
    cut = raw.rfind(b"\n", start, end) + 1
    if cut and raw[start:cut].partition(b"\n")[2].strip():
        return cut
    while (raw[end] & 0xC0) == 0x80:
        end -= 1
    return end


def _section_end(raw, heads, j):
    """The offset of the first heading after j at its level or higher, else the file end."""
    return next((offset for depth, _, offset in heads[j + 1:] if depth <= heads[j][0]), len(raw))


def _section_window(raw, heads, i, j, extend):
    """From heading i through heading j's section, deeper subsections included.

    With extend, whole preceding sections at heading i's level (with their
    subsections) are added while the window stays within the byte cap.
    """
    end = _section_end(raw, heads, j)
    start, index = heads[i][2], i
    while extend:
        index = next((k for k in range(index - 1, -1, -1) if heads[k][0] <= heads[i][0]), None)
        if index is None or heads[index][0] != heads[i][0] or end - heads[index][2] > MAX_EXCERPT_BYTES:
            break
        start = heads[index][2]
    return start, _bounded_end(raw, start, end)


def _prefix_window(raw):
    return 0, _bounded_end(raw, 0, len(raw))


def _entry(kind, raw, sha, start, end):
    text = raw[start:end].decode("utf-8")
    if not text.strip():
        return None
    first = raw[:start].count(b"\n") + 1
    last = first + text.count("\n") - int(text.endswith("\n"))
    return {"kind": kind, "text": text, "retained_sha256": digest(text), "content_sha256": sha,
            "locator": f"{kind}:lines:{first}-{last}:offset:{start}"}


def _named(raw, heads, titles):
    """The span from the first of two named headings through the later one."""
    found = sorted(next((n for n, head in enumerate(heads) if head[1] == title), -1) for title in titles)
    found = [n for n in found if n >= 0]
    return _section_window(raw, heads, found[0], found[-1], extend=True) if found else None


def _converted_title(raw, heads):
    """Whether the second heading is the converted page's title right after the first.

    Paper ingestion writes the metadata title, which keeps any line break of
    the export API's title, then the converted page. The converted title is
    the next heading, at most at the title's level, after only those wrapped
    title lines and blank lines. Its text can differ (a footnote mark, rendered
    math), but it repeats more than half of the metadata title's words, which
    a first section heading such as "6 Limitations" does not. Small capitals
    can split a capital from the rest of its word ("t ask- a ware g ating"), so
    a heading that contains all of the title's letters in order also repeats it.
    """
    if len(heads) < 2 or heads[1][0] > heads[0][0]:
        return False
    title_end = raw.find(b"\n", heads[0][2]) + 1
    lines = raw[title_end:heads[1][2]].decode("utf-8").split("\n")
    wrapped = list(itertools.takewhile(str.strip, lines))
    if any(line.strip() for line in lines[len(wrapped):]):
        return False
    title = " ".join([heads[0][1], *wrapped]).casefold()
    words = set(_WORDS.findall(title))
    if 2 * len(words & set(_WORDS.findall(heads[1][1]))) > len(words):
        return True
    letters = "".join(_WORDS.findall(title))
    return bool(letters) and letters in "".join(_WORDS.findall(heads[1][1].casefold()))


def _author_section(raw, heads):
    """The paper's first Limitations section before References, else its Conclusion.

    The first heading is taken as the document title (paper ingestion writes
    one). The converted page repeats it at the same level, possibly with
    footnote or math text that makes the two differ. A title's span can be the
    whole paper, so the title, its exact repeats and a converted title right
    after it are neither candidate sections nor reference boundaries. A
    section heading right after the title that does not repeat most of its
    words stays a section.
    """
    first = 2 if _converted_title(raw, heads) else 1
    sections = [n for n in range(first, len(heads)) if heads[n][:2] != heads[0][:2]]
    stop = next((k for k, n in enumerate(sections) if _REFERENCES.search(heads[n][1])), len(sections))
    for word in ("limitation", "conclusion"):
        for n in sections[:stop]:
            # Judge the body on the whole section, not on its clipped window.
            if word in heads[n][1] and raw[heads[n][2]:_section_end(raw, heads, n)].partition(b"\n")[2].strip():
                return _section_window(raw, heads, n, n, extend=False)
    return None


def _window(kind, raw, complete):
    """The kind's section window; the prefix when the scan was incomplete or no section applies."""
    if complete:
        heads = _headings(raw)
        span = _author_section(raw, heads) if kind == "full_text" else _named(raw, heads, _TITLES[kind])
        if span is not None:
            return span
    return _prefix_window(raw)


def _file_entry(reader, source_id, kind):
    scan = _read_file(reader, source_id, kind)
    if scan is None:
        return None
    raw, sha, complete = scan
    return _entry(kind, raw, sha, *_window(kind, raw, complete))


def _passage(hit):
    return {"kind": "indexed_passage", "text": hit["excerpt"], "retained_sha256": digest(hit["excerpt"]),
            "content_sha256": hit["content_sha256"], "locator": hit["evidence_id"]}


def paper_evidence(reader, source, passages):
    """At most MAX_SOURCE_EVIDENCE entries for one source, highest priority first.

    Slots: first passage, notes, full_text, second passage, grounding, further
    passages. Files are read only while a slot remains. A candidate whose text is
    already retained verbatim adds nothing and yields its slot.
    """
    valid, seen = [], set()
    for hit in passages:
        if hit["section"] != "__title__" and hit["excerpt"].strip() and hit["evidence_id"] not in seen:
            seen.add(hit["evidence_id"])
            valid.append(hit)
    slots = ([("indexed_passage", hit) for hit in valid[:1]] + [("notes", None), ("full_text", None)]
             + [("indexed_passage", hit) for hit in valid[1:2]] + [("grounding", None)]
             + [("indexed_passage", hit) for hit in valid[2:]])
    evidence = []
    for kind, hit in slots:
        if len(evidence) >= MAX_SOURCE_EVIDENCE:
            break
        entry = _passage(hit) if hit is not None else _file_entry(reader, source["id"], kind)
        if entry is not None and not any(entry["text"] in kept["text"] for kept in evidence):
            evidence.append(entry)
    return evidence
