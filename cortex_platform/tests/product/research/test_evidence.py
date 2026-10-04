"""Section-aware paper evidence over real Control, adoption, FTS and corpus files."""

import hashlib

import pytest

from cortex_platform.product.control import ControlStore
from cortex_platform.product.research import evidence as module
from cortex_platform.product.research.context import (
    MAX_SNAPSHOT_BYTES, canonical, digest, validate_snapshot,
)
from cortex_platform.product.research.evidence import (
    _entry, _headings, _prefix_window, _read_file, _section_window, _window, paper_evidence,
)
from cortex_platform.product.research.service import ResearchService, document_excerpts
from cortex_platform.product.sources import reader as reader_module
from cortex_platform.product.sources.reader import SourceKnowledgeReader, SourceQueryInvalid
from cortex_platform.tests.product.research.test_dossier_execution import (
    DOSSIER, IDEA, dossier, started,
)
from cortex_platform.tests.product.research.test_execution import (
    CHINESE, ENGLISH, AnswerBackend, add_chunks, adopt_papers, context_request, execute, next_run,
    queued,
)
from cortex_platform.tests.product.sources.test_adoption_reader import corpus, database
from cortex_platform.tests.product.sources.test_knowledge_reader import knowledge

KINDS = ["indexed_passage", "notes", "full_text", "indexed_passage"]


def notes_text(lead_bytes):
    """Legacy-shaped synthetic notes whose results and limitations follow a long summary."""
    lead = "Lead line of synthetic reading notes.\n" * (lead_bytes // 38 + 1)
    return ("# Synthetic notes\n\n> Source: synthetic publication line\n\n## Summary\n" + lead
            + "## Method\nMethod notes.\n"
            + "## Key Results\n- Result one improves decoding.\n- Result two holds on robots.\n"
            + "## Limitations\n- Evaluated on one synthetic benchmark.\n"
            + "## Keywords\ndecoding\n")


GROUNDING = (
    "---\npaper: synthetic\n# a YAML comment, not a heading\n---\n"
    "# Synthetic grounding\n\n"
    "```json\n"
    '{"human": {"limitations": "JSON limitation line"},\n'
    '## not a heading inside the fence\n'
    '"key_results": ["fenced"]}\n'
    "```\n\n"
    "## mechanism\nMechanism summary.\n"
    "## key_results\n- Grounded result.\n"
    "## evaluation\nEvaluation summary.\n"
    "## open_threads\n- Open thread.\n"
    "## anchors\n- Anchor line.\n"
)

FULL_TEXT = (
    "# Synthetic paper\n\n## 1 Introduction\nIntro text.\n"
    "## 5 Conclusion\nWe conclude.\n"
    "## 6 Limitations\nOnly one benchmark.\n### 6.1 Data\nSmall data.\n"
    "## References\n[1] Ref.\n"
)


def hit(source_id, number, text, section="Method"):
    """A search result shaped like `search._result` for one indexed chunk."""
    sha = digest(text)
    return {"source_id": source_id, "canonical_id": "synthetic", "title": "Synthetic",
            "evidence_id": f"source:{source_id}:chunk:{number}:sha256:{sha}",
            "section": section, "excerpt": text, "content_sha256": sha}


def english(store):
    return next(s for s in store.list_sources() if s["canonical_id"] == "arxiv:2609.00001")


def write_files(root, paper_dir, notes=None, full_text=None, grounding=None):
    for name, text in (("notes.md", notes), ("full_text.md", full_text), ("grounding.md", grounding)):
        path = root / paper_dir / name
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")


def located(entry, root, paper_dir):
    """Reproduce an entry from its file: offset slice and whole-file hash."""
    raw = (root / paper_dir / f"{entry['kind']}.md").read_bytes()
    offset = int(entry["locator"].rsplit(":offset:", 1)[1])
    data = entry["text"].encode("utf-8")
    return raw[offset:offset + len(data)] == data and hashlib.sha256(raw).hexdigest() == entry["content_sha256"]


class Spy:
    """Record (kind, cursor) for every real reader page request."""

    def __init__(self, monkeypatch, change=None):
        self.calls = []
        original = SourceKnowledgeReader.read

        def read(reader, source_id, kind="notes", cursor=None, limit=20000):
            self.calls.append((kind, cursor))
            page = original(reader, source_id, kind=kind, cursor=cursor, limit=limit)
            return page if change is None else change(kind, cursor, page)

        monkeypatch.setattr(SourceKnowledgeReader, "read", read)

    def kinds(self):
        return [kind for kind, _ in self.calls]


# Heading outline.

def test_headings_skip_front_matter_and_fences_with_exact_offsets():
    raw = (
        "---\n# yaml comment\ntitle: x\n---\n"
        "# Título\n"
        "```json\n## inside backticks\n~~~\n## still inside\n``\n````\n"
        "## 结果 ##\n"
        "~~~~ text\n# inside tildes\n~~~\n~~~~~\n"
        "#hashtag\n####### seven\n"
        "### Deep\r\n"
        "##\n"
    ).encode()
    assert _headings(raw) == [
        (1, "título", raw.index("# Título".encode())),
        (2, "结果", raw.index("## 结果".encode())),
        (3, "deep", raw.index(b"### Deep")),
        (2, "", raw.rindex(b"##\n")),
    ]


def test_front_matter_must_close_before_headings_count():
    assert _headings(b"---\n# comment\n## Key Results\n") == []
    assert _headings(b"---\na: 1\n...\n# Real\n") == [(1, "real", 13)]
    assert _headings(b"# Not front matter\n---\n## Next\n") == [(1, "not front matter", 0), (2, "next", 23)]


# Windows.

def test_section_window_ends_at_same_or_higher_level_and_keeps_subsections():
    raw = b"# Paper\n## A\na\n### A.1\nx\n## B\nb\n# Next\n"
    heads = _headings(raw)
    assert [title for _, title, _ in heads] == ["paper", "a", "a.1", "b", "next"]
    for index, expected in ((1, b"## A\na\n### A.1\nx\n"), (2, b"### A.1\nx\n"), (3, b"## B\nb\n")):
        start, end = _section_window(raw, heads, index, index, extend=False)
        assert raw[start:end] == expected
    start, end = _section_window(raw, heads, 4, 4, extend=False)
    assert raw[start:end] == b"# Next\n" and end == len(raw)


def test_backward_extension_adds_whole_same_level_sections_within_the_cap():
    filler = lambda count: ("f" * 99 + "\n") * count  # noqa: E731 - 100 bytes per line
    raw = ("# Title\n## Summary\n" + filler(30) + "## Method\n" + filler(10) + "### Detail\n" + filler(5)
           + "## Key Results\n" + filler(20) + "## Limitations\n" + filler(10) + "## Keywords\nk\n").encode()
    heads = _headings(raw)
    key, limits = (next(n for n, h in enumerate(heads) if h[1] == t) for t in ("key results", "limitations"))
    start, end = _section_window(raw, heads, key, limits, extend=True)
    # Method plus its subsection fits (4,551 bytes); adding Summary would exceed 6,000.
    assert (start, end) == (raw.index(b"## Method"), raw.index(b"## Keywords"))
    start, _ = _section_window(raw, heads, key, limits, extend=False)
    assert start == raw.index(b"## Key Results")


def test_backward_extension_stops_at_a_parent_heading():
    raw = b"## Intro\ni\n# Part\n## Key Results\nr\n## Limitations\nl\n"
    heads = _headings(raw)
    start, end = _section_window(raw, heads, 2, 3, extend=True)
    assert raw[start:end] == b"## Key Results\nr\n## Limitations\nl\n"


def test_oversized_window_ends_on_the_last_line_boundary_within_the_cap():
    raw = ("## Key Results\n" + ("r" * 99 + "\n") * 80 + "## Limitations\nl\n").encode()
    start, end = _section_window(raw, _headings(raw), 0, 1, extend=True)
    # Bounded, not complete: the distant Limitations heading does not fit.
    assert (start, end) == (0, 15 + 59 * 100)


def test_a_long_line_without_newline_is_cut_on_a_utf8_boundary():
    raw = ("a" + "结" * 2500).encode()
    assert _prefix_window(raw) == (0, 5998)
    raw[:5998].decode("utf-8")
    assert _prefix_window(b"short\nfile") == (0, 10)
    lines = ("p" * 99 + "\n").encode() * 70
    assert _prefix_window(lines) == (0, 6000)
    assert _prefix_window(b"x" + lines) == (0, 5901)


@pytest.mark.parametrize("raw, start, end, first, last", [
    (b"a\nb\nc\n", 2, 6, 2, 3),
    (b"a\nb\nc", 2, 5, 2, 3),
    (b"a\r\nb\r\n", 3, 6, 2, 2),
])
def test_entry_locator_reproduces_the_file_slice(raw, start, end, first, last):
    sha = hashlib.sha256(raw).hexdigest()
    text = raw[start:end].decode()
    assert _entry("notes", raw, sha, start, end) == {
        "kind": "notes", "text": text, "retained_sha256": digest(text), "content_sha256": sha,
        "locator": f"notes:lines:{first}-{last}:offset:{start}",
    }


def test_whitespace_only_windows_are_not_entries():
    assert _entry("notes", b"a\n \n\t\n", "0" * 64, 2, 6) is None
    assert _entry("notes", b"", "0" * 64, 0, 0) is None


# Per-kind selection.

def span(kind, text, complete=True):
    raw = text.encode()
    start, end = _window(kind, raw, complete)
    return raw[start:end].decode()


def test_notes_window_carries_results_and_limitations_after_byte_4000():
    text = notes_text(6_500)
    window = span("notes", text)
    assert text.index("## Key Results") > 6_000
    assert window.startswith("## Method\n") and window.endswith("- Evaluated on one synthetic benchmark.\n")
    assert "## Key Results" in window and "## Keywords" not in window


def test_notes_single_heading_repeats_and_fallback():
    assert span("notes", "# N\n## Limitations\nfirst\n## Other\no\n## Limitations\nsecond\n") == (
        "## Limitations\nfirst\n")
    assert span("notes", "# N\n## Limitations\nl\n## Key Results\nr\n## Tail\nt\n") == (
        "## Limitations\nl\n## Key Results\nr\n")
    assert span("notes", "notes for a summary-only paper\n") == "notes for a summary-only paper\n"


def test_grounding_window_spans_mechanism_to_open_threads_outside_the_json_block():
    window = span("grounding", GROUNDING)
    assert window == ("## mechanism\nMechanism summary.\n## key_results\n- Grounded result.\n"
                      "## evaluation\nEvaluation summary.\n## open_threads\n- Open thread.\n")
    assert "JSON limitation line" not in window


def test_grounding_without_markdown_sections_falls_back_to_its_prefix():
    text = GROUNDING.split("## mechanism")[0]
    assert span("grounding", text) == text
    assert "JSON limitation line" in text


@pytest.mark.parametrize("text, expected", [
    (FULL_TEXT, "## 6 Limitations\nOnly one benchmark.\n### 6.1 Data\nSmall data.\n"),
    ("# P\n## 5 Conclusions\nWe conclude.\n## References\nr\n", "## 5 Conclusions\nWe conclude.\n"),
    ("# P\n## Conclusion\nc\n## References\nr\n## Limitations\nlate\n", "## Conclusion\nc\n"),
    ("# P\n## Limitations\n## Limitations of scope\nreal\n## Bibliography\nb\n",
     "## Limitations of scope\nreal\n"),
    ("# P\n## Method\nOnly method text.\n", "# P\n## Method\nOnly method text.\n"),
])
def test_full_text_prefers_the_papers_own_limitations_then_conclusion(text, expected):
    assert span("full_text", text) == expected


def test_incomplete_scans_never_treat_unscanned_bytes_as_absent():
    filler = "f" * 99 + "\n"
    # A Limitations section may follow beyond the scan, so Conclusion is not chosen.
    unscanned = "# P\n## Conclusion\nc\n## Method\n" + filler * 80 + "## Lim"
    assert span("full_text", unscanned, complete=False) == ("# P\n## Conclusion\nc\n## Method\n" + filler * 59)
    # References was scanned, so the candidate list is complete.
    closed = "# P\n## Conclusion\nc\n## References\n" + filler * 80
    assert span("full_text", closed, complete=False) == "## Conclusion\nc\n"
    # A determined Limitations window is used; one running into the boundary is not.
    assert span("full_text", "# P\n## Limitations\nl\n## Next\n" + filler * 80, complete=False) == (
        "## Limitations\nl\n")
    assert span("full_text", "# P\n## Limitations\nl\n", complete=False) == "# P\n## Limitations\nl\n"
    # A heading split at the scan boundary is not parsed; Key Results alone is not enough.
    split = "# N\n## Key Results\nr\n## Other\n" + filler * 70 + "## Limi"
    assert span("notes", split, complete=False).startswith("# N\n## Key Results\nr\n## Other\n")
    assert span("notes", split, complete=True) == "## Key Results\nr\n"


# Paging.

def test_read_file_pages_with_cursors_and_reproduces_the_file(knowledge, monkeypatch):
    store, root, _, reader = knowledge
    text = notes_text(45_000)
    write_files(root, ENGLISH, notes=text)
    spy = Spy(monkeypatch)
    raw, sha, complete = _read_file(reader, english(store)["id"], "notes")
    assert raw == text.encode() and sha == hashlib.sha256(raw).hexdigest() and complete
    assert len(spy.calls) == 3 and spy.calls[0][1] is None and all(c for _, c in spy.calls[1:])


def test_read_file_stops_after_scan_pages(knowledge, monkeypatch):
    store, root, _, reader = knowledge
    text = ("l" * 99 + "\n") * 20
    write_files(root, ENGLISH, notes=text)
    monkeypatch.setattr(module, "MAX_PAGE_BYTES", 100)
    spy = Spy(monkeypatch)
    raw, sha, complete = _read_file(reader, english(store)["id"], "notes")
    assert len(spy.calls) == module.SCAN_PAGES == 8
    assert raw == text.encode()[:800] and sha == hashlib.sha256(text.encode()).hexdigest()
    assert not complete


def test_read_file_is_lossless_across_utf8_and_line_cap_page_breaks(knowledge, monkeypatch):
    store, root, _, reader = knowledge
    text = "ab中文字\n## 标题\n第一行\n二\n"
    write_files(root, ENGLISH, notes=text)
    monkeypatch.setattr(module, "MAX_PAGE_BYTES", 10)
    monkeypatch.setattr(reader_module, "MAX_PAGE_LINES", 1)
    spy = Spy(monkeypatch)
    raw, _, complete = _read_file(reader, english(store)["id"], "notes")
    # Pages: "ab中文" (UTF-8 cut), "字\n" (line cap), then one line per page.
    assert raw == text.encode() and complete and len(spy.calls) == 5


@pytest.mark.parametrize("fault", ["page_hash", "rewrite", "root_revision", "root_disabled", "missing",
                                   "redacted"])
def test_read_file_refuses_inconsistent_or_unavailable_versions(knowledge, monkeypatch, fault):
    store, root, _, reader = knowledge
    path = root / ENGLISH / "notes.md"
    write_files(root, ENGLISH, notes=notes_text(30_000))

    def change(kind, cursor, page):
        if cursor is None:
            asset = store.get_asset_root("research-corpus")
            if fault == "page_hash":
                return page
            if fault == "rewrite":
                path.write_text(notes_text(31_000), encoding="utf-8")
            elif fault in {"root_revision", "root_disabled"}:
                store.update_asset_root(
                    root_id=asset.root_id, private_path=asset.private_path, max_bytes=asset.max_bytes - 1,
                    enabled=fault == "root_revision", expected_revision=asset.revision,
                    actor_id="operator", idempotency_key=f"root-change-{fault}")
            return page
        return dict(page, content_sha256="0" * 64) if fault == "page_hash" else page

    if fault == "missing":
        path.unlink()
    elif fault == "redacted":
        path.write_text("# N\nsecret line\n## Key Results\nr\n", encoding="utf-8")
        reader = SourceKnowledgeReader(store, redact_line=lambda line: "secret" in line)
    Spy(monkeypatch, change)
    assert _read_file(reader, english(store)["id"], "notes") is None


def test_read_file_handles_empty_files_and_propagates_invalid_queries(knowledge):
    store, root, _, reader = knowledge
    write_files(root, ENGLISH, notes="")
    assert _read_file(reader, english(store)["id"], "notes") == (b"", hashlib.sha256(b"").hexdigest(), True)
    with pytest.raises(SourceQueryInvalid):
        _read_file(reader, "", "notes")


# Slot policy.

def test_slot_order_fills_four_entries_and_skips_grounding_reads(knowledge, monkeypatch):
    store, root, _, reader = knowledge
    source = english(store)
    write_files(root, ENGLISH, notes=notes_text(6_500), full_text=FULL_TEXT, grounding=GROUNDING)
    spy = Spy(monkeypatch)
    passages = [hit(source["id"], n, f"decoding passage {n}") for n in (1, 2, 3)]
    evidence = paper_evidence(reader, source, passages)
    assert [e["kind"] for e in evidence] == KINDS
    assert [evidence[0]["text"], evidence[3]["text"]] == ["decoding passage 1", "decoding passage 2"]
    assert evidence[0]["locator"] == passages[0]["evidence_id"]
    assert evidence[0]["content_sha256"] == passages[0]["content_sha256"]
    assert "grounding" not in spy.kinds()
    assert all(located(e, root, ENGLISH) for e in evidence[1:3])


@pytest.mark.parametrize("files, count, kinds", [
    ({"notes": True, "full_text": True, "grounding": True}, 1,
     ["indexed_passage", "notes", "full_text", "grounding"]),
    ({"notes": True, "full_text": True, "grounding": False}, 1, ["indexed_passage", "notes", "full_text"]),
    ({"notes": False, "full_text": True, "grounding": True}, 3,
     ["indexed_passage", "full_text", "indexed_passage", "grounding"]),
    ({"notes": False, "full_text": True, "grounding": False}, 3,
     ["indexed_passage", "full_text", "indexed_passage", "indexed_passage"]),
    ({"notes": True, "full_text": True, "grounding": True}, 0, ["notes", "full_text", "grounding"]),
    ({"notes": False, "full_text": False, "grounding": False}, 0, []),
])
def test_unavailable_files_pass_their_slot_to_the_next_candidate(knowledge, files, count, kinds):
    store, root, _, reader = knowledge
    source = english(store)
    texts = {"notes": notes_text(6_500), "full_text": FULL_TEXT, "grounding": GROUNDING}
    write_files(root, ENGLISH, **{kind: texts[kind] if present else None for kind, present in files.items()})
    passages = [hit(source["id"], n, f"decoding passage {n}") for n in range(1, count + 1)]
    assert [e["kind"] for e in paper_evidence(reader, source, passages)] == kinds


def test_first_passage_is_the_first_valid_distinct_hit(knowledge):
    store, root, _, reader = knowledge
    source = english(store)
    write_files(root, ENGLISH, notes=None, full_text=None, grounding=None)
    first, second = hit(source["id"], 1, "decoding first"), hit(source["id"], 2, "decoding second")
    passages = [hit(source["id"], 0, "Speculative Decoding", section="__title__"),
                hit(source["id"], 9, "  \n"), first, dict(first), second]
    assert [e["text"] for e in paper_evidence(reader, source, passages)] == ["decoding first", "decoding second"]


def test_equal_section_names_never_discard_evidence(knowledge):
    store, root, _, reader = knowledge
    source = english(store)
    head = "Only one benchmark."
    full_text = FULL_TEXT.replace(head, head + " The tail caveat: results may not transfer.")
    write_files(root, ENGLISH, notes=notes_text(6_500), full_text=full_text, grounding=None)
    passages = [hit(source["id"], 1, head, section="6 Limitations"),
                hit(source["id"], 2, "decoding limitation chunk", section="6 Limitations")]
    evidence = paper_evidence(reader, source, passages)
    assert [e["kind"] for e in evidence] == KINDS
    assert evidence[2]["text"].startswith("## 6 Limitations") and "tail caveat" in evidence[2]["text"]
    assert evidence[3]["text"] == "decoding limitation chunk"


def test_a_passage_already_retained_verbatim_yields_its_slot(knowledge):
    store, root, _, reader = knowledge
    source = english(store)
    write_files(root, ENGLISH, notes=notes_text(6_500), full_text=FULL_TEXT, grounding=GROUNDING)
    passages = [hit(source["id"], 1, "decoding first"),
                hit(source["id"], 2, "- Result one improves decoding.")]
    assert [e["kind"] for e in paper_evidence(reader, source, passages)] == [
        "indexed_passage", "notes", "full_text", "grounding"]


# Research turns.

def test_research_packet_carries_located_results_and_limitations(knowledge):
    store, root, *_ = knowledge
    write_files(root, ENGLISH, notes=notes_text(6_500))
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    snapshot = context["snapshot"]
    assert snapshot["schema_version"] == 1
    source = next(s for s in snapshot["sources"] if s["canonical_id"] == "arxiv:2609.00001")
    notes = next(e for e in source["evidence"] if e["kind"] == "notes")
    assert "## Key Results" in notes["text"] and "## Limitations" in notes["text"]
    assert not notes["locator"].endswith(":offset:0") and located(notes, root, ENGLISH)
    assert validate_snapshot(snapshot, context["sha256"])


def test_distant_results_are_found_across_reader_pages(knowledge, monkeypatch):
    store, root, *_ = knowledge
    text = notes_text(30_000)
    write_files(root, ENGLISH, notes=text)
    spy = Spy(monkeypatch)
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    assert sum(1 for kind, cursor in spy.calls if kind == "notes" and cursor) >= 1
    source = next(s for s in context["snapshot"]["sources"] if s["canonical_id"] == "arxiv:2609.00001")
    notes = next(e for e in source["evidence"] if e["kind"] == "notes")
    assert notes["locator"].endswith(f":offset:{text.encode().index(b'## Method')}")
    assert located(notes, root, ENGLISH)


def test_a_version_change_between_pages_omits_only_that_file(knowledge, monkeypatch):
    store, root, *_ = knowledge
    write_files(root, ENGLISH, notes=notes_text(30_000), full_text=FULL_TEXT)
    Spy(monkeypatch, lambda kind, cursor, page: (
        dict(page, content_sha256="0" * 64) if kind == "notes" and cursor else page))
    run = queued(store)
    result, *_ = execute(store, run)
    assert result["state"] == "completed", store.list_run_events(run["id"])
    snapshot = store.get_research_context(run["id"])["snapshot"]
    source = next(s for s in snapshot["sources"] if s["canonical_id"] == "arxiv:2609.00001")
    assert [e["kind"] for e in source["evidence"]] == ["indexed_passage", "full_text"]


def test_followup_reuses_section_evidence_after_file_and_index_changes(knowledge):
    store, root, database, _ = knowledge
    write_files(root, ENGLISH, notes=notes_text(6_500), full_text=FULL_TEXT)
    run = queued(store)
    result, backend, releases, _ = execute(store, run)
    assert result["state"] == "completed"
    first = store.get_research_context(run["id"])
    write_files(root, ENGLISH, notes="rewritten notes\n", full_text="rewritten full text\n")
    add_chunks(database, ENGLISH, ["decoding decoding replacement passage"])
    followup = next_run(store, run["thread_id"], "Compare the limitations")
    result, *_ = execute(store, followup, backend, releases=releases)
    assert result["state"] == "completed"
    second = store.get_research_context(followup["id"])
    assert canonical(second["snapshot"]["sources"]) == canonical(first["snapshot"]["sources"])
    assert second["snapshot"]["query"] == "Compare the limitations"
    assert second["sha256"] != first["sha256"]


# Budget.

QUOTES = '"' * 99 + "\n"


def write_heavy_paper(root, database, paper_dir, number):
    """Near-6,000-byte section windows whose quotes double under canonical JSON."""
    write_files(
        root, paper_dir,
        notes=("# Synthetic notes\n" + "intro line\n" * 400 + "## Key Results\n" + QUOTES * 28
               + "## Limitations\n" + QUOTES * 28 + "## Keywords\nk\n"),
        full_text=("# Paper\n## 1 Introduction\nintro\n## 5 Limitations\n" + QUOTES * 55
                   + "## 6 Conclusion\nc\n## References\nr\n"),
        grounding=("## mechanism\n" + QUOTES * 20 + "## key_results\n" + QUOTES * 20
                   + "## open_threads\n" + QUOTES * 15 + "## anchors\na\n"),
    )
    heavy = '"' * 1_990
    add_chunks(database, paper_dir, [f"decoding passage p{number}x{index} {heavy}" for index in range(2)])


def heavy_corpus(store, root, database):
    extra = [f"20260907-Synthetic-{index}" for index in range(4)]
    adopt_papers(store, root, database, extra)
    for number, paper_dir in enumerate([ENGLISH, CHINESE, *extra]):
        write_heavy_paper(root, database, paper_dir, number)


def assert_trimmed(store, context):
    snapshot = context["snapshot"]
    assert validate_snapshot(snapshot, context["sha256"])
    assert len(canonical(snapshot).encode("utf-8")) <= MAX_SNAPSHOT_BYTES
    assert store.get_research_context(context["run_id"])["snapshot"] == snapshot
    kinds = [[e["kind"] for e in source["evidence"]] for source in snapshot["sources"]]
    assert len(kinds) == 6
    # Every source offered four entries; the existing loop removed the lowest-ranked tails.
    assert kinds[0] == KINDS and kinds[-1] == ["indexed_passage"]
    assert sum(map(len, kinds)) < 24
    notes = snapshot["sources"][0]["evidence"][1]["text"]
    assert "## Key Results" in notes and "## Limitations" in notes
    return snapshot


def test_six_heavy_sources_are_trimmed_by_the_existing_loop(knowledge):
    store, root, database, reader = knowledge
    heavy_corpus(store, root, database)
    run = queued(store)
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    snapshot = assert_trimmed(store, context)
    last = store.get_source(snapshot["sources"][-1]["source_id"])
    found = reader.search("decoding", limit=6, per_source=2)["results"]
    assert len(paper_evidence(reader, last, [h for h in found if h["source_id"] == last["id"]])) == 4


def test_mixed_dossier_and_heavy_sources_trim_dossier_excerpts_first(dossier, knowledge):
    store, *_ = dossier
    _, root, database, _ = knowledge
    heavy_corpus(store, root, database)
    run = started(store, question="/research decoding")
    context = ResearchService(store).prepare(run, store.list_messages(run["thread_id"]))
    snapshot = assert_trimmed(store, context)
    assert snapshot["schema_version"] == 2 and snapshot["item"]["id"] == IDEA
    assert len(document_excerpts(DOSSIER, "decoding")) == 2
    assert [[e["kind"] for e in d["excerpts"]] for d in snapshot["documents"]] == [["document_prefix"]] * 2


# Prefix-era contract.

def prefix_era_source(source):
    def entry(kind, text, locator):
        return {"kind": kind, "text": text, "retained_sha256": digest(text),
                "content_sha256": digest(text), "locator": locator}

    passage = "speculative decoding improves robot inference"
    return {"label": "S1", "source_id": source["id"], "canonical_id": source["canonical_id"],
            "engine_ref": source["engine_ref"], "evidence": [
                entry("indexed_passage", passage, f"source:{source['id']}:chunk:1:sha256:{digest(passage)}"),
                entry("grounding", "grounding prefix", "grounding:lines:1-1:offset:0"),
                entry("notes", "notes for Speculative Decoding", "notes:lines:1-1:offset:0"),
                entry("full_text", "full text", "full_text:lines:1-1:offset:0")]}


def record_literal(store, run, snapshot):
    message = store.list_messages(run["thread_id"])[-1]
    snapshot["authority"]["message_id"] = message["id"]
    sha = digest(canonical(snapshot))
    assert validate_snapshot(snapshot, sha)
    store.record_research_context(**context_request(store, run, {
        "message_id": message["id"], "query": snapshot["query"], "snapshot": snapshot, "sha256": sha}))
    assert store.get_research_context(run["id"])["snapshot"] == snapshot
    replayed = ResearchService(ControlStore(store.path)).prepare(
        store.get_run(run["id"]), store.list_messages(run["thread_id"]))
    assert replayed["snapshot"] == snapshot and replayed["sha256"] == sha
    assert "model-written summaries" in ResearchService.system_message(replayed)


def literal_packet(source):
    return {"schema_version": 1, "query": "decoding", "retrieval_query": "decoding",
            "retrieval_mode": "fts5_or",
            "authority": {"kind": "user_requested_adopted_library_read_only", "message_id": ""},
            "sources": [prefix_era_source(source)]}


def test_prefix_era_v1_context_loads_and_replays_unchanged(knowledge):
    store, *_ = knowledge
    record_literal(store, queued(store), literal_packet(english(store)))


def test_prefix_era_v2_context_loads_and_replays_unchanged(dossier):
    store, *_ = dossier
    run = started(store, question="/research decoding")
    selection = store.get_research_thread_item(run["thread_id"])
    text = "# Robotic decoding dossier"
    packet = literal_packet(english(store)) | {"schema_version": 2, "item": {
        key: selection[key] for key in ("id", "kind", "origin_id", "title")}
        | {"selection_revision": selection["selection_revision"]}}
    packet["documents"] = [{
        "label": f"D{index}", "document_version_id": entry["id"], "document_id": entry["document_id"],
        "title": entry["title"], "version": entry["version"], "media_type": entry["media_type"],
        "byte_length": entry["byte_length"], "sha256": entry["sha256"],
        "excerpts": [{"kind": "document_prefix", "text": text, "retained_sha256": digest(text),
                      "locator": "lines:1-1:offset:0"}]}
        for index, entry in enumerate(store.list_research_documents(IDEA), 1)]
    record_literal(store, run, packet)
