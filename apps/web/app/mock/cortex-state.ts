import {
  g0CanonicalEvents,
  g0Decision,
  g0Fixture,
  type DecisionView,
  type EventView,
  type G0Action,
  type RunState,
  type TimelineStage,
} from "./cortex-fixtures";

export type G0ViewState = {
  runState: RunState;
  pendingCount: 0 | 1;
  decision: DecisionView;
  timeline: TimelineStage[];
  events: EventView[];
  controlDetail: string;
  researchDetail: string;
  sourcePlan: string;
};

export const initialG0State: G0ViewState = {
  runState: "waiting_for_decision",
  pendingCount: 1,
  decision: g0Decision,
  timeline: [
    { id: "control", label: "Run + attempt saved", state: "done" },
    { id: "conflict", label: "Conflict detected", state: "done" },
    { id: "decision", label: "Decision pending", state: "active" },
    { id: "research", label: "Research blocked", state: "blocked" },
    { id: "artifacts", label: "No artifacts", state: "blocked" },
  ],
  events: g0CanonicalEvents,
  controlDetail: "Run, attempt, source intent, decision rev 1, and events 1–6 committed",
  researchDetail: `${g0Fixture.researchSideEffects.transactions} transactions · ${g0Fixture.researchSideEffects.materializations} materializations`,
  sourcePlan: "Awaiting human source resolution",
};

export function resolveG0(action: G0Action): G0ViewState {
  const canceled = action === "cancel";
  const runState: RunState = canceled ? "canceled" : "resuming";
  const resolutionEvent: EventView = {
    id: `event-ui-g0-007-${action}`,
    sequence: 7,
    type: "decision.resolved",
    durability: "ui_local",
    summary: `${action} selected · decision rev 2 preview only`,
    origin: "mock_transition",
  };
  const runEvent: EventView = {
    id: `event-ui-g0-008-${runState}`,
    sequence: 8,
    type: canceled ? "run.canceled" : "run.resuming",
    durability: "ui_local",
    summary: canceled
      ? "Cancellation previewed before research"
      : "Resume previewed from decision boundary",
    origin: "mock_transition",
  };

  return {
    runState,
    pendingCount: 0,
    decision: {
      ...g0Decision,
      status: "resolved",
      revision: 2,
      selected: action,
      origin: "mock_transition",
    },
    timeline: canceled
      ? [
          { id: "control", label: "Run + attempt saved", state: "done" },
          { id: "decision", label: "Decision resolved · UI preview", state: "done" },
          { id: "canceled", label: "Run canceled · UI preview", state: "canceled" },
          { id: "research", label: "Research unchanged", state: "blocked" },
          { id: "artifacts", label: "No artifacts", state: "blocked" },
        ]
      : [
          { id: "control", label: "Run + attempt saved", state: "done" },
          { id: "decision", label: "Decision resolved · UI preview", state: "done" },
          { id: "resume", label: "Run resuming · UI preview", state: "active" },
          { id: "research", label: "Research not started", state: "upcoming" },
          { id: "artifacts", label: "Artifacts unavailable", state: "blocked" },
        ],
    events: [...g0CanonicalEvents, resolutionEvent, runEvent],
    controlDetail:
      `Canonical ledger remains at pending decision rev 1 · UI preview: ` +
      `decision rev 2, run ${runState}`,
    researchDetail: `${g0Fixture.researchSideEffects.transactions} transactions · ${g0Fixture.researchSideEffects.materializations} materializations`,
    sourcePlan:
      action === "keep_both"
        ? "UI preview: resume with Echo and LingBot as separate sources"
        : action === "replace_url_with_echo"
          ? "UI preview: resume with Echo-Infinity only"
          : "UI preview: canceled before import or materialization",
  };
}
