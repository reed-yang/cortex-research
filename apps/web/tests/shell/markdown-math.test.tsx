import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TextMessagePartProvider } from "@assistant-ui/react";
import { MarkdownDocument, MarkdownText } from "@/components/assistant-ui/elements/markdown-text";
import { ArtifactDocument, splitRetainedProvenance } from "@/app/control/artifact-document";
import type { ArtifactVersionContent } from "@/app/control/research-contracts";

const formula = String.raw`$c_\text{act}\in\mathbb{R}^{T\times12}$`;
const body = `# Action conditioning\n\n${formula}\n\nA **retained** result.\n`;
const appendix = `\n\n## Retained source provenance\n\n\`\`\`json\n${JSON.stringify({
  context_sha256: "a".repeat(64), run_id: "run_1", attempt_id: "attempt_1",
  runtime_release_id: "release_1", runtime_worker_protocol: "1",
  retrieval_mode: "adopted", citation_status: "labels_valid_claims_unverified", sources: [{ source_id: "source_1" }],
}, null, 2)}\n\`\`\`\n`;
function content(text = body + appendix, media_type: ArtifactVersionContent["media_type"] = "text/markdown"): ArtifactVersionContent {
  return { artifact_version_id: "version_1", sha256: "b".repeat(64), byte_length: new TextEncoder().encode(text).length, media_type, content: text };
}

describe("shared assistant-ui math", () => {
  it("renders the reported inline formula and display math with accessible MathML", () => {
    const { container } = render(<MarkdownDocument text={`${formula}\n\n$$\n\\sum_{i=1}^{n} i = \\frac{n(n+1)}2\n$$`} />);
    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelectorAll("math")).toHaveLength(2);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
    expect(container.querySelector("annotation")?.textContent).toBe(formula.slice(1, -1));
  });

  it("keeps inline code, code fences, escaped dollars and ordinary currency literal", () => {
    const text = `Inline \`${formula}\`\n\n\`\`\`latex\n${formula}\n\`\`\`\n\nCost: \\$20.00. Escaped: \\$x\\$.`;
    const { container } = render(<MarkdownDocument text={text} />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.textContent).toContain("Cost: $20.00. Escaped: $x$.");
    expect(container.querySelector("pre code")?.textContent).toContain(formula);
  });

  it("leaves an unmatched currency dollar as text", () => {
    const { container } = render(<MarkdownDocument text="Cost: $20.00." />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.textContent).toBe("Cost: $20.00.");
  });

  it("retains GFM tables and safe links without interpreting raw HTML", () => {
    const { container } = render(<MarkdownDocument text={'| Input | Space |\n| --- | --- |\n| action | $x$ |\n\n<script>alert(1)</script>\n\n[bad](javascript:alert%281%29)'} />);
    expect(container.querySelector("table .katex")).not.toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("falls back to readable invalid math and refuses trusted TeX commands", () => {
    const { container } = render(<MarkdownDocument text={String.raw`$\frac{1}{$ and $\notARealCommand{x}$ and $\href{javascript:alert(1)}{click}$`} />);
    expect(container.querySelector(".katex-error")).not.toBeNull();
    expect(container.textContent).toContain("notARealCommand");
    expect(container.querySelector("a")).toBeNull();
  });

  it("bounds macro expansion", () => {
    const { container } = render(<MarkdownDocument text={String.raw`$\def\loop{\loop}\loop$`} />);
    expect(container.querySelector(".katex-error")).not.toBeNull();
  });

  it("updates a streaming part from an incomplete expression to completed math", async () => {
    const view = render(<TextMessagePartProvider text="$c_" isRunning><MarkdownText smooth={false} /></TextMessagePartProvider>);
    view.rerender(<TextMessagePartProvider text={formula}><MarkdownText smooth={false} /></TextMessagePartProvider>);
    await waitFor(() => expect(view.container.querySelector(".katex")).not.toBeNull());
  });
});

describe("artifact preview and source", () => {
  it("defaults to rendered prose and folds the generated appendix, with an explicit source toggle", async () => {
    const user = userEvent.setup();
    const { container } = render(<ArtifactDocument content={content()} hasRetainedProvenance />);
    expect(screen.getByRole("button", { name: "Preview" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("heading", { name: "Action conditioning" })).toBeTruthy();
    expect(container.querySelector(".katex")).not.toBeNull();
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(screen.getByRole("region", { name: "Rendered Markdown" }).textContent).not.toContain("context_sha256");
    await user.click(screen.getByRole("button", { name: "Source" }));
    expect(screen.getByRole("button", { name: "Source" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("region", { name: "Markdown source" }).textContent).toBe(body);
    expect(container.querySelector(".katex")).toBeNull();
    await user.click(screen.getByText("Source provenance · 1 source"));
    expect(details.open).toBe(true);
    expect(details.querySelector("pre")?.textContent).toBe(appendix);
    await user.click(screen.getByRole("button", { name: "Copy source" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy());
    expect(await navigator.clipboard.readText()).toBe(body + appendix);
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(screen.getByRole("heading", { name: "Action conditioning" })).toBeTruthy();
  });

  it("does not interpret a text/plain document as Markdown", () => {
    const { container } = render(<ArtifactDocument content={content(body, "text/plain")} />);
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(screen.getByRole("region", { name: "Plain text document" }).textContent).toBe(body);
    expect(container.querySelector(".katex")).toBeNull();
  });

  it("preserves unknown, malformed or nonterminal provenance-like content", () => {
    for (const text of [body + appendix + "More prose", body + appendix.replace('"sources": [', '"sources": nope ['), body + "\n\n## Retained source provenance\n\nordinary prose"]) {
      expect(splitRetainedProvenance(text, true).body).toBe(text);
    }
    expect(splitRetainedProvenance(body + appendix, false).body).toBe(body + appendix);
    const split = splitRetainedProvenance(body + appendix, true);
    expect(split.body + split.appendix).toBe(body + appendix);
  });
});
