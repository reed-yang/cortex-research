"use client";

import { useEffect, useRef } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { Capture, CaptureState } from "../control/contracts";
import { copy, label } from "./copy";
import type { CaptureActionName } from "./types";

// Dismissal is legal from pending, approved and uncertain. A claimed row is
// absent on purpose: the lease has to expire into uncertain before the
// operator may close it, so offering the button would promise a refusal.
const DISMISSABLE = new Set<CaptureState>(["pending", "approved", "uncertain"]);

export function CaptureCard({
  capture,
  disabled,
  confirmingReopen,
  selected,
  onDecide,
  onReopenIntent,
}: {
  capture: Capture;
  disabled: boolean;
  confirmingReopen: boolean;
  selected: boolean;
  onDecide: (capture: Capture, action: CaptureActionName) => void;
  onReopenIntent: (captureId: string | null) => void;
}) {
  const imported = capture.consumed_source_ids ?? [];
  const node = useRef<HTMLElement | null>(null);
  // A refused duplicate points at the row it collided with. Saying "already in
  // the inbox" without showing which one leaves the operator to find it by eye.
  useEffect(() => {
    if (!selected) return;
    node.current?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);
  return (
    <article
      aria-current={selected ? "true" : undefined}
      aria-label={copy.capture.label}
      className={`flex flex-col gap-2 rounded-lg border p-3 ${selected ? "border-primary ring-1 ring-primary" : ""}`}
      data-capture-id={capture.id}
      ref={node}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{capture.kind === "url" ? copy.capture.link : copy.capture.note}</Badge>
        {/* An exact-string corpus hit is evidence, never identity, so it reads
            as a hint and refuses nothing. */}
        {capture.known_source_id ? <Badge variant="secondary">{copy.capture.maybeKnown}</Badge> : null}
      </div>
      <p className="text-sm break-words">{capture.payload}</p>
      {capture.note ? <p className="text-sm text-muted-foreground">{capture.note}</p> : null}
      {/* ⟦V-R3 / P9-3⟧ Which run still holds this capture's carrier thread. The
          operator cannot decide it until that run ends, so the card says so
          rather than offering a button that would be refused. */}
      {capture.blocked_by ? (
        <p className="text-sm text-muted-foreground">{copy.capture.blocked}</p>
      ) : null}
      {capture.state === "failed" && !capture.blocked_by ? (
        <p className="text-sm text-muted-foreground">{copy.capture.failed}</p>
      ) : null}
      {imported.length ? (
        <p className="text-sm text-muted-foreground">
          {imported.length === 1 ? copy.capture.importedOne : label.importedAsMany(imported.length)}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {capture.state === "pending" ? (
          <Button disabled={disabled} onClick={() => onDecide(capture, "approve")} size="sm" type="button">{copy.inbox.approve}</Button>
        ) : null}
        {DISMISSABLE.has(capture.state) ? (
          <Button disabled={disabled} onClick={() => onDecide(capture, "dismiss")} size="sm" type="button" variant="destructive">
            {copy.inbox.dismiss}
          </Button>
        ) : null}
        {/* The trigger keeps its slot and goes inert once armed. Swapping a
            committing button into the place the pointer is already resting
            would let a plain double-click record the acknowledgement. */}
        {capture.state === "uncertain" ? (
          <Button
            disabled={disabled || confirmingReopen}
            onClick={() => onReopenIntent(capture.id)}
            size="sm"
            type="button"
            variant="outline"
          >
            {copy.inbox.reopen}
          </Button>
        ) : null}
      </div>
      {/* The acknowledgement is a gesture, not free text: the operator is
          confirming that a lost consumer may already have imported this. It is
          a deliberate second reach, below the actions row. */}
      {confirmingReopen ? (
        <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3" role="note">
          <p className="text-sm">{copy.capture.reopenExplanation}</p>
          <div className="flex flex-wrap gap-2">
            <Button disabled={disabled} onClick={() => onDecide(capture, "reopen")} size="sm" type="button">{copy.capture.confirmReopen}</Button>
            <Button disabled={disabled} onClick={() => onReopenIntent(null)} size="sm" type="button" variant="ghost">{copy.capture.cancelReopen}</Button>
          </div>
        </div>
      ) : null}
      {/* Everything a support conversation needs and the screen does not. */}
      <details className="text-xs text-muted-foreground" data-details>
        <summary>{copy.capture.details}</summary>
        <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3">
          <dt>{copy.details.captureId}</dt><dd>{capture.id}</dd>
          <dt>{copy.details.state}</dt><dd>{capture.state}</dd>
          <dt>{copy.details.revision}</dt><dd>{capture.revision}</dd>
          <dt>{copy.details.created}</dt><dd>{capture.created_at}</dd>
          {capture.failure_category ? <><dt>{copy.details.failure}</dt><dd>{capture.failure_category}</dd></> : null}
          {capture.blocked_by ? <><dt>{copy.details.blockedBy}</dt><dd>{capture.blocked_by}</dd></> : null}
          {capture.known_source_id ? <><dt>{copy.details.corpusHint}</dt><dd>{capture.known_source_id}</dd></> : null}
          {imported.length ? <><dt>{copy.details.importedAs}</dt><dd>{imported.join(", ")}</dd></> : null}
        </dl>
      </details>
    </article>
  );
}
