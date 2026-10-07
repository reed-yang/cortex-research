"use client";

import { useCallback, useEffect, useState } from "react";

// A pane width the operator set by dragging, kept in this browser only. Null
// is the layout's own width. Storage is read after mount, so the server render
// and the first client render agree; a stored value that is not a positive
// number is ignored.
export function useStoredWidth(key: string): [number | null, (width: number | null) => void] {
  const [width, setWidth] = useState<number | null>(null);
  useEffect(() => {
    try {
      const stored = Number(window.localStorage.getItem(key) ?? Number.NaN);
      if (Number.isFinite(stored) && stored > 0) setWidth(stored);
    } catch {
      // Storage refused (private mode, a policy): the default layout stays.
    }
  }, [key]);
  const update = useCallback((next: number | null) => {
    setWidth(next);
    try {
      if (next === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, String(Math.round(next)));
    } catch {
      // The width still applies until the page reloads.
    }
  }, [key]);
  return [width, update];
}
