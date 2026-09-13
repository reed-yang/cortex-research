"use client";

import { ENGINE_THREAD_SENTENCE } from "../control/assistant-adapter";
// Aliased: `Fact` below takes its own `label` prop.
import { copy, label as phrase } from "./copy";
import type { ViewProps } from "./types";

// What the rail badge and this view say about the runtime dispatch gate.
// `null` is "the daemon does not report it" (no bridge or an unbound worker)
// or "health could not be read"; both are unknown, never a guess either way.
export function dispatchGateLabel(gate: boolean | null): string {
  if (gate === true) return copy.status.enabled;
  if (gate === false) return copy.status.disabled;
  return copy.status.unknown;
}

function Fact({ id, label, value }: { id: string; label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b py-2 last:border-b-0">
      <dt className="text-muted-foreground" id={id}>{label}</dt>
      <dd aria-labelledby={id} className="font-mono">{value}</dd>
    </div>
  );
}

// The one place in the product that speaks the system's own vocabulary:
// dispatch, replay and engine are facts here, not chrome.
export function StatusView({ state, actions }: ViewProps) {
  const capabilities = Object.entries(state.capabilities ?? {});
  return (
    <section aria-label={copy.status.title} className="flex flex-1 flex-col overflow-y-auto bg-background p-6 text-foreground">
      <div className="mx-auto flex w-full max-w-[44rem] flex-col gap-6">
        <header className="flex flex-col gap-1">
          <h1 className="text-lg font-medium">{copy.status.title}</h1>
          <p className="text-sm text-muted-foreground">{copy.status.subtitle}</p>
        </header>

        <dl className="text-sm">
          <Fact id="status-api-version" label={copy.status.apiVersion} value={state.apiVersion ?? copy.status.unknown} />
          <Fact id="status-runtime-dispatch" label={copy.status.dispatch} value={dispatchGateLabel(state.dispatchGate)} />
          <Fact id="status-live-updates" label={copy.status.live} value={copy.status.replay[state.replayState]} />
        </dl>

        <section aria-labelledby="status-capabilities-title" className="flex flex-col gap-1">
          <h2 className="text-sm font-medium" id="status-capabilities-title">{copy.status.capabilitiesTitle}</h2>
          {capabilities.length === 0 ? (
            <p className="text-sm text-muted-foreground">{copy.status.noCapabilities}</p>
          ) : (
            <ul className="text-sm text-muted-foreground">
              {capabilities.map(([name, available]) => (
                <li key={name}>{phrase.capability(name, available)}</li>
              ))}
            </ul>
          )}
        </section>

        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              aria-label={copy.status.showEngine}
              checked={state.showEngine}
              className="size-4 accent-primary scheme-light-dark"
              onChange={(event) => actions.setShowEngine(event.target.checked)}
              role="switch"
              type="checkbox"
            />
            {copy.status.showEngine}
          </label>
          <p className="text-sm text-muted-foreground">{ENGINE_THREAD_SENTENCE}</p>
        </div>
      </div>
    </section>
  );
}
