# Cortex paper chunking: Flex-RSI contribution candidate

Status: public CPU prototype, NOT registered, NOT an official Flex-RSI adapter.
The destination repository returned 404 on 2026-10-12. Its task schema, category
enum, existing tasks, run board and CLI arguments have not been verified.
`task.json` is a transfer record with the fields requested in the contributor
email, not a claim of schema compatibility. No benchmark agent run has started.

## Task and initial state

Improve the real Markdown paper chunker while preserving evidence content,
section attribution and stable identities under a bounded chunk size.

- Source: `reed-yang/cortex-research`.
- Frozen initial commit: `e975c242dc3955ff01593606b56dc6a310afa728`.
- Initial module: `profiles/research/src/cortex_research/chunker.py`.
- Initial Git blob: `6a22455c77621bbf937ee3aafb54bc019b4d8a08`.
- No bugs have been injected and no candidate solution is supplied.
- Only this module is editable. Keep `chunk_paper(path, title, max_tokens)` and
  its output keys. Use Python 3.12 standard library only. Helpers may be added
  inside the same module. No network, external model, corpus download or GPU.
- Evaluators, generators, task metadata, tests, data, dependency files, startup
  hooks and import paths are not editable submission artifacts.

This is an evidence-preprocessing/software-engineering task. It does not measure
research taste, answer correctness, OCR quality or autonomous scientific discovery.
Its value is preserving the information subsequently available to a research agent.
If Flex-RSI admits only long-horizon research optimization, use the Spec-WAM
proposal or expand to retrieval after calibration; do not relabel this as one.

## Public contract v1

1. Input is valid UTF-8 Markdown. A leading YAML block begins on a complete `---`
   line and ends on a complete `---` or `...` line. Delimiters embedded inside a
   metadata value do not end it. Metadata is omitted. An unclosed leading YAML
   block is a `ValueError` in the eventual full suite.
2. Structural headings are Markdown ATX levels 2 through 6 outside fences.
   Leading indentation up to three spaces and optional closing hashes follow
   ATX syntax. An initial level-1 title is document metadata, not body evidence.
   Subsequent level-1 headings are out of scope for v1. Setext headings and HTML
   parsing are out of scope. The first heading is not required to exist.
3. Fences use at least three matching backticks or tildes; only the same marker
   with at least the opening length closes one. An unclosed fence continues to
   EOF. Fence contents, including heading-looking lines, remain verbatim body
   content apart from whitespace normalization, in the surrounding section.
4. Keep all nonempty preamble as `Abstract` when headings exist, or `Full Text`
   when no structural heading exists. Preserve body order and repeated sections.
5. Exclude a subtree only for a heading whose casefolded title is exactly
   `references`, `bibliography`, `acknowledgment`, `acknowledgments`, or `appendix`.
   Numbered forms (e.g. `7 References`) are also excluded; normalize only the
   leading dotted Arabic section number and spaces. A subtree ends at the next
   heading of equal or lower depth. Substring matches such as `Learned Preferences`
   must not exclude a section. Exclusion is a task contract, not a general claim
   that appendices lack research value.
6. Retain mixed prose and equations. Pure-formula filtering follows the frozen
   `_is_mostly_latex` predicate (>70% matched formula characters) applied to each
   structural section body before splitting; do not change that predicate in v1.
7. Each returned body's length is at most `4 * max_tokens` Unicode code points;
   prefix is excluded. This preserves the existing approximate character budget,
   NOT a real tokenizer bound. `max_tokens` must be a positive integer or raise
   `ValueError`. Split long paragraphs as needed; never truncate content.
8. Each chunk retains the exact prefix `Paper: {title} | Section: {section}\n\n`,
   correct parent-directory identity and a unique nonempty chunk ID within that
   paper. IDs and complete outputs are deterministic for repeated inputs.
   `chunk_idx` remains a nonnegative integer; global indexing is done elsewhere
   in Cortex and is not redefined here.
9. Ignoring whitespace only, the ordered `(section, character)` stream of all
   bodies must match the declared retained content exactly. This forbids dropped,
   duplicated, fabricated, reordered or misattributed evidence while permitting
   different valid chunk boundaries. Whitespace/code indentation fidelity is
   not measured by v1; do not claim executable-code preservation.
10. Limit total chunks to `sum_sections(2 * ceil(body_characters / (4 * max_tokens)) + 1)`
    over nonempty retained section occurrences. This allows flexible boundaries
    while rejecting one-character fragmentation. Body lengths include whitespace.
11. Inputs are read-only. For final evaluation, 1-64 KiB/document, 1-32 sections,
    `max_tokens` in [16, 2048]. No filename, title or fixture-ID dependence.

## Public development commands

Run from the repository root; no package install is necessary:

```sh
python3 benchmarks/flex_rsi/chunking/prepare.py --output /tmp/flex-chunk-public
python3 benchmarks/flex_rsi/chunking/evaluate.py \
  --candidate profiles/research/src/cortex_research/chunker.py \
  --cases /tmp/flex-chunk-public/dev.jsonl \
  --output /tmp/flex-chunk-public/dev-report.json
python3 -m unittest discover -s benchmarks/flex_rsi/chunking -p 'test_*.py' -v
```

`prepare.py` materializes 48 train and 24 dev documents from 12 public families.
All are authored synthetic examples, not personal research data or copied papers.
`baseline.dev.json` records the actual frozen module's public development score.
It is a calibration measurement, not an official result or an agent improvement.
The scorer imports candidate code in-process for developer convenience. It is
explicitly NOT safe to use for official hidden grading of untrusted submissions.

## Score, splits and budget

Primary score: `100 * mean_family(mean_document(all_required_checks_pass)))`.
One document gets 1 only when all its content, attribution, budget, identity,
determinism and input-preservation checks pass. Property failures are diagnostics,
not separately optimizable rewards. Errors and timeouts stay in the denominator.
Also report absolute score, score delta from A0 and all family scores. Do not
use percentage gain over a small baseline as the only metric.

- Train: 48 labeled public examples; unlimited local inspection within run budget.
- Dev: 24 labeled public examples; at most 20 official feedback calls, if the
  project supports this distinction. These public sets establish a smoke floor.
- Hidden, PROPOSED NOT CREATED: 240 documents, 20 per family, independently
  authored compositions and templates. Include deeper nesting, duplicate section
  names, CRLF, closing hashes, mixed scripts and randomly assigned filenames.
  Also run a separate all-or-nothing regression set for documented edge contracts
  not yet covered in the public prototype (initial h1, empty input, no headings,
  pure formulas, invalid budgets, unclosed YAML). All hidden rules are public;
  hidden content is not. Assign template families before instances and keep every
  source/template variant together. New seeds alone are insufficient isolation.
- The entire present Git history is public/development exposure. Nothing committed
  here, including future versions of these examples, can count as pristine hidden.
- Suggested final machine: Linux x86_64, CPython 3.12, one assigned CPU core,
  2 GiB RAM, no GPU/network. Pin the eventual OCI image digest. CPU speed, kernel,
  interpreter build and image remain unverified deployment fields.
- Per model/run proposed cap: 2 wall hours, 1 CPU core, 100,000 generated tokens
  (reasoning included when billable), USD 15 billed spend, 20 dev feedback calls,
  100 MiB submission artifacts. Stop at the first cap. These are authorization
  proposals, not measured resource needs or approved provider spending.
- Final evaluator: process timeout 2 s/document (including import), memory 512 MiB,
  output 2 MiB/document, 10 minutes total; whole-run duplicate/determinism replay.
  Calibrate overhead on the selected machine before freezing these limits.

## Hidden evaluator boundary and reward-hacking review

The maintainer builds the hidden set and gold labels outside the agent workspace
and outside all source Git history. Commit hashes of the sealed package before
candidate runs, without publishing labels, cases or generator seeds. The agent
gets only train/dev and frozen task instructions. Final score is released only
after its candidate artifact has been selected and hashed.

Do NOT simply hide a JSON filename or chmod a file in the same privileged process.
The official controller must run in a separate trust boundary and send only raw
input/title/budget to the candidate sandbox. No labels, evaluator source, paths,
test-family names or final score files are mounted there. Accept a bounded JSON
output schema, count crashes as failures, and recompute the score externally.
Use a fresh workspace without `.git`, credentials, caches or neighboring jobs.
Validate an allowlisted diff; rebuild from the pinned base and only that diff.
Enforce read-only input, denied network and measured limits outside Python.

Specific attacks and controls:

| Shortcut | Control |
|---|---|
| Return nothing / drop long paragraphs / drop Chinese | Whole-document content equality, fixed denominator, stratified families |
| Return entire document as one chunk | Per-body bound and correct section attribution |
| One-character chunks | Fixed chunk-count cap from contract 10, plus artifact and output caps |
| Hardcode example titles, IDs or headings | New templates/content and random paths, with public rules unchanged |
| Modify scorer, use stack inspection, read labels | External isolated controller; only output JSON crosses boundary |
| Weaken tests or alter dependencies/startup | One-file diff allowlist and frozen image |
| Leak through git, shared caches, final feedback | No history/mounts, per-run cache namespace, one sealed final evaluation |
| Optimize only simple public formatting | Compositional hidden templates and held-out real-style documents reviewed before freeze |

Five evaluator unittest methods check valid alternative segmentation and reject
empty, missing, duplicated, oversized, injected, mislabeled and misidentified
outputs. These are scoring sanity checks, NOT a security audit of the sandbox.

## Release gates and Flex-RSI handoff

1. Restore authorized access to the project repository; read README, registry,
   catalog, run board, contribution examples and `rsib --help` from one commit.
2. Search for this source/maintainer/task to avoid duplicate registration. Map
   the proposed category/state to actual enums and reconcile split conventions.
3. Complete independent hidden fixtures, edge regressions, one honest stronger
   maintainer solution and adversarial checks. Verify the task is not instantly
   saturated by all selected agents. Freeze task/data/image/evaluator versions.
4. Register before experiments and claim the actual run-board entry. Resolve
   exact API model IDs, reasoning settings and provider costs/access; use the
   same agent harness/tool policy and separate branches per model and seed.
5. Implement the real task adapter. The email establishes command families
   `rsib task prepare`, `rsib run start`, `rsib report`, `rsib push`; no flags or
   return schemas are inferred here. Do not replace these with a lookalike CLI.
6. Pilot 2 agent models x 1 run, then 3 models x 3 seeds only if the task discriminates.
   Freeze A0 and A_final, independent runs, all failed attempts and cost receipts.
   Runs use `runs/cortex-paper-chunking-v1/<model-slug>/<seed>` and never merge into
   main. This `contrib/` branch is task infrastructure, not an experiment run.

Project code currently has no declared license. Do not silently assign one or
repackage it into a public third-party benchmark until contribution terms are
settled with the owner. This branch stays in the original source repository.
