"use client";

import { useMemo, useRef, type CSSProperties } from "react";
import { useStoredWidth } from "@/hooks/use-stored-width";

// The reader beside the list keeps at least this much room, next to the 1.5rem
// track the resize handle sits in.
const READER_MIN = 360;
const TRACK = 24;
const LIST_MIN = 240;

function track(width: number): string {
  return `max(${LIST_MIN}px, min(${Math.round(width)}px, 100% - ${READER_MIN + TRACK}px))`;
}

// The list column of a list-and-reader view (Library, Research) at the width
// the operator dragged it to. Until they do, `--list-width` stays unset and
// the view keeps its own one-to-two split; the stored width is re-clamped by
// the stylesheet when the window is narrower than when it was set.
export function useListColumn(storageKey: string) {
  const [width, setWidth] = useStoredWidth(storageKey);
  const section = useRef<HTMLElement>(null);
  const handle = useMemo(() => ({
    bounds: (element: HTMLElement) => {
      const grid = element.parentElement;
      if (!grid) return { min: LIST_MIN, max: LIST_MIN };
      const style = getComputedStyle(grid);
      const content = grid.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0");
      return { min: LIST_MIN, max: content - READER_MIN - TRACK };
    },
    onPreview: (next: number) => section.current?.style.setProperty("--list-width", track(next)),
    onCommit: (next: number) => setWidth(next),
    onReset: () => setWidth(null),
  }), [setWidth]);
  const style = width === null ? undefined : ({ "--list-width": track(width) } as CSSProperties);
  return { section, style, handle };
}
