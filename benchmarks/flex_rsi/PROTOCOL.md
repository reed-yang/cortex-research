# Evidence, design and handoff

## Access and protocol status

On 2026-10-12, authenticated GitHub user `reed-yang` could read all three
personal repositories. Flex-RSI README and git-tree requests returned 404;
listing accessible repositories for owner `flex-rsi` returned none. This does
not prove that the repository or a registration is absent. Latest task registry,
catalog, run board, duplicate tasks, official states/categories and CLI flags
are unverified. Command names below come from the supplied email, not README:
`rsib task prepare`, `rsib run start`, `rsib report`, `rsib push`.

Before official use, read README and the catalog, check duplicates and maintainer
records, map registry.json to the actual schema, and verify the CLI with its
installed help. Do not invent flags. Claim each task/model/seed in the run board
before running. Prepare the dataset using the real task preparation interface,
freeze source/data/evaluator hashes, then start the claimed run. Report and push
only completed validated receipts. Every baseline model has an independent run
branch, e.g. `runs/reed-yang/cortex-chunking/<actual-model-id>/seed-0`; never
merge model run branches into main. Contribution preparation is separate from
the model runs. No official registration or run-board claim has been made.

## Repository observations

Inspected Cortex main at e975c242dc3955ff01593606b56dc6a310afa728:
`profiles/research/src/cortex_research/chunker.py` blob
6a22455c77621bbf937ee3aafb54bc019b4d8a08. The scanner matches only ##/### using
a multiline regex with no fence state. Chunk IDs use paper-dir, section title
and per-section local index, so repeated section titles can collide. Oversize
splitting is paragraph-based and does not split a single long paragraph. The
section-exclusion regex matches substrings such as references/bibliography/
appendix, not a complete section label. These are code-level observations,
not claims about measured downstream retrieval quality. The newer
`cortex_platform/product/research/evidence.py` already handles fenced blocks,
frontmatter and UTF-8 windows, so a task over the older chunker must not claim
to fix every evidence path. It also provides a visible solution-related hint:
hide repository-wide browsing if measuring problem-solving rather than reuse.

Spec-WAM architecture defines REAL/CURRENT/NEXT isolation, fresh-history
verification, one-action publication and defect-controlled action solves.
The inspected main ref is 4d320ac73090274f613326a5be0c94043210a5f7. CPU tests and
real closed-loop harnesses exist, but they are different evaluation tiers.
Current AGENTS.md says scene and first-action hashes are recorded rather than
required to align across hardware; do not infer identity constraints from stale
README language. The synthetic integration prototype measures numerical solver
quality and NFE, not robot success or GPU wall latency.

Streaming Human Replacement main tree was inspected, including AGENTS.md and
efficiency scripts. Its rules require live GPU occupancy/host-memory checks,
explicit allocation and receipts before GPU jobs. Complete replacement quality
depends on identity, source motion, scene and interaction preservation; pose
animation is not replacement. Published finite-clip throughput is not streaming
latency. There is no confirmed occupancy, checkpoint availability or task cost
in this environment. Therefore no GPU evaluation was launched or registered.

## Why select one task now

1. Chunking repair: objectively verifiable, CPU-only, synthetic data can have
   exact provenance and gold ranges, known failures and small editable surface.
   It is a bounded software-engineering benchmark related to research agents,
   not a measure of research taste, autonomous discovery or proof of RSI.
2. Speculative runtime event/lineage repair: potentially stronger systems task,
   but requires a trusted event simulator and independently verified schedules.
   Real runtime already has extensive tests; a nontrivial unfixed baseline defect
   has not been demonstrated. Do not assert headroom solely because tests exist.
3. Integration optimization: implemented as feasibility prototype. Dense Euler
   costs 256 queries/case; fixed 16-step RK4 costs 64 and saturates the capped
   score on all nine cases. This pilot is too easy to distinguish advanced agents;
   hold it back instead of filling the three-task quota with a weak task.
4. Full Spec-WAM robot optimization or H3 replacement: strong scientific
   relevance but expensive, noisy and vulnerable to quality/protocol changes.
   H3 operator-only efficiency kernels could be a future third task after the
   exact operator, dtype, shapes, hardware and numeric tolerances are frozen.

## Final chunking dataset construction (still pending)

Proposed 48 public training documents, 40 public validation documents and 200
sealed test documents. Split by document AND generator-template family, not
individual chunks or alternate renderings of one paper. Eight preflight cases
in this package are diagnostic demos and excluded from the final split.

Define a v1 grammar before generating gold: ATX levels 1-6, exact frontmatter
delimiters at file start, backtick and tilde fenced code with marker-specific
closing length, CRLF and LF, repeated heading labels, nested section scopes,
Unicode bodies, unbroken long paragraphs, exact References/Bibliography titles
and eligible appendices. Gold comes from a document AST used to render bytes,
not from running the candidate parser. Record section byte spans during rendering.
Cross-check by a second parser and manually inspect ambiguous fixtures. Do not
secretly add unsupported Markdown syntax to hidden tests.

Suggested final sealed families: 40 fence/frontmatter cases, 40 hierarchy and
repeated-label cases, 40 Unicode/byte-limit cases, 40 reference-scope cases and
40 combinations. Training and validation use other layouts and text vocabularies;
sealed examples must include authored compositions and separately implemented
renderers, not just new seeds from the public demo generator. Hidden gold and
generator source stay with evaluator maintainers. Only input documents are sent
to the candidate at scoring time. Hold out exact hidden documents from both
agent development and external repositories. Seal before baseline comparisons.

Initial code: original candidate.py in this package. Editable scope: that one
file only, stdlib, no imports from sibling product modules. Candidate input cap
256 KiB UTF-8/document; max_bytes ranges 128-4096. Output cap 2 MiB, candidate
CPU wall timeout 5 s/case, 2 GiB memory, 2 cores. Preserve all valid inputs and
count every case; no selective reruns/dropped failures. Stable IDs must be
deterministic for identical input; final evaluator runs selected duplicates
under fresh processes to verify this. Sorting can vary only if coverage,
nonoverlap and section identities remain valid.

Primary metric: per-family mean exact-case pass rate, then macro-average over
the five families. Secondary diagnostics: covered eligible-byte fraction,
reference leakage, duplicated bytes/IDs, wrong labels, UTF-8 errors, oversize
chunks and wall time. No weighted byte score as primary: one huge easy section
must not overwhelm a failed provenance boundary. Require no severe integrity
regression relative to baseline before accepting a faster implementation.

## Leakage and gaming controls

- Same-user subprocesses are not isolation. Candidate must execute as a separate
  restricted principal/container; no network, repository history, evaluator path,
  gold, secrets, sibling source access or writable evaluator volume.
- Use an allowlist of changed paths on the final diff, rebuild trusted evaluation
  from its pinned commit, and hash fixtures. Candidate cannot edit tests, scoring,
  dependencies, environment or the rsib wrapper.
- Bind source bytes, offsets and labels; dumping whole files, blank outputs,
  summarization, Unicode normalization and dropping hard sections fail the case.
  Strict size, duplicate and reference-scope checks prevent cheap shortcuts.
- No hidden per-case feedback during optimization; validation feedback is capped
  at 10 evaluations per run. Final hidden score is returned once per frozen run.
  Do not expose private RNG seeds or case identifiers to the candidate.
- Audit candidate imports, dynamic execution, FS/network/system calls and output
  sizes. Host evaluator must enforce timeouts on partial lines, terminate the
  process group and resource-limit children. Public dev_eval.py does not provide
  these controls and is explicitly not certified for hidden execution.
- Use an independent immutable grader and fixture hashes. Verify blank/copy-all,
  duplicate, oversize, wrong-label and timeout baselines score as intended.
  Existing two unit checks verify several grader shortcuts and numerical reference
  convergence; they do not constitute a complete security audit.

## Agent-model comparison and proposed ceilings

Task runtime uses no model API. Baseline models are the code-editing agents,
separate from task models/checkpoints. Confirm exact supported IDs/providers and
access in the official catalog; do not invent a GPT/Claude configuration. Aim
for three available agent families, same harness/tool permissions and three
seeds each. Include an unmodified-code baseline and a human feasibility baseline.
Compare both absolute final score and improvement from the same starting code.
Freeze the prompt, tool schema, model version, sampling/reasoning configuration,
token accounting and iteration policy. Different accessible reasoning budgets
must be reported, not silently treated as equivalent.

Per agent model/seed: maximum 30 minutes, 60 tool calls, 100,000 billed tokens
including reasoning where available, 10 validation requests; first exhausted
limit ends the run. Provider-specific dollar cost requires verified live prices
and project quota. No API campaign is authorized by merely having this proposed
ceiling; do not start until access/quota is confirmed. Task final CPU evaluation
ceiling 20 minutes/run (200 x 5 s upper bound plus startup), 2 cores, 2 GiB. Nine
agent runs imply <=4.5 agent wall-hours and <=3 CPU evaluation wall-hours, plus
generation/QA. These are caps, not measured forecasts or a funding commitment.

## Integration reserve hardening

Only tasks/integration/candidate.py would be editable. Field calls cross a
trusted JSON RPC boundary; all calls, including failed ones, must count.
Candidate observes y0, horizon, tolerances and past oracle responses, never
field parameters or targets. Return states at t=.25,.5,.75,1. Current prototype
counts calls externally, rejects nonfinite values, and gates the entire score
on every case meeting the 0.01 path tolerance. Reference is RK4 at 4096 steps;
final use needs 8192-step convergence checks for every field and an error bound
far below the candidate tolerance. No theoretical defect certificate is claimed.

To revive this task: add independently implemented nonlinear, time-local pulse,
coupled-matrix and high-frequency families; tune tolerances and NFE ranges so
all baselines have measurable headroom and fixed RK4 cannot saturate; require
family-wide accuracy gates, count compilation/preparation and total field work
separately, and verify any reported batched field cost against the actual model.
Do not put oracle equations, reference values or hidden fixtures in the candidate
container. Endpoint-only scoring would admit cancellation shortcuts; the current
prototype also checks three intermediate states. This still does not imply
closed-loop control fidelity. Sealed dataset and resource contract remain pending.

## Schedule toward October 21

- Oct 12-13: resolve Flex-RSI repository access; confirm duplicates/schema and
  accept selected task; finish grammar and two independent gold checks.
- Oct 14-15: generate and seal grouped splits, integrate actual rsib adapter,
  calibrate baseline/difficulty and perform sandbox/shortcut checks.
- Oct 16-19: claim model runs, run bounded CPU agent comparisons, preserve
  independent model branches and receipts.
- Oct 20: frozen hidden scoring, failure audit, report/push.
- Oct 21: hand off results and limitations for technical report/site/release.

If access or hidden-set isolation remains unresolved, deliver this as a proposal,
not a completed benchmark. Do not compress these requirements into a claimed
ready state to meet a date.
