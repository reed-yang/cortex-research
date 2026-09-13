"use client";

import { useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CAPTURE_STATES, type Capture, type CaptureState, type Decision } from "../control/contracts";
import { CaptureCard } from "./capture-card";
import { copy, decisionKindLabel, label } from "./copy";
import type { CaptureActionName, ViewProps } from "./types";

const GROUP_LABELS: Record<CaptureState, string> = copy.captureGroups;

// A decision prompt can be several paragraphs. The row is an index into the
// thread that owns it, so it carries the ask and nothing more.
function firstLine(prompt: string): string {
  const line = prompt.split("\n").find((candidate) => candidate.trim().length > 0);
  return line ? line.trim() : prompt;
}

function CaptureComposer({ disabled, onCapture }: {
  disabled: boolean;
  onCapture: (payload: string, note: string, approveNow: boolean) => Promise<boolean>;
}) {
  const [payload, setPayload] = useState("");
  const [note, setNote] = useState("");
  const [approveNow, setApproveNow] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    // The payload crosses verbatim. Trimming is the store's key derivation,
    // and rewriting a submission here would be resolution before approval.
    if (!payload.trim()) return;
    const staged = await onCapture(payload, note, approveNow);
    // A refused capture keeps its text: the operator pasted it once.
    if (!staged) return;
    setPayload("");
    setNote("");
    setApproveNow(false);
  }

  return (
    <form className="flex flex-col gap-2 rounded-lg border p-3" onSubmit={(event) => void submit(event)}>
      <Textarea
        aria-label={copy.inbox.payloadLabel}
        disabled={disabled}
        onChange={(event) => setPayload(event.target.value)}
        placeholder={copy.inbox.capturePlaceholder}
        rows={2}
        value={payload}
      />
      <Input
        aria-label={copy.inbox.noteLabel}
        disabled={disabled}
        onChange={(event) => setNote(event.target.value)}
        placeholder={copy.inbox.notePlaceholder}
        type="text"
        value={note}
      />
      <div className="flex items-center justify-between gap-3">
        {/* Approval is a second recorded decision, never a combined command,
            and the box resets after each one so it is never implied. */}
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <input
            checked={approveNow}
            className="size-4 accent-primary scheme-light-dark"
            disabled={disabled}
            onChange={(event) => setApproveNow(event.target.checked)}
            type="checkbox"
          />
          {copy.inbox.approveNow}
        </label>
        <Button disabled={disabled || !payload.trim()} size="sm" type="submit">{copy.inbox.capture}</Button>
      </div>
    </form>
  );
}

export function InboxView({ state, actions, client }: ViewProps) {
  const [reopenConfirmId, setReopenConfirmId] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  // Offline, a command can only resolve into a refusal, so the gesture is not
  // offered at all (spec section 10).
  const disabled = state.offline || state.commandPending;

  const threadTitles = new Map(
    [...state.threads, ...state.archivedThreads].map((thread) => [thread.id, thread.title] as const),
  );

  // A pending decision names its run, and only the run names the thread the
  // operator has to land on. The open thread's runs are already loaded; any
  // other one is a single read. The thread may belong to another project, so
  // landing on it is the hook's `openThread`, not a bare thread selection.
  async function openDecision(decision: Decision) {
    setOpenError(null);
    setOpening(decision.id);
    try {
      const loaded = state.runs.find((run) => run.id === decision.run_id);
      const threadId = loaded ? loaded.thread_id : (await client.getRun(decision.run_id)).thread_id;
      const opened = await actions.openThread(threadId);
      setOpening(null);
      if (!opened) setOpenError(copy.inbox.threadNotOpened);
    } catch {
      setOpening(null);
      setOpenError(copy.inbox.threadNotOpenedRetry);
    }
  }

  function decide(capture: Capture, action: CaptureActionName) {
    setReopenConfirmId(null);
    void actions.decideCapture(capture, action);
  }

  const groups = CAPTURE_STATES
    .map((group) => ({ group, items: state.captures.filter((capture) => capture.state === group) }))
    .filter((entry) => entry.items.length > 0);
  // A reread while rows are on screen keeps them on screen: unmounting the
  // list would drop the focus of whoever just decided one of them.
  const firstLoad = state.capturesLoading && state.captures.length === 0;
  const refreshing = state.capturesLoading && state.captures.length > 0;

  return (
    <section aria-label={copy.inbox.title} className="flex flex-1 flex-col overflow-y-auto bg-background p-6 text-foreground">
      <div className="mx-auto flex w-full max-w-[44rem] flex-col gap-6">
        <header className="flex flex-col gap-1">
          <h1 className="text-lg font-medium">{copy.inbox.title}</h1>
          <p className="text-sm text-muted-foreground">{copy.inbox.subtitle}</p>
        </header>

        <CaptureComposer disabled={disabled} onCapture={actions.capture} />

        <section aria-labelledby="inbox-decisions-title" className="flex flex-col gap-2">
          <h2 className="text-sm font-medium" id="inbox-decisions-title">{copy.inbox.decisions}</h2>
          {openError ? <p className="text-sm text-destructive" role="alert">{openError}</p> : null}
          {state.pendingDecisions.length === 0 ? (
            <p className="text-sm text-muted-foreground">{copy.inbox.noDecisions}</p>
          ) : null}
          {state.pendingDecisions.map((decision) => {
            const loaded = state.runs.find((run) => run.id === decision.run_id);
            const title = loaded ? threadTitles.get(loaded.thread_id) : undefined;
            return (
              <article aria-label={copy.inbox.pendingDecision} className="flex flex-col gap-2 rounded-lg border p-3" key={decision.id}>
                <div className="flex items-start justify-between gap-3">
                  <Badge variant="outline">{decisionKindLabel(decision.kind)}</Badge>
                  <Button
                    disabled={opening === decision.id}
                    onClick={() => void openDecision(decision)}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    {copy.inbox.open}
                  </Button>
                </div>
                <p className="text-sm">{firstLine(decision.prompt)}</p>
                <p className="text-xs text-muted-foreground">{title ? label.inThread(title) : copy.inbox.inAnotherThread}</p>
                <details className="text-xs text-muted-foreground" data-details>
                  <summary>{copy.inbox.details}</summary>
                  <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3">
                    <dt>{copy.details.decisionId}</dt><dd>{decision.id}</dd>
                    <dt>{copy.details.runId}</dt><dd>{decision.run_id}</dd>
                    <dt>{copy.details.attemptId}</dt><dd>{decision.attempt_id}</dd>
                    <dt>{copy.details.raisedAt}</dt><dd>{decision.created_at}</dd>
                  </dl>
                </details>
              </article>
            );
          })}
        </section>

        <section aria-busy={state.capturesLoading} aria-labelledby="inbox-captures-title" className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-sm font-medium" id="inbox-captures-title">{copy.inbox.captures}</h2>
            {/* A capture belongs to no run, so no stream can carry it. The view
                reads the rows on entry and whenever the operator asks. */}
            <Button disabled={state.capturesLoading} onClick={() => actions.refreshCaptures()} size="sm" type="button" variant="ghost">
              {copy.inbox.refresh}
            </Button>
          </div>
          {firstLoad ? <p className="text-sm text-muted-foreground">{copy.inbox.loadingCaptures}</p> : null}
          {refreshing ? <p className="text-sm text-muted-foreground">{copy.inbox.refreshingCaptures}</p> : null}
          {!state.capturesLoading && state.capturesError ? (
            <div className="flex flex-col gap-1" role="alert">
              <p className="text-sm text-destructive">{copy.inbox.capturesUnreadable}</p>
              <details className="text-xs text-muted-foreground" data-details>
                <summary>{copy.inbox.details}</summary>
                <p>{state.capturesError}</p>
              </details>
            </div>
          ) : null}
          {!firstLoad && !state.capturesError && groups.length === 0 ? (
            <p className="text-sm text-muted-foreground">{copy.inbox.noCaptures}</p>
          ) : null}
          {groups.map(({ group, items }) => (
            <div className="flex flex-col gap-2" data-capture-group={group} key={group}>
              <h3 className="text-xs font-medium text-muted-foreground">{GROUP_LABELS[group]} ({items.length})</h3>
              {items.map((capture) => (
                <CaptureCard
                  capture={capture}
                  confirmingReopen={capture.id === reopenConfirmId}
                  disabled={disabled}
                  key={capture.id}
                  onDecide={decide}
                  onReopenIntent={setReopenConfirmId}
                  selected={capture.id === state.selectedCaptureId}
                />
              ))}
            </div>
          ))}
        </section>
      </div>
    </section>
  );
}
