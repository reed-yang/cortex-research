import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom implements neither ResizeObserver nor scrolling; the assistant-ui thread
// primitives observe their viewport and auto-scroll it on mount. No-op stubs are
// enough for rendering assertions.
class NoopResizeObserver implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver ??= NoopResizeObserver;
Element.prototype.scrollTo ??= function scrollTo(): void {};

// Vitest runs without `globals`, so Testing Library registers no automatic
// cleanup of its own: without this every rendered tree stays in the document
// and the next test in the file queries a DOM holding both.
afterEach(cleanup);
