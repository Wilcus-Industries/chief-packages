---
name: google-workspace
description: Gmail, Calendar, and Drive via a pinned Google Workspace MCP server — reads are open, writes go through the screening gate. Stub; built out over slices #4-#9.
---

# Google Workspace

> **Stub.** High-level shape only; the full skill lands across slices #4-#9. Each
> section names the slice that fills it in.

Gmail, Calendar, and Drive access for chief, backed by a pinned upstream Google
Workspace MCP server ([taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp))
run over stdio. Per-account: the owner grants one Google account and its scope is
recorded in the `google_workspace` config key.

## Reading

Read tools (list/search mail, read events, list Drive files) are open — no gate.
TODO(#4): catalog the exact read tools this server exposes.

## Writing is gated

Every write — sending mail, creating/editing calendar events, modifying Drive —
routes through the `screening` package for explicit owner approval. Never send or
mutate on the owner's behalf without that confirmation.
TODO(#5): screening wiring. TODO(#6): the write-tool → gate binding.

## Setup, at a glance

- Owner consent + OAuth credentials live in the `google_workspace_credentials`
  secret. TODO(#8).
- The server launches via a pinned `uvx` command. TODO(#7): the finalized,
  pinned launch command (placeholder today: `uvx workspace-mcp`).
- Version + uninstall bookkeeping. TODO(#9).
