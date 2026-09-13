import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { BANNED_WORDS, copy } from "../../app/shell/copy";
import { Shell } from "../../app/shell/shell";
import { FakeControl } from "./fake-control";

// `tests/jsdom-setup.ts` owns the Testing Library cleanup for every suite; this
// hook also resets the query the shell reads once on mount, and unmounts first
// so the reset never lands under a mounted tree.
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); });

function seeded() {
  const control = new FakeControl();
  control.workspace("ws_1", "Echo memory");
  control.thread("thread_1", "ws_1", "First question");
  return control;
}

async function openStatus(control: FakeControl) {
  window.history.replaceState(null, "", "/?project=ws_1&view=status");
  const view = render(<Shell client={control.client()} />);
  await screen.findByLabelText("Status");
  return view;
}

describe("StatusView", () => {
  it("reports what the daemon says about itself", async () => {
    const control = seeded();
    control.health = { api_version: "v1", capabilities: { control_store: true }, runtime_dispatch_enabled: false };
    await openStatus(control);

    await waitFor(() => expect(screen.getByRole("definition", { name: "API version" }).textContent).toBe("v1"));
    expect(screen.getByRole("definition", { name: "Runtime dispatch" }).textContent).toBe("disabled");
    expect(screen.getByRole("definition", { name: "Live updates" }).textContent).toBe(copy.status.replay.idle);
  });

  it("says what the update stream is doing in words, not in its own token", async () => {
    const control = seeded();
    await openStatus(control);

    const live = await screen.findByRole("definition", { name: "Live updates" });
    // Every state the replay controller can report has a sentence; none of
    // them is the token itself.
    for (const [token, sentence] of Object.entries(copy.status.replay)) {
      expect(sentence, token).not.toBe(token);
    }
    expect(live.textContent).toBe(copy.status.replay.idle);
  });

  it("calls the dispatch gate unknown when the daemon does not report it", async () => {
    const control = seeded();
    control.health = { api_version: "v1", capabilities: { control_store: true } };
    await openStatus(control);

    await waitFor(() => expect(screen.getByRole("definition", { name: "Runtime dispatch" }).textContent).toBe("unknown"));
  });

  it("lists what the build can do in the product's words, not the daemon's keys", async () => {
    const control = seeded();
    // The key list the real `/health` reports (cortex_platform/product/api/app.py),
    // plus a key this app has never heard of whose own name is Control's.
    control.health = {
      api_version: "v1",
      capabilities: {
        control_store: true, event_replay: true, event_stream: true, source_resolution: false,
        artifact_metadata: true, research_pipeline: true, runtime_dispatch: true,
        telegram_adapter: false, telegram_mode: "off",
        durable_operation_deduplication: true,
      },
      runtime_dispatch_enabled: false,
    };
    await openStatus(control);

    const region = await screen.findByRole("region", { name: copy.status.capabilitiesTitle });
    await screen.findByText(copy.status.capabilities.control_store!);
    expect(screen.getByText(copy.status.capabilities.event_replay!)).toBeTruthy();
    expect(screen.getByText(copy.status.capabilitiesOff.source_resolution!)).toBeTruthy();
    expect(screen.getByText(copy.status.capabilitiesOff.telegram_adapter!)).toBeTruthy();
    // An unknown key is spoken, and every word Control owns -- including the
    // morphological variants the stemmed list catches -- is dropped from it
    // rather than sentence-cased onto the screen.
    expect(screen.getByText(`Operation ${copy.status.available}`)).toBeTruthy();
    expect(region.textContent ?? "").not.toMatch(BANNED_WORDS);
    // A capability whose value is not a boolean is dropped, never read as "not
    // available".
    expect(screen.queryByText(/[Tt]elegram mode/)).toBeNull();
  });

  it("says nothing about capabilities the daemon does not report", async () => {
    const control = seeded();
    control.health = { api_version: "v1", runtime_dispatch_enabled: false };
    await openStatus(control);

    await screen.findByText("This daemon reports no capabilities.");
  });

  it("turns engine projects and threads on through the shell state", async () => {
    const control = seeded();
    const user = userEvent.setup();
    await openStatus(control);

    const toggle = screen.getByRole("switch", { name: "Show engine projects and threads" });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    await user.click(toggle);

    await waitFor(() => expect((screen.getByRole("switch", { name: "Show engine projects and threads" }) as HTMLInputElement).checked).toBe(true));
    expect(screen.getByText("This thread belongs to the research engine; its runs are created by the engine only.")).toBeTruthy();
  });
});
