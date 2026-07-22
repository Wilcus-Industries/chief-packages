# Installing google-workspace

> **Stub.** This slice adds the manifest, its test harness, and the alias-helper
> install wiring (step 1 below). The rest of the document-driven install is
> stubbed with `TODO(#N)` pointers to the slices that build each step out. Do
> not treat it as complete yet.

Gmail, Calendar, and Drive for chief, via a pinned upstream Google Workspace
MCP server ([taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp),
PyPI `workspace-mcp`) launched over stdio. Reads are open; writes are gated.
Depends on the `screening` package for that gating.

1. Run `bash data/packages/google-workspace/install.sh` with the `shell` tool.
   It copies the skill and the bundled `gw_aliases.py` to
   `skills/google-workspace/scripts/` (the path the skill documents) and
   scaffolds an empty `data/google-workspace/aliases.txt` — edit that file to
   add your `name: address` account aliases.
2. Install `screening` first if it isn't already — writes route through it.
   TODO(#5): document the screening wiring for this server's write tools.
3. Owner consent + OAuth. TODO(#8): capture the owner's Google account grant and
   store the OAuth client/credentials under the `google_workspace_credentials`
   secret; record account scope in the `google_workspace` config key.
4. Tool catalog + read/write split. TODO(#4): enumerate the server's tools and
   mark which are reads vs. writes.
5. Write gate. TODO(#6): bind the write tools to a `screening` approval gate so
   sending mail / editing events / modifying Drive requires owner confirmation.
6. Pin + patch the upstream server. TODO(#7): finalize and pin the exact `uvx`
   launch command (currently the well-formed placeholder `uvx workspace-mcp`)
   and record any local patch.
7. Record it: add a `google-workspace` entry to `data/installed.yaml`.
   TODO(#9): define the version stamp written here.
8. `restart` — the done-check runs and the server goes live.
