"use client";

import { useEffect, useRef, useState } from "react";
import { ArtifactDocument } from "./artifact-document";
import { ControlProblemError, type CortexControlClient } from "./client";
import type {
  ArtifactVersionContent,
  ArtifactVersionProjection,
  ResearchArtifactProjection,
  ResearchWorkflowProjection,
} from "./research-contracts";

// The July card rules paint `#fbfcf9` inside `#dedfd8` and the stage accents
// come from bridged tokens; only an unaccented stage needs its border fixed.
const CARD = "border-border! bg-card! text-card-foreground!";
const STAGE_ACCENTED = new Set(["completed", "running", "waiting", "failed"]);

function StagePanel({ research }: { research: ResearchWorkflowProjection }) {
  const stages = research.workflow?.stages ?? [];
  return (
    <section aria-labelledby="workflow-stage-title" className="research-section workflow-stage-panel">
      <div className="research-section-heading">
        <div><span className="eyebrow text-muted-foreground!">Workflow</span><h2 id="workflow-stage-title">Research stages</h2></div>
        <span className="meta-chip bg-muted! text-muted-foreground!">{research.workflow?.state ?? "not installed"}</span>
      </div>
      {stages.length ? (
        <ol className="research-stage-list">
          {stages.map((stage) => (
            <li className={`research-stage ${stage.state} bg-muted/50! ${STAGE_ACCENTED.has(stage.state) ? "" : "border-t-border!"}`} key={stage.key}>
              <span className="research-stage-index text-muted-foreground!">{String(stage.position + 1).padStart(2, "0")}</span>
              <div><strong>{stage.key.replaceAll("_", " ")}</strong><span className="text-muted-foreground!">{stage.effect} · {stage.state}</span><details data-details><summary>Details</summary>Revision {stage.revision}</details></div>
            </li>
          ))}
        </ol>
      ) : <p className="research-empty text-muted-foreground!">No sealed workflow is attached to this run.</p>}
    </section>
  );
}

function SourcePanel({ research }: { research: ResearchWorkflowProjection }) {
  return (
    <section aria-labelledby="research-source-title" className="research-section" id="mobile-sources">
      <div className="research-section-heading">
        <div><span className="eyebrow text-muted-foreground!">Source identity</span><h2 id="research-source-title">Sources and gates</h2></div>
        <span className="meta-chip bg-muted! text-muted-foreground!">{research.sources.length} {research.sources.some((source) => source.research_selection) ? "sources" : "bound"}</span>
      </div>
      {research.source_gates.map((gate) => (
        <article className="source-gate border-t-border!" key={gate.id}>
          <div className="source-gate-observations">
            <div className={CARD}><span className="text-muted-foreground!">Title observation</span><strong>{gate.title_observation ?? "Unavailable"}</strong></div>
            <div className={CARD}><span className="text-muted-foreground!">Locator observation</span><strong>{gate.locator_observation ?? "Unavailable"}</strong></div>
          </div>
          <div className="source-candidates">
            {gate.candidates.map((candidate) => (
              <div className={`source-candidate ${CARD}`} key={candidate.id}>
                <span className="text-muted-foreground!">{candidate.claim_kind}</span>
                <strong>{candidate.official_title}</strong>
                <code className="text-muted-foreground!">{candidate.canonical_id}</code>
              </div>
            ))}
          </div>
          <p className="source-gate-state text-muted-foreground!">{gate.state.replaceAll("_", " ")}</p>
          <details data-details><summary>Details</summary>Revision {gate.revision}</details>
        </article>
      ))}
      {research.source_gates.length === 0 ? <p className="research-empty text-muted-foreground!">No Source gate was required for this run.</p> : null}
      <div className="bound-source-list">
        {research.sources.map((binding) => (
          <article className={`bound-source ${CARD}`} key={binding.id}>
            <span className="text-muted-foreground!">{binding.research_selection ? `${binding.research_selection.label} · Research selection` : binding.disposition}</span><strong>{binding.source.official_title}</strong>
            <code className="text-muted-foreground!">{binding.source.canonical_id}</code>
          </article>
        ))}
      </div>
    </section>
  );
}

function LineagePanel({ research }: { research: ResearchWorkflowProjection }) {
  const nodes = new Map(research.lineage.nodes.map((node) => [node.id, node]));
  return (
    <section aria-labelledby="research-lineage-title" className="research-section">
      <div className="research-section-heading">
        <div><span className="eyebrow text-muted-foreground!">Lineage</span><h2 id="research-lineage-title">Idea evolution</h2></div>
        <span className="meta-chip bg-muted! text-muted-foreground!">{research.lineage.links.length} direct links</span>
      </div>
      {research.lineage.nodes.length ? (
        <div className="research-lineage">
          <div className="lineage-node-list">
            {research.lineage.nodes.map((node) => (
              <article className={`lineage-node ${node.status} ${CARD} ${node.status === "dormant" ? "border-l-muted-foreground!" : ""}`} key={node.id}>
                <span className="text-muted-foreground!">{node.status}</span><strong>{node.title}</strong><details data-details><summary>Details</summary>Revision {node.revision}</details>
              </article>
            ))}
          </div>
          <ul className="lineage-link-list">
            {research.lineage.links.map((link) => (
              <li className="border-t-border!" key={link.id}>
                <strong>{nodes.get(link.from_node_id)?.title ?? "Unavailable idea"}</strong>
                <span className="text-muted-foreground!">{link.relation.replaceAll("_", " ")}</span>
                <strong>{nodes.get(link.to_node_id)?.title ?? "Unavailable idea"}</strong>
              </li>
            ))}
          </ul>
        </div>
      ) : <p className="research-empty text-muted-foreground!">No lineage effect has been sealed for this run.</p>}
    </section>
  );
}

function versionLabel(version: ArtifactVersionProjection): string {
  return `v${version.logical_version}`;
}

function ArtifactMetadata({ version }: { version: ArtifactVersionProjection }) {
  return (
    <dl className="artifact-metadata [&>div]:border-l-border! [&_dt]:text-muted-foreground!">
      <div><dt>Version ID</dt><dd>{version.id}</dd></div>
      <div><dt>Digest</dt><dd>{version.sha256}</dd></div>
      <div><dt>Generator</dt><dd>{version.generator.name} {version.generator.version}</dd></div>
      <div><dt>Tool</dt><dd>{version.tool.name} {version.tool.version}</dd></div>
      <div><dt>Paper sources</dt><dd>{version.source_ids.join(", ") || "None selected"}</dd></div>
      {version.provenance.document_version_ids?.length ? <div><dt>Dossier documents</dt><dd>{version.provenance.document_version_ids.length} retained document versions</dd></div> : null}
      <div><dt>Parents</dt><dd>{version.parents.length ? version.parents.map((parent) => parent.artifact_version_id).join(", ") : "Root version"}</dd></div>
    </dl>
  );
}

// The refusal categories the artifact content route actually answers with
// (`cortex_platform/product/api/app.py`). Anything outside this set keeps the
// general sentence: the projection carries no failure category of its own, and
// a run that merely failed does not name a reason it never reported.
const CONTENT_REFUSALS: Record<string, string> = {
  artifact_content_unavailable: "This version's content is missing or could not be verified. Try another version.",
  not_found: "This version is no longer stored. Reopen the run to see its current outputs.",
  control_store_unavailable: "Cortex could not read this version just now. Try again in a moment.",
};

function contentError(offline: boolean, category: string | null): string {
  if (offline) return "Content was not cached. Reconnect to read this version.";
  return (category && CONTENT_REFUSALS[category]) ?? "Verified content is unavailable.";
}

function selectHead(artifact: ResearchArtifactProjection): ArtifactVersionProjection | null {
  return artifact.versions.find((version) => version.id === artifact.head_artifact_version_id)
    ?? artifact.versions.at(-1)
    ?? null;
}

function OutputPanel({
  client,
  offline,
  research,
}: {
  client: CortexControlClient;
  offline: boolean;
  research: ResearchWorkflowProjection;
}) {
  const [artifactId, setArtifactId] = useState<string | null>(research.artifacts[0]?.id ?? null);
  const artifact = research.artifacts.find((item) => item.id === artifactId) ?? research.artifacts[0] ?? null;
  const [versionId, setVersionId] = useState<string | null>(artifact ? selectHead(artifact)?.id ?? null : null);
  const version = artifact?.versions.find((item) => item.id === versionId) ?? (artifact ? selectHead(artifact) : null);
  const [contentResult, setContentResult] = useState<{
    versionId: string;
    state: "idle" | "error";
    value: ArtifactVersionContent | null;
    category: string | null;
  } | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const requestGeneration = generation.current + 1;
    generation.current = requestGeneration;
    if (!version) return;
    void client.getArtifactVersionContent(version.id).then((value) => {
      if (generation.current !== requestGeneration) return;
      setContentResult({ versionId: version.id, state: "idle", value, category: null });
    }).catch((error: unknown) => {
      if (generation.current === requestGeneration) {
        setContentResult({
          versionId: version.id,
          state: "error",
          value: null,
          category: error instanceof ControlProblemError ? error.problem.category : null,
        });
      }
    });
  }, [client, version]);

  const contentState = !version
    ? "idle"
    : contentResult?.versionId === version.id
      ? contentResult.state
      : "loading";
  const content = contentResult && version && contentResult.versionId === version.id
    ? contentResult.value
    : null;
  const contentCategory = contentResult && version && contentResult.versionId === version.id
    ? contentResult.category
    : null;

  const selectArtifact = (next: ResearchArtifactProjection) => {
    generation.current += 1;
    setArtifactId(next.id);
    setVersionId(selectHead(next)?.id ?? null);
  };

  return (
    <section aria-labelledby="research-output-title" className="research-section output-panel border-t-0! pt-2!" id="mobile-artifacts">
      <div className="research-section-heading flex-row! items-center! mb-2!">
        <h2 className="text-sm!" id="research-output-title">Research artifacts</h2>
        <span className="text-xs text-muted-foreground">{research.artifacts.length} {research.artifacts.length === 1 ? "output" : "outputs"}</span>
      </div>
      {research.artifacts.length ? <>
        <div aria-label="Research outputs" className={`output-tabs ${research.artifacts.length === 1 ? "hidden!" : ""}`} role="tablist">
          {research.artifacts.map((item) => (
            <button aria-selected={item.id === artifact?.id} className={item.id === artifact?.id ? "text-foreground!" : "text-muted-foreground!"} key={item.id} onClick={() => selectArtifact(item)} role="tab" type="button">{item.title}</button>
          ))}
        </div>
        {artifact && version ? <div className="artifact-reader">
          <div className="artifact-reader-toolbar flex-row! items-center! mb-3">
            <div className="min-w-0"><strong className="truncate" title={artifact.title}>{artifact.title}</strong><span className="text-muted-foreground!">Latest version {selectHead(artifact)?.logical_version}</span></div>
            <label className="text-muted-foreground!">Version<select aria-label={`${artifact.title} version`} className="min-w-0! w-auto! min-h-11! bg-background! text-foreground!" onChange={(event) => setVersionId(event.target.value)} value={version.id}>{artifact.versions.map((item) => <option key={item.id} value={item.id}>{versionLabel(item)}</option>)}</select></label>
          </div>
          <div aria-label={`${artifact.title} document`} className="artifact-document border-border! bg-card! text-card-foreground!" role="region">
            {contentState === "loading" ? <p>Loading verified content…</p> : null}
            {contentState === "error" ? <p>{contentError(offline, contentCategory)}</p> : null}
            {content ? <ArtifactDocument key={version?.id} content={content} hasRetainedProvenance={version?.generator.name === "research-response" && version?.tool.name === "application-research"} /> : null}
          </div>
          <details className="mt-3" data-details data-artifact-metadata><summary className="cursor-pointer py-3 text-sm text-muted-foreground">Version details</summary><ArtifactMetadata version={version} /></details>
        </div> : null}
      </> : <p className="research-empty text-muted-foreground!">No committed research artifact is available for this run.</p>}
      <div className="snapshot-list">
        {research.snapshots.map((snapshot) => (
          <article className="artifact-snapshot border-border! bg-muted/40!" key={snapshot.id}>
            <span className="text-muted-foreground!">Immutable snapshot</span><strong>{snapshot.name}</strong>
            <ul className="text-muted-foreground!">{snapshot.members.map((member) => <li key={member.artifact_id}>{research.artifacts.find((item) => item.id === member.artifact_id)?.title ?? "Saved output"} · v{member.logical_version}</li>)}</ul>
          </article>
        ))}
      </div>
    </section>
  );
}

export function ResearchWorkflowView({
  client,
  offline,
  research,
}: {
  client: CortexControlClient;
  offline: boolean;
  research: ResearchWorkflowProjection | null;
}) {
  if (!research) {
    return <>
      <section className="research-section" id="mobile-sources"><p className="research-empty text-muted-foreground!">Research workflow details are unavailable for this run.</p></section>
      <section className="research-section" id="mobile-artifacts"><p className="research-empty text-muted-foreground!">No verified output is available for this run.</p></section>
    </>;
  }
  return <>
    <OutputPanel client={client} offline={offline} research={research} />
    <details className="research-workflow-details mt-3" data-details>
      <summary className="cursor-pointer py-3 text-sm text-muted-foreground">Sources and workflow details</summary>
      <StagePanel research={research} />
      <SourcePanel research={research} />
      <LineagePanel research={research} />
    </details>
  </>;
}
