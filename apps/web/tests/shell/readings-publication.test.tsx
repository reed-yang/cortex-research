import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CortexControlClient } from "../../app/control/client";
import { decodeReadingsStatus } from "../../app/control/readings-contracts";
import { ReadingsPublication } from "../../app/shell/readings-publication";

function client(value: unknown) {
  return new CortexControlClient({ fetcher: vi.fn(async () => new Response(JSON.stringify(value), {
    status: 200, headers: { "Content-Type": "application/json" },
  })) });
}

describe("readings publication", () => {
  it("shows publication failure independently of source import", async () => {
    render(<ReadingsPublication client={client({ enabled: true, items: [{ source_id: "source_1", state: "failed" }] })} sourceId="source_1" />);
    expect(await screen.findByText("Readings publication failed; retry is scheduled")).toBeTruthy();
    expect(screen.getByText("Library import and readings publication have separate status.")).toBeTruthy();
  });

  it("does not call an unscheduled source published", async () => {
    render(<ReadingsPublication client={client({ enabled: true, items: [{ source_id: "source_2", state: "published" }] })} sourceId="source_1" />);
    expect(await screen.findByText("This source is not scheduled for readings publication.")).toBeTruthy();
    expect(screen.queryByText("Published to readings")).toBeNull();
  });

  it("hides disabled publication", async () => {
    const api = client({ enabled: false, items: [] });
    const spy = vi.spyOn(api, "getReadingsStatus");
    render(<ReadingsPublication client={api} sourceId={null} />);
    await waitFor(() => expect(spy).toHaveBeenCalledOnce());
    expect(screen.queryByLabelText("Readings publication")).toBeNull();
  });

  it("fails visibly on an invalid response", async () => {
    render(<ReadingsPublication client={client({ enabled: true, items: [{ source_id: "source_1", state: "deleted" }] })} sourceId="source_1" />);
    expect(await screen.findByText("Readings publication status is unavailable.")).toBeTruthy();
  });

  it("rejects ambiguous duplicate source status", () => {
    expect(() => decodeReadingsStatus({ enabled: true, items: [
      { source_id: "source_1", state: "published" }, { source_id: "source_1", state: "failed" },
    ] })).toThrow("duplicate publication source");
  });
});
