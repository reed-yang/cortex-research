import {
  ContractDecodeError,
  boolean as decodeBoolean,
  decodeRun,
  integer,
  nullableString,
  object,
  string as decodeString,
  type Decision,
  type JsonValue,
  type ListEnvelope,
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
  const text = boundedString(value, path, maximum, minimum);
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
