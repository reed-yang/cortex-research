"use client";

import type { Decision, JsonValue } from "../control/contracts";
import { Button } from "@/components/ui/button";
import { copy, decisionKindLabel, label } from "./copy";
import type { ViewProps } from "./types";

type DecisionChoice = {
  id: string | null;
  label: string;
  description: string | null;
  tone: "primary" | "danger" | "neutral" | null;
};

// The option shapes Control validates, read as a button: a bare string is its
// own id and label, an object names them, and anything else is shown as
// unsupported rather than guessed at.
function choiceView(option: JsonValue, index: number): DecisionChoice {
  if (typeof option === "string") return { id: option, label: option, description: null, tone: null };
  if (typeof option === "object" && option !== null && !Array.isArray(option)) {
    const id = typeof option.id === "string" ? option.id : null;
    const text = typeof option.label === "string" ? option.label : id;
    const description = typeof option.description === "string" ? option.description : null;
    const tone = option.tone === "primary" || option.tone === "danger" || option.tone === "neutral" ? option.tone : null;
    return { id, label: text ?? label.option(index + 1), description, tone };
  }
  return { id: null, label: label.unsupportedOption(index + 1), description: null, tone: null };
}

const TONE_VARIANT = { primary: "default", danger: "destructive", neutral: "outline" } as const;

function DecisionCard({ decision, disabled, onResolve }: { decision: Decision; disabled: boolean; onResolve: (decision: Decision, choice: string) => void }) {
  const choices = decision.options.map(choiceView);
  return (
    <article aria-label={copy.decision.title} className="flex flex-col gap-2 rounded-lg border p-3 text-sm">
      <p className="text-xs font-medium text-muted-foreground">{decisionKindLabel(decision.kind)}</p>
      <p>{decision.prompt}</p>
      <div className="flex flex-wrap gap-2">
        {choices.map((choice, index) => (
          <Button
            className="min-h-11 lg:min-h-7"
            disabled={disabled || choice.id === null}
            key={choice.id ?? `choice-${index}`}
            onClick={() => { if (choice.id !== null) onResolve(decision, choice.id); }}
            size="sm"
            title={choice.description ?? undefined}
            variant={choice.tone ? TONE_VARIANT[choice.tone] : "outline"}
          >
            {choice.label}
          </Button>
        ))}
      </div>
      <details data-details>
        <summary className="cursor-pointer text-xs text-muted-foreground">{copy.decision.details}</summary>
        <p className="text-xs text-muted-foreground">
          {copy.details.runId} {decision.run_id}, {copy.details.attemptId} {decision.attempt_id}, {copy.details.raisedAt} {decision.created_at}.
        </p>
      </details>
    </article>
  );
}

// The decisions the open thread's own run is waiting on; the hook has already
// narrowed them to that run and to the pending ones.
export function DecisionCards({ state, actions }: ViewProps) {
  if (state.decisions.length === 0) return null;
  return (
    <div className="flex flex-col gap-2 px-2">
      {state.decisions.map((decision) => (
        <DecisionCard
          decision={decision}
          disabled={state.commandPending || state.offline}
          key={decision.id}
          onResolve={(target, choice) => { void actions.resolveDecision(target, choice); }}
        />
      ))}
    </div>
  );
}
