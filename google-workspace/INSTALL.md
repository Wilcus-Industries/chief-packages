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
   `skills/google-workspace/scripts/` (the path the skill documents), scaffolds
   an empty `data/google-workspace/aliases.txt` — edit that file to add your
   `name: address` account aliases — and seeds the inbound-Gmail screening
   classifier into `classifiers/gmail-inbound-screen.md` (never clobbered; pass
   `GATE_MODEL=<model-id>` to pin a guard model other than the default
   `openai/gpt-4.1-nano`).
2. Install `screening` first if it isn't already — writes route through it, and
   inbound Gmail content is screened before the model reads it. That screen is
   the manifest's `hooks:` block (`scripts/gmail_screen.py`): a `post_tool` hook
   that runs each Gmail content read through the seeded classifier at chief's
   `Annotate` seam — flagged mail is fenced with an untrusted marker (never
   dropped), a screener error fails safe to clean. It loads automatically once
   the package is recorded in `data/installed.yaml` (step 7). See the skill's
   "Screening inbound mail before the model reads it" section.
3. Owner consent + OAuth. TODO(#8): capture the owner's Google account grant and
   store the OAuth client/credentials under the `google_workspace_credentials`
   secret; record account scope in the `google_workspace` config key.
4. Tool catalog + read/write split. TODO: enumerate the server's tools and
   mark which are reads vs. writes.
   Inbound sender trust (#4): the sender trust catalog
   (`google-workspace/scripts/sender_trust.py`) keys inbound mail on the
   verified auth-result sender — see the skill's "Inbound sender trust" section.
   Unknown verified senders route to owner-confirm; the gate binding is #6.
5. Write gate. TODO(#6): bind the write tools to a `screening` approval gate so
   sending mail / editing events / modifying Drive requires owner confirmation.
6. Pin + patch the upstream server. TODO(#7): finalize and pin the exact `uvx`
   launch command (currently the well-formed placeholder `uvx workspace-mcp`)
   and record any local patch.
7. Record it: add a `google-workspace` entry to `data/installed.yaml`.
   TODO(#9): define the version stamp written here.
8. `restart` — the done-check runs and the server goes live.
