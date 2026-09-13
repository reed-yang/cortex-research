import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { RunEvent } from "../../app/control/contracts";
import type { ResearchArtifactProjection, ResearchWorkflowProjection } from "../../app/control/research-contracts";
import { OutputsPanel } from "../../app/shell/outputs-panel";
import { RunHistory } from "../../app/shell/run-history";
import { actionsBag, baseState, fixtureRun } from "./state-fixture";
import { FakeControl } from "./fake-control";


function failureEvent(runId: string, category: string): RunEvent {
  return {
    cursor: "cursor_1", schema_version: 1, id: "event_1", run_id: runId, attempt_id: "attempt_1",
    sequence: 1, type: "run.failed", occurred_at: new Date().toISOString(), causation_id: null,
    durability: "durable", payload: { failure_category: category },
  };
}

function projection(artifacts: ResearchArtifactProjection[]): ResearchWorkflowProjection {
  return {
    schema_version: 1, run: fixtureRun("run_1", "completed", 5), workflow: null, source_gates: [], sources: [],
    lineage: { nodes: [], links: [], successor_node_id: null }, decisions: [], artifacts, snapshots: [],
  };
}

// The panel only counts artifacts; the projection decoder owns their shape and
// is tested against Control's own payloads elsewhere.
const ARTIFACT = { id: "artifact_1", title: "Echo brief" } as unknown as ResearchArtifactProjection;

async function openRunHistory() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Runs" }));
  return user;
}

describe("run history", () => {
  it("stays collapsed until it is opened", () => {
    render(<RunHistory actions={actionsBag()} client={new FakeControl().client()} state={baseState({ runs: [fixtureRun("run_1", "completed", 90, 1)] })} />);
    expect(screen.getByRole("button", { name: "Runs" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Completed/ })).toBeNull();
  });

  it("lists the thread's runs newest first, by the time each started", async () => {
    // run_1 started earlier but was touched a minute ago; run_2 started later.
    const runs = [fixtureRun("run_1", "completed", 90, 1), fixtureRun("run_2", "waiting_for_decision", 5, 5)];
    render(<RunHistory actions={actionsBag()} client={new FakeControl().client()} state={baseState({ runs, selectedRunId: "run_2" })} />);
    await openRunHistory();
    const history = screen.getByLabelText("Run history");
    const items = within(history).getAllByRole("button").slice(1);
    expect(items.map((item) => item.textContent)).toEqual(["Waiting for you · 5 min ago", "Completed · 1 h ago"]);
    expect(items[0]!.getAttribute("aria-pressed")).toBe("true");
    expect(items[1]!.getAttribute("aria-pressed")).toBe("false");
  });

  it("says why a failed run failed when its events said so", async () => {
    const runs = [fixtureRun("run_1", "failed", 5, 1)];
    render(<RunHistory actions={actionsBag()} client={new FakeControl().client()} state={baseState({ runs, events: [failureEvent("run_1", "worker_unavailable")] })} />);
    await openRunHistory();
    expect(screen.getByRole("button", { name: "Failed · 5 min ago · Worker unavailable" })).toBeTruthy();
  });

  it("leaves out a failure category made of words chrome may not carry", async () => {
    const runs = [fixtureRun("run_1", "failed", 5, 1)];
    render(<RunHistory actions={actionsBag()} client={new FakeControl().client()} state={baseState({ runs, events: [failureEvent("run_1", "revision_conflict")] })} />);
    await openRunHistory();
    expect(screen.getByRole("button", { name: "Failed · 5 min ago" })).toBeTruthy();
  });

  it("selects a run and asks for the older ones", async () => {
    const actions = actionsBag();
    const runs = [fixtureRun("run_1", "completed", 90, 1), fixtureRun("run_2", "failed", 5, 5)];
    render(<RunHistory actions={actions} client={new FakeControl().client()} state={baseState({ runs, selectedRunId: "run_2", nextRunCursor: "cursor_1" })} />);
    const user = await openRunHistory();
    await user.click(screen.getByRole("button", { name: "Completed · 1 h ago" }));
    expect(actions.selectRun).toHaveBeenCalledWith("run_1");
    await user.click(screen.getByRole("button", { name: "Load older" }));
    expect(actions.loadOlderRuns).toHaveBeenCalled();
  });

  it("says nothing when the thread has no runs", () => {
    render(<RunHistory actions={actionsBag()} client={new FakeControl().client()} state={baseState()} />);
    expect(screen.queryByLabelText("Run history")).toBeNull();
  });
});

describe("outputs panel", () => {
  it("is absent while the selected run has no research projection", () => {
    render(<OutputsPanel actions={actionsBag()} client={new FakeControl().client()} state={baseState()} />);
    expect(screen.queryByLabelText("Outputs")).toBeNull();
  });

  it("appears for a projection and marks the one that produced something", () => {
    const { rerender } = render(<OutputsPanel actions={actionsBag()} client={new FakeControl().client()} state={baseState({ research: projection([]) })} />);
    const empty = screen.getByLabelText("Outputs");
    expect(within(empty).queryByText("Outputs")).not.toBeNull();
    expect(empty.querySelector("[data-outputs-dot]")).toBeNull();
    rerender(<OutputsPanel actions={actionsBag()} client={new FakeControl().client()} state={baseState({ research: projection([ARTIFACT]) })} />);
    expect(screen.getByLabelText("Outputs").querySelector("[data-outputs-dot]")).not.toBeNull();
  });
});
