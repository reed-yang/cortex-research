import type { ChatModelAdapter, ThreadMessageLike } from "@assistant-ui/react";
import type { G0Action, RunState, ScenarioId } from "./cortex-fixtures";

export type DecisionResolution = {
  decisionId: string;
  optionId: G0Action;
  revision: number;
};

export type MockCortexTransport = {
  resolveDecision(input: DecisionResolution): Promise<{ accepted: true }>;
  submitSteer(input: { scenarioId: ScenarioId; text: string }): Promise<string>;
};

export const mockCortexTransport: MockCortexTransport = {
  async resolveDecision() {
    return { accepted: true };
  },
  async submitSteer({ scenarioId, text }) {
    await new Promise((resolve) => setTimeout(resolve, 360));
    return scenarioId === "g0"
      ? `Steer noted: “${text}” It will remain queued until the source decision is resolved.`
      : `Steer noted: “${text}” Cortex would append this to the successor run as an audited command.`;
  },
};

export function createCortexAssistantAdapter(
  transport: MockCortexTransport,
  scenarioId: ScenarioId,
): ChatModelAdapter {
  // Assistant UI types terminate here; Cortex fixtures remain transport-neutral.
  return {
    async *run({ messages }) {
      const lastMessage = messages.at(-1);
      const text = lastMessage?.content
        .filter((part) => part.type === "text")
        .map((part) => (part.type === "text" ? part.text : ""))
        .join(" ")
        .trim();
      const reply = await transport.submitSteer({
        scenarioId,
        text: text || "Review the current run",
      });
      yield { content: [{ type: "text", text: reply }] };
    },
  };
}

export function initialAssistantMessages(
  scenarioId: ScenarioId,
  runState?: RunState,
): ThreadMessageLike[] {
  const g0Text =
    runState === "resuming"
      ? "This UI-local source-decision preview shows the run resuming. Canonical control state is still waiting and research import has not started."
      : runState === "canceled"
        ? "This UI-local preview shows cancellation. Canonical control state is still waiting and the research store remains unchanged."
        : "I found a canonical source conflict. The run, attempt, pending decision, and waiting events are durably saved; research import and materialization remain at zero changes.";
  return [
    {
      role: "assistant",
      content: [
        {
          type: "text",
          text:
            scenarioId === "g0"
              ? g0Text
              : "The successor now independently links dormant Helios and graduated Echo work without changing either prior status.",
        },
      ],
    },
  ];
}
