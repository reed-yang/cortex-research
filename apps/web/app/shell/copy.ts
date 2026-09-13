// Every word the shell says, in one file, so spec section 7 can be audited by
// reading it. Product chrome may not carry Control's own vocabulary or a raw
// id; the two exceptions are the Status view, which reports the system as the
// system names it, and the `details` group below, whose strings are rendered
// only inside a `[data-details]` disclosure.
export const copy = Object.freeze({
  app: { title: "Cortex" },
  sidebar: {
    newThread: "New thread",
    search: "Search threads…",
    research: "Research",
    library: "Library",
    inbox: "Inbox",
    status: "Status",
    chooseProject: "Choose a project",
    newProject: "New project…",
    renameProject: "Rename project…",
    projectName: "Project name",
    // The rail itself, its drawer trigger and the group of destinations.
    navigation: "Projects and threads",
    openNavigation: "Open navigation",
    global: "Global",
    archived: "Archived",
    // Stands in for the title of a thread that has none, inside a button's
    // accessible name.
    untitledThread: "thread",
    // The two dialogs the project menu opens.
    createProjectTitle: "New project",
    createProjectSubmit: "Create",
    renameProjectTitle: "Rename project",
    renameProjectSubmit: "Save",
    projectNameHint: "Name this project so you can find it again later.",
    cancel: "Cancel",
  },
  thread: {
    noThread: "Pick a thread or start a new one",
    archived: "Archived",
    unarchive: "Unarchive",
    placeholderChat: "Message Cortex…",
    placeholderResearch: "Ask a research question…",
    modeChat: "Chat",
    modeResearch: "Research",
    runs: "Runs",
    outputs: "Outputs",
    loadOlder: "Load older",
    region: "Thread",
    untitled: "No thread selected",
    mode: "Conversation mode",
    runHistory: "Run history",
  },
  strip: {
    offline: "Offline. Reconnecting…",
    starting: "Starting…",
    gateClosed: "Saved. Runtime dispatch is off, so nothing will answer until it is enabled.",
    gateUnknown: "Saved. This daemon does not report whether dispatch is enabled.",
    working: "Working…",
    paused: "Paused",
    waiting: "Waiting for your decision",
    resuming: "Resuming…",
    failed: "The last turn failed.",
    details: "Details",
    pause: "Pause",
    resume: "Resume",
    cancel: "Cancel",
    retry: "Retry",
    // The thread header owns the Unarchive button, so the line only says what
    // the state is and how it ends.
    archivedThread: "This thread is archived. Unarchive it to continue.",
    turnStarted: "Your message was sent and a run started.",
    // An answer whose own wording carries a code or an id is not shown; the
    // wire sentence stays under the disclosure.
    unspokenOutcome: "Cortex answered the last turn; the wording is under Details.",
  },
  // What a refusal says before the reason: the thing that did not happen.
  leads: {
    turnFailed: "The last turn failed",
    messageNotSent: "Your message was not sent",
    messageSavedNoRun: "Your message was saved, but no run started",
  },
  decision: {
    title: "Decision",
    changed: "This decision changed. Review it again.",
    details: "Details",
    fallbackKind: "Decision",
  },
  inbox: {
    capturePlaceholder: "Paste a link or write a note",
    notePlaceholder: "Why it matters (optional)",
    approveNow: "Approve now",
    capture: "Capture",
    decisions: "Decisions",
    captures: "Captures",
    open: "Open",
    approve: "Approve",
    dismiss: "Dismiss",
    reopen: "Reopen",
    title: "Inbox",
    subtitle: "What is waiting on you, across every project.",
    payloadLabel: "Capture payload",
    noteLabel: "Capture note",
    pendingDecision: "Pending decision",
    noDecisions: "No decision is waiting for you.",
    inAnotherThread: "In another thread",
    refresh: "Refresh",
    loadingCaptures: "Reading your captures…",
    refreshingCaptures: "Refreshing…",
    capturesUnreadable: "Your captures could not be read. Try again.",
    noCaptures: "Nothing has been captured yet.",
    threadNotOpened: "That thread could not be opened.",
    threadNotOpenedRetry: "That thread could not be opened. Try again.",
    details: "Details",
  },
  capture: {
    label: "Capture",
    link: "Link",
    note: "Note",
    maybeKnown: "May already be in your library",
    blocked: "A run still holds this capture. It can be decided once that run ends.",
    failed: "Cortex could not finish with this one.",
    importedOne: "Imported into your library.",
    reopenExplanation:
      "Reopening puts this capture back in the approved queue and accepts that the reader that lost it may already have imported it once.",
    confirmReopen: "Confirm reopen",
    cancelReopen: "Cancel reopen",
    details: "Details",
  },
  // What each capture state is called for the operator, in the store's own
  // order.
  captureGroups: {
    pending: "Waiting for you",
    approved: "Approved",
    claimed: "Being read",
    uncertain: "Needs a second look",
    consumed: "Imported",
    dismissed: "Dismissed",
    failed: "Failed",
  },
  // R1c: the catalog of the operator's own research — the ideas, explorations
  // and projects that already exist — and the dossier one of them opens. The
  // product workspace stays a container for conversations; nothing here renames
  // an original research identity into a project title.
  research: {
    title: "Research",
    subtitle: "Your ideas, explorations and projects, as they were left.",
    kinds: "Research kinds",
    ideas: "Ideas",
    explorations: "Explorations",
    projects: "Projects",
    idea: "Idea",
    exploration: "Exploration",
    project: "Project",
    search: "Search this page…",
    searchLabel: "Search the items on this page",
    status: "Status",
    anyStatus: "Any status",
    list: "Research items",
    loading: "Reading your research…",
    empty: "Nothing of this kind is here.",
    noMatches: "Nothing on this page matches what you typed.",
    unreadable: "Your research could not be read. Try again.",
    retry: "Retry",
    refresh: "Refresh",
    previous: "Previous",
    next: "Next",
    pick: "Pick a research item to open its dossier.",
    dossierLoading: "Opening the dossier…",
    dossierUnreadable: "That research item could not be read.",
    paused: "Stopped because",
    rounds: "Rounds",
    updated: "Last activity",
    noActivity: "No activity is recorded.",
    summary: "Summary",
    noSummary: "This item carries no summary.",
    documents: "Documents",
    documentList: "Registered documents",
    noDocuments: "No document is registered for this item yet.",
    documentLoading: "Reading the document…",
    documentUnavailable: "That document could not be read. It may not have been adopted yet.",
    // What is on screen is the part the operator is allowed to read, and Copy
    // source copies exactly that: the line says so rather than letting a
    // shortened document look like the whole one.
    documentRedacted: "Some private details are omitted. What you see here is what gets copied.",
    history: "History",
    noHistory: "No history is recorded for this item.",
    open: "Open research conversation",
    opening: "Opening…",
    openHint: "Opens this item's conversation, or reuses the one it already has. Nothing runs until you ask.",
    askHint: "In that conversation, send /research and your question.",
    chooseProject: "Pick a project first, so the conversation has somewhere to live.",
    blocked: "This item cannot be continued yet.",
    telegram: "Telegram command",
    telegramHint: "Send this in your bot conversation to work on the same item there.",
    copyCommand: "Copy command",
    copied: "Copied",
    details: "Details",
  },
  library: {
    title: "Library",
    pick: "Pick a source to read it",
    retry: "Retry",
    sources: "Adopted sources",
    empty: "No source has been adopted into this Cortex yet.",
    details: "Details",
  },
  source: {
    loading: "Loading the source record…",
    pick: "Select a source to read its public record.",
    details: "Details",
    aliases: "Aliases",
    noAliases: "No alias is recorded for this source.",
    unreadable: "That source record could not be read.",
    added: "Added",
    updated: "Updated",
  },
  status: {
    apiVersion: "API version",
    dispatch: "Runtime dispatch",
    live: "Live updates",
    showEngine: "Show engine projects and threads",
    title: "Status",
    subtitle: "What this Cortex reports about itself.",
    unknown: "unknown",
    enabled: "enabled",
    disabled: "disabled",
    // What the update stream is doing, for each state the replay controller
    // can report (app/control/event-replay.ts). Status speaks of the system,
    // but it still speaks: the token itself is not a sentence.
    replay: {
      idle: "Nothing is being followed right now.",
      polling: "Updates are checked on a timer.",
      offline: "Not connected.",
      reconnecting: "Reconnecting.",
      error: "Updates could not be followed.",
    },
    capabilitiesTitle: "Capabilities",
    noCapabilities: "This daemon reports no capabilities.",
    available: "is available.",
    unavailable: "is not available.",
    // What each capability the daemon reports means for the operator. The
    // daemon's key is its own vocabulary -- sentence-casing it would spell
    // words product chrome may not carry -- so every key it actually reports
    // (cortex_platform/product/api/app.py) has a sentence of its own, in both
    // directions.
    capabilities: {
      control_store: "Threads and runs are stored on this daemon.",
      event_replay: "Missed events are recovered after a reconnect.",
      event_stream: "Updates arrive as they happen.",
      source_resolution: "A source can be identified before it is imported.",
      artifact_metadata: "A run's outputs can be read back.",
      research_pipeline: "Research turns can run here.",
      runtime_dispatch: "This build can answer a turn it created.",
      telegram_adapter: "Telegram messages reach this Cortex.",
    } as Record<string, string | undefined>,
    capabilitiesOff: {
      control_store: "Threads and runs are not stored on this daemon.",
      event_replay: "Missed events are not recovered after a reconnect.",
      event_stream: "Updates do not arrive as they happen.",
      source_resolution: "A source cannot be identified before it is imported.",
      artifact_metadata: "A run's outputs cannot be read back.",
      research_pipeline: "Research turns cannot run here.",
      runtime_dispatch: "This build cannot answer a turn it created.",
      telegram_adapter: "Telegram messages do not reach this Cortex.",
    } as Record<string, string | undefined>,
    // A key with no sentence and nothing left to say once Control's own words
    // are dropped from it.
    unnamedCapability: "A capability this app cannot name",
  },
  errors: {
    unconfirmed: "Delivery is unconfirmed. Refresh before retrying.",
    unreadable: "Cortex returned something this client cannot read.",
    refused: "Cortex refused this action.",
    unavailable: "Cortex is unavailable.",
    // The sentence a refusal ends with when its own code may not be spoken.
    underDetails: "The reason is under Details.",
  },
  // ⟦P8 V-R2 / ADJ-G-3⟧ Whose thread it is, and the half of that fact the
  // operator's own run on a carrier thread needs. `assistant-adapter.ts`
  // re-exports both under the names its callers already use.
  engine: {
    thread: "This thread belongs to the research engine; its runs are created by the engine only.",
    noNewRun: "This thread belongs to the research engine; no new run can be started on it.",
  },
  // What the shell says after a command it sent on the operator's behalf.
  notice: {
    region: "Notice",
    // Named apart from the capture action of the same verb: one line dismisses
    // a message, the other decides a row.
    dismiss: "Dismiss notice",
    projectCreated: "Project created.",
    threadCreated: "Thread created.",
    working: "Cortex is working on this turn.",
    answerRecorded: "Answer recorded.",
    savedToInbox: "Saved to the inbox.",
    savedAndApproved: "Saved to the inbox and approved.",
    paused: "Paused.",
    resumed: "Resumed.",
    canceled: "Canceled.",
    retrying: "Retrying.",
    approved: "Approved.",
    dismissed: "Dismissed.",
    reopened: "Reopened.",
    alreadySaved: "That message was already saved. Cortex did not add it twice.",
    unknownProject: "That project is not here any more. Cortex opened the first one instead.",
    unknownThread: "That thread is not in this project any more, so nothing is open.",
    goneThread: "That thread could not be opened. It may not be here any more.",
    engineProject: "That thread belongs to a research engine project. Turn on engine projects in Status to open it.",
    researchOpened: "This item's conversation is open. Send /research and your question.",
    unknownResearchItem: "That research item is not here any more, so nothing is open.",
    reloaded: "This changed somewhere else. Cortex reloaded it instead of overwriting it.",
  },
  // Rendered only inside a `[data-details]` disclosure, which is where a code,
  // an identifier and Control's own nouns are allowed to appear.
  details: {
    decisionId: "Decision id",
    runId: "Run id",
    attemptId: "Attempt id",
    raisedAt: "Raised at",
    captureId: "Capture id",
    state: "State",
    revision: "Revision",
    created: "Created",
    failure: "Failure",
    blockedBy: "Blocked by",
    corpusHint: "Corpus hint",
    importedAs: "Imported as",
    stage: (stage: string) => `Stage ${stage}.`,
    category: (category: string) => `Category ${category}.`,
    identifier: "identifier",
    authority: "authority",
    authorityId: "authority id",
    canonicalId: "canonical id",
    sourceKind: "source kind",
    importState: "import state",
    sourceRevision: "revision",
    sourceCreated: "created",
    sourceUpdated: "updated",
    itemId: "research item id",
    originId: "original identity",
    itemKind: "kind",
    itemStatus: "status",
    threadId: "thread id",
    documentId: "document id",
    documentVersionId: "document version id",
    documentVersion: "version",
    documentDigest: "digest",
    documentBytes: "bytes",
    documentShownBytes: "bytes shown",
    documentRetainedBytes: "bytes retained",
  },
});

// The refusals that have a sentence of their own. Everything else falls back
// to `errors.refused`, with the code itself left for a Details disclosure.
const PROBLEMS: Record<string, string> = {
  revision_conflict: "Someone else changed this first. It has been refreshed; try again.",
  machine_thread: "This thread belongs to the research engine.",
  machine_workspace: "This project belongs to the research engine.",
  machine_run: "This run belongs to the research engine.",
  // Raised by archive AND by a second run on the same thread, so the sentence
  // names the thing to do and not one of the two callers.
  thread_active_run: "Finish or cancel the running turn first.",
  // Control refuses the write too, so the refusal says exactly what the strip
  // already says about an archived thread.
  thread_archived: copy.strip.archivedThread,
  thread_has_no_user_message: "Send a message first.",
  managed_worker_unavailable: "No worker is available to run this turn.",
  already_captured: "This is already in your captures.",
  pause_unsupported: "This worker cannot pause.",
};

// What a refusal category says to the operator. An unknown one -- or one whose
// own words are Control's -- says only that Cortex refused, and the code stays
// under Details.
export function problemSentence(category: string): string {
  return PROBLEMS[category] ?? copy.errors.refused;
}

// The vocabulary product chrome may not carry (spec section 7), as ONE list:
// every guard that filters a wire string before it reaches the screen, and the
// audit that checks the screen afterwards, run this regex. Stemmed, because
// the daemon writes `deduplication` where the spec wrote `deduplicated`; but
// `rev` stays exact so `review`, `reverse` and `revert` are ordinary words,
// and `CAS` stays exact so `cast` and `broadcast` are too.
export const BANNED_WORDS =
  /\b(rev|revision\w*|CAS|compare-and-swap|DTO|replay\w*|cursor\w*|dedup\w*|idempoten\w*|Control API|durab\w*)\b/i;

// A refusal code in the words a person reads, or nothing when the code is
// Control's own vocabulary. Used where a code is a phrase in a longer line --
// a failed run's row -- rather than a sentence of its own.
export function humanCategory(code: string): string | null {
  const words = code.replace(/[_-]+/g, " ").trim();
  if (!words || BANNED_WORDS.test(words)) return null;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// What did not happen, then why, then what to do -- with the code itself left
// for the Details disclosure the caller renders.
export function refusalText(lead: string, code: string): string {
  const sentence = PROBLEMS[code];
  if (sentence) return `${lead}. ${sentence}`;
  const phrase = humanCategory(code);
  return phrase ? `${lead}: ${phrase}.` : `${lead}. ${copy.errors.underDetails}`;
}

// Every state a run can be in, as `_RUN_TRANSITIONS` in the Control store
// enumerates them. An unknown one is spaced rather than shown as a token.
const RUN_STATES: Record<string, string> = {
  queued: "Queued",
  starting: "Starting",
  running: "Working",
  paused: "Paused",
  waiting_for_decision: "Waiting for you",
  resuming: "Resuming",
  retrying: "Retrying",
  completed: "Completed",
  failed: "Failed",
  canceled: "Canceled",
  cancel_requested: "Canceling",
  pause_requested: "Pausing",
};

export function runStateLabel(state: string): string {
  return RUN_STATES[state] ?? state.replaceAll("_", " ");
}

// The kinds Control raises are system tokens (`source_conflict`,
// `source_confirmation`). An unknown one is spaced and sentence-cased rather
// than shown raw, so a new kind reads as English on the day it ships.
const DECISION_KINDS: Record<string, string> = {
  approval: "Approval",
  source_conflict: "Source conflict",
  source_confirmation: "Confirm this source",
};

export function decisionKindLabel(kind: string): string {
  const known = DECISION_KINDS[kind];
  if (known) return known;
  const words = kind.replaceAll("_", " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : copy.decision.fallbackKind;
}

// The six statuses the research database persists, in the words a person
// reads. A status outside the six is spaced and sentence-cased rather than
// renamed: the catalog shows what the backend truthfully holds, and killed and
// aborted stay terminal rather than being softened into a pause.
const RESEARCH_STATUSES: Record<string, string> = {
  incubating: "Incubating",
  graduated: "Graduated",
  killed: "Killed",
  aborted: "Aborted",
  dormant: "Dormant",
  awaiting_human: "Waiting for you",
};

export function researchStatusLabel(status: string): string {
  const known = RESEARCH_STATUSES[status];
  if (known) return known;
  const words = status.replaceAll("_", " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : copy.research.status;
}

// What one item is, in the singular, for the row and the dossier badge.
const RESEARCH_KINDS: Record<string, string> = {
  idea: copy.research.idea,
  exploration: copy.research.exploration,
  project: copy.research.project,
};

export function researchKindLabel(kind: string): string {
  return RESEARCH_KINDS[kind] ?? kind;
}

// The few lines that name a row the shell is talking about. They are here, and
// not inline, so the audit sees the whole sentence and not just its halves.
export const label = Object.freeze({
  unarchiveThread: (title: string) => `Unarchive ${title}`,
  inThread: (title: string) => `In ${title}`,
  option: (position: number) => `Option ${position}`,
  unsupportedOption: (position: number) => `Unsupported option ${position}`,
  importedAsMany: (count: number) => `Imported into your library as ${count} sources.`,
  // A reported capability as a sentence: the one written for it, or -- for a
  // key this app has never seen -- the key itself with the words product
  // chrome may not carry dropped before it is read out.
  capability: (name: string, available: boolean) => {
    const known = available ? copy.status.capabilities[name] : copy.status.capabilitiesOff[name];
    if (known) return known;
    const words = name.split(/[_-]+/).filter((word) => word && !BANNED_WORDS.test(word)).join(" ");
    const head = words ? words.charAt(0).toUpperCase() + words.slice(1) : copy.status.unnamedCapability;
    return `${head} ${available ? copy.status.available : copy.status.unavailable}`;
  },
  alreadyCaptured: (state: string) => `This is already in the inbox as ${state}. Cortex kept the one already there.`,
  // Which slice of the catalog is on screen, so paging says where the operator
  // is rather than only offering to move.
  researchPage: (first: number, last: number, total: number) => `${first}–${last} of ${total}`,
  researchDocument: (title: string, version: number) => `${title} · version ${version}`,
  // The bot command that selects the SAME item in the operator's own bound
  // conversation. The identity is the item's, so it is rendered only inside a
  // details disclosure, where an id is allowed; it confers no authorization of
  // its own.
  researchItemCommand: (id: string) => `/research-item ${id}`,
});
