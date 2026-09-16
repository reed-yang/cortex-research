# Managed terminal and file access

The model's tools must agree with the worker's OS boundary. Configure:

```toml
[runtime]
tools = "local"
```

The default is `none`: session history remains available, while file and terminal
tools are omitted. Local mode requires a newly qualified worker with tool-policy
v1. The previous gen9 payload cannot supply the child-environment protection and
is refused before a local-tool turn. Deploy product and worker candidates through
their existing lifecycle, preserving native session bindings and predecessors.
Do not patch an active slot or disable its sandbox to activate this feature.

## Allowed work

Terminal and file tools start in `<worker-state>/workspace`. The slot interpreter
is placed first on PATH. Commands can execute from `/bin`, `/usr/bin` and the slot
interpreter's directory. Enabled research-corpus, research-documents and
research-artifacts registrations, plus `readings.papers_root`, supply explicit
read-only roots. The prompt gives the model those roots rather than asking it
to discover the operator's home. Child processes inherit the same filesystem
and egress policy; cancellation can signal the worker's children.

File tools can read and search the configured libraries and write outputs in
the private workspace. This does not grant arbitrary host-home access or writes
to paper libraries. Model/provider/Telegram/protocol credentials are not copied
to subprocess environments. Hermes auth.json and the sealed sandbox evidence
remain unreadable; plugins, hooks and managed configuration remain protected.
Runtime write authority otherwise remains the existing worker-state subtree.

## Permission failures

A denied command working directory or executable returns exit 126 without
Hermes' transient-error retries. A command that starts but attempts a denied
file access reports its own nonzero exit status. Neither result means every
directory is unreadable. Changing a registered root is observed on the next
worker acquisition, which closes the old process and creates a new policy.
Cancel active work before intentionally changing roots.

Hermes' command-approval flow remains active. Tool availability does not grant
standing approval for dangerous commands. Ancillary loaders that try to create
protected hooks/configuration can still report denials and use their documented
defaults; this is separate from successful file/terminal execution.

## Verification

Run the focused real-process suite. To also exercise the supplied Hermes fork,
point `CORTEX_TEST_HERMES_DESCRIPTOR` at an existing attested descriptor. The test
reads its interpreter/content identities and creates independent temporary
state; it never dispatches a production turn or sends a Telegram message.

```sh
CORTEX_TEST_HERMES_DESCRIPTOR=/absolute/path/to/descriptor.json \
  .venv/bin/python -m pytest -q tests/product/runtime_update/test_local_tools.py
```

Acceptance must verify commands, file reads/search/writes in the workspace,
read-only libraries, symlink-escape refusal, subprocess credential filtering,
cancellation, old-worker refusal and existing-session continuation. Passing a
source test is not an installed-generation or real-provider acceptance.
