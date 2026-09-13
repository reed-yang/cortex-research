"use client";

import { ChevronsUpDownIcon } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { copy } from "./copy";
import type { ViewProps } from "./types";

// The two dialogs differ only in their words and in the command they send, so
// one form covers both and the mode decides the rest.
type Mode = "create" | "rename";

const DIALOG: Record<Mode, { title: string; submit: string }> = {
  create: { title: copy.sidebar.createProjectTitle, submit: copy.sidebar.createProjectSubmit },
  rename: { title: copy.sidebar.renameProjectTitle, submit: copy.sidebar.renameProjectSubmit },
};

// `onProjectSwitched` lets the mobile drawer dismiss itself once the project
// behind it has changed.
export function ProjectSwitcher({ state, actions, onProjectSwitched }: ViewProps & { onProjectSwitched?: () => void }) {
  const [mode, setMode] = useState<Mode | null>(null);
  const [title, setTitle] = useState("");
  const [pending, setPending] = useState(false);
  const inputId = useId();

  const open = (next: Mode) => {
    setTitle(next === "rename" ? (state.workspace?.title ?? "") : "");
    setMode(next);
  };

  const submit = async () => {
    const trimmed = title.trim();
    if (!mode || !trimmed || pending) return;
    setPending(true);
    // A refusal keeps the form open with what was typed; the shell has already
    // said why.
    const done = mode === "create"
      ? await actions.createWorkspace(trimmed)
      : state.workspace
        ? await actions.renameWorkspace(state.workspace, trimmed)
        : false;
    setPending(false);
    if (!done) return;
    setMode(null);
    // Creating a project also opens it; renaming the open one does not move.
    if (mode === "create") onProjectSwitched?.();
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button className="w-full justify-between gap-2" variant="ghost">
            <span className="truncate">{state.workspace?.title ?? copy.sidebar.chooseProject}</span>
            <ChevronsUpDownIcon className="size-4 shrink-0 opacity-60" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-[248px]">
          {state.workspaces.map((workspace) => (
            <DropdownMenuItem
              key={workspace.id}
              onSelect={() => { actions.selectWorkspace(workspace.id); onProjectSwitched?.(); }}
            >
              <span className="truncate">{workspace.title}</span>
            </DropdownMenuItem>
          ))}
          {state.workspaces.length > 0 ? <DropdownMenuSeparator /> : null}
          <DropdownMenuItem onSelect={() => open("create")}>{copy.sidebar.newProject}</DropdownMenuItem>
          <DropdownMenuItem disabled={!state.workspace} onSelect={() => open("rename")}>
            {copy.sidebar.renameProject}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog onOpenChange={(next) => { if (!next) { setMode(null); setPending(false); } }} open={mode !== null}>
        <DialogContent className="sm:max-w-sm">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <DialogHeader>
              <DialogTitle>{mode ? DIALOG[mode].title : ""}</DialogTitle>
              <DialogDescription className="sr-only">{copy.sidebar.projectNameHint}</DialogDescription>
            </DialogHeader>
            <div className="grid gap-2 py-4">
              <label className="text-sm font-medium" htmlFor={inputId}>{copy.sidebar.projectName}</label>
              <Input
                autoFocus
                id={inputId}
                maxLength={500}
                onChange={(event) => setTitle(event.target.value)}
                value={title}
              />
            </div>
            <DialogFooter>
              <Button onClick={() => setMode(null)} type="button" variant="ghost">{copy.sidebar.cancel}</Button>
              <Button disabled={!title.trim() || pending} type="submit">
                {mode ? DIALOG[mode].submit : ""}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
