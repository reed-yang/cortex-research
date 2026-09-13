import { vi } from "vitest";
import type { Run } from "../../app/control/contracts";
import type { ControlActions, ControlState } from "../../app/shell/types";

const ACTION_NAMES = [
  "setView", "setShowEngine", "dismissNotice", "selectWorkspace", "createWorkspace", "renameWorkspace",
  "selectThread", "createThread", "renameThread", "archiveThread", "unarchiveThread", "selectRun",
  "loadOlderRuns", "createRun", "runAction", "resolveDecision", "retainTurn", "takeRetainedTurn",
  "messageCommitted", "runCreated",
  "turnOutcome", "capture", "decideCapture", "refreshCaptures", "selectSource", "refreshSources", "refreshThread", "retry", "openThread",
  "selectResearchKind", "selectResearchStatus", "selectResearchItem", "browseResearchItems",
  "refreshResearchItems", "openResearchThread",
] as const;

export function actionsBag(): ControlActions {
  const bag: Record<string, unknown> = {};
  for (const name of ACTION_NAMES) bag[name] = vi.fn();
  // The one action a view reads an answer from: no retained send by default.
  bag.takeRetainedTurn = vi.fn(() => null);
  return bag as unknown as ControlActions;
}

export function baseState(overrides: Partial<ControlState> = {}): ControlState {
  return {
    loading: false, fatalError: null, offline: false, commandPending: false, replayState: "idle",
    dispatchGate: true, apiVersion: "v1", view: "thread", showEngine: false, notice: null,
    workspaces: [], workspace: null, threads: [], archivedThreads: [], thread: null, loadedThreadId: null,
    messages: [], runs: [], nextRunCursor: null, run: null, selectedRunId: null, events: [],
    decisions: [], pendingDecisions: [], research: null, lastTurnOutcome: null, captures: [],
    capturesLoading: false, capturesError: null, sources: [], sourcesLoading: false, sourcesError: null,
    sourceDetail: null, sourceDetailError: null, selectedSourceId: null, capabilities: null, selectedCaptureId: null,
    researchKind: "idea", researchStatus: null, researchItems: [], researchTotal: 0, researchLimit: 100,
    researchOffset: 0, researchListLoading: false, researchListError: null, selectedResearchItemId: null,
    researchItem: null, researchItemLoading: false, researchItemError: null,
    ...overrides,
  };
}

// Started and last-touched are deliberately different: a fixture that gives a
// run one timestamp cannot tell which of the two a view read.
export function fixtureRun(id: string, state: string, startedMinutesAgo: number, touchedMinutesAgo = 0): Run {
  const at = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
  return {
    id, thread_id: "thread_1", state, active_attempt_id: null, stage: null, latest_sequence: 0,
    engine_owned: false, revision: 0, created_at: at(startedMinutesAgo), updated_at: at(touchedMinutesAgo),
  };
}
