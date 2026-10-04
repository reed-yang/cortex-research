# Retrieval evaluation

`tools/evaluate_retrieval.py` measures adopted-library retrieval and the fresh
research packet on a frozen, checkpointed snapshot. It is a checkout-only
development tool: it is not installed, has no API or Telegram entry point and
is not evidence of answer quality, scientific correctness, deployment or
delivery.

It calls the product code directly: `SourceKnowledgeReader.search` at limits 6
and 20, and the real `ResearchService.prepare` on a fresh, unselected,
library-only `/research` turn. A tool-local reader replaces the live Control
read with the snapshot, and an in-memory store adapter retains the packet. No
run, thread, receipt or context is persisted, and no model, embedding,
network, index refresh or maintenance path is used. Selected-item dossiers and
follow-up turns are not simulated.

## Snapshot preparation (operator action)

Preparing a snapshot is a separate operator action. The tool never discovers,
copies, checkpoints or repairs anything. Use this layout:

```text
<snapshot>/control.db
<snapshot>/research/research.db
<snapshot>/research/corpus/<paper_dir>/{notes.md,full_text.md,grounding.md}
```

- Copy both databases with SQLite's online backup (for example the `sqlite3`
  shell's `.backup` command), so each copy is one checkpointed file. A
  non-empty `-wal` or `-journal` beside either copy is refused, not
  checkpointed. An empty or leftover `-shm` is accepted.
- Copy the corpus as regular files, without preserving hard links (for
  example `cp -R`, or `rsync -a` without `-H`). Symlinks, hard links, missing
  directories, oversized or invalid UTF-8 files are reported as unavailable;
  the tool never follows or repairs them.
- Take all three from one quiescent moment. The tool cannot prove that the
  databases and the corpus were copied coherently.

The corpus argument is an offline relocation of the registered
`research-corpus` root. Root enabled state, byte limit, revision, adoption
entries and engine references still come from the Control snapshot, but the
registered `private_path` is never read. A report therefore describes an
offline relocated snapshot, not current production authorization or index
freshness.

## Running

```sh
.venv/bin/python -B -m tools.evaluate_retrieval \
  --control <snapshot>/control.db \
  --corpus <snapshot>/research/corpus \
  --index <snapshot>/research/research.db \
  --queries <private>/suite.json > <private>/report.json
```

All four paths are required, must be absolute and must not contain `..`.
`--index` must equal the corpus parent's `research.db`, because the product
search reads exactly that file; any other placement is rejected.

Exit status: 0 when the evaluation completed and is a usable baseline
(including queries with no hits); 1 for storage, contract, per-query or drift
failures, or when a baseline blocker is present; 2 for argument or
malformed-suite errors. The tool hashes both databases before and after the
run, so expect two full reads of each file.

## Query suite

The suite is private operator data. Keep it, and every report, outside the
repository. The format is versioned independently of the product:

```json
{
  "schema_version": 1,
  "relevance_sets": {
    "set-a": ["arxiv:2601.00001", "arxiv:2601.00002", "sha256:<64 hex digits>"]
  },
  "queries": [
    {"id": "q-en-1", "query": "synthetic example query", "language": "en",
     "relevance_set": "set-a", "must_find": ["arxiv:2601.00001"]},
    {"id": "q-zh-1", "query": "合成示例查询", "language": "zh", "relevance_set": "set-a"}
  ]
}
```

- Fields are closed; unknown fields and duplicate JSON keys are rejected.
- Identities use the canonical `arxiv:`, `doi:` or `sha256:` forms. arXiv
  versions are dropped and DOI/SHA-256 values are lowercased, matching Control.
  A `sha256:` identity is the adopted primary-file digest and changes if that
  paper is re-adopted with different content.
- Each relevance set is non-empty and duplicate-free. `must_find` must be a
  subset of the query's relevance set. `language` is `en`, `zh` or `mixed`.
- Bounds: 1 MiB file, 100 queries, 100 relevance sets, 10000 identities per
  set, 1024 UTF-8 bytes per query. Query text is passed to the product
  unchanged, so product validation remains visible.

## Report

The report is JSON on stdout with `report_version: 1`. Evidence text is never
printed; packets are summarized by schema, hash, byte count, source identities
and each evidence item's kind, locator and hashes.

Retrieval metrics, per query and limit `k` in {6, 20}, are measured by two
independent searches (the 6-slot result is not derived from the 20-slot one):

- `returned_results_at_k`: result slots returned by `search(limit=k)`.
- `distinct_papers_at_k`: `|P_k|`, the first-occurrence unique canonical
  identities of those slots.
- `relevant_papers_at_k`: `|P_k ∩ R|`.
- `recall_at_k`: `|P_k ∩ R| / |R|` over the complete declared relevance set.
- `relevant_papers_per_six_slots`: `|P_6 ∩ R| / 6`; slot utilization, not
  recall or precision.
- packet `recall`: `|packet canonical_ids ∩ R| / |R|`.

`k` counts reader result slots before identity deduplication; slots can be
chunks or title hits. These figures are not recall at the first k unique
paper ranks, and that definition must stay fixed across before/after runs.
`R` is never reduced to the papers present in the snapshot. Packet fields also
record `query`, `retrieval_query` and `retrieval_mode` verbatim, and list
packet sources missing from the 6-slot direct search. Each search limit and the
packet list at most 50 `must_find_missing` identities; `must_find_missing_count`
and `must_find_missing_truncated` give the full count.

Summaries give overall and per-language macro means with their `n`. Chinese
counts are distinct returned papers for queries tagged `zh`; `mixed` is
reported separately because its English arm does not show Chinese retrieval.
Named failures (for example `search_at_20:source_content_unavailable` or
`packet:research_query_invalid`) are counted and excluded from means, never
converted to zero. A no-hit query is a measurement with recall 0, and
`research_no_evidence` is an expected packet outcome with no hash and null
coverage.

### Section coverage

Coverage counts retained packet bytes, not the existence of a file or heading.
Evidence counts only when its locator, content hash and bytes match the file
read through the product reader, and the retained span overlaps a non-blank
target body:

- `notes_key_results`, `notes_limitations`: Markdown ATX headings whose title
  equals `Key Results` or `Limitations` after `strip().casefold()`, ignoring
  front matter and fenced blocks. A body runs to the next heading of the same
  or a higher level. Nested heading lines are not body text, so a section that
  holds only sub-headings is a known negative. Empty ATX headings (`##`) and
  setext headings (text underlined with `=` or `-`) end a section.
- `grounding_key_results`, `grounding_key_results_human`,
  `grounding_open_threads`, `grounding_limitations_human`: string values under
  the top-level `key_results`, `human.key_results_human`, `open_threads` and
  `human.limitations` of one JSON object, optionally fenced as `json`, after
  optional front matter. Property names and the trailing Markdown mirror never
  count. JSON escapes are decoded before a value is judged non-blank.
- `indexed_results_section`, `indexed_limitations_section`: the full-text
  section name of the packet's own search hit contains `result` or
  `limitation` and the excerpt keeps a body after the generated
  `Paper: … | Section: …` prefix. The name is read from that prefix, because
  search shortens section metadata to 1000 bytes. Indexed text may lag the
  files, and these flags never stand in for notes or grounding coverage.

`has_key_results_text` and `has_limitations_text` combine the notes and
grounding flags of one source. Each flag is `true`, `false` or `null`
(unknown). Unknown covers unsupported grounding layouts, numbered, decorated
or setext near-miss headings, unclosed front matter or fences, unreadable
files, evidence whose locator or hash does not match, a retained span that
cuts through the JSON escape of a non-blank character without keeping a
complete one, and an indexed excerpt whose prefix cannot be parsed. Aggregates report the confirmed
count, the packet-source denominator, the unknown count, a share only when
nothing is unknown, and the confirmed lower bound. This is section-content
coverage, not a judgment that a passage supports a claim.

### Inputs, preflight and blockers

`inputs.fingerprint` records the suite hash, both database hashes and sidecar
sizes, a manifest digest of every adopted source's directory and supported text
file availability and hashes (images and other files are excluded), and the
hashes of the imported product modules. The evaluation repeats the fingerprint
afterwards; any change sets `inputs.unchanged` to false.

`preflight` counts adopted sources by availability reason (for example
`directory:missing` or `notes:hardlinked`) and lists relevance identities that
are not adopted or are unavailable. `baseline_complete` is false, and the exit
status is 1, when any query failed, a relevance identity is not adopted
(`relevance_identity_not_adopted`) or is adopted but unavailable
(`relevance_identity_unavailable`), or an input changed. Both kinds of
identity stay in the recall denominator; correct the suite or the snapshot
before treating a run as a baseline.

## Comparisons

Run the baseline and every later branch against the same snapshot and suite,
and compare only measurement fields with identical `inputs.fingerprint` apart
from the code hashes. Record the commit used for each run. Keep language
cohorts and labels frozen across a comparison and keep misses visible. A
retrieval-only change is not evidence of better answers or production
acceptance; live answer or delivery comparisons need their own authorization.
