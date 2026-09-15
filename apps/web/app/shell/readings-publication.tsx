"use client";

import { useEffect, useState } from "react";
import type { CortexControlClient } from "../control/client";
import type { ReadingsStatus } from "../control/readings-contracts";

const LABELS = {
  pending: "Waiting to publish to readings",
  publishing: "Publishing to readings",
  published: "Published to readings",
  failed: "Readings publication failed; retry is scheduled",
  conflict: "Readings needs attention; automatic updates stopped",
};

export function ReadingsPublication({ client, sourceId }: { client: CortexControlClient; sourceId: string | null }) {
  const [status, setStatus] = useState<ReadingsStatus | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    setStatus(null);
    setFailed(false);
    const refresh = async () => {
      try {
        const value = await client.getReadingsStatus();
        if (active) { setStatus(value); setFailed(false); }
      } catch {
        if (active) { setStatus(null); setFailed(true); }
      } finally {
        if (active) timer = setTimeout(refresh, 30_000);
      }
    };
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [client, sourceId]);
  if (failed) return <p className="text-sm text-muted-foreground" role="status">Readings publication status is unavailable.</p>;
  if (!status?.enabled) return null;
  const publication = status.items.find((item) => item.source_id === sourceId);
  const attention = status.items.filter((item) => item.state === "failed" || item.state === "conflict").length;
  return (
    <div className="rounded-md border border-border p-3 text-sm" role="status" aria-label="Readings publication">
      {status.failure ? <p>Readings publication is paused by an error.</p> : sourceId ? (
        <p>{publication ? LABELS[publication.state] : "This source is not scheduled for readings publication."}</p>
      ) : <p>Automatic readings publication is enabled.{attention ? ` ${attention} papers need attention.` : ""}</p>}
      <p className="mt-1 text-xs text-muted-foreground">Library import and readings publication have separate status.</p>
    </div>
  );
}
