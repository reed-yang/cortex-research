"use client";

import { Button } from "@/components/ui/button";
import type { ConversationMode } from "../control/research-mode";
import { copy } from "./copy";

const MODES: Array<[ConversationMode, string]> = [["chat", copy.thread.modeChat], ["research", copy.thread.modeResearch]];

// Which of the two the next message is sent as. It is a view of the mode, not
// a second source of it: a draft that names its own mode wins, and the segment
// follows it.
export function ComposerMode({ mode, onChange, disabled }: { mode: ConversationMode; onChange: (mode: ConversationMode) => void; disabled: boolean }) {
  return (
    <div aria-label={copy.thread.mode} className="flex items-center gap-1" role="radiogroup">
      {MODES.map(([value, label]) => (
        <Button
          aria-checked={mode === value}
          disabled={disabled}
          key={value}
          onClick={() => onChange(value)}
          role="radio"
          size="xs"
          type="button"
          variant={mode === value ? "secondary" : "ghost"}
        >
          {label}
        </Button>
      ))}
    </div>
  );
}
