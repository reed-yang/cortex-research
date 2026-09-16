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
`keychain://` references, independently of the model provider used by Telegram.
Add the OpenRouter embedding reference through the normal secret configuration,
reload the daemon through the installed lifecycle, and re-index the existing
files without overwriting notes. Keep credential values out of argv, logs,
configuration files and database rows.
