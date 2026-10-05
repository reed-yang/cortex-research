"use client";

import { useEffect, useState } from "react";
import type { CortexControlClient } from "../control/client";
import type { XhsStatus } from "../control/research-contracts";
import { copy, humanCategory, label } from "./copy";

// Scan times carry the hour, because a daily scan's day alone does not say
// whether today's ran; fixed locale and UTC like every other shell date.
const SCAN_TIME = new Intl.DateTimeFormat("en", {
  day: "numeric", hour: "2-digit", hourCycle: "h23", minute: "2-digit", month: "short", timeZone: "UTC", timeZoneName: "short", year: "numeric",
});

function scanTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : SCAN_TIME.format(parsed);
}

function summary(status: XhsStatus): string {
  if (status.enabled) return copy.status.xhs.on;
  if (status.refusal === "disabled_in_config") return copy.status.xhs.offConfig;
  if (status.refusal === "roots_not_ready") return copy.status.xhs.offRoots;
  return copy.status.xhs.offSchedule;
}

export function failurePhrase(category: string | null): string | null {
  if (!category) return null;
  return copy.status.xhs.failures[category] ?? humanCategory(category) ?? copy.status.unknown;
}

// One line for the XHS plugin: whether it scans, then each followed blogger's
// last scan. A scan that found nothing new is not a success with new notes,
// and neither is a provider failure, so each keeps its own words.
export function XhsStatusLine({ client }: { client: CortexControlClient }) {
  const [status, setStatus] = useState<XhsStatus | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    client.getXhsStatus(controller.signal).then(
      (value) => { if (!controller.signal.aborted) setStatus(value); },
      () => { if (!controller.signal.aborted) setFailed(true); },
    );
    return () => controller.abort();
  }, [client]);
  return (
    <section aria-labelledby="status-xhs-title" className="flex flex-col gap-1">
      <h2 className="text-sm font-medium" id="status-xhs-title">{copy.status.xhs.title}</h2>
      {failed ? <p className="text-sm text-muted-foreground" role="status">{copy.status.xhs.unavailable}</p> : null}
      {status ? (
        <>
          <p className="text-sm text-muted-foreground">{summary(status)}</p>
          {status.bloggers.length ? (
            <ul aria-label={copy.status.xhs.bloggers} className="text-sm text-muted-foreground">
              {status.bloggers.map((blogger) => (
                <li data-scan-outcome={blogger.last_scan_outcome ?? "none"} key={blogger.user_id}>
                  {label.xhsScan(
                    blogger.display_name ?? copy.status.xhs.unnamed,
                    blogger.last_scan_at ? scanTime(blogger.last_scan_at) : null,
                    blogger.last_scan_outcome,
                    failurePhrase(blogger.last_scan_error),
                  )}
                  {/* An unnamed blogger is told apart by the identity only. */}
                  {blogger.display_name === null ? (
                    <details className="text-xs" data-details>
                      <summary>{copy.library.details}</summary>
                      <code className="font-mono">{blogger.user_id}</code>
                    </details>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted-foreground">{copy.status.xhs.noBloggers}</p>}
        </>
      ) : null}
    </section>
  );
}
