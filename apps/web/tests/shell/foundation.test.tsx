import { render, screen } from "@testing-library/react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { describe, expect, it } from "vitest";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";

const messages: ThreadMessageLike[] = [
  { id: "m1", role: "assistant", content: [{ type: "text", text: "**bold** answer" }] },
];

function Harness() {
  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: false,
    // 0.15 only fills in message status/metadata for stores that convert.
    convertMessage: (message: ThreadMessageLike) => message,
    onNew: async () => {},
  });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  );
}

describe("assistant-ui foundation", () => {
  it("renders the registry Thread with markdown over an external store", () => {
    render(<Harness />);
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByRole("textbox")).toBeTruthy();
  });
});
