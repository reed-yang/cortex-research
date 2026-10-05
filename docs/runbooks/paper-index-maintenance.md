# Paper index maintenance

The internal research corpus, adopted Control sources and external readings
search cache have separate owners. An index update does not itself adopt a
source, complete a historical Capture, or rebuild an external MCP cache.

## Audit and refresh the internal corpus

Back up research.db and control.db using SQLite backup before maintenance.
Audit with explicit paths from a checkout-owned environment:

```sh
.venv/bin/python -m tools.maintain_paper_index \
  --database /absolute/path/to/research.db \
  --corpus /absolute/path/to/corpus
```

The default only reads the database and corpus. `--apply` refreshes pending
entries; repeat `--paper-dir` to restrict the selection. Configure the normal
embedding credential first. Maintenance refuses `CORTEX_SKIP_EMBED=1` so it
cannot replace real vectors with placeholders. Missing directories and radar
stubs are reported and retained, never interpreted as deletion instructions.

The indexer compares catalog metadata and ordered chunk text, reuses valid
unchanged vectors, validates response counts/dimensions/finite values, and
finishes network calls before acquiring its write transaction. It preserves
omitted source, arXiv identity, publication date and extraction provenance.
Concurrent paper changes or index updates refuse publication for that paper.

For newly copied papers, verify content and authoritative identity, then use
`read_corpus_subset` and `ControlStore.commit_adoption_manifest`. Checkpoint the
research database before the immutable adoption reader. Never rewrite failed
Capture rows to imply that a later maintenance operation succeeded originally.

## Asset-reference audit

Audit Markdown asset references with an explicit real corpus root:

```sh
.venv/bin/python -B -m tools.audit_asset_refs \
  --corpus /absolute/path/to/corpus
```

Repeat `--paper-dir <name>` to select immediate paper directories, spelled
exactly as the directory entry (a case-insensitive file system would otherwise
resolve a variant). A corpus path with a symlink component, such as a readings
`papers` alias, and an unknown, nested, misspelled or symlinked selection are
refused with exit status 2 and no report. The command reads every top-level
`*.md` file of each selected paper through the readings no-follow helpers and
prints one JSON report (`format_version` 1). It writes nothing and opens no
database or network connection. Exit status 1 means some input could not be
read (symlink, hardlink, non-regular file, oversized or invalid UTF-8); those
inputs are listed under `unreadable_inputs` and the rest of the report is still
printed.

`by_file_type` groups results by exact basename, so `full_text.md`,
`notes.md` and any other name never share one denominator. Each group counts
files, unreadable files, files with findings, reference occurrences and unique
targets per class, and repairable destinations. Every finding carries the
Markdown path, line, column and the destination's byte span; every scanned file
carries its SHA-256. Classes:

- `present` / `missing`: image destinations and destinations with an `assets`
  path segment, resolved inside the paper. A regular non-symlink file counts as
  present; images are not decoded. A literal file name that keeps an ingest
  `#...` or `?...` suffix counts as present when that file exists.
- `prefix_candidate`: `papers/<dir>/assets/...` destinations.
- `unsafe`: traversal, absolute paths, `file:` URLs, backslashes, NUL, encoded
  separators, and symlink or non-regular targets. `unknown`: inspection failed.
- `placeholder`: `page=N,bbox=[...]` destinations; they are not missing files.
- `external`: URLs with a scheme such as `https:` or `data:`; never fetched.
  `non_asset`: anchors and other local links, which are not resolved.
- `unsupported`: `srcset`, CSS `url()`, other HTML asset attributes, HTML
  tags that never complete, destinations the scanner cannot parse (including a
  `](...)` destination left without an opening bracket, as when a figure
  caption contains an interval such as `(0, 1]`; HTML tags and CSS `url()`
  in that text keep their own classes), other `page=`/`bbox=` forms,
  and a block comment without `-->` that hides asset-looking text up to the end
  of the file.

The scanner supports inline links and images (angle destinations, titles,
escaped parentheses and up to 32 levels of balanced parentheses, as in cmark),
reference definitions that are used, shortcut references after a failed inline
destination, HTML `img src` and `a href`, and excludes fenced, indented and
inline code and HTML comments. It follows block structure: code spans, inline
comments and brackets never cross a heading, fence, list item, block quote,
HTML block or blank line; an HTML tag keeps backticks and `<!--` in its
attributes; a definition-shaped line inside a paragraph or a footnote is
scanned as text; and a leading byte order mark is skipped while byte offsets
still count it. It does not detect autolinks or bare URLs. These
approximations over-report rather than hide: a destination that fails to parse
is judged from where a destination may start, after spaces and one line ending,
to the first `)` or the line end, or to its own end when balanced parentheses
or `<...>` carry it further; Markdown inside raw HTML blocks
and after a closed block comment is scanned as live text; code, comments and
HTML blocks inside block quotes are not recognized; and a definition-shaped
line inside a paragraph still resolves uses that no other definition matches.
List nesting comes from indentation, so lazy continuation lines across nested
lists, block quotes and HTML blocks can still be misread. It is not a
CommonMark parser and does not measure full-text or scientific completeness.

A prefix candidate is `repairable` only in `full_text.md`, when the directory
name is the current paper, the original target is absent, `assets/<suffix>` is
a regular non-symlink file in the same paper (both tests accept a literal
`#...` or `?...` file name, as for other references), and deleting the literal
`papers/<dir>/` prefix is the only byte change. Other basenames report
`protected_file_type`. `repairable` is report data only: this command never
edits files, and staged repair, repair manifests and ingest asset-quality
receipts are not implemented. Running it against the real corpus, keeping its
report outside the repository, backups and any later repair each need separate
operator approval.

## External readings

Publication has a separate journal. Existing notes are always preserved;
existing files gain no overwrite ownership merely because they match. An
existing non-arXiv paper can establish identity through the exact primary
content digest recorded by Control. A title or directory name is insufficient.

The external paper-search repository owns its cache. Run its existing
`scripts.build_index --incremental` command after publication. Its content-aware
builder reconciles changes and publishes complete hashed generations atomically;
MCP reloads a newly published generation on the next call. A process running the
old MCP implementation must restart once after the code upgrade.

## Credential failures

`OPENROUTER_API_KEY not set` after files were downloaded means materialization
stopped during embedding. The supervised engine resolves only configured
`keychain://` or `age://` references, independently of the model provider used by Telegram.
Add the OpenRouter embedding reference through the normal secret configuration,
reload the daemon through the installed lifecycle, and re-index the existing
files without overwriting notes. Keep credential values out of argv, logs,
configuration files and database rows.

Runtime credentials can also use explicit age references; see [runtime credentials](runtime-credentials.md).
