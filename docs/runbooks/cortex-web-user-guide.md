# Cortex Web user guide

What the Web surface offers, which object each control acts on, and what a
research turn does and does not change. It describes the application, not any
particular installation: the address, the workspaces, the catalog contents and
the configured model are yours.

Open the address your installation serves. A local development server binds
`http://127.0.0.1:3000`; an installed generation serves the loopback port its
launcher configures, optionally behind the private-access path described in
`deployment/private_access/README.md`. There is no public entry point.

## What this release does

Research holds the records this installation has imported: Ideas, Explorations
and Projects, each with the dossiers that were adopted as versioned documents.
You can inspect their recorded history and discuss a selected item's documents
with the configured model.

This release does not resume an autonomous research engine. A discussion saves
messages and Outputs in a linked Cortex thread; it does not increment a legacy
round counter, change a legacy status, rewrite the original dossier, create an
exploration angle or launch an experiment. Documents are retained versions, not
a live synchronization with their original files.

## Find the right surface

| UI label | Use |
|---|---|
| Project selector / New project… | Choose or create a workspace that contains conversations. This is separate from a legacy research project. |
| New thread | Start a normal conversation in that workspace. For an existing catalog item, use its Open research conversation button instead. |
| Research → Ideas | Inspect existing specific hypotheses and their recorded progress and stop reasons. |
| Research → Explorations | Inspect broader research directions and recorded exploration history. Exploration does not mean a runnable experiment. |
| Research → Projects | Inspect legacy research project records and their dossiers. |
| Library | Browse adopted paper and source records and their available content. |
| Inbox | Review captures and pending decisions. It is not an automatic resume queue. |
| Status | Inspect availability and technical service state. |
| Runs | Inspect execution history for a conversation. |
| Outputs | Read saved research artifacts for the selected run. |

On a narrow screen, use Open navigation to reach the sidebar. The Research
detail sits below the list on small screens and beside it on wide screens.
Search this page… filters only the currently loaded page. Clear it, select Any
status and use Previous/Next when an expected item is not visible.

## First guided operation: continue one existing item

1. Open the Research view (`?view=research` on your installation's address).
2. In the project selector, choose a workspace for the conversation, or create
   one with New project… → enter a name → Create. Creating a workspace does not
   create a new item in the research catalog.
3. Select Ideas, set Any status, and pick the item you want to revisit. Search
   accepts keywords from the item's title.
4. Read Summary, Stopped because, Documents and History. In Documents, Preview
   renders Markdown and math; Source shows the Markdown and LaTeX. Rounds and
   status describe the preserved research state, not the number of new chat
   messages.
5. Click Open research conversation. Cortex creates or reuses an item-associated
   conversation. No model runs merely because you opened it. If the button is
   disabled, check the displayed reason and whether a workspace is selected.
6. Send a question with the explicit prefix, regardless of the current
   Chat/Research selection:

   ```text
   /research From the retained material for this item, state the core
   hypothesis, the work already done, why it stopped, and the single question
   most worth testing now. Separate existing evidence from proposals and cite
   the material.
   ```

7. Watch the run status (normally Working… while executing). After the answer,
   open Outputs. Read Preview; switch to Source to inspect the Markdown and
   LaTeX. Source provenance holds the technical evidence record. `[D1]` labels
   identify dossier excerpts; `[S1]` labels identify retrieved paper evidence.
   These labels do not establish that the research claims are true.
8. In the same conversation, send a follow-up without another command:

   ```text
   Using the same material, propose a minimal ablation: baseline, the single
   variable that changes, data and metrics, expected signal and the result that
   would falsify it. Produce a plan only; do not claim it has been run.
   ```

9. Return through the sidebar thread or the item's Open research conversation
   button in the same workspace. Check that messages and saved Outputs remain.
   The item should still have the same status and Rounds: this operation is
   manual research continuation, not a legacy incubation round.

A conversation opened earlier may already contain questions and results. That is
expected when the button reuses it. Different workspaces can hold different
conversations associated with the same item.

## Continue an exploration or project

Use the same steps, selecting Explorations or Projects before opening the item.
For an exploration, a useful first question is:

```text
/research From this exploration's retained material, summarize the directions
already explored, the routes ruled out and the open questions. Propose two
directions worth comparing further, with the basis for each and the missing
information. Cite the material.
```

Then follow up: `Choose one of those two directions and state the evidence and
decision criteria the next step needs.` For a project, ask for its current
milestones, blockers and next experiment plan.

These produce saved proposals; they do not restart an exploration worker or
execute an experiment. There is no New idea / New exploration engine-creation
flow. A new normal thread can develop a new concept, but it does not register
that concept as a catalog item.

## Research mode and commands

| Action | Behavior |
|---|---|
| `/research <question>` | Enter research mode or refresh the evidence selection for this question. Use a nonempty question. |
| Plain follow-up in research mode | Continue with the retained evidence packet while the selected item is unchanged. |
| `/chat <message>` | Leave research mode for this turn and subsequent ordinary conversation. Historical messages remain. |
| Composer Research / Chat buttons | Set the mode for the next submitted message; when switching, the UI adds the matching command. An explicitly typed command takes priority. |

Research mode retrieves bounded excerpts from the adopted library and from the
explicitly selected item's retained dossier, supplies those excerpts to the
configured model, and saves the response as an artifact. It does not mean
automatic web search, paper ingestion, full-file reading, engine advancement or
scheduling. Without an associated research item the question uses adopted-library
evidence only; mentioning an item's title in a generic chat does not select its
dossier.

Use `/research` again when the question changes enough to need new evidence. For
a follow-up that should reuse the same evidence, plain text is sufficient.
Selecting Research while already in that mode does not itself refresh evidence.
If no usable evidence is found, try exact paper or title keywords, or verify
Documents on the selected item. Missing retrieval is not proof that no relevant
work exists. Non-English title matching is limited.

## Read and manage results

Preview is the readable Markdown and math view. Source shows the Markdown and
LaTeX; Copy source copies the shown document, including its appendix where
present. Escape a literal currency dollar as `\$` in math-enabled Markdown.
Original dossier previews may omit private details and say so visibly. A saved
Output is a new artifact, not an overwrite of the original dossier.

Runs selects execution history; Outputs shows the selected run's artifacts.
Thread rename and archive organize conversations, not an item's research state.
If execution fails or dispatch is off, inspect the visible status before
retrying; a saved user message alone is not a completed research run.

## Telegram counterpart

If the installation has a Telegram transport configured, expand Telegram command
in the item's detail and use Copy command. Send the displayed
`/research-item <id>` in the bound bot chat, then `/research <question>`. This
selects the same item for the bot's bound conversation; it does not move the bot
into an arbitrary Web thread. Follow up normally and use `/open` to return to
that conversation on the Web.

Successful Web research does not prove Telegram delivery, and the absence of a
typing indicator does not prove that a run was not received. Keep receive, run
and delivery acceptance separate.

## What a later milestone must add

Actual engine continuation needs an explicit one-step action with eligible item
state, a visible budget and a stop condition, followed by persistent engine
results and updated item history. Exploration continuation and idea incubation
need their own semantics. Only after one-step execution is accepted should
recurring automation be offered.
