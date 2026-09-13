import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CortexControlClient } from "../app/control/client";
import { BANNED_WORDS } from "../app/shell/copy";
import { ResearchWorkflowView } from "../app/control/research-workflow";
import type {
  ArtifactVersionProjection,
  ResearchWorkflowProjection,
} from "../app/control/research-contracts";

afterEach(cleanup);

const at = "2026-09-06T12:00:00Z";
const sha = "a".repeat(64);
const producer = { name: "workflow-research", version: "1" };

function version(id: string, logical: number): ArtifactVersionProjection {
  return {
    id, artifact_id: "artifact_brief", logical_version: logical, resource_uri: "cortex:artifact/1",
    sha256: sha, byte_length: 8, media_type: "text/markdown", run_id: "run_1", attempt_id: "attempt_1",
    parents: [], source_ids: ["source_a", "source_b"], generator: producer, tool: producer,
    state: "committed", created_at: at, committed_at: at,
    provenance: {
      schema_version: 1, run_id: "run_1", attempt_id: "attempt_1", source_ids: ["source_a", "source_b"],
      generator: producer, tool: producer, parents: [], media_type: "text/markdown",
      byte_length: 8, sha256: sha, committed_at: at,
    },
  };
}

function source(id: string, canonical: string, title: string) {
  return {
    id, authority: "arxiv", authority_id: canonical.replace(/^[^:]+:/, ""), canonical_id: canonical,
    source_kind: "paper", official_title: title, import_state: "imported", revision: 0,
    aliases: [], created_at: at, updated_at: at,
  };
}

// One projection carrying every region the Outputs panel can paint, including
// the states whose July rules had no themed accent (a stage that is neither
// completed nor running, a dormant lineage node).
const PROJECTION: ResearchWorkflowProjection = {
  schema_version: 1,
  run: { id: "run_1", thread_id: "thread_1", state: "completed", stage: null, active_attempt_id: null, latest_sequence: 3, engine_owned: false, revision: 3, created_at: at, updated_at: at },
  workflow: {
    id: "workflow_1", definition_id: "research", definition_version: 1, state: "completed",
    current_stage_key: null, revision: 3, created_at: at, updated_at: at,
    stages: [
      { key: "gather_sources", position: 0, effect: "gather", state: "completed", revision: 1, attempt: 1, checkpoint_enabled: true, started_at: at, completed_at: at },
      { key: "await_operator", position: 1, effect: "wait", state: "skipped", revision: 1, attempt: 1, checkpoint_enabled: false, started_at: null, completed_at: null },
      { key: "write_brief", position: 2, effect: "write", state: "failed", revision: 1, attempt: 1, checkpoint_enabled: true, started_at: at, completed_at: null },
    ],
  },
  source_gates: [{
    id: "gate_1", run_id: "run_1", attempt_id: "attempt_1", state: "resolved", revision: 1,
    created_at: at, updated_at: at, title_observation: "Echo-Infinity", locator_observation: "arxiv:2606.04527",
    candidates: [{ id: "candidate_1", claim_kind: "title", canonical_id: "arxiv:2606.04527", official_title: "Echo-Infinity", source_kind: "paper", version: null }],
    decision: null,
  }],
  sources: [{ id: "binding_1", disposition: "reused", created_at: at, source: source("source_a", "arxiv:2606.04527", "Echo-Infinity") }],
  lineage: {
    nodes: [
      { id: "node_1", status: "dormant", title: "Dormant baseline", revision: 1 },
      { id: "node_2", status: "graduated", title: "Graduated baseline", revision: 2 },
    ],
    links: [{ id: "link_1", from_node_id: "node_1", to_node_id: "node_2", relation: "supersedes" }],
    successor_node_id: "node_2",
  },
  decisions: [],
  artifacts: [{
    id: "artifact_brief", workspace_id: "ws_1", thread_id: "thread_1", kind: "living_brief",
    title: "Living Brief", head_artifact_version_id: "version_2", head_revision: 2,
    created_at: at, updated_at: at, versions: [version("version_1", 1), version("version_2", 2)],
  }],
  snapshots: [{
    id: "snapshot_1", workspace_id: "ws_1", name: "Helios immutable snapshot", created_at: at,
    members: [{ artifact_id: "artifact_brief", artifact_version_id: "version_2", logical_version: 2, sha256: sha }],
  }],
};

// The empty half of the panel: `research-empty` is only reachable when a
// section has nothing to show, so the theme contract is checked over both.
const EMPTY_PROJECTION: ResearchWorkflowProjection = {
  ...PROJECTION,
  workflow: null,
  source_gates: [],
  sources: [],
  lineage: { nodes: [], links: [], successor_node_id: null },
  artifacts: [],
  snapshots: [],
};

function refusing(category: string): typeof fetch {
  return (async () => new Response(JSON.stringify({
    type: `urn:cortex:problem:${category}`, title: "Refused", status: 409,
    category, retryable: false, owner: "cortexd",
  }), { status: 409, headers: { "Content-Type": "application/problem+json" } })) as typeof fetch;
}

function mounted(research: ResearchWorkflowProjection = PROJECTION, fetcher = refusing("internal_error"), offline = false) {
  const client = new CortexControlClient({ fetcher });
  return render(<ResearchWorkflowView client={client} offline={offline} research={research} />).container;
}

// The July cockpit's rules live unlayered in `globals.css`, which lane C does
// not own, so each one that carried a fixed light colour is corrected in place
// by an important utility on the same element. This table IS that contract: a
// legacy class reappearing without its override is the dark-mode regression.
const THEMED: Array<[string, string[]]> = [
  ["eyebrow", ["text-muted-foreground!"]],
  ["meta-chip", ["bg-muted!", "text-muted-foreground!"]],
  ["research-empty", ["text-muted-foreground!"]],
  ["source-gate-state", ["text-muted-foreground!"]],
  ["research-stage", ["bg-muted/50!"]],
  ["research-stage-index", ["text-muted-foreground!"]],
  ["source-gate", ["border-t-border!"]],
  ["source-candidate", ["border-border!", "bg-card!", "text-card-foreground!"]],
  ["bound-source", ["border-border!", "bg-card!", "text-card-foreground!"]],
  ["lineage-node", ["border-border!", "bg-card!", "text-card-foreground!"]],
  ["artifact-document", ["border-border!", "bg-card!", "text-card-foreground!"]],
  ["artifact-snapshot", ["border-border!", "bg-muted/40!"]],
];

// The selectors `scripts/verify-control-workflow.mjs` drives the real browser
// with. They are presentational class names used as acceptance hooks, so the
// theme repair had to keep every one of them rather than rewrite the markup.
const ACCEPTANCE_SELECTORS = [
  ".research-stage", ".research-stage.completed", ".source-gate", ".bound-source",
  ".lineage-node", ".lineage-link-list li", ".output-tabs", ".output-tabs [role=tab]",
  ".artifact-snapshot", ".artifact-reader-toolbar", ".artifact-metadata div",
];

describe("research outputs theme", () => {
  it("paints every legacy class from the theme instead of its fixed light colour", () => {
    const containers = [mounted(), mounted(EMPTY_PROJECTION)];
    const missing: string[] = [];
    for (const [legacy, overrides] of THEMED) {
      const nodes = containers.flatMap((container) => [...container.querySelectorAll(`[class~="${legacy}"]`)]);
      expect(nodes.length, `${legacy} is rendered by neither projection`).toBeGreaterThan(0);
      for (const node of nodes) {
        for (const override of overrides) {
          if (!node.classList.contains(override)) missing.push(`${legacy} -> ${override}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("leaves no colour literal in the owned components' own markup", () => {
    const container = mounted();
    for (const node of container.querySelectorAll("[style]")) {
      expect(node.getAttribute("style")).not.toMatch(/#[0-9a-f]{3,8}|rgba?\(|hsla?\(/i);
    }
  });

  it("keeps the class hooks the browser workflow acceptance selects", () => {
    const container = mounted();
    const empty = ACCEPTANCE_SELECTORS.filter((selector) => container.querySelectorAll(selector).length === 0);
    // `.research-stage.completed` is one of the eleven this projection carries
    // as one; everything else must match at least once.
    expect(empty).toEqual([]);
  });

  it("themes a state the July rules gave no accent, and leaves the accented ones alone", () => {
    const container = mounted();
    // `completed` / `running` / `waiting` / `failed` paint from bridged tokens
    // already; only a state outside that set needs its grey border replaced.
    expect(container.querySelector(".research-stage.skipped")?.classList.contains("border-t-border!")).toBe(true);
    expect(container.querySelector(".research-stage.completed")?.classList.contains("border-t-border!")).toBe(false);
    expect(container.querySelector(".research-stage.failed")?.classList.contains("border-t-border!")).toBe(false);
    // The dormant lineage accent was the one hard-coded grey among the three.
    expect(container.querySelector(".lineage-node.dormant")?.classList.contains("border-l-muted-foreground!")).toBe(true);
    expect(container.querySelector(".lineage-node.graduated")?.classList.contains("border-l-muted-foreground!")).toBe(false);
  });
});

// Task 4: the research projection carries no failure category of its own, so the
// only refusals this panel may name are the ones the artifact content route
// actually answers with. A category it has never seen keeps the general
// sentence rather than being dressed up as a provider error.
describe("research outputs refusals", () => {
  it("names a refusal category the content route really answers with", async () => {
    mounted(PROJECTION, refusing("artifact_content_unavailable"));
    await waitFor(() => expect(screen.getByText(/missing or could not be verified/)).not.toBeNull());
  });

  it("keeps the general sentence for a category it does not know", async () => {
    mounted(PROJECTION, refusing("some_unmapped_category"));
    await waitFor(() => expect(screen.getByText("Verified content is unavailable.")).not.toBeNull());
  });

  it("prefers the offline explanation over any category", async () => {
    mounted(PROJECTION, refusing("artifact_content_unavailable"), true);
    await waitFor(() => expect(screen.getByText(/Content was not cached/)).not.toBeNull());
  });

  it("says it is loading before the read answers", () => {
    mounted(PROJECTION, (() => new Promise(() => {})) as unknown as typeof fetch);
    expect(screen.getByText("Loading verified content…")).not.toBeNull();
  });
});


it("keeps revisions and internal identities in closed details while exposing the source result", () => {
  const container = mounted();
  expect(container.querySelector(".source-gate-state")?.textContent).toBe("resolved");
  for (const details of container.querySelectorAll("details[data-details]")) {
    expect((details as HTMLDetailsElement).open).toBe(false);
  }
  expect(container.querySelector(".artifact-metadata")?.closest("details[data-details]")).not.toBeNull();
  const visible = container.cloneNode(true) as HTMLElement;
  visible.querySelectorAll("[data-details]").forEach((node) => node.remove());
  const text = visible.textContent ?? "";
  expect(text).not.toMatch(BANNED_WORDS);
  for (const identity of [sha, "source_a", "source_b", "artifact_brief", "version_2"]) {
    expect(text).not.toContain(identity);
  }
});

it("resets the document to preview on version changes without losing exact source", async () => {
  const { default: userEvent } = await import("@testing-library/user-event");
  const user = userEvent.setup();
  const fetcher = (async (input: string | URL | Request) => {
    const id = String(input).includes("version_1") ? "version_1" : "version_2";
    const text = `# ${id}\n\n$x^2$`;
    return new Response(JSON.stringify({ artifact_version_id: id, media_type: "text/markdown", byte_length: new TextEncoder().encode(text).length, sha256: sha, content: text }), { headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  mounted(PROJECTION, fetcher);
  await screen.findByRole("heading", { name: "version_2" });
  await user.click(screen.getByRole("button", { name: "Source" }));
  expect(screen.getByRole("region", { name: "Markdown source" }).textContent).toBe("# version_2\n\n$x^2$");
  await user.selectOptions(screen.getByRole("combobox", { name: "Living Brief version" }), "version_1");
  await screen.findByRole("heading", { name: "version_1" });
  expect(screen.getByRole("button", { name: "Preview" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.queryByRole("region", { name: "Markdown source" })).toBeNull();
});
