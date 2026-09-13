"use client";

import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useLocalRuntime,
} from "@assistant-ui/react";
import { useMemo, useState } from "react";
import {
  createCortexAssistantAdapter,
  initialAssistantMessages,
  mockCortexTransport,
} from "./mock/cortex-assistant-adapter";
import {
  evidenceRows,
  g0Fixture,
  g1CanonicalEvents,
  g1Decisions,
  g1Fixture,
  g1Timeline,
  immutableSnapshot,
  lineageLinks,
  lineageNodes,
  livingBrief,
  optionLabel,
  sourceCandidates,
  trainingPlan,
  workspace,
  type DecisionView,
  type EventView,
  type G0Action,
  type RunState,
  type ScenarioId,
  type TimelineStage,
} from "./mock/cortex-fixtures";
import { initialG0State, resolveG0, type G0ViewState } from "./mock/cortex-state";
import { ConnectionBanner, useConnectivityState } from "./pwa-client";

function WorkspaceRail({
  activeThread,
  disabled,
  onThreadChange,
}: {
  activeThread: string;
  disabled: boolean;
  onThreadChange: (threadId: string) => void;
}) {
  return (
    <aside className="workspace-rail" aria-label="Workspace navigation">
      <div className="brand-lockup">
        <span className="brand-mark">CX</span>
        <div><span className="brand-name">Cortex</span><span className="brand-subtitle">Research OS</span></div>
      </div>
      <div className="workspace-picker"><p>Workspace</p><strong>{workspace.title}</strong></div>
      <div className="rail-section-label">Research threads</div>
      <nav className="thread-list" aria-label="Research threads">
        {workspace.threads.map((thread) => (
          <button
            aria-label={`${thread.title}${disabled ? " (unavailable before artifact creation)" : ""}`}
            className={`thread-link ${activeThread === thread.id ? "is-active" : ""}`}
            disabled={disabled}
            key={thread.id}
            onClick={() => onThreadChange(thread.id)}
            type="button"
          >
            <span className="thread-icon">{thread.icon}</span>
            <span className="thread-title">{thread.title}</span>
            <span className="thread-count">{disabled ? "—" : thread.count}</span>
          </button>
        ))}
      </nav>
      {disabled ? <p className="thread-disabled-note">Threads unlock only after artifact versions exist.</p> : null}
      <div className="rail-footer">
        <div className="local-indicator"><span className="status-dot" /> Local fixture</div>
        Contract v0.1 · No production access
      </div>
    </aside>
  );
}

function RunTimeline({ runId, stages }: { runId: string; stages: TimelineStage[] }) {
  return (
    <section className="run-timeline" aria-labelledby="run-timeline-title">
      <div className="timeline-topline"><h2 id="run-timeline-title">Run timeline</h2><span className="run-id">{runId}</span></div>
      <div className="timeline-stages">
        {stages.map((stage) => (
          <div className={`timeline-stage ${stage.state}`} data-stage={stage.id} key={stage.id}>
            <span className="stage-node" aria-hidden="true" /><span className="stage-name">{stage.label}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function SourceConflictPanel({ state }: { state: G0ViewState }) {
  const candidates = [sourceCandidates.titleIntent, sourceCandidates.suppliedUrl];
  return (
    <section className="source-conflict" aria-labelledby="source-conflict-title">
      <div className="conflict-kicker"><span className="warning-mark">!</span><span className="eyebrow">Source identity gate</span></div>
      <h2 id="source-conflict-title">Cortex stopped before import</h2>
      <p className="conflict-copy">The requested title and supplied URL resolve to different canonical sources. Control state is saved; research import and materialization are still zero.</p>
      <div className="control-ledger" aria-label="Control and research ledgers">
        <article data-scope={g0Fixture.controlScope}>
          <span>Durable control ledger</span><strong>Saved</strong><p>{state.controlDetail}</p>
        </article>
        <article className="research-zero" data-scope={g0Fixture.researchScope}>
          <span>Research side-effect ledger</span><strong>0 changes</strong><p>{state.researchDetail}</p>
        </article>
      </div>
      <p className="source-plan"><strong>Source plan:</strong> {state.sourcePlan}</p>
      <div className="source-comparison">
        {candidates.map((candidate, index) => (
          <div key={candidate.source_id} style={{ display: "contents" }}>
            {index ? <span className="not-equal" aria-label="Does not equal">≠</span> : null}
            <article className="source-card">
              <span className="mini-label">{candidate.role}</span>
              <h3>{candidate.official_title}</h3>
              <dl className="source-identifiers">
                <div><dt>Canonical ID</dt><dd>{candidate.canonical_id}</dd></div>
                <div><dt>{candidate.aliases[0].kind === "model" ? "Model alias" : "Project alias"}</dt><dd>{candidate.aliases[0].value}</dd></div>
              </dl>
            </article>
          </div>
        ))}
      </div>
    </section>
  );
}

function LineagePanel() {
  const successor = lineageNodes.find((node) => node.node_id === "lineage-helios-echo-successor")!;
  const reused = lineageNodes.filter((node) => node.node_id !== successor.node_id);
  return (
    <section className="lineage-card" aria-labelledby="lineage-title">
      <span className="eyebrow">G1 lineage reuse</span><h2 id="lineage-title">One successor, two independent reuse links</h2>
      <p className="lineage-copy">Dormant and graduated nodes keep their prior status. The active successor points to each one directly.</p>
      <div className="lineage-graph" aria-label="Successor lineage">
        <article className="lineage-node successor"><span className="node-state active">Active successor</span><strong>{successor.title}</strong><p>revision {successor.revision}</p></article>
        <div className="lineage-branches" aria-label="Successor reuse links">
          {reused.map((node) => {
            const link = lineageLinks.find((item) => item.to_node_id === node.node_id)!;
            return (
              <div className="lineage-branch" data-from={link.from_node_id} data-to={link.to_node_id} key={link.link_id}>
                <span className="lineage-link">reuses</span>
                <article className="lineage-node"><span className={`node-state ${node.status}`}>{node.status}</span><strong>{node.title}</strong><p>revision {node.revision} · unchanged</p></article>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function LivingBriefCard() {
  return (
    <article className="artifact-card living-brief">
      <div className="card-heading-row"><div><span className="eyebrow">Living artifact</span><h2>Living Brief</h2></div><span className="version-badge">rev {livingBrief.revision}</span></div>
      <p className="brief-thesis">{livingBrief.thesis}</p><p className="brief-update"><strong>What changed:</strong> {livingBrief.update}</p>
      <div className="approach-strip">{livingBrief.approaches.map((item) => <div className="approach-pill" key={item.title}><strong>{item.title}</strong><span>{item.note}</span></div>)}</div>
    </article>
  );
}

function EvidenceCard() {
  return (
    <article className="artifact-card">
      <div className="card-heading-row"><div><span className="eyebrow">Canonical source claims</span><h2>Evidence</h2></div><span className="version-badge">rev 1</span></div>
      <div aria-label="Evidence table" className="evidence-table-scroll" role="region" tabIndex={0}><table className="evidence-table"><thead><tr><th>Claim</th><th>Source</th><th>Status</th></tr></thead><tbody>{evidenceRows.map((row) => <tr key={row.claim}><td>{row.claim}</td><td>{row.source}</td><td>{row.status}</td></tr>)}</tbody></table></div>
    </article>
  );
}

function TrainingPlanCard() {
  return (
    <article className="artifact-card">
      <div className="card-heading-row"><div><span className="eyebrow">3 staged experiments</span><h2>Training Plan</h2></div><span className="version-badge">rev 1</span></div>
      <div className="training-list">{trainingPlan.map((step, index) => <div className="training-step" key={step}><span className="step-number">0{index + 1}</span><p>{step}</p></div>)}</div>
    </article>
  );
}

function SnapshotCard() {
  return (
    <article className="artifact-card snapshot-card"><div className="card-heading-row"><div><span className="eyebrow">Immutable handoff</span><h2>Snapshot</h2></div><span className="version-badge">rev {immutableSnapshot.revision}</span></div><p>Version bundle locked after both decisions and artifact commits.</p><span className="snapshot-hash">{immutableSnapshot.sha256}</span></article>
  );
}

function G1Artifacts({ activeThread }: { activeThread: string }) {
  const kind = workspace.threads.find((thread) => thread.id === activeThread)?.kind;
  return (
    <section className="artifact-grid" aria-label="Research artifacts" data-thread={activeThread} id="mobile-artifacts">
      {kind === "evidence" ? <EvidenceCard /> : null}
      {kind === "architecture" ? <><LivingBriefCard /><SnapshotCard /></> : null}
      {kind === "training_plan" ? <TrainingPlanCard /> : null}
    </section>
  );
}

function ArtifactGate() {
  return <section className="artifact-gate" aria-label="Research artifacts unavailable" id="mobile-artifacts"><span className="eyebrow">Research outputs</span><h2>Artifacts are not created yet</h2><p>Living Brief, Evidence, Training Plan, and Snapshot stay unavailable while G0 is waiting, resuming, or canceled.</p></section>;
}

function ChatText() { return <p className="chat-text"><MessagePartPrimitive.Text /></p>; }

function AssistantThread({ scenarioId, runState, offline }: { scenarioId: ScenarioId; runState: RunState; offline: boolean }) {
  const adapter = useMemo(() => createCortexAssistantAdapter(mockCortexTransport, scenarioId), [scenarioId]);
  const runtime = useLocalRuntime(adapter, { initialMessages: initialAssistantMessages(scenarioId, runState) });
  return (
    <AssistantRuntimeProvider runtime={runtime}><ThreadPrimitive.Root className="assistant-thread"><ThreadPrimitive.Viewport className="thread-viewport"><ThreadPrimitive.Messages>{({ message }) => <MessagePrimitive.Root className={`thread-message ${message.role}`}><span className="message-role">{message.role === "assistant" ? "Cortex" : "You"}</span><MessagePrimitive.Parts components={{ Text: ChatText }} /></MessagePrimitive.Root>}</ThreadPrimitive.Messages></ThreadPrimitive.Viewport><ComposerPrimitive.Root className="composer"><ComposerPrimitive.Input aria-label="Steer this run" className="composer-input" disabled={offline} placeholder={offline ? "Reconnect before steering this fixture…" : "Steer this run without hiding it in chat…"} /><ComposerPrimitive.Send className="composer-send" disabled={offline}>Send steer</ComposerPrimitive.Send></ComposerPrimitive.Root></ThreadPrimitive.Root></AssistantRuntimeProvider>
  );
}

function ConversationCard({ scenarioId, runState, offline }: { scenarioId: ScenarioId; runState: RunState; offline: boolean }) {
  return <section className="conversation-card" aria-labelledby="conversation-title" id="mobile-conversation"><div className="conversation-heading"><h2 id="conversation-title">Research conversation</h2><p>Assistant UI · mock adapter</p></div><AssistantThread key={`${scenarioId}-${runState}`} scenarioId={scenarioId} runState={runState} offline={offline} /></section>;
}

function RawLog({ events }: { events: EventView[] }) {
  const durableCount = events.filter((event) => event.durability === "durable").length;
  const localCount = events.filter((event) => event.durability === "ui_local").length;
  const raw = events.map((event) => `${String(event.sequence).padStart(2, "0")}  ${event.type.padEnd(30)} ${event.durability.padEnd(9)} ${event.origin.padEnd(15)} ${event.summary}`).join("\n");
  const countLabel = `${durableCount} durable events${localCount ? ` · ${localCount} UI-local transitions` : ""}`;
  return <details className="raw-log"><summary><span>Raw Log</span><span>{countLabel} · collapsed by default</span></summary><pre>{raw}</pre></details>;
}

function DecisionCard({ decision, pending, offline, onResolve }: { decision: DecisionView; pending: boolean; offline: boolean; onResolve?: (action: G0Action) => void }) {
  return (
    <article className={`decision-card ${decision.status === "resolved" ? "resolved" : ""}`}>
      <div className="decision-heading"><span className="decision-type">{decision.kind === "source_conflict" ? "Source conflict" : "Lineage"}</span><span className="decision-revision">rev {decision.revision}</span></div>
      <h3>{decision.title}</h3><p>{decision.prompt}</p>
      {decision.selected ? <div className="resolved-choice"><span className="resolved-check">✓</span><span><strong>{optionLabel(decision.selected)}</strong><br />{decision.origin === "canonical" ? "Canonical fixture resolution" : "UI-local preview · not persisted"}</span></div> : <div className="decision-actions">{decision.options.map((option) => <button className={`decision-button ${option.tone ?? ""}`} disabled={pending || offline} key={option.id} onClick={() => onResolve?.(option.id as G0Action)} type="button">{offline ? `${option.label} · offline` : pending ? "Previewing decision…" : option.label}</button>)}</div>}
    </article>
  );
}

function DecisionInbox({ decisions, pendingCount, pending, offline, onResolve }: { decisions: DecisionView[]; pendingCount: number; pending: boolean; offline: boolean; onResolve?: (action: G0Action) => void }) {
  return (
    <aside className="decision-inbox" aria-label="Decision Inbox" id="mobile-decisions"><div className="inbox-header"><h2>Decision Inbox</h2><span className="inbox-count">{pendingCount}</span></div><p className="inbox-note">{onResolve ? "The pending decision is canonical; mock actions below preview UI-local outcomes only." : "These resolved choices are durable events from the canonical fixture."}</p>{decisions.map((decision) => <DecisionCard decision={decision} key={decision.id} offline={offline} onResolve={onResolve} pending={pending} />)}<div className="inbox-audit">Compare-and-swap revisions remain visible. The prototype mutates UI-local control state only.</div></aside>
  );
}

function MobileNavigation({ pendingCount }: { pendingCount: number }) {
  return (
    <nav aria-label="Mobile workspace sections" className="mobile-nav">
      <a href="#mobile-overview">Overview</a>
      <a href="#mobile-artifacts">Artifacts</a>
      <a href="#mobile-conversation">Chat</a>
      <a href="#mobile-decisions">Decisions{pendingCount ? <span>{pendingCount}</span> : null}</a>
    </nav>
  );
}

const statusLabels: Record<RunState, string> = {
  waiting_for_decision: "Decision required",
  resuming: "Resuming",
  canceled: "Canceled",
  completed: "Completed",
};

export function CortexPrototype({
  captureMode = false,
  initialScenario = "g0",
}: {
  captureMode?: boolean;
  initialScenario?: ScenarioId;
}) {
  const [scenarioId, setScenarioId] = useState<ScenarioId>(initialScenario);
  const [activeThread, setActiveThread] = useState(workspace.threads[0].id);
  const [g0State, setG0State] = useState<G0ViewState>(initialG0State);
  const [pending, setPending] = useState(false);
  const connectivity = useConnectivityState();
  const offline = connectivity === "offline";
  const isG0 = scenarioId === "g0";
  const runState = isG0 ? g0State.runState : g1Fixture.runState;
  const activeThreadLabel = isG0 ? "Outputs unavailable" : workspace.threads.find((thread) => thread.id === activeThread)?.title;
  const statusLabel =
    isG0 && runState !== "waiting_for_decision"
      ? `${statusLabels[runState]} · UI preview`
      : statusLabels[runState];

  async function handleResolve(action: G0Action) {
    setPending(true);
    await mockCortexTransport.resolveDecision({ decisionId: g0State.decision.id, optionId: action, revision: g0State.decision.revision });
    setG0State(resolveG0(action));
    setPending(false);
  }

  function changeScenario(next: ScenarioId) {
    setScenarioId(next);
    setPending(false);
    if (next === "g0") setG0State(initialG0State);
    if (next === "g1") setActiveThread(workspace.threads[0].id);
  }

  return (
    <main
      className={`prototype-shell ${captureMode ? "capture-full-page" : ""}`}
      data-capture-scenario={captureMode ? scenarioId : undefined}
      style={captureMode ? { transform: "scale(0.5)", transformOrigin: "top left" } : undefined}
    >
      <WorkspaceRail activeThread={activeThread} disabled={isG0} onThreadChange={setActiveThread} />
      <section className="main-workspace">
        <ConnectionBanner state={connectivity} />
        <header className="workspace-header" id="mobile-overview"><div><p className="breadcrumb">{workspace.title} / {activeThreadLabel}</p><h1>{isG0 ? "Resolve source identity before research" : "Successor memory research workspace"}</h1><div className="header-meta"><span className={`status-chip ${runState}`}>{statusLabel}</span><span className="meta-chip">{isG0 ? "G0 · source conflict" : "G1 · successor lineage"}</span><span className="meta-chip">{isG0 ? g0Fixture.attemptId : g1Fixture.attemptId}</span></div></div><div className="scenario-switch" aria-label="Golden fixture scenario">{(["g0", "g1"] as ScenarioId[]).map((id) => <button aria-pressed={scenarioId === id} className={`scenario-button ${scenarioId === id ? "is-active" : ""}`} key={id} onClick={() => changeScenario(id)} type="button">{id === "g0" ? "G0 · conflict" : "G1 · resolved"}</button>)}</div></header>
        <RunTimeline runId={isG0 ? g0Fixture.runId : g1Fixture.runId} stages={isG0 ? g0State.timeline : g1Timeline} />
        {isG0 ? <SourceConflictPanel state={g0State} /> : <LineagePanel />}
        {isG0 ? <ArtifactGate /> : <G1Artifacts activeThread={activeThread} />}
        <ConversationCard scenarioId={scenarioId} runState={runState} offline={offline} />
        <RawLog events={isG0 ? g0State.events : g1CanonicalEvents} />
      </section>
      <DecisionInbox decisions={isG0 ? [g0State.decision] : g1Decisions} offline={offline} onResolve={isG0 ? handleResolve : undefined} pending={pending} pendingCount={isG0 ? g0State.pendingCount : 0} />
      <MobileNavigation pendingCount={isG0 ? g0State.pendingCount : 0} />
    </main>
  );
}
