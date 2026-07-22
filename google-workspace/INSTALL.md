# Installing google-workspace

> **Stub.** This slice adds the manifest, its test harness, the alias-helper
> install wiring (step 1), and the gate posture (steps 4–5: the read/write tool
> split and the write-gate registration). The remaining steps are stubbed with
> `TODO(#N)` pointers to the slices that build them out. Do not treat the OAuth,
> server-pin, or bookkeeping steps as complete yet.

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
   `openai/gpt-4.1-nano` — the value must be a plain model id, characters
   outside `[A-Za-z0-9._:/-]` are rejected).
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
4. Tool catalog + read/write split. The manifest command launches the server
   with `--tool-tier core --tools gmail drive calendar`, so it registers only
   the `core`-tier Gmail / Calendar / Drive tools — sixteen, each classified
   read vs. write in `skills/google-workspace/scripts/tool_gate.py`. (Without
   those flags upstream registers every service at every tier — ~60 tools — and
   the split below would not match what the server exposes.) **Reads**
   (search/list/get mail, events, and Drive files; the existing shareable link)
   have no side effect and auto-approve. **Writes** send mail, create/modify
   calendar events, or write to Drive/local disk — including
   `get_drive_file_download_url`, which lands file bytes on local disk in stdio
   mode — and always raise the gate's approval card (step 5). List the sets:
   ```
   TG=skills/google-workspace/scripts/tool_gate.py
   uv run python $TG reads          # read tool names to register (step 5)
   uv run python $TG writes         # write tool names — left OFF, they card
   ```
   Inbound sender trust (#4): the sender trust catalog
   (`google-workspace/scripts/sender_trust.py`) keys inbound mail on the
   verified auth-result sender — see the skill's "Inbound sender trust" section.
   Unknown verified senders route to owner-confirm; the write gate below is the
   control that still fires even for a catalogued sender.
5. Write gate — the read-by-name registration (security-load-bearing). Register
   the **read** tool names on chief's gate by-name approve set so they
   auto-approve, and leave every **write** tool OFF it so it raises the approval
   card. The approve set is `config.yaml`'s `gate.approved` list, which core
   folds into the live `GatePolicy` at boot. Add the read names to it:
   ```
   uv run python skills/google-workspace/scripts/tool_gate.py approved-yaml
   ```
   and merge that `gate.approved` block into `config.yaml` (keep any existing
   entries), then `restart`. **Write tools are deliberately never added.** Under
   chief's default-ask, empty-list posture a tool that is not on the approve set
   raises the card every time — so sending mail, creating/modifying events, and
   Drive/download writes all require an explicit owner "yes" until the owner
   decides otherwise. This is intentional and load-bearing: per the PRD the gate
   card is the one control that survives a clever injection. Do **not** add a
   write tool to `gate.approved` or replace this allowlist posture with a
   denylist — denial must stay explicit (a tool that nobody allowlisted must
   still card). **Never add a `"*"` entry to `gate.approved`**: core's
   `GatePolicy.decide` treats `"*"` as approve-all, so a single wildcard silently
   auto-approves every write and defeats the entire card posture. The heavier
   `screening` classification wiring is a separate concern (TODO(#5)); it layers
   on top of — it does not replace — this card.
6. Pin + patch the upstream server. The command already carries the tool-tier /
   service restriction (`uvx workspace-mcp --tool-tier core --tools gmail drive
   calendar`); TODO(#7): pin the exact `workspace-mcp` version and record any
   local patch. Do **not** drop the `--tool-tier` / `--tools` flags when pinning
   — they are what makes the read/write split above real.
7. Record it: add a `google-workspace` entry to `data/installed.yaml`.
   TODO(#9): define the version stamp written here.
8. `restart` — the done-check runs and the server goes live.
