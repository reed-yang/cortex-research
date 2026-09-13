import {
  ContractDecodeError,
  boolean as decodeBoolean,
  integer,
  nullableString,
  object,
  string as decodeString,
} from "./contracts";

// R1c contract, fixed by the coordinator: the research catalog is a bounded,
// query-only projection of the product-owned research database. Every field a
// screen reads is decoded here, so a payload that carries a raw host path, an
// unbounded document or an identity that does not answer the request never
// reaches the shell.
const ITEM_ID = /^ri_[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
// The same shape `research-contracts.ts` calls a public identifier: no slash,
// no backslash, no whitespace -- a filesystem path cannot decode as one.
const PUBLIC_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/;
const MAX_TITLE = 500;
const MAX_SUMMARY = 20_000;
const MAX_TEXT = 20_000;
const MAX_DOCUMENTS = 200;
const MAX_HISTORY = 200;
const MAX_ITEMS = 200;
const MAX_CONTENT_BYTES = 1_048_576;

export const RESEARCH_ITEM_KINDS = ["idea", "exploration", "project"] as const;
export type ResearchItemKind = (typeof RESEARCH_ITEM_KINDS)[number];

// The six statuses the research database persists. The catalog currently holds
// three of them; a status outside this list is still shown, in the words the
// backend used, because the reader may not invent a state it was not told.
export const RESEARCH_ITEM_STATUSES = [
  "incubating",
  "graduated",
  "killed",
  "aborted",
  "dormant",
  "awaiting_human",
] as const;
export type KnownResearchItemStatus = (typeof RESEARCH_ITEM_STATUSES)[number];

export type ResearchItem = {
  id: string;
  kind: ResearchItemKind;
  // The original `(origin, kind, id)` key, kept as the backend reports it: the
  // product workspace is a container and never a substitute for this identity.
  origin_id: string;
  title: string;
  status: string;
  summary: string;
  pause_reason: string | null;
  round_count: number;
  updated_at: string | null;
};

export type ResearchHistoryEntry = {
  kind: string;
  label: string;
  text: string;
  created_at: string | null;
};

// `id` is the document VERSION identity -- what the content route is read by --
// and `document_id` is the document it is a version of. The two are kept apart
// so a dossier never asks for one by the other's name.
export type ResearchDocumentRef = {
  id: string;
  document_id: string;
  title: string;
  version: number;
  media_type: "text/markdown" | "text/plain";
  byte_length: number;
  sha256: string;
};

export type ResearchItemDetail = ResearchItem & {
  history: ResearchHistoryEntry[];
  documents: ResearchDocumentRef[];
  // The item-linked thread when one already exists; null before the first
  // "Open research conversation".
  thread_id: string | null;
  continuation_ready: boolean;
  unavailable_reason: string | null;
};

export type ResearchDocumentContent = {
  document_version_id: string;
  media_type: "text/markdown" | "text/plain";
  // The bytes actually delivered. When `redacted` is true these are the bytes
  // of the authorized projection, and `sha256` still names the retained
  // version they were projected from -- the digest identifies the version, not
  // the delivery, so the two are not compared here.
  byte_length: number;
  sha256: string;
  content: string;
  // Both optional: a backend that never withholds anything may omit them.
  redacted: boolean;
  retained_byte_length: number | null;
};

export type ResearchItemPage = {
  items: ResearchItem[];
  total: number;
  limit: number;
  offset: number;
};

function fail(path: string, message: string): never {
  throw new ContractDecodeError(path, message);
}

function boundedString(value: unknown, path: string, maximum: number, minimum = 1): string {
  const decoded = decodeString(value, path);
  if (decoded.length < minimum || decoded.length > maximum) {
    fail(path, "string length is outside the contract bound");
  }
  return decoded;
}

function nullableBoundedString(value: unknown, path: string, maximum: number): string | null {
  const decoded = nullableString(value, path);
  return decoded === null ? null : boundedString(decoded, path, maximum);
}

function publicIdentifier(value: unknown, path: string): string {
  const decoded = decodeString(value, path);
  if (!PUBLIC_IDENTIFIER.test(decoded)) fail(path, "expected a public identifier");
  return decoded;
}

export function isResearchItemId(value: string): boolean {
  return ITEM_ID.test(value);
}

function itemId(value: unknown, path: string): string {
  const decoded = decodeString(value, path);
  if (!ITEM_ID.test(decoded)) fail(path, "expected a research item identity");
  return decoded;
}

function digest(value: unknown, path: string): string {
  const decoded = decodeString(value, path);
  if (!SHA256.test(decoded)) fail(path, "expected a lowercase SHA-256");
  return decoded;
}

function textMediaType(value: unknown, path: string): "text/markdown" | "text/plain" {
  const decoded = decodeString(value, path);
  if (decoded !== "text/markdown" && decoded !== "text/plain") {
    fail(path, "expected verified text content");
  }
  return decoded;
}

function itemKind(value: unknown, path: string): ResearchItemKind {
  const decoded = decodeString(value, path);
  if (!(RESEARCH_ITEM_KINDS as readonly string[]).includes(decoded)) {
    fail(path, "expected a research item kind");
  }
  return decoded as ResearchItemKind;
}

function boundedArray<T>(
  value: unknown,
  path: string,
  maximum: number,
  decoder: (item: unknown, itemPath: string) => T,
): T[] {
  if (!Array.isArray(value)) fail(path, "expected an array");
  if (value.length > maximum) fail(path, "array exceeds the contract bound");
  return value.map((item, index) => decoder(item, `${path}[${index}]`));
}

function decodeItemFields(record: Record<string, unknown>, path: string): ResearchItem {
  return {
    id: itemId(record.id, `${path}.id`),
    kind: itemKind(record.kind, `${path}.kind`),
    origin_id: publicIdentifier(record.origin_id, `${path}.origin_id`),
    title: boundedString(record.title, `${path}.title`, MAX_TITLE),
    // A status is never checked against the six known ones: the reader shows
    // whatever the backend truthfully persists, and only the LABEL is a lookup.
    status: boundedString(record.status, `${path}.status`, 64),
    summary: boundedString(record.summary, `${path}.summary`, MAX_SUMMARY, 0),
    pause_reason: nullableBoundedString(record.pause_reason, `${path}.pause_reason`, MAX_TEXT),
    round_count: integer(record.round_count, `${path}.round_count`),
    updated_at: nullableBoundedString(record.updated_at, `${path}.updated_at`, 64),
  };
}

export function decodeResearchItem(value: unknown, path = "research_item"): ResearchItem {
  return decodeItemFields(object(value, path), path);
}

function decodeHistoryEntry(value: unknown, path: string): ResearchHistoryEntry {
  const record = object(value, path);
  return {
    kind: boundedString(record.kind, `${path}.kind`, 64),
    label: boundedString(record.label, `${path}.label`, MAX_TITLE),
    text: boundedString(record.text, `${path}.text`, MAX_TEXT, 0),
    created_at: nullableBoundedString(record.created_at, `${path}.created_at`, 64),
  };
}

function decodeDocumentRef(value: unknown, path: string): ResearchDocumentRef {
  const record = object(value, path);
  const version = integer(record.version, `${path}.version`);
  if (version < 1) fail(`${path}.version`, "expected a positive version");
  const byteLength = integer(record.byte_length, `${path}.byte_length`);
  if (byteLength > MAX_CONTENT_BYTES) fail(`${path}.byte_length`, "content exceeds 1 MiB");
  return {
    id: publicIdentifier(record.id, `${path}.id`),
    document_id: publicIdentifier(record.document_id, `${path}.document_id`),
    title: boundedString(record.title, `${path}.title`, MAX_TITLE),
    version,
    media_type: textMediaType(record.media_type, `${path}.media_type`),
    byte_length: byteLength,
    sha256: digest(record.sha256, `${path}.sha256`),
  };
}

export function decodeResearchItemDetail(value: unknown, path = "research_item"): ResearchItemDetail {
  const record = object(value, path);
  const documents = boundedArray(record.documents, `${path}.documents`, MAX_DOCUMENTS, decodeDocumentRef);
  const versions = documents.map((document) => document.id);
  if (new Set(versions).size !== versions.length) {
    fail(`${path}.documents`, "document versions must be unique");
  }
  return {
    ...decodeItemFields(record, path),
    history: boundedArray(record.history, `${path}.history`, MAX_HISTORY, decodeHistoryEntry),
    documents,
    thread_id: record.thread_id === null ? null : publicIdentifier(record.thread_id, `${path}.thread_id`),
    continuation_ready: decodeBoolean(record.continuation_ready, `${path}.continuation_ready`),
    unavailable_reason: nullableBoundedString(record.unavailable_reason, `${path}.unavailable_reason`, MAX_TEXT),
  };
}

export function decodeResearchItemPage(value: unknown, path = "research_items"): ResearchItemPage {
  const record = object(value, path);
  const items = boundedArray(record.items, `${path}.items`, MAX_ITEMS, decodeResearchItem);
  const limit = integer(record.limit, `${path}.limit`);
  if (limit < 1) fail(`${path}.limit`, "expected a positive page size");
  const total = integer(record.total, `${path}.total`);
  const offset = integer(record.offset, `${path}.offset`);
  if (items.length > limit) fail(`${path}.items`, "page holds more rows than it says");
  const ids = items.map((item) => item.id);
  if (new Set(ids).size !== ids.length) fail(`${path}.items`, "entries must be unique");
  return { items, total, limit, offset };
}

// The bytes are verified exactly as an artifact version's content is: the
// declared length has to be the encoded length, and the text has to survive a
// strict UTF-8 round trip, so a truncated or re-encoded document is refused
// rather than rendered as if it were the stored document.
export function decodeResearchDocumentContent(
  value: unknown,
  path = "research_document_content",
): ResearchDocumentContent {
  const record = object(value, path);
  const byteLength = integer(record.byte_length, `${path}.byte_length`);
  if (byteLength > MAX_CONTENT_BYTES) fail(`${path}.byte_length`, "content exceeds 1 MiB");
  const content = decodeString(record.content, `${path}.content`);
  const bytes = new TextEncoder().encode(content);
  if (bytes.byteLength !== byteLength) fail(`${path}.byte_length`, "content byte length does not match");
  if (new TextDecoder("utf-8", { fatal: true }).decode(bytes) !== content) {
    fail(`${path}.content`, "content is not canonical UTF-8 text");
  }
  // The retained length is bounded on its own terms and never compared with the
  // delivered one: replacing a short private line with a marker makes the
  // authorized projection LONGER than what it was projected from, so an
  // ordering rule here would refuse an honest answer.
  const retained = record.retained_byte_length === undefined || record.retained_byte_length === null
    ? null
    : integer(record.retained_byte_length, `${path}.retained_byte_length`);
  if (retained !== null && (retained < 0 || retained > MAX_CONTENT_BYTES)) {
    fail(`${path}.retained_byte_length`, "retained length is outside the contract bound");
  }
  return {
    document_version_id: publicIdentifier(record.document_version_id, `${path}.document_version_id`),
    media_type: textMediaType(record.media_type, `${path}.media_type`),
    byte_length: byteLength,
    sha256: digest(record.sha256, `${path}.sha256`),
    content,
    redacted: record.redacted === undefined ? false : decodeBoolean(record.redacted, `${path}.redacted`),
    retained_byte_length: retained,
  };
}
