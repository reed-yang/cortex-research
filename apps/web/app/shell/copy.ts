// Every word the shell says, in one file, so spec section 7 can be audited by
// reading it. Product chrome may not carry Control's own vocabulary or a raw
// id; the two exceptions are the Status view, which reports the system as the
// system names it, and the `details` group below, whose strings are rendered
// only inside a `[data-details]` disclosure.
export const copy = Object.freeze({
  app: { title: "Cortex" },
  // The draggable boundaries between the rail, a list and its reader.
  layout: { resizeHint: "Drag to resize. Double-click to reset." },
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
    resize: "Resize the sidebar",
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
    capturePlaceholder: "Paste an arXiv link or id",
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
    // Saving an idea is its own gesture: it keeps the text as written and
    // starts nothing, unlike a source, which Cortex imports once approved.
    ideaTitle: "Save an idea",
    sourceTitle: "Add a source (arXiv)",
    ideaLabel: "Idea text",
    ideaPlaceholder: "Write the idea as it is. Saving it starts nothing.",
    ideaNoteLabel: "Idea note",
    ideaNotePlaceholder: "Context for later (optional)",
    saveIdea: "Save idea",
    ideas: "Ideas",
    refreshIdeas: "Refresh ideas",
    loadingIdeas: "Reading your ideas…",
    ideasUnreadable: "Your ideas could not be read. Try again.",
    noIdeas: "No idea has been saved yet.",
    // The XHS recommendations the weekly review left to the operator. The
    // section is shown only when one is waiting or the list could not be read.
    xhsTitle: "XHS recommendations",
    xhsIntro: "The weekly review could not decide these. Open the note to import, fix or exclude each one.",
    xhsItem: "XHS recommendation",
    xhsUnreadable: "The XHS recommendations waiting for you could not be read.",
    xhsUntitledNote: "In an untitled note",
  },
  fragment: {
    label: "Idea",
    fromTelegram: "From Telegram",
    savedHere: "Saved here",
    note: "Note",
    details: "Details",
  },
  capture: {
    label: "Capture",
    link: "Link",
    note: "Note",
    maybeKnown: "May already be in your library",
    blocked: "A run still holds this capture. It can be decided once that run ends.",
    failed: "Cortex could not finish with this one.",
    // `capability_unavailable`: the paper has no HTML version and this
    // installation has no accepted OCR skill. The paper is fine; capturing it
    // again works once OCR is ready, and the reason is on the Cortex machine.
    ocrUnavailable:
      "This paper is PDF only, and OCR is not ready on this Cortex installation. Run cortex skills status there to see why, then capture it again.",
    importedOne: "Imported into your library.",
    // The text around the one paper a submitted payload names. The operator's
    // own note keeps its place above it, unlabelled as before.
    payloadNote: "Note from submitted text",
    // A failed capture whose paper has since reached the library some other
    // way. The capture's own outcome and history do not change.
    availableInLibrary: "This paper is now in your library; this capture remains failed.",
    openSource: "Open source",
    reopenExplanation:
      "Reopening puts this capture back in the approved queue and accepts that the reader that lost it may already have imported it once.",
    confirmReopen: "Confirm reopen",
    cancelReopen: "Cancel reopen",
    details: "Details",
  },
  // What each capture state is called for the operator, shown as the badge
  // on its card.
  captureStates: {
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
    resize: "Resize the research list",
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
    resize: "Resize the source list",
    pick: "Pick a source to read it",
    retry: "Retry",
    sources: "Adopted sources",
    empty: "No source has been adopted into this Cortex yet.",
    details: "Details",
    // The kind filter; search covers papers only, and says so.
    kinds: "Source kinds",
    kindAll: "All",
    kindPapers: "Papers",
    kindBlogs: "Blogs",
    kindNotes: "XHS notes",
    searchScope: "Searches stored papers",
    emptyKind: "No source of this kind is in the library yet.",
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
    // What each kind is called on its badge; paper keeps the word it had.
    kinds: { paper: "paper", blog: "blog", xhs_note: "XHS note" } as Record<string, string | undefined>,
    notPeerReviewed: "Not peer-reviewed",
  },
  // "Recommended in": the XHS notes that recommend a paper or a blog.
  links: {
    title: "Recommended in",
    caption: "caption",
    unreadable: "The notes that recommend this source could not be read.",
  },
  // The XHS note record: who posted it, what it recommends (each with the
  // image or caption it came from), importing them, and its failed images.
  // Transcription, Identified (auto) and Caption say where each text came from.
  xhs: {
    loading: "Loading the note…",
    unreadable: "This note's recommendations and images could not be read.",
    by: "By",
    roles: { curator: "Curator", author: "Author" },
    // What each role means today: an author's notes become research evidence
    // in a later release.
    roleScope: { curator: "Library only", author: "Research evidence later" },
    published: "Published",
    permalink: "Open on Xiaohongshu",
    processing: "Cortex is still processing this note; a new version is saved when it finishes.",
    noteFailed: "Processing this note failed",
    recommendations: "Recommendations",
    noRecommendations: "No recommendation was identified in this note.",
    importSelected: "Import selected",
    importing: "Importing…",
    importNotDone: "Nothing was imported",
    importTooMany: "Select at most 100 recommendations to import at once.",
    evidence: "Evidence",
    transcription: "Transcription",
    identified: "Identified (auto)",
    caption: "Caption",
    transcriptionLoading: "Loading the transcription…",
    transcriptionUnreadable: "The transcription could not be read.",
    transcriptionMissing: "This image has no transcription in the saved copy.",
    imageMissing: "This image is not in the saved copy yet.",
    fields: { kind: "Kind", title: "Title", quote: "Quote", arxiv: "arXiv", link: "Link" },
    kinds: { paper: "paper", blog: "blog", other: "other" },
    open: "Open",
    linkLabel: "Blog link",
    saveLink: "Save link",
    linkSaved: "Link saved.",
    linkInvalid: "Enter an http or https link.",
    linkNotSaved: "Link not saved",
    // Where a recommendation's link came from, or why there is none.
    urlStates: {
      none: "No link yet",
      from_text: "Link from the note",
      auto_matched: "Link found and checked",
      unverified: "Link found, not checked",
      not_found: "No link found",
      operator_set: "Link set by you",
      failed: "Link lookup failed",
    },
    importStates: { staged: "In the inbox", importing: "Importing", imported: "Imported", failed: "Import failed" } as Record<string, string | undefined>,
    // What importing did for each selected row. A paper is staged as a
    // Capture and then approved as its own decision, so either step can fail.
    outcomes: {
      approved: "Approved; Cortex imports it next.",
      notApproved: "In the inbox but not approved",
      blogQueued: "Blog import queued.",
      refused: {
        not_found: "Not imported: this recommendation is no longer on the note.",
        already_imported: "Already imported.",
        excluded: "Not imported: this recommendation is excluded.",
        not_importable: "Not imported: only papers and blogs can be imported.",
        no_url: "Not imported: this blog has no link yet.",
        no_arxiv_id: "Not imported: this paper has no arXiv id.",
      },
    },
    failedImages: "Failed images",
    download: "download",
    ocr: "transcription",
    retry: "Retry",
    retryQueued: "Retry queued; the note is saved again when it finishes.",
    retryNotDone: "Not retried",
    // A row's review: the weekly review's reading of it, or the operator's.
    reviews: {
      excluded: "Excluded",
      corrected: "Corrected automatically",
      importReady: "Paper identified — import when ready",
      notOnArxiv: "Not on arXiv — Cortex imports arXiv papers only",
      needsDecision: "Needs your decision",
    },
    correctedFields: { kind: "kind", arxiv_id: "arXiv id", url: "link" },
    // Why a row was excluded or left to the operator, when the review itself
    // gives no sentence of its own.
    reviewReasons: {
      arxiv_link: "Its link is an arXiv paper.",
      duplicate: "Another recommendation in this note is the same paper.",
      not_on_arxiv: "This paper is not on arXiv.",
      not_a_blog: "The link is not a blog.",
      not_a_recommendation: "This is not a recommendation.",
      insufficient_evidence: "The evidence was not enough to decide.",
      conflicting_evidence: "The evidence points different ways.",
      title_mismatch: "The page found does not match the title.",
      fetch_failed: "The page could not be read.",
      outcome_unknown: "The automatic review did not report back.",
      operator: "Excluded by you.",
    } as Record<string, string | undefined>,
    exclude: "Exclude",
    excludeLabel: "Reason to exclude",
    excludePlaceholder: "Why it does not belong here",
    excludeDone: "Excluded. It is not imported until you restore it.",
    excludeNotDone: "Not excluded",
    restore: "Restore",
    restoreDone: "Restored. The weekly review leaves it to you from now on.",
    restoreNotDone: "Not restored",
  },
  // The Library reader's own words around a stored document: Preview, Source
  // and Copy source are the shared document controls' labels.
  reader: {
    loading: "Loading source content…",
    empty: "This document is empty.",
    retry: "Retry content",
    tooLarge: "This document is too large to preview, so it opens as paged source.",
    copyUnavailable: "The whole document could not be read, so Copy source is unavailable.",
    figureMissing: "Figure not in the stored copy",
    imageNotLoaded: "Image not loaded from this reference",
    // The tabs a blog and an XHS note map onto the stored files.
    article: "Article",
    notes: "Notes",
    note: "Note",
    transcription: "Transcription",
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
    // The XHS plugin's line: whether it scans, and each followed blogger's
    // last scan. Success, nothing new and a provider failure stay distinct.
    xhs: {
      title: "XHS notes",
      on: "Scanning is on.",
      offConfig: "Scanning is off: it is not enabled in the configuration.",
      offRoots: "Scanning is off: the note and blog folders are not ready.",
      offSchedule: "Scanning is off: its schedule is disabled.",
      bloggers: "Followed bloggers",
      noBloggers: "No blogger is followed.",
      unnamed: "Unnamed blogger",
      notScanned: "not scanned yet",
      unavailable: "XHS status could not be read.",
      outcomes: {
        ok: "last scan succeeded",
        no_new_notes: "last scan found no new notes",
        failed: "last scan failed",
      },
      failures: {
        auth: "credentials refused",
        payment: "payment required",
        rate_limited: "rate limited",
        transient: "temporary provider failure",
        outcome_unknown: "outcome unknown",
        upstream_error: "provider error",
        not_found: "not found",
        invalid_response: "unreadable provider answer",
        url_expired: "image link expired",
      } as Record<string, string | undefined>,
      // The weekly review of unimported recommendations: one line.
      fallback: {
        off: "Weekly review is off.",
        on: "Weekly review is on.",
        firstRun: "The first run starts when a recommendation is waiting.",
        nothingChanged: "nothing changed",
        digest: {
          pending: "Telegram summary waiting",
          sent: "Telegram summary sent",
          suppressed: "no Telegram summary needed",
          blocked: "Telegram summary not sent",
        } as Record<string, string | undefined>,
        digestReasons: {
          transport_disabled: "Telegram is not running",
          shadow: "Telegram is in shadow mode",
          recipient_unavailable: "no Telegram recipient is set up",
          recipient_ambiguous: "more than one Telegram recipient",
          web_origin_missing: "the Web address is not configured",
          outcome_unknown: "delivery unknown; check the chat",
          delivery_rejected: "Telegram refused it",
        } as Record<string, string | undefined>,
      },
    },
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
    ideaSaved: "Idea saved as written. Nothing was started.",
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
    ideaId: "Idea id",
    contextItemId: "Research item at the time",
    state: "State",
    revision: "Revision",
    created: "Created",
    failure: "Failure",
    blockedBy: "Blocked by",
    corpusHint: "Corpus hint",
    importedAs: "Imported as",
    librarySource: "Library source",
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
  // A remote image in a stored copy is never loaded; it is offered as a link
  // that says where it lives.
  remoteImage: (host: string) => `Image on ${host}`,
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
  // Where in a note a recommendation came from: one image, or the caption.
  noteEvidence: (ordinal: number | null) => (ordinal === null ? copy.links.caption : `image ${ordinal}`),
  sourceKind: (kind: string) => copy.source.kinds[kind] ?? kind.replaceAll("_", " "),
  // The XHS note record's rows: a recommendation to select, one carousel
  // image, and an image that failed with the step and the reason.
  selectRecommendation: (title: string) => `Select ${title}`,
  xhsImage: (ordinal: number) => `Image ${ordinal}`,
  xhsImageFailed: (ordinal: number, step: string, reason: string) => `Image ${ordinal}: ${step} failed (${reason})`,
  retryImage: (ordinal: number) => `Retry image ${ordinal}`,
  restoreRecommendation: (title: string) => `Restore ${title}`,
  correctedFields: (fields: string[]) => `${copy.xhs.reviews.corrected}: ${fields.join(", ")}`,
  // The Inbox's XHS rows: which note each came from, and how many wait beyond
  // the page shown.
  inNote: (title: string) => (title ? `In ${title}` : copy.inbox.xhsUntitledNote),
  xhsWaitingBeyond: (shown: number, total: number) => `Showing ${shown} of ${total}.`,
  // The weekly review's Status line, in parts.
  xhsFallbackRunning: (remaining: number, items: number) => `Weekly review is running: ${remaining} of ${items} left.`,
  xhsFallbackLast: (when: string, counts: string, digest: string | null) => `Last run ${when}: ${counts}${digest ? `; ${digest}` : ""}.`,
  xhsFallbackNext: (when: string) => `Next run after ${when}.`,
  xhsFallbackDigest: (state: string, reason: string | null) => (reason ? `${state} (${reason})` : state),
  xhsFallbackCounts: (counts: Partial<Record<string, number>>) => {
    const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
    const parts = [
      counts.blog_queued ? plural(counts.blog_queued, "blog queued", "blogs queued") : null,
      counts.paper_corrected ? plural(counts.paper_corrected, "paper corrected", "papers corrected") : null,
      counts.paper_kept ? `${counts.paper_kept} not on arXiv` : null,
      counts.excluded ? `${counts.excluded} excluded` : null,
      counts.needs_operator ? plural(counts.needs_operator, "needs your decision", "need your decision") : null,
      counts.stale ? `${counts.stale} changed meanwhile` : null,
    ].filter((part) => part !== null);
    return parts.length ? parts.join(", ") : copy.status.xhs.fallback.nothingChanged;
  },
  // One followed blogger's last scan, as the Status line says it.
  xhsScan: (name: string, when: string | null, outcome: string | null, failure: string | null) => {
    if (!when || !outcome) return `${name}: ${copy.status.xhs.notScanned}`;
    const said = copy.status.xhs.outcomes[outcome as keyof typeof copy.status.xhs.outcomes] ?? outcome.replaceAll("_", " ");
    return `${name}: ${said}${failure ? ` (${failure})` : ""} · ${when}`;
  },
  // The bot command that selects the SAME item in the operator's own bound
  // conversation. The identity is the item's, so it is rendered only inside a
  // details disclosure, where an id is allowed; it confers no authorization of
  // its own.
  researchItemCommand: (id: string) => `/research-item ${id}`,
});
