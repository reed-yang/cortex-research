import { describe, expect, it } from "vitest";
import { readShellLocation, writeShellLocation } from "../../app/shell/url-state";

describe("shell url state", () => {
  it("reads project, thread and view and ignores junk", () => {
    expect(readShellLocation("?project=ws_1&thread=thread_2&view=inbox")).toEqual({ project: "ws_1", thread: "thread_2", item: null, view: "inbox" });
    expect(readShellLocation("?project=../x&view=nope")).toEqual({ project: null, thread: null, item: null, view: "thread" });
  });
  it("keeps a research item only when it is spelled like one", () => {
    const id = `ri_${"a".repeat(32)}`;
    expect(readShellLocation(`?view=research&item=${id}`)).toEqual({ project: null, thread: null, item: id, view: "research" });
    expect(readShellLocation("?view=research&item=ri_nope")).toEqual({ project: null, thread: null, item: null, view: "research" });
  });
  it("writes with replaceState and omits the default view", () => {
    writeShellLocation({ project: "ws_1", thread: null, item: null, view: "thread" });
    expect(window.location.search).toBe("?project=ws_1");
    writeShellLocation({ project: "ws_1", thread: "thread_2", item: null, view: "library" });
    expect(window.location.search).toBe("?project=ws_1&thread=thread_2&view=library");
  });
  it("writes the selected research item beside the view", () => {
    const id = `ri_${"b".repeat(32)}`;
    writeShellLocation({ project: "ws_1", thread: null, item: id, view: "research" });
    expect(window.location.search).toBe(`?project=ws_1&item=${id}&view=research`);
  });
});
