import type { PreparedMutation } from "../control/client";
import type { ReplayState } from "../control/event-replay";
import type { Capture, Decision, Message, Run, RunEvent, Thread, Workspace } from "../control/contracts";
import type { ResearchWorkflowProjection, SourceProjection } from "../control/research-contracts";
import type {
  ResearchItem,
  ResearchItemDetail,
  ResearchItemKind,
} from "../control/research-items-contracts";

export type ShellView = "thread" | "research" | "library" | "inbox" | "status";
export type RunActionName = "pause" | "resume" | "cancel" | "retry";
export type CaptureActionName = "approve" | "dismiss" | "reopen";

export type Notice = { tone: "success" | "warning" | "error"; text: string; details?: string };

export type ControlState = {
  loading: boolean;
  fatalError: string | null;
  offline: boolean;
  commandPending: boolean;
  replayState: ReplayState;
  dispatchGate: boolean | null;
  // Reported by the daemon's health read; null when it does not say.
  apiVersion: string | null;
  // The build capabilities the same health read reports; null when it says
  // nothing. These are properties of the build, never the dispatch gate.
  capabilities: Record<string, boolean> | null;
  view: ShellView;
  showEngine: boolean;
  notice: Notice | null;
  workspaces: Workspace[];
  workspace: Workspace | null;
  threads: Thread[];
  archivedThreads: Thread[];
  thread: Thread | null;
  loadedThreadId: string | null;
  messages: Message[];
  runs: Run[];
  nextRunCursor: string | null;
  run: Run | null;
  selectedRunId: string | null;
  events: RunEvent[];
  decisions: Decision[];
  pendingDecisions: Decision[];
  research: ResearchWorkflowProjection | null;
  lastTurnOutcome: string | null;
  captures: Capture[];
  // The capture the operator was last pointed at -- the row a refused
  // duplicate collided with. The Inbox marks it; nothing else depends on it.
  selectedCaptureId: string | null;
  capturesLoading: boolean;
  capturesError: string | null;
  sources: SourceProjection[];
  sourcesLoading: boolean;
  sourcesError: string | null;
  sourceDetail: SourceProjection | null;
  sourceDetailError: string | null;
  selectedSourceId: string | null;
  // R1c: the research catalog. The listing is one page at a time -- `total`,
  // `limit` and `offset` are the backend's own answer -- because browsing 28
  // seeds and their explorations is paging, not an endless scroll.
  researchKind: ResearchItemKind;
  // The status filter, or null for every status the catalog holds.
  researchStatus: string | null;
  researchItems: ResearchItem[];
  researchTotal: number;
  researchLimit: number;
  researchOffset: number;
  researchListLoading: boolean;
  researchListError: string | null;
  // The item the operator selected, kept apart from the dossier itself so the
  // catalog can mark the row while its dossier is still being read.
  selectedResearchItemId: string | null;
  researchItem: ResearchItemDetail | null;
  researchItemLoading: boolean;
  researchItemError: string | null;
};

export type ControlActions = {
  setView(view: ShellView): void;
  setShowEngine(on: boolean): void;
  dismissNotice(): void;
  selectWorkspace(id: string): void;
  createWorkspace(title: string): Promise<Workspace | null>;
  renameWorkspace(workspace: Workspace, title: string): Promise<boolean>;
  selectThread(id: string | null): void;
  // Opens a thread that may belong to another project: switches the project
  // first when it does. `false` when the thread could not be opened, with a
  // notice saying why.
  openThread(id: string): Promise<boolean>;
  createThread(title: string): Promise<Thread | null>;
  renameThread(thread: Thread, title: string): Promise<boolean>;
  archiveThread(thread: Thread): Promise<boolean>;
  unarchiveThread(thread: Thread): Promise<boolean>;
  selectRun(id: string): void;
  loadOlderRuns(): void;
  createRun(): Promise<void>;
  runAction(action: RunActionName): Promise<void>;
  resolveDecision(decision: Decision, choice: string): Promise<void>;
  // A send whose delivery Control never confirmed, kept whole so the retry is
  // the same message and not a second one: the command carries the idempotency
  // key it was prepared with, and `content` is the operator's own text, the key
  // a retry is recognised by. Passing `null` forgets the thread's retained send.
  retainTurn(threadId: string, prepared: PreparedMutation<Message> | null, content: string): void;
  // The retained command for this thread when the operator is sending the same
  // text again; anything else forgets it and answers null.
  takeRetainedTurn(threadId: string, content: string): PreparedMutation<Message> | null;
  messageCommitted(message: Message, replayed: boolean): void;
  runCreated(run: Run): void;
  turnOutcome(text: string | null): void;
  capture(payload: string, note: string, approveNow: boolean): Promise<boolean>;
  decideCapture(capture: Capture, action: CaptureActionName): Promise<void>;
  refreshCaptures(): void;
  selectSource(id: string): void;
  refreshSources(): void;
  // Ideas, Explorations or Projects: a new kind reads the first page and drops
  // whatever dossier was open, because the item behind it is not in this list.
  selectResearchKind(kind: ResearchItemKind): void;
  selectResearchStatus(status: string | null): void;
  // `null` closes the dossier and leaves the catalog listed.
  selectResearchItem(id: string | null): void;
  // Explicit paging: the offset the operator asked for, never an implicit one.
  browseResearchItems(offset: number): void;
  refreshResearchItems(): void;
  // Opens or reuses the thread linked to the open dossier, in the project the
  // operator has selected, and navigates to it. `false` when it could not be
  // opened, with a notice saying why. It starts no run: the operator asks the
  // question in the ordinary composer afterwards.
  openResearchThread(): Promise<boolean>;
  refreshThread(): void;
  // Reads the whole shell again, from the projects down. The answer offered
  // with the "Cortex is unavailable" state, where no thread is open to refresh.
  retry(): void;
};

export type ViewProps = { state: ControlState; actions: ControlActions; client: import("../control/client").CortexControlClient };
