import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useMemo } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { DecisionCards } from "../../app/shell/decision-card";
import { useControlState } from "../../app/shell/use-control-state";
import { FakeControl } from "./fake-control";

// `tests/jsdom-setup.ts` owns the Testing Library cleanup for every suite; this
// hook also resets the query the shell reads once on mount, and unmounts first
// so the reset never lands under a mounted tree.
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function Harness({ control }: { control: FakeControl }) {
  const client = useMemo(() => control.client(), [control]);
  const [state, actions] = useControlState(client);
  if (state.loading) return <p>Loading</p>;
  return (
    <>
      <p>{state.notice ? `Notice: ${state.notice.text}` : "No notice"}</p>
      <DecisionCards actions={actions} client={client} state={state} />
    </>
  );
}

function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question", { active_run_id: "run_1" });
  control.message("msg_1", "thread_1", "user", "hello", 1);
  const run = control.run("run_1", "thread_1", "waiting_for_decision");
  control.decision("decision_1", "run_1", "Adopt this source as evidence?", [
    { id: "approve_once", label: "Approve once", tone: "primary" },
    { id: "decline", label: "Decline", tone: "danger" },
  ]);
  control.research.run_1 = {
    schema_version: 1, run, workflow: null, source_gates: [], sources: [],
    lineage: { nodes: [], links: [], successor_node_id: null },
    decisions: control.decisions, artifacts: [], snapshots: [],
  };
  return control;
}

async function mounted(control: FakeControl) {
  render(<Harness control={control} />);
  await waitFor(() => expect(screen.queryByText("Loading")).toBeNull());
  return userEvent.setup();
}

describe("decision cards", () => {
  it("renders one card per option and resolves the chosen one", async () => {
    const control = seeded();
    const user = await mounted(control);
    const card = screen.getByRole("article", { name: "Decision" });
    expect(within(card).getByText("Adopt this source as evidence?")).toBeTruthy();
    expect(within(card).getAllByRole("button").map((button) => button.textContent)).toEqual(["Approve once", "Decline"]);
    await user.click(within(card).getByRole("button", { name: "Approve once" }));
    await waitFor(() => expect(control.posts).toHaveLength(1));
    expect(control.posts[0]).toMatchObject({ path: "decisions/decision_1/resolve", body: { choice: "approve_once", expected_revision: 0 } });
  });

  it("keeps the card and says so when the answer meets a newer state", async () => {
    const control = seeded();
    const user = await mounted(control);
    control.failNext = { path: /^decisions\/decision_1\/resolve$/, status: 409, category: "revision_conflict" };
    await user.click(screen.getByRole("button", { name: "Approve once" }));
    await waitFor(() => expect(screen.getByText(/^Notice: /).textContent).toBe("Notice: This decision changed. Review it again."));
    expect(screen.getByRole("article", { name: "Decision" })).toBeTruthy();
  });
});
