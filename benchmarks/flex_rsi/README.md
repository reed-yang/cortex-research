# Flex-RSI contribution preparation, 2026-10-12

This is an original, dependency-free preparation package. It does not copy
Cortex, Spec-WAM, model weights, research papers or robot data. It is not an
official rsib adapter and is not yet registered with Flex-RSI.

**Selected task:** `cortex-evidence-chunking-v1` (software-engineering repair).
**Research prototype only:** `specwam-field-integration-v0` (not selected;
public pilot saturates with elementary RK4 and needs harder families).

## Run public preflight

Requires Python 3.12 and only the standard library. From this directory:

```sh
python evaluator/dev_eval.py --task chunking
python evaluator/dev_eval.py --task integration
python -m unittest discover -s evaluator -p test_dev_eval.py
```

Only `tasks/chunking/candidate.py` is editable for the selected task. The
contract is one stdin JSON object `{document, max_bytes}` and one stdout JSON
array of `{id, section, start, end}`. Start and end are half-open UTF-8 byte
offsets. The output must preserve every non-whitespace byte of eligible
section bodies exactly once, use the corresponding section label, omit
headings/frontmatter/excluded sections, keep each interval within max_bytes,
avoid broken UTF-8, and use nonempty unique string IDs. Bodies inside fenced
code belong to their surrounding section, including heading-looking lines.
Repeated section titles are distinct occurrences but retain the same label.
Only exact References/Bibliography section titles are excluded, including
their descendants until a same-or-higher-level nonexcluded heading.
Titles such as Learned Preferences are eligible. ATX levels 1-6 outside
frontmatter/fences are structural; level-1 document title has no body in v1.
Appendices and acknowledgments are eligible in this task. Setext headings,
HTML block parsing and inline Markdown semantics are outside v1.
Whitespace-only gaps are allowed. No normalization or generated summaries.

The starter is intentionally simpler than the current product: this is a
research-derived challenge, not a frozen reproduction of the full repository.
Current product observations motivating it are recorded in PROTOCOL.md.

## Scoring and readiness

The public preflight contains eight readable chunking cases, not the promised
sealed final split. Baseline: 3/8 = 0.375. Primary score is macro case accuracy
with equal per-family weighting at final evaluation; byte-coverage diagnostics
cannot compensate for an invalid case. Candidate failure, timeout or invalid
output scores zero for that case. Independent immutable evaluator owns gold.

The public evaluator launches subprocesses for convenience. **It does not
provide sandbox security:** a candidate running as the same user can read or
modify local files. Do not use it unchanged for a hidden evaluation. The final
runner must put only candidate code and public assets inside a no-network
container; private gold, field parameters and evaluator remain outside. See
PROTOCOL.md for the required process, resource and feedback controls.

Registry metadata is in registry.json, with explicit draft states. Branch and
version refer to this preparation source, not an official registration or a
baseline-model run. No agent-provider API runs, GPU jobs, official runs or
main merges were performed. The source repositories do not currently provide
a blanket license for all their code; no redistribution permission for those
repositories is implied by this original package. Package licensing remains
an owner/project decision before external redistribution.
