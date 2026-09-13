export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type Workspace = {
  id: string;
  title: string;
  // P9-2: whether the research engine created the workspace (its `Capture
  // consumer` workspace); computed by the API on every workspace it returns,
  // never stored. The server decides whose it is; the cockpit only filters.
  engine_owned: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type Thread = {
  id: string;
  workspace_id: string;
  title: string;
  status: string;
  active_run_id: string | null;
  // Archived threads are hidden from the default list and never deleted;
  // null means the thread is live.
  archived_at: string | null;
  // ADJ-1: whether the research engine owns the thread (its capture carrier
  // threads); computed by the API on every thread it returns, never stored.
  engine_owned: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type Message = {
  id: string;
  thread_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  position: number;
  created_at: string;
};

export type Run = {
  id: string;
  thread_id: string;
  state: string;
  active_attempt_id: string | null;
  stage: string | null;
  latest_sequence: number;
  // ADJ-A: whether the research engine owns THIS RUN -- it carries a workflow,
  // or its create receipt names the capture consumer. The same pair the API
  // refuses cancel and pause on, so the cockpit can hide exactly those buttons
  // and no others. Not the thread's ownership: a run an operator opened on a
  // carrier thread is theirs to end, and ending it is the ForeignCarrierRun
  // recovery.
  engine_owned: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type Decision = {
  id: string;
  run_id: string;
  attempt_id: string;
  kind: string;
  prompt: string;
  options: JsonValue[];
  state: "pending" | "resolved" | "expired";
  resolution: JsonValue;
  revision: number;
  created_at: string;
  resolved_at: string | null;
};

export const CAPTURE_STATES = [
  "pending",
  "approved",
  "claimed",
  "uncertain",
  "consumed",
  "dismissed",
  "failed",
] as const;

export type CaptureState = (typeof CAPTURE_STATES)[number];

export type Capture = {
  id: string;
  capture_key: string;
  payload: string;
  kind: "url" | "text";
  note: string;
  state: CaptureState;
  known_source_id: string | null;
  consumed_source_ids: string[] | null;
  failure_category: string | null;
  // ⟦V-R3 / P9-3⟧ The run or thread still holding this capture's carrier
  // thread, when its failure_category names one. Computed by the daemon on
  // read from the capture's audit row -- the captures table has no column for
  // it -- and null whenever the category points at nothing.
  blocked_by: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type RunEvent = {
  cursor: string;
  schema_version: number;
  id: string;
  run_id: string;
  attempt_id: string | null;
  sequence: number;
  type: string;
  occurred_at: string;
  causation_id: string | null;
  durability: "durable";
  payload: JsonValue;
};

export type Problem = {
  type: string;
  title: string;
  status: number;
  category: string;
  retryable: boolean;
  owner: string;
  retry_after_ms?: number;
  current?: { [key: string]: JsonValue };
};

export type ListEnvelope<T> = {
  items: T[];
  next_cursor: string | null;
};

export type ObjectValue = Record<string, unknown>;
export type Decoder<T> = (value: unknown, path?: string) => T;

export class ContractDecodeError extends Error {
  constructor(readonly path: string, message: string) {
    super(`Invalid Cortex response at ${path}: ${message}`);
    this.name = "ContractDecodeError";
  }
}

export function object(value: unknown, path: string): ObjectValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractDecodeError(path, "expected an object");
  }
  return value as ObjectValue;
}

export function string(value: unknown, path: string): string {
  if (typeof value !== "string") throw new ContractDecodeError(path, "expected a string");
  return value;
}

export function nullableString(value: unknown, path: string): string | null {
  return value === null ? null : string(value, path);
}

export function integer(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new ContractDecodeError(path, "expected a non-negative integer");
  }
  return value as number;
}

export function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new ContractDecodeError(path, "expected a boolean");
  return value;
}

function json(value: unknown, path: string, depth = 0): JsonValue {
  if (depth > 32) throw new ContractDecodeError(path, "JSON nesting is too deep");
  if (value === null) return null;
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map((item, index) => json(item, `${path}[${index}]`, depth + 1));
  const record = object(value, path);
  return Object.fromEntries(
    Object.entries(record).map(([key, item]) => [key, json(item, `${path}.${key}`, depth + 1)]),
  );
}

function baseMutable(value: unknown, path: string) {
  const record = object(value, path);
  return {
    record,
    id: string(record.id, `${path}.id`),
    revision: integer(record.revision, `${path}.revision`),
    created_at: string(record.created_at, `${path}.created_at`),
    updated_at: string(record.updated_at, `${path}.updated_at`),
  };
}

export const decodeWorkspace: Decoder<Workspace> = (value, path = "workspace") => {
  const base = baseMutable(value, path);
  return {
    id: base.id,
    title: string(base.record.title, `${path}.title`),
    engine_owned: boolean(base.record.engine_owned, `${path}.engine_owned`),
    revision: base.revision,
    created_at: base.created_at,
    updated_at: base.updated_at,
  };
};

export const decodeThread: Decoder<Thread> = (value, path = "thread") => {
  const base = baseMutable(value, path);
  return {
    id: base.id,
    workspace_id: string(base.record.workspace_id, `${path}.workspace_id`),
    title: string(base.record.title, `${path}.title`),
    status: string(base.record.status, `${path}.status`),
    active_run_id: nullableString(base.record.active_run_id, `${path}.active_run_id`),
    archived_at: nullableString(base.record.archived_at, `${path}.archived_at`),
    engine_owned: boolean(base.record.engine_owned, `${path}.engine_owned`),
    revision: base.revision,
    created_at: base.created_at,
    updated_at: base.updated_at,
  };
};

export const decodeMessage: Decoder<Message> = (value, path = "message") => {
  const record = object(value, path);
  const role = string(record.role, `${path}.role`);
  if (role !== "user" && role !== "assistant" && role !== "system") {
    throw new ContractDecodeError(`${path}.role`, "expected user, assistant, or system");
  }
  return {
    id: string(record.id, `${path}.id`),
    thread_id: string(record.thread_id, `${path}.thread_id`),
    role,
    content: string(record.content, `${path}.content`),
    position: integer(record.position, `${path}.position`),
    created_at: string(record.created_at, `${path}.created_at`),
  };
};

export const decodeRun: Decoder<Run> = (value, path = "run") => {
  const base = baseMutable(value, path);
  return {
    id: base.id,
    thread_id: string(base.record.thread_id, `${path}.thread_id`),
    state: string(base.record.state, `${path}.state`),
    active_attempt_id: nullableString(base.record.active_attempt_id, `${path}.active_attempt_id`),
    stage: nullableString(base.record.stage, `${path}.stage`),
    latest_sequence: integer(base.record.latest_sequence, `${path}.latest_sequence`),
    engine_owned: boolean(base.record.engine_owned, `${path}.engine_owned`),
    revision: base.revision,
    created_at: base.created_at,
    updated_at: base.updated_at,
  };
};

export const decodeDecision: Decoder<Decision> = (value, path = "decision") => {
  const record = object(value, path);
  const state = string(record.state, `${path}.state`);
  if (state !== "pending" && state !== "resolved" && state !== "expired") {
    throw new ContractDecodeError(`${path}.state`, "expected a decision state");
  }
  if (!Array.isArray(record.options)) throw new ContractDecodeError(`${path}.options`, "expected an array");
  return {
    id: string(record.id, `${path}.id`),
    run_id: string(record.run_id, `${path}.run_id`),
    attempt_id: string(record.attempt_id, `${path}.attempt_id`),
    kind: string(record.kind, `${path}.kind`),
    prompt: string(record.prompt, `${path}.prompt`),
    options: record.options.map((item, index) => json(item, `${path}.options[${index}]`)),
    state,
    resolution: json(record.resolution, `${path}.resolution`),
    revision: integer(record.revision, `${path}.revision`),
    created_at: string(record.created_at, `${path}.created_at`),
    resolved_at: nullableString(record.resolved_at, `${path}.resolved_at`),
  };
};

// The consumer lease is private by contract, so these names are refused rather
// than dropped, and the whole key set is pinned: a capture DTO is frozen at
// contract v0.3, and quietly discarding an unenumerated field would hide the
// exact leak the contract's verification gate exists to catch.
const CAPTURE_LEASE_FIELDS = ["claim_owner", "claim_epoch", "claim_expires_at"];
const CAPTURE_FIELDS = [
  "id",
  "capture_key",
  "payload",
  "kind",
  "note",
  "state",
  "known_source_id",
  "consumed_source_ids",
  "failure_category",
  "blocked_by",
  "revision",
  "created_at",
  "updated_at",
];

function captureSourceIds(value: unknown, path: string): string[] | null {
  if (value === null) return null;
  if (!Array.isArray(value) || value.length < 1 || value.length > 100) {
    throw new ContractDecodeError(path, "expected null or 1 to 100 source identifiers");
  }
  return value.map((item, index) => string(item, `${path}[${index}]`));
}

export const decodeCapture: Decoder<Capture> = (value, path = "capture") => {
  const record = object(value, path);
  for (const name of Object.keys(record)) {
    if (CAPTURE_LEASE_FIELDS.includes(name)) {
      throw new ContractDecodeError(`${path}.${name}`, "the consumer lease must never reach a client");
    }
    if (!CAPTURE_FIELDS.includes(name)) {
      throw new ContractDecodeError(`${path}.${name}`, "unexpected capture field");
    }
  }
  const missing = CAPTURE_FIELDS.find((name) => !(name in record));
  if (missing) throw new ContractDecodeError(`${path}.${missing}`, "expected a capture field");
  const kind = string(record.kind, `${path}.kind`);
  if (kind !== "url" && kind !== "text") {
    throw new ContractDecodeError(`${path}.kind`, "expected url or text");
  }
  const state = string(record.state, `${path}.state`);
  if (!(CAPTURE_STATES as readonly string[]).includes(state)) {
    throw new ContractDecodeError(`${path}.state`, "expected a capture state");
  }
  return {
    id: string(record.id, `${path}.id`),
    capture_key: string(record.capture_key, `${path}.capture_key`),
    payload: string(record.payload, `${path}.payload`),
    kind,
    note: string(record.note, `${path}.note`),
    state: state as CaptureState,
    known_source_id: nullableString(record.known_source_id, `${path}.known_source_id`),
    consumed_source_ids: captureSourceIds(record.consumed_source_ids, `${path}.consumed_source_ids`),
    failure_category: nullableString(record.failure_category, `${path}.failure_category`),
    blocked_by: nullableString(record.blocked_by, `${path}.blocked_by`),
    revision: integer(record.revision, `${path}.revision`),
    created_at: string(record.created_at, `${path}.created_at`),
    updated_at: string(record.updated_at, `${path}.updated_at`),
  };
};

export const decodeRunEvent: Decoder<RunEvent> = (value, path = "event") => {
  const record = object(value, path);
  const durability = string(record.durability, `${path}.durability`);
  if (durability !== "durable") throw new ContractDecodeError(`${path}.durability`, "expected durable");
  return {
    cursor: string(record.cursor, `${path}.cursor`),
    schema_version: integer(record.schema_version, `${path}.schema_version`),
    id: string(record.id, `${path}.id`),
    run_id: string(record.run_id, `${path}.run_id`),
    attempt_id: nullableString(record.attempt_id, `${path}.attempt_id`),
    sequence: integer(record.sequence, `${path}.sequence`),
    type: string(record.type, `${path}.type`),
    occurred_at: string(record.occurred_at, `${path}.occurred_at`),
    causation_id: nullableString(record.causation_id, `${path}.causation_id`),
    durability,
    payload: json(record.payload, `${path}.payload`),
  };
};

export const decodeProblem: Decoder<Problem> = (value, path = "problem") => {
  const record = object(value, path);
  const current = record.current === undefined ? undefined : object(json(record.current, `${path}.current`), `${path}.current`) as { [key: string]: JsonValue };
  const retryAfter = record.retry_after_ms === undefined ? undefined : integer(record.retry_after_ms, `${path}.retry_after_ms`);
  return {
    type: string(record.type, `${path}.type`),
    title: string(record.title, `${path}.title`),
    status: integer(record.status, `${path}.status`),
    category: string(record.category, `${path}.category`),
    retryable: boolean(record.retryable, `${path}.retryable`),
    owner: string(record.owner, `${path}.owner`),
    ...(retryAfter === undefined ? {} : { retry_after_ms: retryAfter }),
    ...(current === undefined ? {} : { current }),
  };
};

export function listDecoder<T>(itemDecoder: Decoder<T>): Decoder<ListEnvelope<T>> {
  return (value, path = "list") => {
    const record = object(value, path);
    if (!Array.isArray(record.items)) throw new ContractDecodeError(`${path}.items`, "expected an array");
    return {
      items: record.items.map((item, index) => itemDecoder(item, `${path}.items[${index}]`)),
      next_cursor: nullableString(record.next_cursor, `${path}.next_cursor`),
    };
  };
}
