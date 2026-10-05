import {
  CAPTURE_STATES,
  ContractDecodeError,
  boolean as decodeBoolean,
  decodeRun,
  integer,
  nullableString,
  object,
  string as decodeString,
  type CaptureState,
  type Decision,
  type JsonValue,
  type ListEnvelope,
  type ObjectValue,
  type Run,
} from "./contracts";

const MAX_COLLECTION = 200;
const MAX_HISTORY = 100;
const MAX_CONTENT_BYTES = 1_048_576;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MEDIA_TYPE = /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/;

export type WorkflowStageProjection = {
  key: string;
  position: number;
  effect: string;
  state: string;
  revision: number;
  attempt: number;
  checkpoint_enabled: boolean;
  started_at: string | null;
  completed_at: string | null;
};

export type WorkflowProjection = {
  id: string;
  definition_id: string;
  definition_version: number;
  state: string;
  current_stage_key: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  stages: WorkflowStageProjection[];
};

export type SourceCandidateProjection = {
  id: string;
  claim_kind: string;
  canonical_id: string;
  official_title: string;
  source_kind: string;
  version: number | null;
};

export type SourceGateProjection = {
  id: string;
  run_id: string;
  attempt_id: string;
  state: string;
  revision: number;
  created_at: string;
  updated_at: string;
  title_observation: string | null;
  locator_observation: string | null;
  candidates: SourceCandidateProjection[];
  decision: Decision | null;
};

export type SourceAliasProjection = {
  id: string;
  authority: string;
  value: string;
  created_at: string;
};

export type SourceProjection = {
  id: string;
  authority: string;
  authority_id: string;
  canonical_id: string;
  source_kind: string;
  official_title: string;
  import_state: string;
  revision: number;
  aliases: SourceAliasProjection[];
  created_at: string;
  updated_at: string;
};

export type RunSourceProjection = {
  id: string;
  disposition: string;
  created_at: string;
  source: SourceProjection;
  research_selection?: {
    kind: "research_context";
    run_id: string;
    message_id: string;
    authority_message_id: string;
    context_sha256: string;
    label: string;
  };
};

export type LineageNodeProjection = {
  id: string;
  status: string;
  title: string;
  revision: number;
};

export type LineageLinkProjection = {
  id: string;
  from_node_id: string;
  to_node_id: string;
  relation: string;
};

export type LineageProjection = {
  nodes: LineageNodeProjection[];
  links: LineageLinkProjection[];
  successor_node_id: string | null;
};

export type ProducerIdentity = {
  name: string;
  version: string;
};

export type ParentVersionProjection = {
  artifact_version_id: string;
  sha256: string;
};

export type ArtifactProvenanceProjection = {
  schema_version: 1 | 2;
  document_version_ids?: string[];
  research_context_sha256?: string;
  run_id: string;
  attempt_id: string;
  source_ids: string[];
  generator: ProducerIdentity;
  tool: ProducerIdentity;
  parents: ParentVersionProjection[];
  media_type: string;
  byte_length: number;
  sha256: string;
  committed_at: string;
};

export type ArtifactVersionProjection = {
  id: string;
  artifact_id: string;
  logical_version: number;
  resource_uri: string;
  sha256: string;
  byte_length: number;
  media_type: string;
  run_id: string;
  attempt_id: string;
  parents: ParentVersionProjection[];
  source_ids: string[];
  generator: ProducerIdentity;
  tool: ProducerIdentity;
  state: "committed";
  provenance: ArtifactProvenanceProjection;
  created_at: string;
  committed_at: string;
};

export type ResearchArtifactProjection = {
  id: string;
  workspace_id: string;
  thread_id: string;
  kind: string;
  title: string;
  head_artifact_version_id: string | null;
  head_revision: number;
  created_at: string;
  updated_at: string;
  versions: ArtifactVersionProjection[];
};

export type SnapshotMemberProjection = {
  artifact_id: string;
  artifact_version_id: string;
  logical_version: number;
  sha256: string;
};

export type ArtifactSnapshotProjection = {
  id: string;
  workspace_id: string;
  name: string;
  members: SnapshotMemberProjection[];
  created_at: string;
};

export type ResearchWorkflowProjection = {
  schema_version: 1;
  run: Run;
  workflow: WorkflowProjection | null;
  source_gates: SourceGateProjection[];
  sources: RunSourceProjection[];
  lineage: LineageProjection;
  decisions: Decision[];
  artifacts: ResearchArtifactProjection[];
  snapshots: ArtifactSnapshotProjection[];
};

export type ArtifactVersionContent = {
  artifact_version_id: string;
  media_type: "text/markdown" | "text/plain";
  byte_length: number;
  sha256: string;
  content: string;
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

function identifier(value: unknown, path: string): string {
  const decoded = decodeString(value, path);
  if (!IDENTIFIER.test(decoded)) fail(path, "expected a public identifier");
  return decoded;
}

function timestamp(value: unknown, path: string): string {
  return boundedString(value, path, 64);
}

function nullableTimestamp(value: unknown, path: string): string | null {
  const decoded = nullableString(value, path);
  return decoded === null ? null : timestamp(decoded, path);
}

function nullableBoundedString(value: unknown, path: string, maximum: number): string | null {
  const decoded = nullableString(value, path);
  return decoded === null ? null : boundedString(decoded, path, maximum);
}

function positiveInteger(value: unknown, path: string): number {
  const decoded = integer(value, path);
  if (decoded < 1) fail(path, "expected a positive integer");
  return decoded;
}

function schemaOne(value: unknown, path: string): 1 {
  if (value !== 1) fail(path, "expected schema version 1");
  return 1;
}

function digest(value: unknown, path: string): string {
  const decoded = decodeString(value, path);
  if (!SHA256.test(decoded)) fail(path, "expected a lowercase SHA-256");
  return decoded;
}

function mediaType(value: unknown, path: string): string {
  const decoded = boundedString(value, path, 200);
  if (!MEDIA_TYPE.test(decoded)) fail(path, "expected a canonical media type");
  return decoded;
}

function boundedArray<T>(
  value: unknown,
  path: string,
  maximum: number,
  decoder: (item: unknown, itemPath: string) => T,
): T[] {
  if (!Array.isArray(value)) fail(path, "expected an array");
  if (value.length > maximum) fail(path, "array exceeds the contract bound");
  return value.map((item, index) => decoder(item, path + "[" + index + "]"));
}

function requireUnique(values: string[], path: string): void {
  if (new Set(values).size !== values.length) fail(path, "entries must be unique");
}

function requireCanonicalOrder(values: string[], path: string): void {
  for (let index = 1; index < values.length; index += 1) {
    if (values[index - 1] >= values[index]) fail(path, "entries must use canonical order");
  }
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function decodePublicRun(value: unknown, path: string): Run {
  const decoded = decodeRun(value, path);
  identifier(decoded.id, path + ".id");
  identifier(decoded.thread_id, path + ".thread_id");
  if (decoded.active_attempt_id !== null) identifier(decoded.active_attempt_id, path + ".active_attempt_id");
  boundedString(decoded.state, path + ".state", 100);
  if (decoded.stage !== null) boundedString(decoded.stage, path + ".stage", 200);
  timestamp(decoded.created_at, path + ".created_at");
  timestamp(decoded.updated_at, path + ".updated_at");
  return decoded;
}

function decodeResearchDecision(value: unknown, path: string): Decision {
  const record = object(value, path);
  const state = decodeString(record.state, path + ".state");
  if (state !== "pending" && state !== "resolved" && state !== "expired") {
    fail(path + ".state", "expected a decision state");
  }
  const options = boundedArray(record.options, path + ".options", 100, decodeDecisionOption);
  const resolution = record.resolution === null
    ? null
    : decodeDecisionResolution(record.resolution, path + ".resolution");
  return {
    id: identifier(record.id, path + ".id"),
    run_id: identifier(record.run_id, path + ".run_id"),
    attempt_id: identifier(record.attempt_id, path + ".attempt_id"),
    kind: boundedString(record.kind, path + ".kind", 100),
    prompt: boundedString(record.prompt, path + ".prompt", 20_000),
    options,
    state,
    resolution,
    revision: integer(record.revision, path + ".revision"),
    created_at: timestamp(record.created_at, path + ".created_at"),
    resolved_at: nullableTimestamp(record.resolved_at, path + ".resolved_at"),
  };
}

function decodeDecisionOption(value: unknown, path: string): JsonValue {
  const record = object(value, path);
  const result: { [key: string]: JsonValue } = {
    id: identifier(record.id, path + ".id"),
  };
  if (record.label !== undefined) {
    result.label = boundedString(record.label, path + ".label", 500);
  }
  if (record.description !== undefined) {
    result.description = boundedString(record.description, path + ".description", 2_000);
  }
  if (record.tone !== undefined) {
    const tone = decodeString(record.tone, path + ".tone");
    if (tone !== "primary" && tone !== "danger" && tone !== "neutral") {
      fail(path + ".tone", "expected a public decision tone");
    }
    result.tone = tone;
  }
  return result;
}

function decodeDecisionResolution(value: unknown, path: string): JsonValue {
  const record = object(value, path);
  return { choice: identifier(record.choice, path + ".choice") };
}

function decodeStage(value: unknown, path: string): WorkflowStageProjection {
  const record = object(value, path);
  return {
    key: boundedString(record.key, path + ".key", 128),
    position: integer(record.position, path + ".position"),
    effect: boundedString(record.effect, path + ".effect", 128),
    state: boundedString(record.state, path + ".state", 100),
    revision: integer(record.revision, path + ".revision"),
    attempt: integer(record.attempt, path + ".attempt"),
    checkpoint_enabled: decodeBoolean(record.checkpoint_enabled, path + ".checkpoint_enabled"),
    started_at: nullableTimestamp(record.started_at, path + ".started_at"),
    completed_at: nullableTimestamp(record.completed_at, path + ".completed_at"),
  };
}

function decodeWorkflow(value: unknown, path: string): WorkflowProjection | null {
  if (value === null) return null;
  const record = object(value, path);
  const stages = boundedArray(record.stages, path + ".stages", MAX_COLLECTION, decodeStage);
  if (stages.length === 0) fail(path + ".stages", "installed workflow must contain stages");
  requireUnique(stages.map((stage) => stage.key), path + ".stages");
  for (let index = 1; index < stages.length; index += 1) {
    if (stages[index - 1].position >= stages[index].position) {
      fail(path + ".stages", "stage positions must be strictly increasing");
    }
  }
  const currentStageKey = nullableBoundedString(record.current_stage_key, path + ".current_stage_key", 128);
  if (currentStageKey !== null && !stages.some((stage) => stage.key === currentStageKey)) {
    fail(path + ".current_stage_key", "current stage is not in the sealed workflow");
  }
  return {
    id: identifier(record.id, path + ".id"),
    definition_id: identifier(record.definition_id, path + ".definition_id"),
    definition_version: positiveInteger(record.definition_version, path + ".definition_version"),
    state: boundedString(record.state, path + ".state", 100),
    current_stage_key: currentStageKey,
    revision: integer(record.revision, path + ".revision"),
    created_at: timestamp(record.created_at, path + ".created_at"),
    updated_at: timestamp(record.updated_at, path + ".updated_at"),
    stages,
  };
}

function decodeSourceCandidate(value: unknown, path: string): SourceCandidateProjection {
  const record = object(value, path);
  const rawVersion = record.version;
  const version = rawVersion === null ? null : positiveInteger(rawVersion, path + ".version");
  return {
    id: identifier(record.id, path + ".id"),
    claim_kind: boundedString(record.claim_kind, path + ".claim_kind", 100),
    canonical_id: boundedString(record.canonical_id, path + ".canonical_id", 500),
    official_title: boundedString(record.official_title, path + ".official_title", 2_000),
    source_kind: boundedString(record.source_kind, path + ".source_kind", 100),
    version,
  };
}

export function decodeSourceGate(value: unknown, path = "source_gate"): SourceGateProjection {
  const record = object(value, path);
  const runId = identifier(record.run_id, path + ".run_id");
  const attemptId = identifier(record.attempt_id, path + ".attempt_id");
  const candidates = boundedArray(
    record.candidates,
    path + ".candidates",
    MAX_COLLECTION,
    decodeSourceCandidate,
  );
  if (candidates.length === 0) fail(path + ".candidates", "source gate must contain candidates");
  requireUnique(candidates.map((candidate) => candidate.id), path + ".candidates");
  const decision = record.decision === null
    ? null
    : decodeResearchDecision(record.decision, path + ".decision");
  if (decision !== null && (decision.run_id !== runId || decision.attempt_id !== attemptId)) {
    fail(path + ".decision", "Source decision owner does not match its gate");
  }
  return {
    id: identifier(record.id, path + ".id"),
    run_id: runId,
    attempt_id: attemptId,
    state: boundedString(record.state, path + ".state", 100),
    revision: integer(record.revision, path + ".revision"),
    created_at: timestamp(record.created_at, path + ".created_at"),
    updated_at: timestamp(record.updated_at, path + ".updated_at"),
    title_observation: nullableBoundedString(record.title_observation, path + ".title_observation", 2_000),
    locator_observation: nullableBoundedString(record.locator_observation, path + ".locator_observation", 2_000),
    candidates,
    decision,
  };
}

function decodeAlias(value: unknown, path: string): SourceAliasProjection {
  const record = object(value, path);
  return {
    id: identifier(record.id, path + ".id"),
    authority: boundedString(record.authority, path + ".authority", 64),
    value: boundedString(record.value, path + ".value", 500),
    created_at: timestamp(record.created_at, path + ".created_at"),
  };
}

export function decodeSource(value: unknown, path = "source"): SourceProjection {
  const record = object(value, path);
  const aliases = boundedArray(record.aliases, path + ".aliases", MAX_COLLECTION, decodeAlias);
  requireUnique(aliases.map((alias) => alias.id), path + ".aliases");
  return {
    id: identifier(record.id, path + ".id"),
    authority: boundedString(record.authority, path + ".authority", 64),
    authority_id: boundedString(record.authority_id, path + ".authority_id", 500),
    canonical_id: boundedString(record.canonical_id, path + ".canonical_id", 500),
    source_kind: boundedString(record.source_kind, path + ".source_kind", 100),
    official_title: boundedString(record.official_title, path + ".official_title", 2_000),
    import_state: boundedString(record.import_state, path + ".import_state", 100),
    revision: integer(record.revision, path + ".revision"),
    aliases,
    created_at: timestamp(record.created_at, path + ".created_at"),
    updated_at: timestamp(record.updated_at, path + ".updated_at"),
  };
}

function decodeRunSource(value: unknown, path: string): RunSourceProjection {
  const record = object(value, path);
  let selection: RunSourceProjection["research_selection"];
  if (record.research_selection !== undefined) {
    const selected = object(record.research_selection, path + ".research_selection");
    if (selected.kind !== "research_context" || Object.keys(selected).sort().join(",") !== "authority_message_id,context_sha256,kind,label,message_id,run_id") {
      fail(path + ".research_selection", "invalid research selection authority");
    }
    const label = boundedString(selected.label, path + ".research_selection.label", 2);
    if (!/^S[1-6]$/.test(label)) fail(path + ".research_selection.label", "invalid citation label");
    selection = {
      kind: "research_context",
      run_id: identifier(selected.run_id, path + ".research_selection.run_id"),
      message_id: identifier(selected.message_id, path + ".research_selection.message_id"),
      authority_message_id: identifier(selected.authority_message_id, path + ".research_selection.authority_message_id"),
      context_sha256: digest(selected.context_sha256, path + ".research_selection.context_sha256"),
      label,
    };
  }
  if (record.disposition === "research_selected" && selection === undefined) {
    fail(path + ".research_selection", "research-selected source has no authority");
  }
  return {
    id: identifier(record.id, path + ".id"),
    disposition: boundedString(record.disposition, path + ".disposition", 100),
    created_at: timestamp(record.created_at, path + ".created_at"),
    source: decodeSource(record.source, path + ".source"),
    ...(selection ? { research_selection: selection } : {}),
  };
}

function decodeLineageNode(value: unknown, path: string): LineageNodeProjection {
  const record = object(value, path);
  return {
    id: identifier(record.id, path + ".id"),
    status: boundedString(record.status, path + ".status", 100),
    title: boundedString(record.title, path + ".title", 2_000),
    revision: integer(record.revision, path + ".revision"),
  };
}

function decodeLineageLink(value: unknown, path: string): LineageLinkProjection {
  const record = object(value, path);
  return {
    id: identifier(record.id, path + ".id"),
    from_node_id: identifier(record.from_node_id, path + ".from_node_id"),
    to_node_id: identifier(record.to_node_id, path + ".to_node_id"),
    relation: boundedString(record.relation, path + ".relation", 100),
  };
}

function decodeLineage(value: unknown, path: string): LineageProjection {
  const record = object(value, path);
  const nodes = boundedArray(record.nodes, path + ".nodes", MAX_COLLECTION, decodeLineageNode);
  const links = boundedArray(record.links, path + ".links", MAX_COLLECTION, decodeLineageLink);
  requireUnique(nodes.map((node) => node.id), path + ".nodes");
  requireUnique(links.map((link) => link.id), path + ".links");
  const nodeIds = new Set(nodes.map((node) => node.id));
  for (const link of links) {
    if (!nodeIds.has(link.from_node_id) || !nodeIds.has(link.to_node_id)) {
      fail(path + ".links", "lineage link points outside the projection");
    }
  }
  const successorNodeId = record.successor_node_id === null
    ? null
    : identifier(record.successor_node_id, path + ".successor_node_id");
  if (successorNodeId !== null && !nodeIds.has(successorNodeId)) {
    fail(path + ".successor_node_id", "successor node is not in the projection");
  }
  return { nodes, links, successor_node_id: successorNodeId };
}

function decodeProducer(value: unknown, path: string): ProducerIdentity {
  const record = object(value, path);
  return {
    name: identifier(record.name, path + ".name"),
    version: boundedString(record.version, path + ".version", 128),
  };
}

function decodeParent(value: unknown, path: string): ParentVersionProjection {
  const record = object(value, path);
  return {
    artifact_version_id: identifier(record.artifact_version_id, path + ".artifact_version_id"),
    sha256: digest(record.sha256, path + ".sha256"),
  };
}

function decodeStringIds(value: unknown, path: string, allowEmpty = false): string[] {
  const result = boundedArray(value, path, MAX_COLLECTION, identifier);
  if (!allowEmpty && result.length === 0) fail(path, "source IDs must not be empty");
  requireUnique(result, path);
  requireCanonicalOrder(result, path);
  return result;
}

function decodeProvenance(value: unknown, path: string): ArtifactProvenanceProjection {
  const record = object(value, path);
  const parents = boundedArray(record.parents, path + ".parents", MAX_COLLECTION, decodeParent);
  requireUnique(parents.map((parent) => parent.artifact_version_id), path + ".parents");
  requireCanonicalOrder(parents.map((parent) => parent.artifact_version_id), path + ".parents");
  const schemaVersion = record.schema_version === 2 ? 2 : schemaOne(record.schema_version, path + ".schema_version");
  return {
    schema_version: schemaVersion,
    ...(schemaVersion === 2 ? {
      document_version_ids: decodeStringIds(record.document_version_ids, path + ".document_version_ids"),
      research_context_sha256: digest(record.research_context_sha256, path + ".research_context_sha256"),
    } : {}),
    run_id: identifier(record.run_id, path + ".run_id"),
    attempt_id: identifier(record.attempt_id, path + ".attempt_id"),
    source_ids: decodeStringIds(record.source_ids, path + ".source_ids", schemaVersion === 2),
    generator: decodeProducer(record.generator, path + ".generator"),
    tool: decodeProducer(record.tool, path + ".tool"),
    parents,
    media_type: mediaType(record.media_type, path + ".media_type"),
    byte_length: integer(record.byte_length, path + ".byte_length"),
    sha256: digest(record.sha256, path + ".sha256"),
    committed_at: timestamp(record.committed_at, path + ".committed_at"),
  };
}

function decodeArtifactVersion(value: unknown, path: string): ArtifactVersionProjection {
  const record = object(value, path);
  if (record.state !== "committed") fail(path + ".state", "expected committed");
  const parents = boundedArray(record.parents, path + ".parents", MAX_COLLECTION, decodeParent);
  requireUnique(parents.map((parent) => parent.artifact_version_id), path + ".parents");
  requireCanonicalOrder(parents.map((parent) => parent.artifact_version_id), path + ".parents");
  const provenance = decodeProvenance(record.provenance, path + ".provenance");
  const sourceIds = decodeStringIds(record.source_ids, path + ".source_ids", provenance.schema_version === 2);
  const generator = decodeProducer(record.generator, path + ".generator");
  const tool = decodeProducer(record.tool, path + ".tool");
  const result: ArtifactVersionProjection = {
    id: identifier(record.id, path + ".id"),
    artifact_id: identifier(record.artifact_id, path + ".artifact_id"),
    logical_version: positiveInteger(record.logical_version, path + ".logical_version"),
    resource_uri: boundedString(record.resource_uri, path + ".resource_uri", 4_000),
    sha256: digest(record.sha256, path + ".sha256"),
    byte_length: integer(record.byte_length, path + ".byte_length"),
    media_type: mediaType(record.media_type, path + ".media_type"),
    run_id: identifier(record.run_id, path + ".run_id"),
    attempt_id: identifier(record.attempt_id, path + ".attempt_id"),
    parents,
    source_ids: sourceIds,
    generator,
    tool,
    state: "committed",
    provenance,
    created_at: timestamp(record.created_at, path + ".created_at"),
    committed_at: timestamp(record.committed_at, path + ".committed_at"),
  };
  if (
    provenance.run_id !== result.run_id ||
    provenance.attempt_id !== result.attempt_id ||
    !sameValue(provenance.source_ids, result.source_ids) ||
    !sameValue(provenance.generator, result.generator) ||
    !sameValue(provenance.tool, result.tool) ||
    !sameValue(provenance.parents, result.parents) ||
    provenance.media_type !== result.media_type ||
    provenance.byte_length !== result.byte_length ||
    provenance.sha256 !== result.sha256 ||
    provenance.committed_at !== result.committed_at
  ) {
    fail(path + ".provenance", "provenance does not match the immutable version");
  }
  return result;
}

function decodeArtifact(value: unknown, path: string): ResearchArtifactProjection {
  const record = object(value, path);
  const versions = boundedArray(record.versions, path + ".versions", MAX_COLLECTION, decodeArtifactVersion);
  if (versions.length === 0) fail(path + ".versions", "artifact must contain committed versions");
  requireUnique(versions.map((version) => version.id), path + ".versions");
  for (let index = 1; index < versions.length; index += 1) {
    if (versions[index - 1].logical_version >= versions[index].logical_version) {
      fail(path + ".versions", "logical versions must be strictly increasing");
    }
  }
  const artifactId = identifier(record.id, path + ".id");
  for (const version of versions) {
    if (version.artifact_id !== artifactId) {
      fail(path + ".versions", "artifact version owner does not match");
    }
    const expectedUri = "cortex://artifacts/" + artifactId + "/" + version.id;
    if (version.resource_uri !== expectedUri) {
      fail(path + ".versions", "resource URI does not identify the exact version");
    }
  }
  const headId = record.head_artifact_version_id === null
    ? null
    : identifier(record.head_artifact_version_id, path + ".head_artifact_version_id");
  const headRevision = integer(record.head_revision, path + ".head_revision");
  if (headId === null && headRevision !== 0) {
    fail(path + ".head_revision", "artifact without a head must have revision zero");
  }
  if (headId !== null && !versions.some((version) => version.id === headId)) {
    fail(path + ".head_artifact_version_id", "artifact head is not in version history");
  }
  return {
    id: artifactId,
    workspace_id: identifier(record.workspace_id, path + ".workspace_id"),
    thread_id: identifier(record.thread_id, path + ".thread_id"),
    kind: boundedString(record.kind, path + ".kind", 128),
    title: boundedString(record.title, path + ".title", 2_000),
    head_artifact_version_id: headId,
    head_revision: headRevision,
    created_at: timestamp(record.created_at, path + ".created_at"),
    updated_at: timestamp(record.updated_at, path + ".updated_at"),
    versions,
  };
}

function requireAcyclicArtifactParents(
  versions: Map<string, ArtifactVersionProjection>,
  path: string,
): void {
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (versionId: string) => {
    if (visiting.has(versionId)) fail(path, "artifact parent graph contains a cycle");
    if (visited.has(versionId)) return;
    visiting.add(versionId);
    for (const parent of versions.get(versionId)?.parents ?? []) {
      if (versions.has(parent.artifact_version_id)) visit(parent.artifact_version_id);
    }
    visiting.delete(versionId);
    visited.add(versionId);
  };
  for (const versionId of versions.keys()) visit(versionId);
}

function decodeSnapshotMember(value: unknown, path: string): SnapshotMemberProjection {
  const record = object(value, path);
  return {
    artifact_id: identifier(record.artifact_id, path + ".artifact_id"),
    artifact_version_id: identifier(record.artifact_version_id, path + ".artifact_version_id"),
    logical_version: positiveInteger(record.logical_version, path + ".logical_version"),
    sha256: digest(record.sha256, path + ".sha256"),
  };
}

function decodeSnapshot(value: unknown, path: string): ArtifactSnapshotProjection {
  const record = object(value, path);
  const members = boundedArray(record.members, path + ".members", MAX_COLLECTION, decodeSnapshotMember);
  if (members.length === 0) fail(path + ".members", "snapshot must contain members");
  requireUnique(members.map((member) => member.artifact_id), path + ".members");
  requireUnique(members.map((member) => member.artifact_version_id), path + ".members");
  return {
    id: identifier(record.id, path + ".id"),
    workspace_id: identifier(record.workspace_id, path + ".workspace_id"),
    name: boundedString(record.name, path + ".name", 2_000),
    members,
    created_at: timestamp(record.created_at, path + ".created_at"),
  };
}

export function decodeResearchWorkflow(
  value: unknown,
  path = "research_workflow",
): ResearchWorkflowProjection {
  const record = object(value, path);
  const run = decodePublicRun(record.run, path + ".run");
  const workflow = decodeWorkflow(record.workflow, path + ".workflow");
  const sourceGates = boundedArray(
    record.source_gates,
    path + ".source_gates",
    MAX_COLLECTION,
    decodeSourceGate,
  );
  const sources = boundedArray(record.sources, path + ".sources", MAX_COLLECTION, decodeRunSource);
  const lineage = decodeLineage(record.lineage, path + ".lineage");
  const decisions = boundedArray(
    record.decisions,
    path + ".decisions",
    MAX_COLLECTION,
    decodeResearchDecision,
  );
  const artifacts = boundedArray(
    record.artifacts,
    path + ".artifacts",
    MAX_COLLECTION,
    decodeArtifact,
  );
  const snapshots = boundedArray(
    record.snapshots,
    path + ".snapshots",
    MAX_COLLECTION,
    decodeSnapshot,
  );
  schemaOne(record.schema_version, path + ".schema_version");

  requireUnique(sourceGates.map((gate) => gate.id), path + ".source_gates");
  requireUnique(sources.map((binding) => binding.id), path + ".sources");
  requireUnique(sources.map((binding) => binding.source.id), path + ".sources");
  const selections = sources.flatMap((binding) => binding.research_selection ? [binding.research_selection] : []);
  requireUnique(selections.map((selection) => selection.label), path + ".sources");
  for (const selection of selections) {
    if (selection.run_id !== run.id) fail(path + ".sources", "research selection belongs to another run");
    if (selections.some((other) => other.context_sha256 !== selection.context_sha256 || other.message_id !== selection.message_id || other.authority_message_id !== selection.authority_message_id)) {
      fail(path + ".sources", "research selections disagree on context authority");
    }
  }
  requireUnique(decisions.map((decision) => decision.id), path + ".decisions");
  requireUnique(artifacts.map((artifact) => artifact.id), path + ".artifacts");
  requireUnique(snapshots.map((snapshot) => snapshot.id), path + ".snapshots");

  const decisionsById = new Map(decisions.map((decision) => [decision.id, decision]));
  for (const gate of sourceGates) {
    if (gate.run_id !== run.id) fail(path + ".source_gates", "Source gate belongs to another run");
    if (gate.decision !== null) {
      const topLevel = decisionsById.get(gate.decision.id);
      if (topLevel === undefined || !sameValue(topLevel, gate.decision)) {
        fail(path + ".source_gates", "Source decision does not match the decision list");
      }
    }
  }
  for (const decision of decisions) {
    if (decision.run_id !== run.id) fail(path + ".decisions", "decision belongs to another run");
  }

  const versions = new Map<string, ArtifactVersionProjection>();
  for (const artifact of artifacts) {
    if (artifact.thread_id !== run.thread_id) {
      fail(path + ".artifacts", "artifact belongs to another thread");
    }
    if (!artifact.versions.some((version) => version.run_id === run.id)) {
      fail(path + ".artifacts", "artifact has no version from the selected run");
    }
    for (const version of artifact.versions) {
      if (versions.has(version.id)) fail(path + ".artifacts", "artifact version is duplicated");
      versions.set(version.id, version);
    }
  }
  const sourceIds = new Set(sources.map((binding) => binding.source.id));
  for (const version of versions.values()) {
    for (const parent of version.parents) {
      const projectedParent = versions.get(parent.artifact_version_id);
      if (projectedParent !== undefined && projectedParent.sha256 !== parent.sha256) {
        fail(path + ".artifacts", "artifact parent hash does not match its projected version");
      }
    }
    if (version.run_id === run.id && version.source_ids.some((sourceId) => !sourceIds.has(sourceId))) {
      fail(path + ".artifacts", "selected-run artifact references an unbound Source");
    }
  }
  requireAcyclicArtifactParents(versions, path + ".artifacts");

  const runVersions = new Map(
    [...versions].filter(([, version]) => version.run_id === run.id),
  );
  for (const snapshot of snapshots) {
    for (const member of snapshot.members) {
      const version = runVersions.get(member.artifact_version_id);
      if (
        version === undefined ||
        version.artifact_id !== member.artifact_id ||
        version.logical_version !== member.logical_version ||
        version.sha256 !== member.sha256
      ) {
        fail(path + ".snapshots", "snapshot member does not match a selected-run version");
      }
    }
  }

  return {
    schema_version: 1,
    run,
    workflow,
    source_gates: sourceGates,
    sources,
    lineage,
    decisions,
    artifacts,
    snapshots,
  };
}

export function decodeRunHistory(
  value: unknown,
  expectedThreadId: string,
  path = "run_history",
): ListEnvelope<Run> {
  identifier(expectedThreadId, path + ".expected_thread_id");
  const record = object(value, path);
  const items = boundedArray(record.items, path + ".items", MAX_HISTORY, decodePublicRun);
  requireUnique(items.map((run) => run.id), path + ".items");
  if (items.some((run) => run.thread_id !== expectedThreadId)) {
    fail(path + ".items", "run history contains another thread");
  }
  const nextCursor = record.next_cursor === null
    ? null
    : identifier(record.next_cursor, path + ".next_cursor");
  if (nextCursor !== null && (items.length === 0 || items.at(-1)?.id !== nextCursor)) {
    fail(path + ".next_cursor", "history cursor must name the last returned run");
  }
  return { items, next_cursor: nextCursor };
}

export function decodeArtifactVersionContent(
  value: unknown,
  path = "artifact_content",
): ArtifactVersionContent {
  const record = object(value, path);
  const media = decodeString(record.media_type, path + ".media_type");
  if (media !== "text/markdown" && media !== "text/plain") {
    fail(path + ".media_type", "expected verified text content");
  }
  const byteLength = integer(record.byte_length, path + ".byte_length");
  if (byteLength > MAX_CONTENT_BYTES) fail(path + ".byte_length", "content exceeds 1 MiB");
  const content = decodeString(record.content, path + ".content");
  const bytes = new TextEncoder().encode(content);
  if (bytes.byteLength !== byteLength) fail(path + ".byte_length", "content byte length does not match");
  if (new TextDecoder("utf-8", { fatal: true }).decode(bytes) !== content) {
    fail(path + ".content", "content is not canonical UTF-8 text");
  }
  return {
    artifact_version_id: identifier(record.artifact_version_id, path + ".artifact_version_id"),
    media_type: media,
    byte_length: byteLength,
    sha256: digest(record.sha256, path + ".sha256"),
    content,
  };
}

export type SourceContentKind = "notes" | "full_text" | "grounding";
export type SourceContent = {
  source_id: string;
  canonical_id: string;
  kind: SourceContentKind;
  text: string;
  content_sha256: string;
  start_line: number;
  end_line: number;
  next_cursor: string | null;
};
// One whole retained document, read once for Preview and Copy source. The text
// is the same public projection the paged route serves; `content_sha256` names
// the retained file, not `text`.
export type SourceDocument = {
  source_id: string;
  canonical_id: string;
  kind: SourceContentKind;
  text: string;
  content_sha256: string;
  retained_bytes: number;
  redacted: boolean;
};
export type SourceSearchResult = {
  source_id: string;
  canonical_id: string;
  title: string;
  evidence_id: string;
  section: string;
  excerpt: string;
  content_sha256: string;
};
export type SourceSearch = { query: string; retrieval_mode: string; results: SourceSearchResult[] };

function sourceText(value: unknown, path: string, maximum: number, minimum = 1): string {
  return publicSourceText(boundedString(value, path, maximum, minimum), path);
}

function publicSourceText(text: string, path: string): string {
  if (/(?:\/Users\/|\/home\/|\/private\/|\/tmp\/|\/etc\/|\/var\/|\/opt\/|\/usr\/|\/root\/|\/Volumes\/|\/workspace\/|[A-Za-z]:\\|file:\/\/)/i.test(text)) {
    fail(path, "private location in source projection");
  }
  return text;
}

export function decodeSourceContent(value: unknown, path = "source_content"): SourceContent {
  const record = object(value, path);
  const kind = decodeString(record.kind, path + ".kind");
  if (!["notes", "full_text", "grounding"].includes(kind)) fail(path + ".kind", "unknown content kind");
  const start = integer(record.start_line, path + ".start_line");
  const end = integer(record.end_line, path + ".end_line");
  if (!((start === 0 && end === 0 && record.text === "") || (start >= 1 && end >= start))) fail(path, "invalid line range");
  const digest = decodeString(record.content_sha256, path + ".content_sha256");
  if (!SHA256.test(digest)) fail(path, "invalid document digest");
  const cursor = nullableString(record.next_cursor, path + ".next_cursor");
  if (cursor !== null && !/^[A-Za-z0-9_-]{1,512}$/.test(cursor)) fail(path, "invalid cursor");
  return {
    source_id: identifier(record.source_id, path + ".source_id"),
    canonical_id: sourceText(record.canonical_id, path + ".canonical_id", 500),
    kind: kind as SourceContentKind,
    text: sourceText(record.text, path + ".text", MAX_CONTENT_BYTES, 0),
    content_sha256: digest,
    start_line: start,
    end_line: end,
    next_cursor: cursor,
  };
}

// The document route refuses a retained file over 2 MiB, and the bound here is
// the same one in the same unit: UTF-8 bytes, not UTF-16 string length, which
// would admit a CJK document three times the size the route promises.
export const MAX_SOURCE_DOCUMENT_BYTES = 2_097_152;

export function decodeSourceDocument(value: unknown, path = "source_document"): SourceDocument {
  const record = object(value, path);
  const kind = decodeString(record.kind, path + ".kind");
  if (!["notes", "full_text", "grounding"].includes(kind)) fail(path + ".kind", "unknown content kind");
  const text = decodeString(record.text, path + ".text");
  // Every UTF-16 unit encodes to at least one byte, so an over-long string is
  // refused before it is encoded.
  if (text.length > MAX_SOURCE_DOCUMENT_BYTES || new TextEncoder().encode(text).byteLength > MAX_SOURCE_DOCUMENT_BYTES) {
    fail(path + ".text", "document exceeds 2 MiB");
  }
  const retained = integer(record.retained_bytes, path + ".retained_bytes");
  if (retained > MAX_SOURCE_DOCUMENT_BYTES) fail(path + ".retained_bytes", "document exceeds 2 MiB");
  return {
    source_id: identifier(record.source_id, path + ".source_id"),
    canonical_id: sourceText(record.canonical_id, path + ".canonical_id", 500),
    kind: kind as SourceContentKind,
    text: publicSourceText(text, path + ".text"),
    content_sha256: digest(record.content_sha256, path + ".content_sha256"),
    retained_bytes: retained,
    redacted: decodeBoolean(record.redacted, path + ".redacted"),
  };
}

export function decodeSourceSearch(value: unknown, path = "source_search"): SourceSearch {
  const record = object(value, path);
  return {
    query: sourceText(record.query, path + ".query", 1_024),
    retrieval_mode: sourceText(record.retrieval_mode, path + ".retrieval_mode", 100),
    results: boundedArray(record.results, path + ".results", 50, (value, path) => {
      const item = object(value, path);
      const digest = decodeString(item.content_sha256, path + ".content_sha256");
      if (!SHA256.test(digest)) fail(path, "invalid document digest");
      return {
        source_id: identifier(item.source_id, path + ".source_id"),
        canonical_id: sourceText(item.canonical_id, path + ".canonical_id", 500),
        title: sourceText(item.title, path + ".title", 2_000),
        evidence_id: sourceText(item.evidence_id, path + ".evidence_id", 2_000),
        section: sourceText(item.section, path + ".section", 2_000, 0),
        excerpt: sourceText(item.excerpt, path + ".excerpt", 20_000, 0),
        content_sha256: digest,
      };
    }),
  };
}

// -- XHS notes, blogs and recommendation links -------------------------------
//
// Every projection below pins its whole key set, the way a capture does: an
// unenumerated field is refused rather than dropped, because these routes sit
// beside task rows that hold signed image URLs and raw provider answers, and a
// field that should never have left Control must fail loudly here.

export const SOURCE_KINDS = ["paper", "blog", "xhs_note"] as const;
export type SourceKind = (typeof SOURCE_KINDS)[number];

export function isSourceKind(value: unknown): value is SourceKind {
  return (SOURCE_KINDS as readonly unknown[]).includes(value);
}

export const XHS_NOTE_STATES = [
  "discovered", "detail_ok", "assets_done", "ocr_done", "identified", "saved", "unsupported", "failed",
] as const;
export type XhsNoteState = (typeof XHS_NOTE_STATES)[number];
const XHS_STEP_STATES = ["pending", "ok", "failed"] as const;
export type XhsStepState = (typeof XHS_STEP_STATES)[number];
const XHS_OCR_FLAGS = ["empty", "truncated"] as const;
export const XHS_RECOMMENDATION_KINDS = ["paper", "blog", "other"] as const;
export type XhsRecommendationKind = (typeof XHS_RECOMMENDATION_KINDS)[number];
export const XHS_URL_STATES = [
  "none", "from_text", "auto_matched", "unverified", "not_found", "operator_set", "failed",
] as const;
export type XhsUrlState = (typeof XHS_URL_STATES)[number];
const XHS_URL_BEARING_STATES: readonly XhsUrlState[] = ["from_text", "auto_matched", "unverified", "operator_set"];
const XHS_ORIGINS = ["rule", "model", "rule+model"] as const;
export const XHS_IMPORT_STATES = ["none", "staged", "importing", "imported", "failed"] as const;
export type XhsImportState = (typeof XHS_IMPORT_STATES)[number];
const XHS_ROLES = ["curator", "author"] as const;
export type XhsRole = (typeof XHS_ROLES)[number];
export const XHS_SCAN_OUTCOMES = ["ok", "no_new_notes", "failed"] as const;
export type XhsScanOutcome = (typeof XHS_SCAN_OUTCOMES)[number];
export const XHS_IMPORT_DISPOSITIONS = ["capture_staged", "capture_reused", "blog_import_queued", "refused"] as const;
export type XhsImportDisposition = (typeof XHS_IMPORT_DISPOSITIONS)[number];
export const XHS_IMPORT_REFUSALS = ["not_found", "already_imported", "not_importable", "no_url", "no_arxiv_id"] as const;
export type XhsImportRefusal = (typeof XHS_IMPORT_REFUSALS)[number];
const XHS_REFUSALS = ["disabled_in_config", "roots_not_ready"] as const;
export type XhsRefusal = (typeof XHS_REFUSALS)[number];
const XHS_ROOT_IDS = ["xhs-notes", "blogs"] as const;
const XHS_ROOT_STATES = ["ready", "disabled", "missing", "overlaps_corpus"] as const;
const XHS_SCHEDULE_KEYS = ["xhs-pull", "xhs-drain"] as const;
const XHS_SCHEDULE_OUTCOMES = ["ran", "skipped", "refused", "failed"] as const;
const XHS_TASK_STATES = ["canceled", "done", "failed", "pending", "running"] as const;
const XHS_USAGE_PROVIDERS = ["gpt", "ocr", "tikhub"] as const;
export const XHS_FAILURE_PROVIDERS = ["tikhub", "cdn", "ocr", "gpt", "blog"] as const;
export type XhsFailureProvider = (typeof XHS_FAILURE_PROVIDERS)[number];
const XHS_IMAGE_MEDIA_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"] as const;
const XHS_ID = /^[0-9a-f]{24}$/;
// A failure category as Control stores it: a short snake_case token, never text.
const XHS_CATEGORY = /^[a-z_]{1,64}$/;
const XHS_ENGINE = /^[0-9a-z.-]{1,64}$/;
const XHS_ASSET_PATH = /^assets\/(?:[1-9][0-9]?|100)-[0-9a-f]{12}\.(?:jpg|png|webp|gif)$/;
const XHS_TASK_ID = /^[\x21-\x7e]{1,300}$/;
const ARXIV_ID = /^[0-9]{4}\.[0-9]{4,5}$/;
const PUBLIC_ID = /^[A-Za-z0-9_-]{1,200}$/;
const MAX_XHS_IMAGES = 100;
const MAX_XHS_RECOMMENDATIONS = 500;
const MAX_XHS_IMPORT = 100;
const MAX_SOURCE_LINKS = 1_000;
const MAX_XHS_BLOGGERS = 500;

export type XhsNoteHeader = {
  source_id: string;
  note_id: string;
  title: string;
  state: XhsNoteState;
  last_error: string | null;
  content_version: number;
  revision: number;
};

export type XhsImage = {
  ordinal: number;
  // Relative to the note's latest saved version, for the asset route; null
  // until the image is downloaded.
  asset_path: string | null;
  media_type: string | null;
  width: number | null;
  height: number | null;
  download_state: XhsStepState;
  download_error: string | null;
  ocr_state: XhsStepState;
  ocr_error: string | null;
  ocr_engine: string | null;
  ocr_flags: Array<(typeof XHS_OCR_FLAGS)[number]>;
};

export type XhsRecommendation = {
  id: string;
  // Null when the caption, not an image, is the evidence.
  image_ordinal: number | null;
  kind: XhsRecommendationKind;
  title: string;
  quote: string;
  arxiv_id: string | null;
  url: string | null;
  url_state: XhsUrlState;
  url_checked_title: string | null;
  origin: (typeof XHS_ORIGINS)[number];
  identify_run: string;
  capture_id: string | null;
  capture_state: CaptureState | null;
  capture_revision: number | null;
  import_state: XhsImportState;
  imported_source_id: string | null;
  imported_source_kind: SourceKind | null;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type XhsNote = XhsNoteHeader & {
  blogger: { user_id: string; name: string | null; role: XhsRole | null };
  permalink: string;
  published_at: string | null;
  caption: string;
  caption_complete: boolean;
  images: XhsImage[];
  recommendations: XhsRecommendation[];
};

export type SourceLinkEntry = {
  source_id: string;
  source_kind: SourceKind;
  title: string;
  image_ordinal: number | null;
  recommendation_id: string;
  created_at: string;
};

export type SourceLinks = {
  // The notes that recommend this source; only an XHS note recommends.
  recommended_in: SourceLinkEntry[];
  // What this source recommends; empty for anything but a note.
  recommends: SourceLinkEntry[];
};

export type XhsImportItem = {
  recommendation_id: string;
  disposition: XhsImportDisposition;
  reason: XhsImportRefusal | null;
  capture_id: string | null;
  capture_revision: number | null;
  capture_state: CaptureState | null;
  recommendation: XhsRecommendation | null;
};

export type XhsImportResult = { note_source_id: string; items: XhsImportItem[] };
export type XhsLinkResult = { recommendation: XhsRecommendation };
export type XhsImageRetryResult = { note: XhsNoteHeader; image: XhsImage };

export type XhsBloggerStatus = {
  user_id: string;
  display_name: string | null;
  role: XhsRole;
  followed: boolean;
  last_scan_at: string | null;
  last_scan_outcome: XhsScanOutcome | null;
  last_scan_error: string | null;
  last_new_note_at: string | null;
};

export type XhsScheduleStatus = {
  enabled: boolean;
  revision: number;
  interval_seconds: number;
  next_due_at: string | null;
  last_outcome: (typeof XHS_SCHEDULE_OUTCOMES)[number] | null;
};

export type XhsStatus = {
  // Configured, both roots ready and both schedule rows armed.
  enabled: boolean;
  enabled_in_config: boolean;
  roots_ready: boolean;
  refusal: XhsRefusal | null;
  roots: Record<(typeof XHS_ROOT_IDS)[number], (typeof XHS_ROOT_STATES)[number]>;
  schedules: Record<(typeof XHS_SCHEDULE_KEYS)[number], XhsScheduleStatus>;
  bloggers: XhsBloggerStatus[];
  tasks: Record<(typeof XHS_TASK_STATES)[number], number>;
  usage: Record<(typeof XHS_USAGE_PROVIDERS)[number], { calls: number; cap: number }>;
  last_failures: Record<XhsFailureProvider, string | null>;
};

function exactRecord(value: unknown, path: string, fields: readonly string[]): ObjectValue {
  const record = object(value, path);
  for (const name of Object.keys(record)) {
    if (!fields.includes(name)) fail(path + "." + name, "unexpected field");
  }
  const missing = fields.find((name) => !(name in record));
  if (missing) fail(path + "." + missing, "expected a field");
  return record;
}

function oneOf<T extends string>(value: unknown, path: string, allowed: readonly T[]): T {
  const decoded = decodeString(value, path);
  if (!(allowed as readonly string[]).includes(decoded)) fail(path, "unknown value");
  return decoded as T;
}

function nullableOneOf<T extends string>(value: unknown, path: string, allowed: readonly T[]): T | null {
  return value === null ? null : oneOf(value, path, allowed);
}

function matching(value: unknown, path: string, pattern: RegExp): string {
  const decoded = decodeString(value, path);
  if (!pattern.test(decoded)) fail(path, "unexpected shape");
  return decoded;
}

function nullableMatching(value: unknown, path: string, pattern: RegExp): string | null {
  return value === null ? null : matching(value, path, pattern);
}

function boundedInteger(value: unknown, path: string, minimum: number, maximum: number): number {
  const decoded = integer(value, path);
  if (decoded < minimum || decoded > maximum) fail(path, "integer is outside the contract bound");
  return decoded;
}

function nullableBoundedInteger(value: unknown, path: string, minimum: number, maximum: number): number | null {
  return value === null ? null : boundedInteger(value, path, minimum, maximum);
}

function nullableSourceText(value: unknown, path: string, maximum: number): string | null {
  return value === null ? null : sourceText(value, path, maximum);
}

function category(value: unknown, path: string): string | null {
  return nullableMatching(value, path, XHS_CATEGORY);
}

// A link shown to the operator: http or https only, never with credentials.
function publicLink(value: unknown, path: string): string {
  const text = boundedString(value, path, 2_000);
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch {
    fail(path, "expected an absolute URL");
  }
  if ((parsed.protocol !== "https:" && parsed.protocol !== "http:") || parsed.username || parsed.password) {
    fail(path, "expected an http or https URL without credentials");
  }
  return text;
}

const XHS_NOTE_HEADER_FIELDS = ["source_id", "note_id", "title", "state", "last_error", "content_version", "revision"];

function noteHeader(record: ObjectValue, path: string): XhsNoteHeader {
  return {
    source_id: identifier(record.source_id, path + ".source_id"),
    note_id: matching(record.note_id, path + ".note_id", XHS_ID),
    title: sourceText(record.title, path + ".title", 500, 0),
    state: oneOf(record.state, path + ".state", XHS_NOTE_STATES),
    last_error: category(record.last_error, path + ".last_error"),
    content_version: positiveInteger(record.content_version, path + ".content_version"),
    revision: integer(record.revision, path + ".revision"),
  };
}

export function decodeXhsNoteHeader(value: unknown, path = "xhs_note"): XhsNoteHeader {
  return noteHeader(exactRecord(value, path, XHS_NOTE_HEADER_FIELDS), path);
}

const XHS_IMAGE_FIELDS = [
  "ordinal", "asset_path", "media_type", "width", "height", "download_state", "download_error",
  "ocr_state", "ocr_error", "ocr_engine", "ocr_flags",
];

export function decodeXhsImage(value: unknown, path = "xhs_image"): XhsImage {
  const record = exactRecord(value, path, XHS_IMAGE_FIELDS);
  const downloadState = oneOf(record.download_state, path + ".download_state", XHS_STEP_STATES);
  const ocrState = oneOf(record.ocr_state, path + ".ocr_state", XHS_STEP_STATES);
  const downloadError = category(record.download_error, path + ".download_error");
  const ocrError = category(record.ocr_error, path + ".ocr_error");
  const assetPath = nullableMatching(record.asset_path, path + ".asset_path", XHS_ASSET_PATH);
  const flags = boundedArray(record.ocr_flags, path + ".ocr_flags", XHS_OCR_FLAGS.length, (item, itemPath) => oneOf(item, itemPath, XHS_OCR_FLAGS));
  requireUnique(flags, path + ".ocr_flags");
  // The pairs Control's own table enforces: an error exactly when a step
  // failed, a file only once it is downloaded, flags only on a transcription.
  if ((downloadError !== null) !== (downloadState === "failed")) fail(path + ".download_error", "error does not match the download state");
  if ((ocrError !== null) !== (ocrState === "failed")) fail(path + ".ocr_error", "error does not match the transcription state");
  if (assetPath !== null && downloadState !== "ok") fail(path + ".asset_path", "an image that is not downloaded has no file");
  if (flags.length && ocrState !== "ok") fail(path + ".ocr_flags", "flags belong to a transcription");
  const ordinal = boundedInteger(record.ordinal, path + ".ordinal", 1, MAX_XHS_IMAGES);
  if (assetPath !== null && !assetPath.startsWith(`assets/${ordinal}-`)) fail(path + ".asset_path", "file belongs to another image");
  return {
    ordinal,
    asset_path: assetPath,
    media_type: nullableOneOf(record.media_type, path + ".media_type", XHS_IMAGE_MEDIA_TYPES),
    width: nullableBoundedInteger(record.width, path + ".width", 1, 100_000),
    height: nullableBoundedInteger(record.height, path + ".height", 1, 100_000),
    download_state: downloadState,
    download_error: downloadError,
    ocr_state: ocrState,
    ocr_error: ocrError,
    ocr_engine: nullableMatching(record.ocr_engine, path + ".ocr_engine", XHS_ENGINE),
    ocr_flags: flags,
  };
}

const XHS_RECOMMENDATION_FIELDS = [
  "id", "image_ordinal", "kind", "title", "quote", "arxiv_id", "url", "url_state", "url_checked_title",
  "origin", "identify_run", "capture_id", "capture_state", "capture_revision", "import_state",
  "imported_source_id", "imported_source_kind", "revision", "created_at", "updated_at",
];

export function decodeXhsRecommendation(value: unknown, path = "xhs_recommendation"): XhsRecommendation {
  const record = exactRecord(value, path, XHS_RECOMMENDATION_FIELDS);
  const kind = oneOf(record.kind, path + ".kind", XHS_RECOMMENDATION_KINDS);
  const arxivId = nullableMatching(record.arxiv_id, path + ".arxiv_id", ARXIV_ID);
  const url = record.url === null ? null : publicLink(record.url, path + ".url");
  const urlState = oneOf(record.url_state, path + ".url_state", XHS_URL_STATES);
  const captureId = record.capture_id === null ? null : identifier(record.capture_id, path + ".capture_id");
  const captureState = nullableOneOf(record.capture_state, path + ".capture_state", CAPTURE_STATES);
  const captureRevision = record.capture_revision === null ? null : integer(record.capture_revision, path + ".capture_revision");
  const importState = oneOf(record.import_state, path + ".import_state", XHS_IMPORT_STATES);
  const importedId = record.imported_source_id === null ? null : identifier(record.imported_source_id, path + ".imported_source_id");
  const importedKind = nullableOneOf(record.imported_source_kind, path + ".imported_source_kind", SOURCE_KINDS);
  if (kind !== "paper" && arxivId !== null) fail(path + ".arxiv_id", "only a paper carries an arXiv identifier");
  // Control withholds a link that looks sensitive, so a link-bearing state can
  // arrive without one; a link never arrives under any other state.
  if (url !== null && !XHS_URL_BEARING_STATES.includes(urlState)) fail(path + ".url", "link does not match its state");
  if ((captureId === null) !== (captureState === null) || (captureId === null) !== (captureRevision === null)) {
    fail(path + ".capture_id", "capture state does not match the capture");
  }
  if ((importedId !== null) !== (importState === "imported") || (importedId === null) !== (importedKind === null)) {
    fail(path + ".imported_source_id", "imported source does not match the import state");
  }
  return {
    id: matching(record.id, path + ".id", PUBLIC_ID),
    image_ordinal: nullableBoundedInteger(record.image_ordinal, path + ".image_ordinal", 1, MAX_XHS_IMAGES),
    kind,
    title: sourceText(record.title, path + ".title", 1_000),
    quote: sourceText(record.quote, path + ".quote", 4_000),
    arxiv_id: arxivId,
    url,
    url_state: urlState,
    url_checked_title: nullableSourceText(record.url_checked_title, path + ".url_checked_title", 1_000),
    origin: oneOf(record.origin, path + ".origin", XHS_ORIGINS),
    identify_run: matching(record.identify_run, path + ".identify_run", XHS_TASK_ID),
    capture_id: captureId,
    capture_state: captureState,
    capture_revision: captureRevision,
    import_state: importState,
    imported_source_id: importedId,
    imported_source_kind: importedKind,
    revision: integer(record.revision, path + ".revision"),
    created_at: timestamp(record.created_at, path + ".created_at"),
    updated_at: timestamp(record.updated_at, path + ".updated_at"),
  };
}

export function decodeXhsNote(value: unknown, path = "xhs_note"): XhsNote {
  const record = exactRecord(value, path, [
    ...XHS_NOTE_HEADER_FIELDS, "blogger", "permalink", "published_at", "caption", "caption_complete",
    "images", "recommendations",
  ]);
  const header = noteHeader(record, path);
  const blogger = exactRecord(record.blogger, path + ".blogger", ["user_id", "name", "role"]);
  // The permalink is derived from the note's own identity and nothing else.
  const permalink = decodeString(record.permalink, path + ".permalink");
  if (permalink !== `https://www.xiaohongshu.com/explore/${header.note_id}`) fail(path + ".permalink", "permalink does not name this note");
  const images = boundedArray(record.images, path + ".images", MAX_XHS_IMAGES, decodeXhsImage);
  requireCanonicalOrder(images.map((image) => String(image.ordinal).padStart(3, "0")), path + ".images");
  const recommendations = boundedArray(record.recommendations, path + ".recommendations", MAX_XHS_RECOMMENDATIONS, decodeXhsRecommendation);
  requireUnique(recommendations.map((item) => item.id), path + ".recommendations");
  const ordinals = new Set(images.map((image) => image.ordinal));
  recommendations.forEach((item, index) => {
    if (item.image_ordinal !== null && !ordinals.has(item.image_ordinal)) {
      fail(path + ".recommendations[" + index + "].image_ordinal", "recommendation cites an image the note does not have");
    }
  });
  return {
    ...header,
    blogger: {
      user_id: matching(blogger.user_id, path + ".blogger.user_id", XHS_ID),
      name: nullableSourceText(blogger.name, path + ".blogger.name", 200),
      role: nullableOneOf(blogger.role, path + ".blogger.role", XHS_ROLES),
    },
    permalink,
    published_at: nullableTimestamp(record.published_at, path + ".published_at"),
    caption: sourceText(record.caption, path + ".caption", 20_000, 0),
    caption_complete: decodeBoolean(record.caption_complete, path + ".caption_complete"),
    images,
    recommendations,
  };
}

function decodeSourceLinkEntry(value: unknown, path: string): SourceLinkEntry {
  const record = exactRecord(value, path, ["source_id", "source_kind", "title", "image_ordinal", "recommendation_id", "created_at"]);
  return {
    source_id: identifier(record.source_id, path + ".source_id"),
    source_kind: oneOf(record.source_kind, path + ".source_kind", SOURCE_KINDS),
    title: sourceText(record.title, path + ".title", 2_000),
    image_ordinal: nullableBoundedInteger(record.image_ordinal, path + ".image_ordinal", 1, MAX_XHS_IMAGES),
    recommendation_id: matching(record.recommendation_id, path + ".recommendation_id", PUBLIC_ID),
    created_at: timestamp(record.created_at, path + ".created_at"),
  };
}

export function decodeSourceLinks(value: unknown, path = "source_links"): SourceLinks {
  const record = exactRecord(value, path, ["recommended_in", "recommends"]);
  const recommendedIn = boundedArray(record.recommended_in, path + ".recommended_in", MAX_SOURCE_LINKS, decodeSourceLinkEntry);
  const recommends = boundedArray(record.recommends, path + ".recommends", MAX_SOURCE_LINKS, decodeSourceLinkEntry);
  // A link's from end is always a note, and one recommendation makes one link.
  recommendedIn.forEach((entry, index) => {
    if (entry.source_kind !== "xhs_note") fail(path + ".recommended_in[" + index + "].source_kind", "only a note recommends");
  });
  requireUnique(recommendedIn.map((entry) => entry.recommendation_id), path + ".recommended_in");
  requireUnique(recommends.map((entry) => entry.recommendation_id), path + ".recommends");
  return { recommended_in: recommendedIn, recommends };
}

function decodeXhsImportItem(value: unknown, path: string): XhsImportItem {
  const record = exactRecord(value, path, [
    "recommendation_id", "disposition", "reason", "capture_id", "capture_revision", "capture_state", "recommendation",
  ]);
  const recommendationId = matching(record.recommendation_id, path + ".recommendation_id", PUBLIC_ID);
  const disposition = oneOf(record.disposition, path + ".disposition", XHS_IMPORT_DISPOSITIONS);
  const reason = nullableOneOf(record.reason, path + ".reason", XHS_IMPORT_REFUSALS);
  const captureId = record.capture_id === null ? null : identifier(record.capture_id, path + ".capture_id");
  const captureRevision = record.capture_revision === null ? null : integer(record.capture_revision, path + ".capture_revision");
  const captureState = nullableOneOf(record.capture_state, path + ".capture_state", CAPTURE_STATES);
  const recommendation = record.recommendation === null ? null : decodeXhsRecommendation(record.recommendation, path + ".recommendation");
  if ((disposition === "refused") !== (reason !== null)) fail(path + ".reason", "a refusal, and only a refusal, carries a reason");
  const staged = disposition === "capture_staged" || disposition === "capture_reused";
  if (staged !== (captureId !== null) || staged !== (captureRevision !== null) || staged !== (captureState !== null)) {
    fail(path + ".capture_id", "a Capture is reported exactly for a staged paper");
  }
  if (recommendation === null ? disposition !== "refused" : recommendation.id !== recommendationId) {
    fail(path + ".recommendation", "recommendation does not match the item");
  }
  return {
    recommendation_id: recommendationId,
    disposition,
    reason,
    capture_id: captureId,
    capture_revision: captureRevision,
    capture_state: captureState,
    recommendation,
  };
}

export function decodeXhsImportResult(value: unknown, path = "xhs_import"): XhsImportResult {
  const record = exactRecord(value, path, ["note_source_id", "items"]);
  const items = boundedArray(record.items, path + ".items", MAX_XHS_IMPORT, decodeXhsImportItem);
  requireUnique(items.map((item) => item.recommendation_id), path + ".items");
  return { note_source_id: identifier(record.note_source_id, path + ".note_source_id"), items };
}

export function decodeXhsLinkResult(value: unknown, path = "xhs_link"): XhsLinkResult {
  const record = exactRecord(value, path, ["recommendation"]);
  return { recommendation: decodeXhsRecommendation(record.recommendation, path + ".recommendation") };
}

export function decodeXhsImageRetryResult(value: unknown, path = "xhs_image_retry"): XhsImageRetryResult {
  const record = exactRecord(value, path, ["note", "image"]);
  return {
    note: decodeXhsNoteHeader(record.note, path + ".note"),
    image: decodeXhsImage(record.image, path + ".image"),
  };
}

function keyedRecord<K extends string, T>(
  value: unknown,
  path: string,
  keys: readonly K[],
  decoder: (item: unknown, itemPath: string) => T,
): Record<K, T> {
  const record = exactRecord(value, path, keys);
  return Object.fromEntries(keys.map((key) => [key, decoder(record[key], path + "." + key)])) as Record<K, T>;
}

function decodeXhsBloggerStatus(value: unknown, path: string): XhsBloggerStatus {
  const record = exactRecord(value, path, [
    "user_id", "display_name", "role", "followed", "last_scan_at", "last_scan_outcome", "last_scan_error", "last_new_note_at",
  ]);
  const scannedAt = nullableTimestamp(record.last_scan_at, path + ".last_scan_at");
  const outcome = nullableOneOf(record.last_scan_outcome, path + ".last_scan_outcome", XHS_SCAN_OUTCOMES);
  const error = category(record.last_scan_error, path + ".last_scan_error");
  // Success, nothing new and failure stay three answers: only a failure has a
  // category, and only a scan that ran has an outcome.
  if ((scannedAt === null) !== (outcome === null)) fail(path + ".last_scan_outcome", "outcome does not match the scan time");
  if ((error !== null) !== (outcome === "failed")) fail(path + ".last_scan_error", "a failed scan, and only a failed scan, carries a category");
  return {
    user_id: matching(record.user_id, path + ".user_id", XHS_ID),
    display_name: nullableSourceText(record.display_name, path + ".display_name", 200),
    role: oneOf(record.role, path + ".role", XHS_ROLES),
    followed: decodeBoolean(record.followed, path + ".followed"),
    last_scan_at: scannedAt,
    last_scan_outcome: outcome,
    last_scan_error: error,
    last_new_note_at: nullableTimestamp(record.last_new_note_at, path + ".last_new_note_at"),
  };
}

export function decodeXhsStatus(value: unknown, path = "xhs_status"): XhsStatus {
  const record = exactRecord(value, path, [
    "enabled", "enabled_in_config", "roots_ready", "refusal", "roots", "schedules", "bloggers", "tasks", "usage", "last_failures",
  ]);
  return {
    enabled: decodeBoolean(record.enabled, path + ".enabled"),
    enabled_in_config: decodeBoolean(record.enabled_in_config, path + ".enabled_in_config"),
    roots_ready: decodeBoolean(record.roots_ready, path + ".roots_ready"),
    refusal: nullableOneOf(record.refusal, path + ".refusal", XHS_REFUSALS),
    roots: keyedRecord(record.roots, path + ".roots", XHS_ROOT_IDS, (item, itemPath) => oneOf(item, itemPath, XHS_ROOT_STATES)),
    schedules: keyedRecord(record.schedules, path + ".schedules", XHS_SCHEDULE_KEYS, (item, itemPath) => {
      const schedule = exactRecord(item, itemPath, ["enabled", "revision", "interval_seconds", "next_due_at", "last_outcome"]);
      return {
        enabled: decodeBoolean(schedule.enabled, itemPath + ".enabled"),
        revision: integer(schedule.revision, itemPath + ".revision"),
        interval_seconds: positiveInteger(schedule.interval_seconds, itemPath + ".interval_seconds"),
        next_due_at: nullableTimestamp(schedule.next_due_at, itemPath + ".next_due_at"),
        last_outcome: nullableOneOf(schedule.last_outcome, itemPath + ".last_outcome", XHS_SCHEDULE_OUTCOMES),
      };
    }),
    bloggers: boundedArray(record.bloggers, path + ".bloggers", MAX_XHS_BLOGGERS, decodeXhsBloggerStatus),
    tasks: keyedRecord(record.tasks, path + ".tasks", XHS_TASK_STATES, integer),
    usage: keyedRecord(record.usage, path + ".usage", XHS_USAGE_PROVIDERS, (item, itemPath) => {
      const usage = exactRecord(item, itemPath, ["calls", "cap"]);
      return { calls: integer(usage.calls, itemPath + ".calls"), cap: integer(usage.cap, itemPath + ".cap") };
    }),
    last_failures: keyedRecord(record.last_failures, path + ".last_failures", XHS_FAILURE_PROVIDERS, category),
  };
}
