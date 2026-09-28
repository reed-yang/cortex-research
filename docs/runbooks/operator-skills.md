# Operator skills

The bundle ships no OCR. An arXiv paper without a LaTeXML HTML version can be
ingested only through an OCR skill the operator installs, prepares and accepts.
A skill is an [Agent Skills](https://agentskills.io/specification) directory; the
same `SKILL.md` keeps working in Claude Code and Codex.

The engine reaches a skill only through a named capability slot. `ocr` is the
only slot with a consumer: strict arXiv ingestion when a paper has no HTML.
Model-side skills are a separate question and are not part of this contract.

## Declare the capability

The skill's `SKILL.md` frontmatter names the capability in the `metadata` map,
which the Agent Skills specification reserves for client-specific string values:

```yaml
---
name: paper-ingestion
description: ...
metadata:
  cortex-capability: ocr
  cortex-entry: scripts/ingest_paper.py
  cortex-interpreter: .venv/bin/python
---
```

`cortex-entry` must be a file inside the skill, also after resolving symlinks.
`cortex-interpreter` must be inside the skill directory; for a virtual
environment it is a symlink to a managed CPython, which is expected. Values are
single-line scalars. The product reads this one block with a line parser, not a
YAML library.

An `ocr` skill honours the paper-ingestion command line:

```sh
<interpreter> -B <entry> <pdf path or URL> --engine <name> --output-dir <dir> --image-format png
```

and prints one JSON object whose `status` is `success`, with `markdown_path` and
`paper_dir`. Figures live in `<paper_dir>/assets/`.

## Enable OCR

1. Put the skill under one directory, for example a checkout of your skills
   repository, and prepare its environment. For a uv project:

   ```sh
   cd /path/to/skills/paper-ingestion
   uv sync --frozen
   ```

2. Name that directory in `config.toml` and restart Cortex (configuration is read
   once at start):

   ```toml
   [skills]
   root = "/path/to/skills"
   ```

3. Configure at least one OCR engine credential: `novita` for `deepseek-ocr`,
   or `glm-app-id` plus `glm` for `glm-ocr`. See
   [runtime credentials](runtime-credentials.md).
4. Review the skill, then accept it:

   ```sh
   cortex skills status
   cortex skills accept
   ```

   `status` and `accept` print JSON with the state, the package, its path and
   digest. Acceptance applies to the next engine effect; no restart is needed.
   `cortex doctor` repeats the state on its `skills:` line.

## What acceptance pins

Acceptance records a sha256 over the skill's files: relative paths, file bytes
and symlink targets, never following a link. It excludes `.git`, `.venv`,
`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules`,
`.agent-sync-backups`, `.env`, `.DS_Store` and `*.pyc`. The record lives at
`<state dir>/skills/accepted.json`.

Every engine effect recomputes the digest. Any change to the package, including
`git pull`, a manual edit or a sync tool replacing a file, makes the capability
`changed`: the engine stops running it until you review and accept again. That
is the failure the digest exists for; on 2026-09-14 a profile sync replaced the
paper-ingestion entry file with an older copy and nothing noticed.

The prepared environment is not pinned. After a change to `pyproject.toml` or
`uv.lock` (which the digest does cover), run `uv sync --frozen` before
accepting; `uv sync --frozen --check` reports whether the environment matches
the lock.

| State | Meaning | Action |
| --- | --- | --- |
| `ready` | Accepted and unchanged | None |
| `unconfigured` | No `[skills] root` | Configure it and restart |
| `missing` | No skill under the root declares the capability | Add or fix the declaration |
| `ambiguous` | Several skills declare it | Keep exactly one |
| `invalid` | Entry or interpreter path is unusable | Fix `SKILL.md` |
| `unprepared` | The interpreter is not an executable file | Prepare the environment |
| `not_accepted` | Never accepted, or the record is unreadable | `cortex skills accept` |
| `changed` | Differs from what was accepted | Review, then `cortex skills accept` |

## How the engine runs it

Before each effect the supervisor resolves the capability. Only `ready` binds
`CORTEX_PAPER_INGEST_SKILL` and `CORTEX_PAPER_INGEST_PYTHON` into the child's
environment. uv never runs inside an effect: `uv run` without a prepared
environment downloads a CPython and creates `.venv` inside the skill.

The skill runs with the skill directory as its working directory and the
child's bound environment: `HOME` and `TMPDIR` inside product state, the minimal
system `PATH`, and the OCR engine credentials. The embedding credential is
removed. When readings publication is configured, the child's write-denying
sandbox covers the skill too. The engine tries `deepseek-ocr` then `glm-ocr`,
skipping an engine without credentials, within a 420-second budget that fits
inside the 600-second effect timeout.

Limits: verification and execution are separate steps, so a change in between
is caught on the next effect rather than this one. The skill is operator code
running with the OCR credentials. The engine child's stderr is not persisted;
a refusal keeps only its category and message.

## Failures

A capture fails as `capability_unavailable` when the paper has no HTML version
and OCR cannot run here: the capability is not ready, or no OCR engine has
credentials. The paper is fine. The inbox says so; `cortex skills status` names
the reason. Capture it again once OCR is ready.

`materialization_failed` with "OCR produced no usable body" means OCR ran and
every engine failed or returned too little text.

## Disable

Remove `[skills]` and restart, or delete `<state dir>/skills/accepted.json`; the
next effect then reads the capability as `not_accepted`. Product 0.1.26 and
earlier ignore an unknown `[skills]` section.

A skill that declares a capability without a product consumer is listed under
`ignored` by `cortex skills status`. Adding a capability means adding its
consumer to the product.
