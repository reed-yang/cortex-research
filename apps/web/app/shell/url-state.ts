import { isResearchItemId } from "../control/research-items-contracts";
import type { ShellView } from "./types";

export type ShellLocation = {
  project: string | null;
  thread: string | null;
  // The research item the catalog has open, kept apart from `thread` because
  // an item is an original research identity and a thread is a conversation.
  item: string | null;
  view: ShellView;
};
const VIEWS: ShellView[] = ["thread", "research", "library", "inbox", "status"];
const ID = /^[A-Za-z0-9_-]{1,128}$/;

export function readShellLocation(search: string): ShellLocation {
  const params = new URLSearchParams(search);
  const project = params.get("project");
  const thread = params.get("thread");
  const item = params.get("item");
  const view = params.get("view");
  return {
    project: project && ID.test(project) ? project : null,
    thread: thread && ID.test(thread) ? thread : null,
    // Only the catalog's own identity shape is accepted, so a link that names
    // anything else opens the catalog with nothing selected.
    item: item && isResearchItemId(item) ? item : null,
    view: VIEWS.includes(view as ShellView) ? (view as ShellView) : "thread",
  };
}

// Client-only: rewrites the query without a Next navigation. `mode`, `capture`
// and `scenario` are never written, so page.tsx's server-side checks stay inert.
export function writeShellLocation(location: ShellLocation): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams();
  if (location.project) params.set("project", location.project);
  if (location.thread) params.set("thread", location.thread);
  if (location.item) params.set("item", location.item);
  if (location.view !== "thread") params.set("view", location.view);
  const query = params.toString();
  const next = `${window.location.pathname}${query ? `?${query}` : ""}`;
  if (next !== `${window.location.pathname}${window.location.search}`) window.history.replaceState(null, "", next);
}
