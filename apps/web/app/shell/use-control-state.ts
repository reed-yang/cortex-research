"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NO_TURN_TO_DRIVE_NOTICE } from "../control/assistant-adapter";
import {
  alreadyCapturedCurrent,
  ControlNetworkError,
  ControlProblemError,
  type CortexControlClient,
  type PreparedMutation,
} from "../control/client";
import {
  ContractDecodeError,
  type Capture,
  type Decision,
  type Message,
  type Run,
  type RunEvent,
  type Thread,
  type Workspace,
} from "../control/contracts";
import { EventReplayController, invalidatedRunId, type ReplayState } from "../control/event-replay";
import type { ResearchWorkflowProjection, SourceProjection } from "../control/research-contracts";
import type {
  ResearchItem,
  ResearchItemDetail,
  ResearchItemKind,
} from "../control/research-items-contracts";
import { useConnectivityState } from "../pwa-client";
import { copy, label, problemSentence } from "./copy";
import { readShellLocation, writeShellLocation, type ShellLocation } from "./url-state";
import type {
  CaptureActionName,
  ControlActions,
  ControlState,
  Notice,
  RunActionName,
  ShellView,
} from "./types";

type CommandOptions = {
  onConflict?: () => Promise<void>;
  // What a conflict says, when the row has a sentence of its own.
  conflictText?: string;
  onProblem?: (error: ControlProblemError) => boolean;
  // When a second command follows inside the same gesture, the first must not
  // announce success: the last notice has to describe what actually happened.
  quietSuccess?: boolean;
};

// Control serves captures oldest first. The inbox is a queue the operator
// works from the top, so the newest submission leads.
function newestFirst(captures: Capture[]): Capture[] {
  return [...captures].sort((left, right) =>
    right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id));
}

// The sentence says what happened; the Details disclosure carries the protocol
// category a refusal is filed under, so an operator can quote it without the
// sentence having to name it. A failure that is not a refusal shows its own
// message there, unless that is already the sentence.
function noticeDetails(error: unknown, text: string): string | undefined {
  if (error instanceof ControlProblemError) return error.problem.category;
  const message = error instanceof Error ? error.message : "";
  return message && message !== text ? message : undefined;
}

// A refusal that names a newer state of the row is answered by rereading the
// row, never by trusting the copy the refusal carried with it.
const ROW_RELOAD_CATEGORIES = ["revision_conflict", "thread_active_run"];

const RUN_ACTION_NOTICE: Record<RunActionName, string> = {
  pause: copy.notice.paused,
  resume: copy.notice.resumed,
  cancel: copy.notice.canceled,
  retry: copy.notice.retrying,
};

// One page of the research catalog, as the contract's own default: browsing is
// explicit, so the page the operator asked for is the page they get.
const RESEARCH_PAGE_SIZE = 100;

const CAPTURE_ACTION_NOTICE: Record<CaptureActionName, string> = {
  approve: copy.notice.approved,
  dismiss: copy.notice.dismissed,
  reopen: copy.notice.reopened,
};

// A refusal never speaks in the words the wire sent: the category is looked up
// in the copy table, and the category itself stays under Details.
function refusalNotice(error: unknown): string {
  return error instanceof ControlProblemError ? problemSentence(error.problem.category) : copy.errors.refused;
}

// A read that failed under the operator's own selection. Spec section 11
// reserves the unavailable state for "no Control" -- one row that could not be
// read says so and leaves the shell standing -- and it wants the sentence to
// name what happened: a refusal, a connection that answered nothing, and an
// answer this client cannot parse are three different facts and only one of
// them is Cortex refusing.
function failedRead(error: unknown): Notice {
  const text = error instanceof ControlNetworkError
    ? copy.strip.offline
    : error instanceof ContractDecodeError
      ? copy.errors.unreadable
      : refusalNotice(error);
  return { tone: "error", text, details: noticeDetails(error, text) };
}

export function useControlState(client: CortexControlClient): [ControlState, ControlActions] {
  // The caller may hand a new client object on every render (tests do). Every
  // command reads the latest one through this ref, so a fresh object never
  // restarts the load or the event listener.
  const clientRef = useRef(client);
  useEffect(() => { clientRef.current = client; }, [client]);
  // The query is read once, before anything can rewrite it.
  const [initialLocation] = useState<ShellLocation>(
    () => readShellLocation(typeof window === "undefined" ? "" : window.location.search),
  );

  const [workspaceRows, setWorkspaceRows] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  // One list holds live and archived threads. The two groups are derived, so a
  // thread that changes side is one replaced row, not a move between states.
  const [threadRows, setThreadRows] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadedThreadId, setLoadedThreadId] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [nextRunCursor, setNextRunCursor] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [research, setResearch] = useState<ResearchWorkflowProjection | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [pendingDecisions, setPendingDecisions] = useState<Decision[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [commandPending, setCommandPending] = useState(false);
  const [replayState, setReplayState] = useState<ReplayState>("idle");
  const [view, setView] = useState<ShellView>(initialLocation.view);
  const [showEngine, setShowEngine] = useState(false);
  const [lastTurnOutcome, setLastTurnOutcome] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceProjection[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null);
  const [sourceDetail, setSourceDetail] = useState<SourceProjection | null>(null);
  const [sourceDetailError, setSourceDetailError] = useState<string | null>(null);
  const [captures, setCaptures] = useState<Capture[]>([]);
  const [capturesLoading, setCapturesLoading] = useState(false);
  const [capturesError, setCapturesError] = useState<string | null>(null);
  const [dispatchGate, setDispatchGate] = useState<boolean | null>(null);
  const [apiVersion, setApiVersion] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<Record<string, boolean> | null>(null);
  const [selectedCaptureId, setSelectedCaptureId] = useState<string | null>(null);
  const [researchKind, setResearchKind] = useState<ResearchItemKind>("idea");
  const [researchStatus, setResearchStatus] = useState<string | null>(null);
  const [researchItems, setResearchItems] = useState<ResearchItem[]>([]);
  const [researchTotal, setResearchTotal] = useState(0);
  const [researchLimit, setResearchLimit] = useState(RESEARCH_PAGE_SIZE);
  const [researchOffset, setResearchOffset] = useState(0);
  const [researchListLoading, setResearchListLoading] = useState(false);
  const [researchListError, setResearchListError] = useState<string | null>(null);
  const [selectedResearchItemId, setSelectedResearchItemId] = useState<string | null>(initialLocation.item);
  const [researchItem, setResearchItem] = useState<ResearchItemDetail | null>(null);
  const [researchItemLoading, setResearchItemLoading] = useState(initialLocation.item !== null);
  const [researchItemError, setResearchItemError] = useState<string | null>(null);

  const captureGeneration = useRef(0);
  // The catalog listing and the open dossier are guarded exactly like Sources:
  // a generation counter plus the requested identity. Both also carry an abort
  // controller, so a superseded read is cancelled rather than left to land --
  // one item's documents may never appear under another item's name.
  const researchListGeneration = useRef(0);
  const researchItemGeneration = useRef(0);
  const researchListAbort = useRef<AbortController | null>(null);
  const researchItemAbort = useRef<AbortController | null>(null);
  const selectedResearchItemIdRef = useRef<string | null>(initialLocation.item);
  const researchOffsetRef = useRef(0);
  // An uncertain "open the conversation", by item: the same prepared command is
  // reused when the operator asks again, so an unconfirmed delivery is retried
  // and never becomes a second thread.
  const retainedResearchThreads = useRef(new Map<string, PreparedMutation<Thread>>());
  const sourceGeneration = useRef(0);
  const sourceListGeneration = useRef(0);
  const selectedSourceIdRef = useRef<string | null>(null);
  const activeThreadId = useRef<string | null>(null);
  const activeWorkspaceId = useRef<string | null>(null);
  const selectedRunIdRef = useRef<string | null>(null);
  const runHistoryRef = useRef<Run[]>([]);
  const threadRunIdsRef = useRef<Set<string>>(new Set());
  const selectionGeneration = useRef(0);
  // Uncertain sends, by thread: a ref and not state, because nothing on screen
  // reads it -- it only decides which command the next send executes.
  const retainedTurns = useRef(new Map<string, { prepared: PreparedMutation<Message>; content: string }>());

  const connectivity = useConnectivityState();
  const offline = connectivity === "offline";

  const workspace = workspaceRows.find((item) => item.id === workspaceId) ?? null;
  const thread = threadRows.find((item) => item.id === threadId) ?? null;

  // P9-2: the engine's own project and its carrier threads are the engine's
  // bookkeeping, not the operator's research. Hidden until Status turns them
  // on, and whatever is selected right now is always listed -- a list that hid
  // its own selection would be lying about what you are looking at.
  const workspaces = useMemo(
    () => workspaceRows.filter((item) => showEngine || !item.engine_owned || item.id === workspaceId),
    [showEngine, workspaceId, workspaceRows],
  );
  const threads = useMemo(
    () => threadRows.filter((item) =>
      item.archived_at === null && (showEngine || !item.engine_owned || item.id === threadId)),
    [showEngine, threadId, threadRows],
  );
  // Archived threads read oldest-archived first, so one just archived lands at
  // the end of a group the operator is not looking at instead of jumping to
  // the top of it.
  const archivedThreads = useMemo(
    () => threadRows
      .filter((item) => item.archived_at !== null && (showEngine || !item.engine_owned || item.id === threadId))
      .sort((left, right) =>
        (left.archived_at ?? "").localeCompare(right.archived_at ?? "") || left.id.localeCompare(right.id)),
    [showEngine, threadId, threadRows],
  );

  useEffect(() => {
    let live = true;
    // P9: the dispatch gate and the API version as the health read reports
    // them; a read that fails is "unknown", never a guess either way, and a
    // stale read never lands after a later one.
    clientRef.current.getRuntimeStatus()
      .then((status) => {
        if (!live) return;
        setDispatchGate(status.dispatchGate);
        setApiVersion(status.apiVersion);
        setCapabilities(status.capabilities);
      })
      .catch(() => {
        if (!live) return;
        setDispatchGate(null);
        setApiVersion(null);
        setCapabilities(null);
      });
    return () => { live = false; };
  }, [connectivity]);

  const retainTurn = useCallback((threadId: string, prepared: PreparedMutation<Message> | null, content: string) => {
    if (prepared) retainedTurns.current.set(threadId, { prepared, content });
    else retainedTurns.current.delete(threadId);
  }, []);

  const takeRetainedTurn = useCallback((threadId: string, content: string) => {
    const retained = retainedTurns.current.get(threadId);
    if (retained && retained.content === content) return retained.prepared;
    retainedTurns.current.delete(threadId);
    return null;
  }, []);

  const clearThreadDetail = useCallback(() => {
    // The sentence that told the operator to retry goes with the thread detail,
    // so what it named goes with it too.
    retainedTurns.current.clear();
    setMessages([]);
    setLoadedThreadId(null);
    setRun(null);
    setRuns([]);
    setNextRunCursor(null);
    setSelectedRunId(null);
    selectedRunIdRef.current = null;
    runHistoryRef.current = [];
    threadRunIdsRef.current = new Set();
    setResearch(null);
    setDecisions([]);
    setLastTurnOutcome(null);
  }, []);

  const loadResearch = useCallback(async (runId: string, generation: number) => {
    try {
      const projection = await clientRef.current.getResearchWorkflow(runId);
      if (selectionGeneration.current !== generation || selectedRunIdRef.current !== runId) return;
      setResearch(projection);
      setDecisions(projection.decisions.filter((decision) => decision.state === "pending"));
    } catch {
      if (selectionGeneration.current === generation) setReplayState("error");
    }
  }, []);

  // Sources are corpus-wide, so the listing and the record are guarded by their
  // own generation counter plus the requested id, exactly like run selection.
  const loadSources = useCallback(async () => {
    const generation = ++sourceListGeneration.current;
    sourceGeneration.current += 1;
    selectedSourceIdRef.current = null;
    setSelectedSourceId(null);
    setSourceDetail(null);
    setSourceDetailError(null);
    setSourcesLoading(true);
    setSourcesError(null);
    try {
      const envelope = await clientRef.current.listSources();
      if (sourceListGeneration.current !== generation) return;
      setSources(envelope.items);
    } catch (error) {
      if (sourceListGeneration.current !== generation) return;
      setSources([]);
      setSourcesError(error instanceof Error ? error.message : copy.errors.unreadable);
    } finally {
      if (sourceListGeneration.current === generation) setSourcesLoading(false);
    }
  }, []);

  const selectSource = useCallback((id: string) => {
    const generation = sourceGeneration.current + 1;
    sourceGeneration.current = generation;
    selectedSourceIdRef.current = id;
    setSelectedSourceId(id);
    setSourceDetail(null);
    setSourceDetailError(null);
    void clientRef.current.getSource(id)
      .then((detail) => {
        if (sourceGeneration.current !== generation || selectedSourceIdRef.current !== id) return;
        setSourceDetail(detail);
      })
      .catch((error) => {
        if (sourceGeneration.current !== generation || selectedSourceIdRef.current !== id) return;
        setSourceDetailError(error instanceof Error ? error.message : copy.errors.unreadable);
      });
  }, []);

  const loadResearchItems = useCallback(async (
    kind: ResearchItemKind,
    status: string | null,
    offset: number,
  ) => {
    const generation = ++researchListGeneration.current;
    researchListAbort.current?.abort();
    const controller = new AbortController();
    researchListAbort.current = controller;
    researchOffsetRef.current = offset;
    setResearchListLoading(true);
    setResearchListError(null);
    try {
      const page = await clientRef.current.listResearchItems({
        kind,
        status: status ?? undefined,
        limit: RESEARCH_PAGE_SIZE,
        offset,
        signal: controller.signal,
      });
      if (researchListGeneration.current !== generation) return;
      setResearchItems(page.items);
      setResearchTotal(page.total);
      setResearchLimit(page.limit);
      setResearchOffset(page.offset);
      researchOffsetRef.current = page.offset;
    } catch (error) {
      if (researchListGeneration.current !== generation || controller.signal.aborted) return;
      setResearchItems([]);
      setResearchTotal(0);
      setResearchOffset(offset);
      setResearchListError(error instanceof Error ? error.message : copy.errors.unreadable);
    } finally {
      if (researchListGeneration.current === generation) setResearchListLoading(false);
    }
  }, []);

  // The dossier is emptied before the read starts, so the previous item's
  // documents are off the screen the moment another item is asked for.
  const loadResearchItem = useCallback(async (id: string) => {
    const generation = ++researchItemGeneration.current;
    researchItemAbort.current?.abort();
    const controller = new AbortController();
    researchItemAbort.current = controller;
    setResearchItem(null);
    setResearchItemError(null);
    setResearchItemLoading(true);
    try {
      const detail = await clientRef.current.getResearchItem(id, controller.signal);
      if (researchItemGeneration.current !== generation || selectedResearchItemIdRef.current !== id) return;
      setResearchItem(detail);
    } catch (error) {
      if (researchItemGeneration.current !== generation || controller.signal.aborted) return;
      if (selectedResearchItemIdRef.current !== id) return;
      setResearchItemError(error instanceof Error ? error.message : copy.errors.unreadable);
    } finally {
      if (researchItemGeneration.current === generation) setResearchItemLoading(false);
    }
  }, []);

  const selectResearchItem = useCallback((id: string | null) => {
    selectedResearchItemIdRef.current = id;
    setSelectedResearchItemId(id);
    if (id === null) {
      researchItemGeneration.current += 1;
      researchItemAbort.current?.abort();
      researchItemAbort.current = null;
      setResearchItem(null);
      setResearchItemError(null);
      setResearchItemLoading(false);
      return;
    }
    void loadResearchItem(id);
  }, [loadResearchItem]);

  // Captures have no event lane, so the inbox reads row state on entry and on
  // demand, guarded by its own generation counter like the Sources listing.
  const loadCaptures = useCallback(async () => {
    const generation = captureGeneration.current + 1;
    captureGeneration.current = generation;
    setCapturesLoading(true);
    setCapturesError(null);
    try {
      const envelope = await clientRef.current.listCaptures();
      if (captureGeneration.current !== generation) return;
      setCaptures(newestFirst(envelope.items));
    } catch (error) {
      if (captureGeneration.current !== generation) return;
      setCaptures([]);
      setCapturesError(error instanceof Error ? error.message : copy.errors.unreadable);
    } finally {
      if (captureGeneration.current === generation) setCapturesLoading(false);
    }
  }, []);

  // Inbox counts every thread's pending decision, so it is read whole here and
  // narrowed to the open thread's run in `decisions`.
  const loadPendingDecisions = useCallback(async () => {
    try {
      const envelope = await clientRef.current.listDecisions("pending");
      setPendingDecisions(envelope.items);
    } catch {
      // A missing inbox count is not worth failing the screen for.
    }
  }, []);

  const loadThread = useCallback(async (id: string) => {
    const generation = selectionGeneration.current + 1;
    selectionGeneration.current = generation;
    const [freshThread, messageEnvelope, decisionEnvelope, runEnvelope] = await Promise.all([
      clientRef.current.getThread(id),
      clientRef.current.listMessages(id),
      clientRef.current.listDecisions("pending"),
      clientRef.current.listRuns(id),
    ]);
    if (selectionGeneration.current !== generation || activeThreadId.current !== id) return;
    setThreadRows((items) => items.map((item) => item.id === id ? freshThread : item));
    setMessages(messageEnvelope.items);
    setPendingDecisions(decisionEnvelope.items);
    const retainedSelection = runHistoryRef.current.find((item) => item.id === selectedRunIdRef.current);
    const nextRuns = retainedSelection && !runEnvelope.items.some((item) => item.id === retainedSelection.id)
      ? [...runEnvelope.items, retainedSelection]
      : runEnvelope.items;
    setRuns(nextRuns);
    runHistoryRef.current = nextRuns;
    setNextRunCursor(runEnvelope.next_cursor);
    threadRunIdsRef.current = new Set(nextRuns.map((item) => item.id));
    const preservedRunId = selectedRunIdRef.current && nextRuns.some((item) => item.id === selectedRunIdRef.current)
      ? selectedRunIdRef.current
      : null;
    const nextSelectedRunId = freshThread.active_run_id ?? preservedRunId ?? nextRuns[0]?.id ?? null;
    const changedRun = selectedRunIdRef.current !== nextSelectedRunId;
    selectedRunIdRef.current = nextSelectedRunId;
    setSelectedRunId(nextSelectedRunId);
    const selectedRun = nextRuns.find((item) => item.id === nextSelectedRunId) ?? null;
    setRun(selectedRun);
    setDecisions(decisionEnvelope.items.filter((decision) => decision.run_id === nextSelectedRunId));
    if (changedRun) setResearch(null);
    setLoadedThreadId(id);
    if (nextSelectedRunId) await loadResearch(nextSelectedRunId, generation);
    else setResearch(null);
  }, [loadResearch]);

  const loadWorkspace = useCallback(async (id: string, preferredThreadId?: string, fromUrl = false) => {
    const [freshWorkspace, threadEnvelope] = await Promise.all([
      clientRef.current.getWorkspace(id),
      clientRef.current.listThreads(id, { includeArchived: true }),
    ]);
    setWorkspaceRows((items) => items.map((item) => item.id === id ? freshWorkspace : item));
    setThreadRows(threadEnvelope.items);
    const rows = threadEnvelope.items;
    const preferredIsHere = preferredThreadId !== undefined && rows.some((item) => item.id === preferredThreadId);
    // A thread the query named and the project does not hold opens nothing:
    // silently landing on a different conversation than the link asked for
    // would be the shell answering a question nobody asked.
    const unknownFromUrl = fromUrl && preferredThreadId !== undefined && !preferredIsHere;
    if (unknownFromUrl) setNotice({ tone: "warning", text: copy.notice.unknownThread });
    const selectedThreadId = preferredIsHere
      ? preferredThreadId
      : unknownFromUrl
        ? null
        // P9-2: same rule as the project above -- the operator's own live thread
        // is what a project opens on, and a carrier or archived thread only when
        // there is nothing else in it.
        : rows.find((item) => !item.engine_owned && item.archived_at === null)?.id
          ?? rows.find((item) => item.archived_at === null)?.id ?? null;
    setThreadId(selectedThreadId);
    activeThreadId.current = selectedThreadId;
    setLoadedThreadId(null);
    if (selectedThreadId) await loadThread(selectedThreadId);
    else clearThreadDetail();
  }, [clearThreadDetail, loadThread]);

  const loadWorkspaces = useCallback(async (preferredId?: string, preferredThreadId?: string, fromUrl = false) => {
    const envelope = await clientRef.current.listWorkspaces();
    setWorkspaceRows(envelope.items);
    const preferredIsHere = preferredId !== undefined && envelope.items.some((item) => item.id === preferredId);
    // A project the query named and Cortex does not hold falls back to the
    // first one, and the thread the query named goes with it: it belonged to
    // the project that is gone.
    const unknownFromUrl = fromUrl && preferredId !== undefined && !preferredIsHere;
    if (unknownFromUrl) setNotice({ tone: "warning", text: copy.notice.unknownProject });
    // P9-2: a cold load lands on the operator's own work. The engine's project
    // is only the fallback, for a state that has nothing else -- better to open
    // it than to open nothing.
    const selectedWorkspaceId = preferredIsHere
      ? preferredId
      : envelope.items.find((item) => !item.engine_owned)?.id ?? envelope.items[0]?.id ?? null;
    setWorkspaceId(selectedWorkspaceId);
    activeWorkspaceId.current = selectedWorkspaceId;
    if (selectedWorkspaceId) {
      await loadWorkspace(
        selectedWorkspaceId,
        unknownFromUrl ? undefined : preferredThreadId,
        fromUrl && !unknownFromUrl,
      );
    } else {
      setThreadRows([]);
      setThreadId(null);
      activeThreadId.current = null;
      clearThreadDetail();
    }
    return envelope.items;
  }, [clearThreadDetail, loadWorkspace]);

  // The one read whose failure is fatal: without the projects there is no
  // shell to show. It is cleared on success, not on entry, so the state the
  // operator is looking at stays put while the retry is in flight.
  const load = useCallback(async (location: ShellLocation) => {
    setLoading(true);
    try {
      await loadWorkspaces(location.project ?? undefined, location.thread ?? undefined, true);
      setFatalError(null);
    } catch (error) {
      setFatalError(error instanceof Error ? error.message : copy.errors.unavailable);
    } finally {
      setLoading(false);
    }
  }, [loadWorkspaces]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void load(initialLocation); }, 0);
    return () => window.clearTimeout(timer);
  }, [initialLocation, load]);

  // Library and Inbox read on entry, and entry is the view changing -- whether
  // the operator switched into it or the query opened straight onto it. An
  // effect keyed on the view is the only place that sees both.
  useEffect(() => {
    // Deferred past the commit, like the first load: a loader flips its own
    // pending flag before it awaits anything.
    const timer = window.setTimeout(() => {
      if (view === "library") void loadSources();
      if (view === "research") void loadResearchItems(researchKind, researchStatus, researchOffsetRef.current);
      if (view === "inbox") {
        void loadCaptures();
        void loadPendingDecisions();
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadCaptures, loadPendingDecisions, loadResearchItems, loadSources, researchKind, researchStatus, view]);

  // A link that named a research item opens its dossier once, whichever view
  // the query asked for: the item stays selected while the operator reads a
  // thread, so coming back to Research does not lose their place.
  useEffect(() => {
    const item = initialLocation.item;
    if (!item) return;
    const timer = window.setTimeout(() => { void loadResearchItem(item); }, 0);
    return () => window.clearTimeout(timer);
  }, [initialLocation.item, loadResearchItem]);

  // The query carries the open project, thread and view so a refresh and a PWA
  // relaunch land on the same screen. It is written only once the first load
  // settled: a failed load must not erase what the operator asked for.
  useEffect(() => {
    if (loading || fatalError) return;
    writeShellLocation({ project: workspaceId, thread: threadId, item: selectedResearchItemId, view });
  }, [fatalError, loading, selectedResearchItemId, threadId, view, workspaceId]);

  useEffect(() => {
    let cursorStorage: Storage | undefined;
    try {
      cursorStorage = window.localStorage;
    } catch {
      cursorStorage = undefined;
    }
    const controller = new EventReplayController({
      client: { listEvents: (after) => clientRef.current.listEvents(after) },
      storage: cursorStorage,
      online: () => navigator.onLine,
      visible: () => document.visibilityState !== "hidden",
      onEvents: (fresh) => {
        setEvents((current) => {
          const known = new Set(current.map((event) => event.id));
          return [...current, ...fresh.filter((event) => !known.has(event.id))].slice(-500);
        });
        const currentThreadId = activeThreadId.current;
        const affectsCurrentThread = fresh.some((event) => threadRunIdsRef.current.has(invalidatedRunId(event)));
        if (currentThreadId && affectsCurrentThread) {
          void loadThread(currentThreadId).catch(() => setReplayState("error"));
        }
      },
      onState: setReplayState,
    });
    controller.start();
    const wake = () => controller.wake();
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
      controller.stop();
    };
  }, [loadThread]);

  // `runPrepared` keeps the prepared command it was given, so a retry after an
  // ambiguous outcome reuses the same prepared mutation and its key.
  const runPrepared = useCallback(async <T>(
    successText: string,
    command: PreparedMutation<T>,
    onSuccess?: (value: T) => Promise<void> | void,
    options: CommandOptions = {},
  ): Promise<T | null> => {
    setCommandPending(true);
    setNotice(null);
    try {
      const result = await command.execute();
      if (onSuccess) await onSuccess(result.value);
      if (!options.quietSuccess) setNotice({ tone: "success", text: successText });
      return result.value;
    } catch (error) {
      if (error instanceof ControlProblemError && options.onProblem?.(error)) return null;
      if (error instanceof ControlProblemError && error.problem.category === "revision_conflict") {
        const conflict = options.conflictText ?? copy.notice.reloaded;
        setNotice({ tone: "warning", text: conflict, details: noticeDetails(error, conflict) });
        if (options.onConflict) await options.onConflict().catch(() => undefined);
        else if (activeThreadId.current) await loadThread(activeThreadId.current).catch(() => undefined);
        else if (activeWorkspaceId.current) await loadWorkspace(activeWorkspaceId.current).catch(() => undefined);
      } else if (error instanceof ControlNetworkError) {
        setNotice({ tone: "warning", text: copy.errors.unconfirmed, details: noticeDetails(error, copy.errors.unconfirmed) });
      } else {
        const text = refusalNotice(error);
        setNotice({ tone: "error", text, details: noticeDetails(error, text) });
      }
      return null;
    } finally {
      setCommandPending(false);
    }
  }, [loadThread, loadWorkspace]);

  // Rename and archive answer with the row itself, so the row is replaced
  // rather than the whole project reloaded. A refusal rereads the row instead
  // of decoding whatever the refusal carried.
  const runRowCommand = useCallback(async <T extends { id: string }>(
    command: PreparedMutation<T>,
    apply: (row: T) => void,
    reload: () => Promise<void>,
  ): Promise<boolean> => {
    setCommandPending(true);
    setNotice(null);
    try {
      const result = await command.execute();
      apply(result.value);
      return true;
    } catch (error) {
      if (error instanceof ControlProblemError) {
        if (ROW_RELOAD_CATEGORIES.includes(error.problem.category)) await reload().catch(() => undefined);
        const text = refusalNotice(error);
        setNotice({ tone: "error", text, details: noticeDetails(error, text) });
      } else if (error instanceof ControlNetworkError) {
        setNotice({ tone: "error", text: copy.errors.unconfirmed, details: noticeDetails(error, copy.errors.unconfirmed) });
      } else {
        setNotice({ tone: "error", text: copy.errors.refused, details: noticeDetails(error, copy.errors.refused) });
      }
      return false;
    } finally {
      setCommandPending(false);
    }
  }, []);

  const applyWorkspaceRow = useCallback((row: Workspace) => {
    setWorkspaceRows((items) => items.map((item) => item.id === row.id ? row : item));
  }, []);
  const applyThreadRow = useCallback((row: Thread) => {
    setThreadRows((items) => items.map((item) => item.id === row.id ? row : item));
  }, []);
  const reloadWorkspaceRow = useCallback(async (id: string) => {
    applyWorkspaceRow(await clientRef.current.getWorkspace(id));
  }, [applyWorkspaceRow]);
  const reloadThreadRow = useCallback(async (id: string) => {
    applyThreadRow(await clientRef.current.getThread(id));
  }, [applyThreadRow]);

  const selectThread = useCallback((id: string | null) => {
    selectionGeneration.current += 1;
    activeThreadId.current = id;
    setThreadId(id);
    clearThreadDetail();
    if (id === null) return;
    setView("thread");
    void loadThread(id).catch((error) => setNotice(failedRead(error)));
  }, [clearThreadDetail, loadThread]);

  const selectWorkspace = useCallback((id: string) => {
    setView("thread");
    selectionGeneration.current += 1;
    activeThreadId.current = null;
    activeWorkspaceId.current = id;
    setWorkspaceId(id);
    setThreadRows([]);
    setThreadId(null);
    clearThreadDetail();
    void loadWorkspace(id).catch((error) => setNotice(failedRead(error)));
  }, [clearThreadDetail, loadWorkspace]);

  // A thread named from outside the open project -- the Inbox lists pending
  // decisions across every project -- is opened by switching the project and
  // landing on that thread in ONE load. Calling `selectWorkspace` and then
  // `selectThread` would race: the project load picks its own thread after it
  // resolves, and that pick would land last and win.
  const openThread = useCallback(async (id: string): Promise<boolean> => {
    let target: Thread;
    try {
      target = await clientRef.current.getThread(id);
    } catch {
      setNotice({ tone: "warning", text: copy.notice.goneThread });
      return false;
    }
    if (target.workspace_id === activeWorkspaceId.current) {
      selectThread(id);
      return true;
    }
    const owner = workspaceRows.find((item) => item.id === target.workspace_id);
    if (!owner) {
      setNotice({ tone: "warning", text: copy.notice.unknownProject });
      return false;
    }
    // The engine's own projects are hidden until Status turns them on, so
    // opening one behind the operator's back would put the shell in a project
    // its own picker refuses to list.
    if (owner.engine_owned && !showEngine) {
      setNotice({ tone: "warning", text: copy.notice.engineProject });
      return false;
    }
    selectionGeneration.current += 1;
    activeWorkspaceId.current = owner.id;
    activeThreadId.current = null;
    setWorkspaceId(owner.id);
    setThreadRows([]);
    setThreadId(null);
    clearThreadDetail();
    setView("thread");
    try {
      await loadWorkspace(owner.id, id);
    } catch {
      setNotice({ tone: "warning", text: copy.notice.goneThread });
      return false;
    }
    return true;
  }, [clearThreadDetail, loadWorkspace, selectThread, showEngine, workspaceRows]);

  const selectView = useCallback((next: ShellView) => { setView(next); }, []);

  const createWorkspace = useCallback((title: string) => runPrepared(
    copy.notice.projectCreated,
    clientRef.current.prepareCreateWorkspace(title),
    async (created) => { await loadWorkspaces(created.id); },
  ), [loadWorkspaces, runPrepared]);

  const renameWorkspace = useCallback((target: Workspace, title: string) => runRowCommand(
    clientRef.current.prepareRenameWorkspace(target, title),
    applyWorkspaceRow,
    () => reloadWorkspaceRow(target.id),
  ), [applyWorkspaceRow, reloadWorkspaceRow, runRowCommand]);

  const createThread = useCallback(async (title: string) => {
    if (!workspace) return null;
    return runPrepared(
      copy.notice.threadCreated,
      clientRef.current.prepareCreateThread(workspace, title),
      async (created) => { await loadWorkspace(workspace.id, created.id); },
    );
  }, [loadWorkspace, runPrepared, workspace]);

  const renameThread = useCallback((target: Thread, title: string) => runRowCommand(
    clientRef.current.prepareRenameThread(target, title),
    applyThreadRow,
    () => reloadThreadRow(target.id),
  ), [applyThreadRow, reloadThreadRow, runRowCommand]);

  const archiveThread = useCallback(async (target: Thread) => {
    const archived = await runRowCommand(
      clientRef.current.prepareArchiveThread(target),
      applyThreadRow,
      () => reloadThreadRow(target.id),
    );
    // Archiving the open thread leaves the project open with nothing selected:
    // the shell never keeps a thread it just took off the list.
    if (archived && activeThreadId.current === target.id) selectThread(null);
    return archived;
  }, [applyThreadRow, reloadThreadRow, runRowCommand, selectThread]);

  const unarchiveThread = useCallback((target: Thread) => runRowCommand(
    clientRef.current.prepareUnarchiveThread(target),
    applyThreadRow,
    () => reloadThreadRow(target.id),
  ), [applyThreadRow, reloadThreadRow, runRowCommand]);

  const selectRun = useCallback((id: string) => {
    const selected = runHistoryRef.current.find((item) => item.id === id);
    if (!selected || id === selectedRunIdRef.current) return;
    const generation = selectionGeneration.current + 1;
    selectionGeneration.current = generation;
    selectedRunIdRef.current = id;
    setSelectedRunId(id);
    setRun(selected);
    setResearch(null);
    setDecisions([]);
    void loadResearch(id, generation);
  }, [loadResearch]);

  const loadOlderRuns = useCallback(() => {
    const openThreadId = activeThreadId.current;
    if (!openThreadId || !nextRunCursor) return;
    void clientRef.current.listRuns(openThreadId, nextRunCursor).then((envelope) => {
      if (activeThreadId.current !== openThreadId) return;
      setRuns((current) => {
        const known = new Set(current.map((item) => item.id));
        const merged = [...current, ...envelope.items.filter((item) => !known.has(item.id))];
        runHistoryRef.current = merged;
        threadRunIdsRef.current = new Set(merged.map((item) => item.id));
        return merged;
      });
      setNextRunCursor(envelope.next_cursor);
    }).catch(() => setReplayState("error"));
  }, [nextRunCursor]);

  const createRun = useCallback(async () => {
    if (!thread) return;
    await runPrepared(
      copy.notice.working,
      clientRef.current.prepareCreateRun(thread),
      async (created) => {
        selectedRunIdRef.current = created.id;
        setSelectedRunId(created.id);
        setRun(created);
        await loadThread(thread.id);
      },
      {
        // V-2: the API refuses a run on a thread with nothing to answer rather
        // than committing one nothing will pick up. Say what to do about it.
        onProblem: (error) => {
          if (error.problem.category !== "thread_has_no_user_message") return false;
          setNotice({ tone: "warning", text: NO_TURN_TO_DRIVE_NOTICE });
          return true;
        },
      },
    );
  }, [loadThread, runPrepared, thread]);

  const runAction = useCallback(async (action: RunActionName) => {
    if (!run || !thread) return;
    await runPrepared(
      RUN_ACTION_NOTICE[action],
      clientRef.current.prepareRunAction(run, action),
      async (updated) => { setRun(updated); await loadThread(thread.id); },
    );
  }, [loadThread, run, runPrepared, thread]);

  const resolveDecision = useCallback(async (decision: Decision, choice: string) => {
    const openThreadId = activeThreadId.current;
    if (!openThreadId) return;
    const reload = async () => { await loadThread(openThreadId); };
    // Spec section 5.4: a decision that moved under the operator is refreshed
    // and says so in its own words, not the row-conflict sentence.
    const conflict = { conflictText: copy.decision.changed };
    const sourceGate = research?.source_gates.find((gate) => gate.decision?.id === decision.id);
    if (sourceGate) {
      await runPrepared(copy.notice.answerRecorded, clientRef.current.prepareResolveSourceIntent(sourceGate, choice), reload, conflict);
      return;
    }
    await runPrepared(copy.notice.answerRecorded, clientRef.current.prepareResolveDecision(decision, choice), reload, conflict);
  }, [loadThread, research, runPrepared]);

  const messageCommitted = useCallback((message: Message, replayed: boolean) => {
    const openThreadId = activeThreadId.current;
    if (!openThreadId) return;
    setMessages((current) => current.some((item) => item.id === message.id) ? current : [...current, message]);
    void clientRef.current.getThread(openThreadId)
      .then((fresh) => setThreadRows((items) => items.map((item) => item.id === fresh.id ? fresh : item)))
      .catch(() => undefined);
    if (replayed) setNotice({ tone: "success", text: copy.notice.alreadySaved });
  }, []);

  const runCreated = useCallback((created: Run) => {
    selectedRunIdRef.current = created.id;
    setSelectedRunId(created.id);
    setRun(created);
    const openThreadId = activeThreadId.current;
    if (openThreadId) void loadThread(openThreadId).catch(() => undefined);
  }, [loadThread]);

  // Capture is the default gesture, and approval is a separate command that
  // only runs once the capture itself is saved: one operator gesture, two
  // recorded decisions, never a combined endpoint that would record one.
  const capture = useCallback(async (payload: string, note: string, approveNow: boolean): Promise<boolean> => {
    const created = await runPrepared(
      copy.notice.savedToInbox,
      clientRef.current.prepareCreateCapture({ payload, note }),
      // The approval runs inside the create's success continuation, so
      // whichever attempt finally delivers the capture carries the second
      // decision with it. A row left waiting is invisible to the consumer.
      async (created) => {
        setSelectedCaptureId(created.id);
        await loadCaptures();
        if (!approveNow) return;
        await runPrepared(
          copy.notice.savedAndApproved,
          clientRef.current.prepareApproveCapture(created),
          async () => { await loadCaptures(); },
          { onConflict: () => loadCaptures() },
        );
      },
      {
        quietSuccess: approveNow,
        onConflict: () => loadCaptures(),
        onProblem: (error) => {
          if (error.problem.category !== "already_captured") return false;
          let current: Capture | null = null;
          try {
            current = alreadyCapturedCurrent(error);
          } catch {
            return false;
          }
          if (!current) return false;
          setNotice({ tone: "warning", text: label.alreadyCaptured(current.state) });
          // The notice says a row exists; this is what says WHICH one, so the
          // operator does not have to find it by eye.
          setSelectedCaptureId(current.id);
          setView("inbox");
          void loadCaptures();
          return true;
        },
      },
    );
    // The composer clears itself on `true` and keeps what the operator typed
    // on `false`, so a refused submission is never lost to the screen.
    return created !== null;
  }, [loadCaptures, runPrepared]);

  const decideCapture = useCallback(async (target: Capture, action: CaptureActionName) => {
    const command = action === "approve"
      ? clientRef.current.prepareApproveCapture(target)
      : action === "dismiss"
        ? clientRef.current.prepareDismissCapture(target)
        : clientRef.current.prepareReopenCapture(target);
    await runPrepared(
      CAPTURE_ACTION_NOTICE[action],
      command,
      async () => { await loadCaptures(); },
      { onConflict: () => loadCaptures() },
    );
  }, [loadCaptures, runPrepared]);

  const refreshCaptures = useCallback(() => {
    void loadCaptures();
    void loadPendingDecisions();
  }, [loadCaptures, loadPendingDecisions]);

  const refreshSources = useCallback(() => { void loadSources(); }, [loadSources]);

  // Another kind is another list, so the dossier that belonged to the old one
  // closes with it and paging starts again at the top.
  const selectResearchKind = useCallback((kind: ResearchItemKind) => {
    researchOffsetRef.current = 0;
    setResearchOffset(0);
    setResearchKind(kind);
    selectResearchItem(null);
  }, [selectResearchItem]);

  const selectResearchStatus = useCallback((status: string | null) => {
    researchOffsetRef.current = 0;
    setResearchOffset(0);
    setResearchStatus(status);
  }, []);

  const browseResearchItems = useCallback((offset: number) => {
    void loadResearchItems(researchKind, researchStatus, Math.max(0, offset));
  }, [loadResearchItems, researchKind, researchStatus]);

  const refreshResearchItems = useCallback(() => {
    void loadResearchItems(researchKind, researchStatus, researchOffsetRef.current);
  }, [loadResearchItems, researchKind, researchStatus]);

  // Spec R1c: the operator's own selected project is where an item's
  // conversation lives. When none is selected the command is not sent and the
  // shell says so -- it never creates a project to have somewhere to put it.
  const openResearchThread = useCallback(async (): Promise<boolean> => {
    const item = researchItem;
    if (!item || item.id !== selectedResearchItemIdRef.current) return false;
    if (!workspace) {
      setNotice({ tone: "warning", text: copy.research.chooseProject });
      return false;
    }
    if (!item.continuation_ready) {
      setNotice({ tone: "warning", text: copy.research.blocked });
      return false;
    }
    const forget = () => { retainedResearchThreads.current.delete(item.id); };
    const command = retainedResearchThreads.current.get(item.id)
      ?? clientRef.current.prepareOpenResearchThread(item, workspace);
    retainedResearchThreads.current.set(item.id, command);
    const opened = await runPrepared(
      copy.notice.researchOpened,
      command,
      async (thread) => {
        forget();
        // The navigation says its own sentence when it fails, so the success
        // line is only spoken once the thread is actually open.
        if (await openThread(thread.id)) setNotice({ tone: "success", text: copy.notice.researchOpened });
      },
      {
        quietSuccess: true,
        onConflict: async () => {
          forget();
          if (activeWorkspaceId.current) await loadWorkspace(activeWorkspaceId.current);
        },
        // A refusal is a definite answer, so the command it refused is not kept
        // for a retry; only an unconfirmed delivery is.
        onProblem: () => { forget(); return false; },
      },
    );
    return opened !== null;
  }, [loadWorkspace, openThread, researchItem, runPrepared, workspace]);

  // The whole shell, read again from the top: the answer to a load that failed
  // before there was a project or a thread to refresh. It reads the query as
  // it stands now -- the shell keeps it current -- and not the one this tab
  // opened on.
  const retry = useCallback(() => {
    void load(readShellLocation(typeof window === "undefined" ? "" : window.location.search));
  }, [load]);

  const refreshThread = useCallback(() => {
    const openThreadId = activeThreadId.current;
    if (openThreadId) void loadThread(openThreadId).catch(() => undefined);
  }, [loadThread]);

  const state = useMemo<ControlState>(() => ({
    loading,
    fatalError,
    offline,
    commandPending,
    replayState,
    dispatchGate,
    apiVersion,
    capabilities,
    view,
    showEngine,
    notice,
    workspaces,
    workspace,
    threads,
    archivedThreads,
    thread,
    loadedThreadId,
    messages,
    runs,
    nextRunCursor,
    run,
    selectedRunId,
    events,
    decisions,
    pendingDecisions,
    research,
    lastTurnOutcome,
    captures,
    selectedCaptureId,
    capturesLoading,
    capturesError,
    sources,
    sourcesLoading,
    sourcesError,
    sourceDetail,
    sourceDetailError,
    selectedSourceId,
    researchKind,
    researchStatus,
    researchItems,
    researchTotal,
    researchLimit,
    researchOffset,
    researchListLoading,
    researchListError,
    selectedResearchItemId,
    researchItem,
    researchItemLoading,
    researchItemError,
  }), [
    apiVersion, archivedThreads, capabilities, captures, capturesError, capturesLoading, commandPending,
    decisions, dispatchGate, events, fatalError, lastTurnOutcome, loadedThreadId, loading, messages,
    nextRunCursor, notice, offline, pendingDecisions, replayState, research, researchItem, researchItemError,
    researchItemLoading, researchItems, researchKind, researchLimit, researchListError, researchListLoading,
    researchOffset, researchStatus, researchTotal, run, runs, selectedCaptureId, selectedResearchItemId,
    selectedRunId, selectedSourceId, showEngine, sourceDetail, sourceDetailError, sources, sourcesError,
    sourcesLoading, thread, threads, view, workspace, workspaces,
  ]);

  const actions = useMemo<ControlActions>(() => ({
    setView: selectView,
    setShowEngine,
    dismissNotice: () => setNotice(null),
    selectWorkspace,
    createWorkspace,
    renameWorkspace,
    selectThread,
    openThread,
    createThread,
    renameThread,
    archiveThread,
    unarchiveThread,
    selectRun,
    loadOlderRuns,
    createRun,
    runAction,
    resolveDecision,
    retainTurn,
    takeRetainedTurn,
    messageCommitted,
    runCreated,
    turnOutcome: setLastTurnOutcome,
    capture,
    decideCapture,
    refreshCaptures,
    selectSource,
    refreshSources,
    selectResearchKind,
    selectResearchStatus,
    selectResearchItem,
    browseResearchItems,
    refreshResearchItems,
    openResearchThread,
    refreshThread,
    retry,
  }), [
    archiveThread, browseResearchItems, capture, createRun, createThread, createWorkspace, decideCapture,
    loadOlderRuns, messageCommitted, openResearchThread, openThread, refreshCaptures, refreshResearchItems,
    refreshSources, refreshThread, renameThread, renameWorkspace, resolveDecision, retainTurn, retry,
    runAction, runCreated, selectResearchItem, selectResearchKind, selectResearchStatus, selectRun,
    selectSource, selectThread, selectView, selectWorkspace, takeRetainedTurn, unarchiveThread,
  ]);

  return [state, actions];
}
