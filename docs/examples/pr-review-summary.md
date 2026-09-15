# Proposed English PR summary

Presentation preview only; this comment has not been posted. It uses the completed
PR #3 report from [run 34913343300](https://github.com/reed-yang/cortex-research/actions/runs/34913343300).
Prose is translated and condensed to illustrate the proposed layout, not another
model run. The current renderer still uses the longer per-reviewer report format.

---

<!-- independent-pr-review:v1 -->
## AI code review

**Completed · 0 P1 · 0 P2**

Reviewed [524af4a](https://github.com/reed-yang/cortex-research/commit/524af4aea74849e5940d437e805593ed3edaffea)
against base `399706e`.

| Reviewer | Result | Findings |
| --- | --- | --- |
| Grok 4.6 | Completed | None meeting the reporting threshold |
| Gemini 3.1 Pro High via agy | Completed | None meeting the reporting threshold |

No actionable defects were identified in the supplied documentation changes.

<details>
<summary>Scope and limitations</summary>

- Reviewed two Markdown files; no files were omitted.
- No tests were executed by these reviewers.
- Workflow code and live settings were outside this packet, so the reviewers
  could not independently verify the documentation's runtime claims.
- Both opinions are retained independently in the full report. An empty findings
  list is not a guarantee of correctness or approval to merge.

</details>

[Run and full reports](https://github.com/reed-yang/cortex-research/actions/runs/34913343300)

---

For a partial run, the first line should say **Review incomplete**, the failed
reviewer should say **Unavailable**, and available findings should remain visible.
A future inline comment should give one specific trigger, consequence and smallest
relevant changed range. Only verified findings get an inline thread; missing
anchors remain in this summary. Do not add an all-clear comment after every push.
