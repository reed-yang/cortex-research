import { decodeReadingsStatus, type ReadingsStatus } from "./readings-contracts";
import {
  ContractDecodeError,
  decodeCapture,
  decodeDecision,
  decodeMessage,
  decodeProblem,
  decodeRun,
  decodeRunEvent,
  decodeThread,
  decodeWorkspace,
  listDecoder,
  type Capture,
  type CaptureState,
  type Decoder,
  type Decision,
  type ListEnvelope,
  type Message,
  type Problem,
  type Run,
  type RunEvent,
  type Thread,
  type Workspace,
} from "./contracts";
import {
  decodeArtifactVersionContent,
  decodeResearchWorkflow,
  decodeRunHistory,
  decodeSource,
  decodeSourceGate,
  decodeSourceContent,
  decodeSourceSearch,
  type SourceContent,
  type SourceContentKind,
  type SourceSearch,
  type ArtifactVersionContent,
  type ResearchWorkflowProjection,
  type SourceGateProjection,
  type SourceProjection,
} from "./research-contracts";
import {
  decodeResearchDocumentContent,
  decodeResearchItemDetail,
  decodeResearchItemPage,
  type ResearchDocumentContent,
  type ResearchItemDetail,
  type ResearchItemKind,
  type ResearchItemPage,
} from "./research-items-contracts";

export class ControlProblemError extends Error {
  constructor(readonly problem: Problem) {
    super(problem.title);
    this.name = "ControlProblemError";
  }
}

export class ControlNetworkError extends Error {
  constructor(message = "Cortex Control is unreachable") {
    super(message);
    this.name = "ControlNetworkError";
  }
}

// `already_captured` carries the open row it collided with, so the omnibox can
// point at the existing capture instead of guessing. A malformed or
// lease-carrying `current` throws rather than degrading: an unvalidated row
// must not reach the inbox.
export function alreadyCapturedCurrent(error: unknown): Capture | null {
  if (!(error instanceof ControlProblemError)) return null;
  if (error.problem.category !== "already_captured" || error.problem.current === undefined) return null;
  return decodeCapture(error.problem.current, "problem.current");
}

export type MutationResult<T> = {
  value: T;
  replayed: boolean;
};

export type PreparedMutation<T> = {
  readonly idempotencyKey: string;
  execute(): Promise<MutationResult<T>>;
};

type ClientOptions = {
  basePath?: string;
  fetcher?: typeof fetch;
  idempotencyKeyFactory?: () => string;
};

function randomIdempotencyKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `web-${uuid}`;
  const bytes = new Uint8Array(24);
  globalThis.crypto.getRandomValues(bytes);
  return `web-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

export class CortexControlClient {
  private readonly basePath: string;
  private readonly fetcher: typeof fetch;
  private readonly keyFactory: () => string;

  constructor(options: ClientOptions = {}) {
    this.basePath = (options.basePath ?? "/api/cortex").replace(/\/$/, "");
    this.fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
    this.keyFactory = options.idempotencyKeyFactory ?? randomIdempotencyKey;
  }

  listWorkspaces(): Promise<ListEnvelope<Workspace>> {
    return this.read("/workspaces", listDecoder(decodeWorkspace));
  }

  getWorkspace(id: string): Promise<Workspace> {
    return this.read(`/workspaces/${encodeURIComponent(id)}`, decodeWorkspace);
  }

  listThreads(workspaceId: string, options: { includeArchived?: boolean } = {}): Promise<ListEnvelope<Thread>> {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    if (options.includeArchived) query.set("include_archived", "true");
    return this.read(`/threads?${query.toString()}`, listDecoder(decodeThread));
  }

  getThread(id: string): Promise<Thread> {
    return this.read(`/threads/${encodeURIComponent(id)}`, decodeThread);
  }

  listMessages(threadId: string): Promise<ListEnvelope<Message>> {
    return this.read(`/threads/${encodeURIComponent(threadId)}/messages`, listDecoder(decodeMessage));
  }

  listRuns(threadId: string, afterId?: string): Promise<ListEnvelope<Run>> {
    const query = afterId ? `?after_id=${encodeURIComponent(afterId)}` : "";
    return this.read(
      `/threads/${encodeURIComponent(threadId)}/runs${query}`,
      (value, path = "run_history") => decodeRunHistory(value, threadId, path),
    );
  }

  getRun(id: string): Promise<Run> {
    return this.read(`/runs/${encodeURIComponent(id)}`, decodeRun);
  }

  // P8: the runtime dispatch gate as the health payload reports it -- what
  // decides whether a queued run is driven now or kept for when dispatch is
  // enabled. `null` when the daemon could not read it; never a guess.
  getRuntimeDispatchGate(): Promise<boolean | null> {
    return this.read("/health", (value) => {
      const gate = typeof value === "object" && value !== null ? (value as Record<string, unknown>).runtime_dispatch_enabled : undefined;
      return typeof gate === "boolean" ? gate : null;
    });
  }

  // The same health read as one call: the gate above, plus the API version and
  // the build capability map the Status view names. A field the daemon did not
  // report reads `null`; a capability whose value is not a boolean is dropped
  // rather than coerced, so an unreadable entry never renders as "not
  // available".
  getRuntimeStatus(): Promise<{
    apiVersion: string | null;
    dispatchGate: boolean | null;
    capabilities: Record<string, boolean> | null;
  }> {
    return this.read("/health", (value) => {
      const record = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
      const reported = record.capabilities;
      let capabilities: Record<string, boolean> | null = null;
      if (typeof reported === "object" && reported !== null && !Array.isArray(reported)) {
        capabilities = {};
        for (const [name, flag] of Object.entries(reported as Record<string, unknown>)) {
          if (typeof flag === "boolean") capabilities[name] = flag;
        }
      }
      return {
        apiVersion: typeof record.api_version === "string" ? record.api_version : null,
        dispatchGate: typeof record.runtime_dispatch_enabled === "boolean" ? record.runtime_dispatch_enabled : null,
        capabilities,
      };
    });
  }

  getResearchWorkflow(runId: string): Promise<ResearchWorkflowProjection> {
    return this.read(
      `/runs/${encodeURIComponent(runId)}/research-workflow`,
      (value, path = "research_workflow") => {
        const decoded = decodeResearchWorkflow(value, path);
        if (decoded.run.id !== runId) {
          throw new ContractDecodeError(path + ".run.id", "research workflow identity does not match the request");
        }
        return decoded;
      },
    );
  }

  getArtifactVersionContent(artifactVersionId: string): Promise<ArtifactVersionContent> {
    return this.read(
      `/artifact-versions/${encodeURIComponent(artifactVersionId)}/content`,
      (value, path = "artifact_content") => {
        const decoded = decodeArtifactVersionContent(value, path);
        if (decoded.artifact_version_id !== artifactVersionId) {
          throw new ContractDecodeError(
            path + ".artifact_version_id",
            "artifact content identity does not match the request",
          );
        }
        return decoded;
      },
    );
  }

  getReadingsStatus(): Promise<ReadingsStatus> {
    return this.read("/readings", decodeReadingsStatus);
  }

  listSources(): Promise<ListEnvelope<SourceProjection>> {
    return this.read("/sources", listDecoder(decodeSource));
  }

  getSource(id: string): Promise<SourceProjection> {
    return this.read(
      `/sources/${encodeURIComponent(id)}`,
      (value, path = "source") => {
        const decoded = decodeSource(value, path);
        if (decoded.id !== id) {
          throw new ContractDecodeError(path + ".id", "source identity does not match the request");
        }
        return decoded;
      },
    );
  }

  getSourceContent(id: string, kind: SourceContentKind = "notes", cursor?: string): Promise<SourceContent> {
    const query = new URLSearchParams({ kind });
    if (cursor) query.set("cursor", cursor);
    return this.read(`/sources/${encodeURIComponent(id)}/content?${query}`, (value, path = "source_content") => {
      const decoded = decodeSourceContent(value, path);
      if (decoded.source_id !== id || decoded.kind !== kind) {
        throw new ContractDecodeError(path, "source content identity does not match the request");
      }
      return decoded;
    });
  }

  searchSources(query: string, limit = 10): Promise<SourceSearch> {
    const params = new URLSearchParams({ q: query.trim(), limit: String(limit) });
    return this.read(`/sources/search?${params}`, decodeSourceSearch);
  }

  // R1c: the research catalog. `signal` is carried through to `fetch`, so a
  // dossier read the operator has already moved past is cancelled and not
  // merely ignored -- one item's documents must never land under another's.
  listResearchItems(options: {
    kind?: ResearchItemKind;
    status?: string;
    limit?: number;
    offset?: number;
    signal?: AbortSignal;
  } = {}): Promise<ResearchItemPage> {
    const query = new URLSearchParams();
    if (options.kind) query.set("kind", options.kind);
    if (options.status) query.set("status", options.status);
    query.set("limit", String(options.limit ?? 100));
    query.set("offset", String(options.offset ?? 0));
    return this.read(`/research-items?${query.toString()}`, decodeResearchItemPage, options.signal);
  }

  getResearchItem(id: string, signal?: AbortSignal): Promise<ResearchItemDetail> {
    return this.read(
      `/research-items/${encodeURIComponent(id)}`,
      (value, path = "research_item") => {
        const decoded = decodeResearchItemDetail(value, path);
        if (decoded.id !== id) {
          throw new ContractDecodeError(path + ".id", "research item identity does not match the request");
        }
        return decoded;
      },
      signal,
    );
  }

  // Read by DOCUMENT VERSION identity: the version is what carries the bytes,
  // and the document it belongs to is a separate identity the dossier keeps.
  getResearchDocumentContent(documentVersionId: string, signal?: AbortSignal): Promise<ResearchDocumentContent> {
    return this.read(
      `/research-documents/${encodeURIComponent(documentVersionId)}/content`,
      (value, path = "research_document_content") => {
        const decoded = decodeResearchDocumentContent(value, path);
        if (decoded.document_version_id !== documentVersionId) {
          throw new ContractDecodeError(
            path + ".document_version_id",
            "research document identity does not match the request",
          );
        }
        return decoded;
      },
      signal,
    );
  }

  // Opens or reuses the thread linked to one research item. It starts no run:
  // the operator asks their question in the ordinary composer afterwards. The
  // answer is an ordinary thread, in the project the operator already has open,
  // and a thread named for another project is refused rather than shown.
  prepareOpenResearchThread(item: { id: string }, workspace: Workspace): PreparedMutation<Thread> {
    return this.prepare(
      `/research-items/${encodeURIComponent(item.id)}/thread`,
      { workspace_id: workspace.id, expected_revision: workspace.revision },
      (value, path = "thread") => {
        const decoded = decodeThread(value, path);
        if (decoded.workspace_id !== workspace.id) {
          throw new ContractDecodeError(path + ".workspace_id", "thread identity does not match the request");
        }
        return decoded;
      },
    );
  }

  listCaptures(state?: CaptureState): Promise<ListEnvelope<Capture>> {
    const query = state ? `?state=${encodeURIComponent(state)}` : "";
    return this.read(`/captures${query}`, listDecoder(decodeCapture));
  }

  getCapture(id: string): Promise<Capture> {
    return this.read(
      `/captures/${encodeURIComponent(id)}`,
      (value, path = "capture") => {
        const decoded = decodeCapture(value, path);
        if (decoded.id !== id) {
          throw new ContractDecodeError(path + ".id", "capture identity does not match the request");
        }
        return decoded;
      },
    );
  }

  listDecisions(state: "pending" | "resolved" | "expired" = "pending"): Promise<ListEnvelope<Decision>> {
    return this.read(`/decisions?state=${state}`, listDecoder(decodeDecision));
  }

  listEvents(afterCursor: string | null): Promise<ListEnvelope<RunEvent>> {
    const query = afterCursor ? `?after_cursor=${encodeURIComponent(afterCursor)}` : "";
    return this.read(`/events${query}`, listDecoder(decodeRunEvent));
  }

  prepareCreateWorkspace(title: string): PreparedMutation<Workspace> {
    return this.prepare("/workspaces", { title }, decodeWorkspace);
  }

  prepareRenameWorkspace(workspace: Workspace, title: string): PreparedMutation<Workspace> {
    return this.prepare(
      `/workspaces/${encodeURIComponent(workspace.id)}/rename`,
      { title, expected_revision: workspace.revision },
      decodeWorkspace,
    );
  }

  prepareRenameThread(thread: Thread, title: string): PreparedMutation<Thread> {
    return this.prepare(
      `/threads/${encodeURIComponent(thread.id)}/rename`,
      { title, expected_revision: thread.revision },
      decodeThread,
    );
  }

  prepareArchiveThread(thread: Thread): PreparedMutation<Thread> {
    return this.prepare(
      `/threads/${encodeURIComponent(thread.id)}/archive`,
      { expected_revision: thread.revision },
      decodeThread,
    );
  }

  prepareUnarchiveThread(thread: Thread): PreparedMutation<Thread> {
    return this.prepare(
      `/threads/${encodeURIComponent(thread.id)}/unarchive`,
      { expected_revision: thread.revision },
      decodeThread,
    );
  }

  prepareCreateThread(workspace: Workspace, title: string): PreparedMutation<Thread> {
    return this.prepare(
      `/workspaces/${encodeURIComponent(workspace.id)}/threads`,
      { title, expected_revision: workspace.revision },
      decodeThread,
    );
  }

  prepareAppendMessage(thread: Thread, content: string): PreparedMutation<Message> {
    return this.prepare(
      `/threads/${encodeURIComponent(thread.id)}/messages`,
      { role: "user", content, expected_revision: thread.revision },
      decodeMessage,
    );
  }

  prepareCreateRun(thread: Thread): PreparedMutation<Run> {
    return this.prepare(
      `/threads/${encodeURIComponent(thread.id)}/runs`,
      { expected_revision: thread.revision },
      decodeRun,
    );
  }

  prepareRunAction(
    run: Run,
    action: "pause" | "resume" | "cancel" | "retry",
    reason?: string,
  ): PreparedMutation<Run> {
    return this.prepare(
      `/runs/${encodeURIComponent(run.id)}/${action}`,
      {
        expected_revision: run.revision,
        ...(action === "retry" ? { reason: reason?.trim() || "Retry requested from the Cortex Web client" } : {}),
      },
      decodeRun,
    );
  }

  prepareResolveDecision(decision: Decision, choice: string): PreparedMutation<Decision> {
    return this.prepare(
      `/decisions/${encodeURIComponent(decision.id)}/resolve`,
      { choice, expected_revision: decision.revision },
      decodeDecision,
    );
  }

  prepareResolveSourceIntent(
    gate: SourceGateProjection,
    choice: string,
  ): PreparedMutation<SourceGateProjection> {
    return this.prepare(
      `/source-intents/${encodeURIComponent(gate.id)}/resolve`,
      { choice, expected_revision: gate.revision },
      (value, path = "source_gate") => {
        const decoded = decodeSourceGate(value, path);
        if (
          decoded.id !== gate.id ||
          decoded.run_id !== gate.run_id ||
          decoded.attempt_id !== gate.attempt_id
        ) {
          throw new ContractDecodeError(path, "resolved Source gate identity does not match the request");
        }
        return decoded;
      },
    );
  }

  // The payload crosses unchanged: `capture_key` derivation belongs to the
  // store, and rewriting the submission here would be resolution performed
  // before the operator has approved anything.
  prepareCreateCapture(input: { payload: string; note: string }): PreparedMutation<Capture> {
    return this.prepare("/captures", { payload: input.payload, note: input.note }, decodeCapture);
  }

  prepareApproveCapture(capture: Capture): PreparedMutation<Capture> {
    return this.prepareCaptureDecision(capture, "approve", { expected_revision: capture.revision });
  }

  prepareDismissCapture(capture: Capture): PreparedMutation<Capture> {
    return this.prepareCaptureDecision(capture, "dismiss", { expected_revision: capture.revision });
  }

  // `acknowledged` is literal and part of the canonical request hash, so a
  // replayed reopen cannot silently return a lost import to the work queue.
  prepareReopenCapture(capture: Capture): PreparedMutation<Capture> {
    return this.prepareCaptureDecision(capture, "reopen", {
      expected_revision: capture.revision,
      acknowledged: true,
    });
  }

  private prepareCaptureDecision(
    capture: Capture,
    action: "approve" | "dismiss" | "reopen",
    body: Record<string, unknown>,
  ): PreparedMutation<Capture> {
    return this.prepare(
      `/captures/${encodeURIComponent(capture.id)}/${action}`,
      body,
      (value, path = "capture") => {
        const decoded = decodeCapture(value, path);
        if (decoded.id !== capture.id) {
          throw new ContractDecodeError(path + ".id", "decided capture identity does not match the request");
        }
        return decoded;
      },
    );
  }

  private prepare<T>(path: string, body: Record<string, unknown>, decoder: Decoder<T>): PreparedMutation<T> {
    const idempotencyKey = this.keyFactory();
    return {
      idempotencyKey,
      execute: () => this.mutate(path, body, decoder, idempotencyKey),
    };
  }

  private async read<T>(path: string, decoder: Decoder<T>, signal?: AbortSignal): Promise<T> {
    const response = await this.request(path, signal ? { method: "GET", signal } : { method: "GET" });
    return decoder(await response.json());
  }

  private async mutate<T>(
    path: string,
    body: Record<string, unknown>,
    decoder: Decoder<T>,
    idempotencyKey: string,
  ): Promise<MutationResult<T>> {
    const response = await this.request(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
        "X-Cortex-Web-Client": "v1",
      },
      body: JSON.stringify(body),
    });
    return {
      value: decoder(await response.json()),
      replayed: response.headers.get("Idempotency-Replayed") === "true",
    };
  }

  private async request(path: string, init: RequestInit): Promise<Response> {
    let response: Response;
    try {
      response = await this.fetcher(`${this.basePath}${path}`, {
        ...init,
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json", ...init.headers },
      });
    } catch {
      throw new ControlNetworkError();
    }
    if (!response.ok) {
      let payload: unknown;
      try {
        payload = await response.json();
      } catch {
        throw new ControlNetworkError("Cortex Control returned an unreadable error");
      }
      throw new ControlProblemError(decodeProblem(payload));
    }
    return response;
  }
}
