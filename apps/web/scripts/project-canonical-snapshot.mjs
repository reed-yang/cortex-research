const sourceProjection = (sources) =>
  sources.map(
    ({ source_id, canonical_id, official_title, aliases, import_state }) => ({
      source_id,
      canonical_id,
      official_title,
      aliases,
      import_state,
    }),
  );

const decisionProjection = (decision) => ({
  decision_id: decision.decision_id,
  kind: decision.kind,
  status: decision.status,
  revision: decision.revision,
  options: decision.options,
  selected: decision.selected,
});

const eventProjection = (events) =>
  events.map(({ id, sequence, type, attempt_id, durability }) => ({
    id,
    sequence,
    type,
    attempt_id,
    durability,
  }));

const artifactProjection = (artifact) => ({
  artifact_id: artifact.artifact_id,
  kind: artifact.kind,
  thread_id: artifact.thread_id,
  mutable: artifact.mutable,
  references: artifact.references,
  versions: artifact.versions,
});

export function projectCanonicalSnapshot(g0, g1) {
  const immutableSnapshot = g1.artifacts.find((artifact) => artifact.kind === "snapshot");
  if (!immutableSnapshot) throw new Error("Canonical G1 snapshot artifact is required");

  return {
    g0: {
      schema_version: g0.schema_version,
      fixture_id: g0.fixture_id,
      fixture_stage: g0.fixture_stage,
      workspace: g0.workspace,
      run: g0.run,
      source_intent: {
        source_intent_id: g0.source_intent.source_intent_id,
        title_canonical_id: g0.source_intent.title_canonical_id,
        url_canonical_id: g0.source_intent.url_canonical_id,
      },
      source_candidates: sourceProjection(g0.sources.candidates),
      decision: decisionProjection(g0.decisions[0]),
      events: eventProjection(g0.events),
      scopes: {
        control: g0.control_state_manifest.scope,
        research: g0.mutation_manifest.scope,
        research_excludes_control_entities: g0.mutation_manifest.excludes_control_entities,
      },
      persisted_control: {
        run_ids: g0.control_state_manifest.persisted_run_ids,
        attempt_ids: g0.control_state_manifest.persisted_attempt_ids,
        source_intent_ids: g0.control_state_manifest.persisted_source_intent_ids,
        decision_ids: g0.control_state_manifest.persisted_decision_ids,
        event_ids: g0.control_state_manifest.persisted_event_ids,
      },
      research_side_effects: {
        transactions: g0.mutation_manifest.transactions.length,
        materializations: g0.mutation_manifest.materializations.length,
      },
    },
    g1: {
      schema_version: g1.schema_version,
      fixture_id: g1.fixture_id,
      fixture_stage: g1.fixture_stage,
      workspace: g1.workspace,
      run: g1.run,
      source_candidates: sourceProjection(g1.sources.candidates),
      decisions: g1.decisions.map(decisionProjection),
      lineage: {
        nodes: g1.lineage.final_nodes,
        links: g1.lineage.links,
      },
      threads: g1.threads.map(({ thread_id, kind, title }) => ({ thread_id, kind, title })),
      artifacts: g1.artifacts.map(artifactProjection),
      immutable_snapshot: artifactProjection(immutableSnapshot),
      events: eventProjection(g1.events),
      scopes: {
        control: g1.control_state_manifest.scope,
        research: g1.mutation_manifest.scope,
        research_excludes_control_entities: g1.mutation_manifest.excludes_control_entities,
      },
    },
  };
}
