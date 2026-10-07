import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResizeHandle } from "../../components/ui/resize-handle";

// jsdom lays nothing out and has no pointer capture, so the pane reports a
// fixed width and capture is a set of pointer ids.
const captured = new Set<number>();
const capture = {
  setPointerCapture: (id: number) => { captured.add(id); },
  releasePointerCapture: (id: number) => { captured.delete(id); },
  hasPointerCapture: (id: number) => captured.has(id),
};
beforeEach(() => {
  captured.clear();
  Object.assign(Element.prototype, capture);
});
afterEach(() => {
  vi.restoreAllMocks();
  for (const name of Object.keys(capture)) Reflect.deleteProperty(Element.prototype, name);
});

function handle() {
  const onPreview = vi.fn();
  const onCommit = vi.fn();
  const onReset = vi.fn();
  render(
    <div>
      <div data-testid="pane" />
      <ResizeHandle bounds={() => ({ min: 200, max: 500 })} label="Resize the pane" onCommit={onCommit} onPreview={onPreview} onReset={onReset} />
    </div>,
  );
  vi.spyOn(screen.getByTestId("pane"), "getBoundingClientRect").mockReturnValue({ width: 300 } as DOMRect);
  return { separator: screen.getByRole("separator", { name: "Resize the pane" }), onPreview, onCommit, onReset };
}

describe("ResizeHandle", () => {
  it("previews a drag within its bounds and commits where the pointer is let go", () => {
    const { separator, onPreview, onCommit } = handle();
    fireEvent.pointerDown(separator, { button: 0, pointerId: 1, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 160 });
    expect(onPreview).toHaveBeenLastCalledWith(360);
    // Another pointer does not move the boundary.
    fireEvent.pointerMove(separator, { pointerId: 2, clientX: 120 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 900 });
    expect(onPreview).toHaveBeenLastCalledWith(500);
    expect(onCommit).not.toHaveBeenCalled();
    fireEvent.pointerUp(separator, { pointerId: 1, clientX: 900 });
    expect(onCommit).toHaveBeenCalledExactlyOnceWith(500);
    expect(captured.size).toBe(0);
    expect(separator.getAttribute("aria-valuenow")).toBe("500");
  });

  it("leaves the width alone on a click without movement and resets on a double click", () => {
    const { separator, onCommit, onReset } = handle();
    fireEvent.pointerDown(separator, { button: 0, pointerId: 1, clientX: 100 });
    fireEvent.pointerUp(separator, { pointerId: 1, clientX: 100 });
    expect(onCommit).not.toHaveBeenCalled();
    fireEvent.doubleClick(separator);
    expect(onReset).toHaveBeenCalledOnce();
  });

  it("ignores a drag that is not the primary button", () => {
    const { separator, onPreview } = handle();
    fireEvent.pointerDown(separator, { button: 2, pointerId: 1, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 160 });
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("moves with the arrow keys and jumps to either limit", () => {
    const { separator, onCommit, onReset } = handle();
    separator.focus();
    expect(separator.getAttribute("aria-valuemin")).toBe("200");
    expect(separator.getAttribute("aria-valuemax")).toBe("500");
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(onCommit).toHaveBeenLastCalledWith(310);
    fireEvent.keyDown(separator, { key: "ArrowLeft", shiftKey: true });
    expect(onCommit).toHaveBeenLastCalledWith(250);
    fireEvent.keyDown(separator, { key: "End" });
    expect(onCommit).toHaveBeenLastCalledWith(500);
    fireEvent.keyDown(separator, { key: "Home" });
    expect(onCommit).toHaveBeenLastCalledWith(200);
    fireEvent.keyDown(separator, { key: "Enter" });
    expect(onReset).toHaveBeenCalledOnce();
  });
});
