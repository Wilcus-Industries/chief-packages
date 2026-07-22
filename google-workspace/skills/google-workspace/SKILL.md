---
name: google-workspace
description: Gmail, Calendar, and Drive via a pinned Google Workspace MCP server — reads are open, writes go through the screening gate. Stub; built out over slices #4-#9.
---

# Google Workspace

> **Stub.** High-level shape only; the full skill lands across slices #4-#9. Each
> section names the slice that fills it in.

Gmail, Calendar, and Drive access for chief, backed by a pinned upstream Google
Workspace MCP server ([taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp))
run over stdio. Multiple Google accounts (e.g. personal + work) coexist in one
process, credentials keyed per address — see **Accounts** below.

## Accounts: every tool call names one, explicitly

There is **no default account**. Every Google Workspace tool call must pass an
explicit `account` argument — the full email address the call acts as. The
upstream server *can* fall back to a default account; this package leaves that
fallback unset on purpose, so an omitted `account` is an error, not a silent
pick of whichever account happens to be first.

The owner keeps friendly names in an **alias file** (`name: address` per line,
`#` comments allowed):

```
work: me@company.com
personal: me@gmail.com
```

Resolve a friendly name to its address *before* the call with the bundled,
stdlib-only helper, run through the **shell** tool from the run root:

```
GW=skills/google-workspace/scripts/gw_aliases.py
uv run python $GW resolve work        # -> me@company.com  (exit 0)
uv run python $GW list                # print the alias file, canonical form
```

Pass the resolved address as `account`. The helper — and this skill — hold the
line rather than guessing:

- **Unknown** name → the helper exits non-zero; don't invent an address.
- **Ambiguous** name (matches more than one account) → the helper errors
  instead of guessing. **Ask the owner** which account they mean, then retry.
- **Omitted** account → never default to one. If the owner didn't say which
  account, ask before calling any tool.

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
