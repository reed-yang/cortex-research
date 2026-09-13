import canonicalSnapshot from "./fixtures/productization-v0.1.snapshot.json";

export type ScenarioId = "g0" | "g1";
export type G0Action = "keep_both" | "replace_url_with_echo" | "cancel";
export type RunState = "waiting_for_decision" | "resuming" | "canceled" | "completed";
export type StageState = "done" | "active" | "upcoming" | "blocked" | "canceled";

export type TimelineStage = {
  id: string;
  label: string;
  state: StageState;
};

export type EventView = {
  id: string;
  sequence: number;
  type: string;
  durability: "durable" | "ui_local";
  summary: string;
  origin: "canonical" | "mock_transition";
};

export type DecisionOption = {
  id: string;
  label: string;
  tone?: "primary" | "danger";
};

export type DecisionView = {
  id: string;
  kind: "source_conflict" | "prior_lineage_disposition";
  title: string;
  prompt: string;
  status: "pending" | "resolved";
  revision: number;
  options: DecisionOption[];
  selected?: string;
  origin: "canonical" | "mock_transition";
};

const snapshot = canonicalSnapshot;

export const fixtureOrigin = snapshot.origin;

export const workspace = {
  id: snapshot.g1.workspace.workspace_id,
  title: snapshot.g1.workspace.name,
  threads: snapshot.g1.threads.map((thread) => ({
    id: thread.thread_id,
    kind: thread.kind,
    title: thread.title,
    icon:
      thread.kind === "evidence"
        ? "EV"
        : thread.kind === "architecture"
          ? "AR"
          : "TP",
    count:
      snapshot.g1.artifacts.filter((artifact) => artifact.thread_id === thread.thread_id)
        .length,
  })),
};

export const sourceCandidates = {
  titleIntent: {
    role: "Title intent",
    ...snapshot.g0.source_candidates[0],
  },
  suppliedUrl: {
    role: "Supplied URL",
    ...snapshot.g0.source_candidates[1],
  },
};

const optionLabels: Record<string, string> = {
  keep_both: "Keep both sources",
  replace_url_with_echo: "Replace URL with Echo-Infinity",
  cancel: "Cancel run",
  create_successor: "Create successor",
  continue_existing: "Continue existing line",
  start_unrelated: "Start unrelated line",
};

export function optionLabel(optionId: string) {
  return optionLabels[optionId] ?? optionId;
}

export const g0Decision: DecisionView = {
  id: snapshot.g0.decision.decision_id,
  kind: "source_conflict",
  title: "Two sources, one request",
  prompt:
    "The title and supplied URL resolve to different canonical papers. Choose before Cortex starts any research import or materialization.",
  status: "pending",
  revision: snapshot.g0.decision.revision,
  options: snapshot.g0.decision.options.map((id) => ({
    id,
    label: optionLabel(id),
    tone: id === "keep_both" ? "primary" : id === "cancel" ? "danger" : undefined,
  })),
  origin: "canonical",
};

export const g1Decisions: DecisionView[] = snapshot.g1.decisions.map((decision) => ({
  id: decision.decision_id,
  kind:
    decision.kind === "source_conflict"
      ? "source_conflict"
      : "prior_lineage_disposition",
  title: decision.kind === "source_conflict" ? "Source conflict" : "Prior-line disposition",
  prompt:
    decision.kind === "source_conflict"
      ? "The existing Echo paper was reused and the LingBot paper was imported as a separate canonical source."
      : "The successor reuses dormant and graduated work without changing either prior status.",
  status: "resolved",
  revision: decision.revision,
  options: decision.options.map((id) => ({ id, label: optionLabel(id) })),
  selected: decision.selected ?? undefined,
  origin: "canonical",
}));

const eventSummaries: Record<string, string> = {
  "run.created": "Logical run and attempt committed",
  "run.started": "Attempt started",
  "source.intent_received": "Title and URL identities recorded separately",
  "source.conflict_detected": "Canonical source mismatch detected",
  "decision.requested": "Pending decision committed",
  "run.waiting_for_decision": "Run waiting on durable decision",
  "decision.resolved": "Decision revision committed",
  "source.reused": "Existing Echo source bound to run",
  "source.imported": "LingBot paper imported once",
  "lineage.candidates_retrieved": "Dormant and graduated lineage retrieved",
  "lineage.successor_created": "Successor and two reuse links committed",
  "checkpoint.committed": "Recovery checkpoint committed",
  "run.recovered": "Attempt recovered from checkpoint",
  "thread.created": "Workspace thread committed",
  "artifact.version_committed": "Artifact version committed",
  "artifact.snapshot_committed": "Immutable snapshot committed",
  "run.completed": "Logical run completed",
};

const canonicalEvents = (
  events: Array<{
    id: string;
    sequence: number;
    type: string;
    durability: string;
  }>,
): EventView[] =>
  events.map((event) => ({
    id: event.id,
    sequence: event.sequence,
    type: event.type,
    durability: "durable",
    summary: eventSummaries[event.type] ?? event.type,
    origin: "canonical",
  }));

export const g0CanonicalEvents = canonicalEvents(snapshot.g0.events);
export const g1CanonicalEvents = canonicalEvents(snapshot.g1.events);

export const g0Fixture = {
  fixtureId: snapshot.g0.fixture_id,
  runId: snapshot.g0.run.run_id,
  attemptId: snapshot.g0.run.attempt_id,
  initialRunState: snapshot.g0.run.state as RunState,
  controlScope: snapshot.g0.scopes.control,
  researchScope: snapshot.g0.scopes.research,
  persistedControl: snapshot.g0.persisted_control,
  researchSideEffects: snapshot.g0.research_side_effects,
};

export const g1Fixture = {
  fixtureId: snapshot.g1.fixture_id,
  runId: snapshot.g1.run.run_id,
  attemptId: snapshot.g1.run.attempt_id,
  runState: snapshot.g1.run.state as RunState,
  controlScope: snapshot.g1.scopes.control,
  researchScope: snapshot.g1.scopes.research,
};

export const g1Timeline: TimelineStage[] = [
  { id: "source", label: "Sources resolved", state: "done" },
  { id: "lineage", label: "Successor linked", state: "done" },
  { id: "recovery", label: "Checkpoint recovered", state: "done" },
  { id: "artifacts", label: "Artifacts committed", state: "done" },
  { id: "snapshot", label: "Snapshot committed", state: "done" },
];

export const lineageNodes = snapshot.g1.lineage.nodes;
export const lineageLinks = snapshot.g1.lineage.links;

export const livingBrief = {
  revision: snapshot.g1.artifacts.find((artifact) => artifact.kind === "living_brief")!
    .versions.at(-1)!.revision,
  versionId: snapshot.g1.artifacts.find((artifact) => artifact.kind === "living_brief")!
    .versions.at(-1)!.version_id,
  thesis:
    "Compare token/KV memory, TTT weight-space memory, a hybrid design, and a cache-only baseline for a Helios-14B-on-Wan2.2 research plan.",
  update:
    "Retain both resolved sources and reuse dormant and graduated lineage before testing the four memory variants.",
  approaches: [
    { title: "Token / KV", note: "Fixed-budget comparison" },
    { title: "TTT weights", note: "Weight-space memory" },
    { title: "Hybrid", note: "Combined memory" },
    { title: "Cache only", note: "No-new-memory baseline" },
  ],
};

export const evidenceRows = [
  {
    claim: "Infinite-video memory is the primary Echo topic.",
    source: "source-echo",
    status: "synthetic",
  },
  {
    claim: "LingBot-Video is a model alias, not the paper title.",
    source: "source-lingbot",
    status: "synthetic",
  },
];

export const trainingPlan = [
  "Establish the cache/no-new-memory baseline.",
  "Compare token/KV, TTT weight-space, and hybrid memory under fixed budgets.",
  "Run staged ablations before any scale-up decision.",
];

const immutableSnapshotVersion = snapshot.g1.immutable_snapshot.versions.at(-1)!;

export const immutableSnapshot = {
  artifactId: snapshot.g1.immutable_snapshot.artifact_id,
  revision: immutableSnapshotVersion.revision,
  versionId: immutableSnapshotVersion.version_id,
  sha256: immutableSnapshotVersion.sha256,
};
