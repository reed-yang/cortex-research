import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CortexPrototype } from "../app/cortex-prototype";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub;
HTMLElement.prototype.scrollTo = () => {};


function expectG0ResearchUnchanged(container: HTMLElement, rawLogOpen = false) {
  expect(screen.getByText("0 changes")).toBeTruthy();
  expect(screen.getByText("0 transactions · 0 materializations")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Artifacts are not created yet" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "Living Brief" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Evidence" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Training Plan" })).toBeNull();
  expect(container.querySelector("details.raw-log")?.hasAttribute("open")).toBe(rawLogOpen);
}

function expectUiLocalTransition(container: HTMLElement, eventType: string) {
  expect(screen.getByText(/6 durable events · 2 UI-local transitions/)).toBeTruthy();
  expect(screen.getByText("Canonical ledger remains at pending decision rev 1 · UI preview: decision rev 2, run " + (eventType === "run.canceled" ? "canceled" : "resuming"))).toBeTruthy();
  expect(screen.getByText("UI-local preview · not persisted")).toBeTruthy();
  const raw = container.querySelector(".raw-log pre")?.textContent ?? "";
  expect(raw).toMatch(/decision\.resolved\s+ui_local\s+mock_transition/);
  expect(raw).toMatch(new RegExp(`${eventType.replace(".", "\\.")}\\s+ui_local\\s+mock_transition`));
  expect(raw).not.toMatch(/decision\.resolved\s+durable\s+mock_transition/);
}

describe("G0 decision state transitions", () => {
  it("shows canonical identity separately and disables artifact threads", () => {
    const { container } = render(<CortexPrototype />);

    expect(container.querySelector(".status-chip")?.textContent).toBe("Decision required");
    expect(container.querySelector(".inbox-count")?.textContent).toBe("1");
    expect(screen.getByText(/mock actions below preview UI-local outcomes only/)).toBeTruthy();
    expect(screen.getByText("arxiv:2607.07675")).toBeTruthy();
    expect(screen.getByText("LingBot-Video")).toBeTruthy();
    expect(screen.getByText("Model alias")).toBeTruthy();
    for (const button of screen.getAllByRole("button", { name: /unavailable before artifact creation/ })) {
      expect(button).toHaveProperty("disabled", true);
    }
    expectG0ResearchUnchanged(container);
  });

  it.each([
    ["Keep both sources", "UI preview: resume with Echo and LingBot as separate sources"],
    ["Replace URL with Echo-Infinity", "UI preview: resume with Echo-Infinity only"],
  ])("moves %s to a resolved resuming state", async (buttonName, sourcePlan) => {
    const user = userEvent.setup();
    const { container } = render(<CortexPrototype />);

    await user.click(screen.getByRole("button", { name: buttonName }));

    expect(container.querySelector(".status-chip")?.textContent).toBe("Resuming · UI preview");
    expect(container.querySelector(".inbox-count")?.textContent).toBe("0");
    expect(container.querySelector(".decision-revision")?.textContent).toBe("rev 2");
    expect(screen.getByText(sourcePlan)).toBeTruthy();
    expect(screen.getByText(/6 durable events · 2 UI-local transitions/)).toBeTruthy();
    fireEvent.click(screen.getByText("Raw Log"));
    expect(screen.getByText(/decision\.resolved/)).toBeTruthy();
    expect(screen.getByText(/run\.resuming/)).toBeTruthy();
    expectUiLocalTransition(container, "run.resuming");
    expectG0ResearchUnchanged(container, true);
  });

  it("cancels instead of marking the run as running or resuming", async () => {
    const user = userEvent.setup();
    const { container } = render(<CortexPrototype />);

    await user.click(screen.getByRole("button", { name: "Cancel run" }));

    expect(container.querySelector(".status-chip")?.textContent).toBe("Canceled · UI preview");
    expect(container.querySelector(".status-chip")?.textContent).not.toBe("Resuming");
    expect(container.querySelector(".inbox-count")?.textContent).toBe("0");
    expect(screen.getByText("UI preview: canceled before import or materialization")).toBeTruthy();
    expect(screen.getByText(/6 durable events · 2 UI-local transitions/)).toBeTruthy();
    fireEvent.click(screen.getByText("Raw Log"));
    expect(screen.getByText(/run\.canceled/)).toBeTruthy();
    expectUiLocalTransition(container, "run.canceled");
    expectG0ResearchUnchanged(container, true);
  });
});

describe("G1 lineage and thread navigation", () => {
  it("initializes directly from the validated G1 page scenario", () => {
    const { container } = render(<CortexPrototype captureMode initialScenario="g1" />);

    expect(container.querySelector("[data-capture-scenario='g1']")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Successor memory research workspace" })).toBeTruthy();
    expect(container.querySelector(".inbox-count")?.textContent).toBe("0");
    expect(screen.getAllByText("Canonical fixture resolution")).toHaveLength(2);
    expect(screen.queryByRole("heading", { name: "Artifacts are not created yet" })).toBeNull();
  });

  it("switches panel state with the selected canonical thread", async () => {
    const user = userEvent.setup();
    render(<CortexPrototype />);
    await user.click(screen.getByRole("button", { name: "G1 · resolved" }));

    expect(screen.getByRole("heading", { name: "Evidence" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Living Brief" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Helios memory architecture" }));
    expect(screen.getByRole("heading", { name: "Living Brief" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Snapshot" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Evidence" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Training and ablation plan" }));
    expect(screen.getByRole("heading", { name: "Training Plan" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Snapshot" })).toBeNull();
  });

  it("renders two direct successor reuse edges and no dormant-to-graduated chain", async () => {
    const user = userEvent.setup();
    const { container } = render(<CortexPrototype />);
    await user.click(screen.getByRole("button", { name: "G1 · resolved" }));

    const graph = screen.getByLabelText("Successor lineage");
    expect(within(graph).getByText("Helios-14B TTT memory feasibility")).toBeTruthy();
    expect(within(graph).getByText("Echo memory architecture survey")).toBeTruthy();
    const links = [...container.querySelectorAll(".lineage-branch")];
    expect(links).toHaveLength(2);
    expect(links.map((link) => link.getAttribute("data-from"))).toEqual([
      "lineage-helios-echo-successor",
      "lineage-helios-echo-successor",
    ]);
    expect(links.map((link) => link.getAttribute("data-to"))).toEqual([
      "lineage-helios-ttt-dormant",
      "lineage-echo-memory-graduated",
    ]);
  });
});

describe("PWA mobile shell", () => {
  it("exposes one mobile section navigator over the shared workspace", () => {
    render(<CortexPrototype />);

    const navigation = screen.getByRole("navigation", { name: "Mobile workspace sections" });
    expect(within(navigation).getByRole("link", { name: "Overview" }).getAttribute("href")).toBe("#mobile-overview");
    expect(within(navigation).getByRole("link", { name: "Artifacts" }).getAttribute("href")).toBe("#mobile-artifacts");
    expect(within(navigation).getByRole("link", { name: "Chat" }).getAttribute("href")).toBe("#mobile-conversation");
    expect(within(navigation).getByRole("link", { name: "Decisions1" }).getAttribute("href")).toBe("#mobile-decisions");
  });

  it("disables fixture mutations offline and queues no action after reconnect", async () => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    render(<CortexPrototype />);

    fireEvent(window, new Event("offline"));
    expect(await screen.findByText("Offline · read-only fixture")).toBeTruthy();
    for (const button of screen.getAllByRole("button", { name: /· offline$/ })) {
      expect(button).toHaveProperty("disabled", true);
    }
    expect(screen.getByRole("button", { name: "Keep both sources · offline" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Replace URL with Echo-Infinity · offline" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel run · offline" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Steer this run" })).toHaveProperty("disabled", true);

    fireEvent(window, new Event("online"));
    expect(await screen.findByText("Connection restored")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Keep both sources" })).toHaveProperty("disabled", false);
    });
    expect(screen.getByText("The canonical fixture shell is available again. No offline action was queued.")).toBeTruthy();
    expect(screen.getByText("0 changes")).toBeTruthy();
  });
});
