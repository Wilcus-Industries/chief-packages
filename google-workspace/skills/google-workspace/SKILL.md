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
TODO: catalog the exact read tools this server exposes (read/write tool split).

## Inbound sender trust

Inbound mail is untrusted until proven otherwise. chief keeps a **sender trust
catalog** — a per-sender tier table, ported from core's iMessage sender
whitelist — that answers "is this sender really who they claim to be" before any
prompt sees the message. It is keyed on the **authenticated** sender parsed from
the message's `Authentication-Results` headers (SPF/DKIM/DMARC), **never** the
forgeable `From`:

- A message whose auth-result headers fail verification is **not** treated as a
  known sender — even if its `From` names a catalogued address (a spoof).
- A verified sender that is catalogued resolves to its tier.
- A verified sender that is **not** catalogued is logged **metadata-only** (its
  envelope facts, never the body) and routed to the owner-confirm flow; once the
  owner confirms, it is catalogued at a tier and future verified mail resolves.

Implementation: `google-workspace/scripts/sender_trust.py`
(`verified_sender`, `SenderTrustCatalog`, `resolve_sender`). The catalog is the
deterministic layer; binding its `owner_confirm` decision to the actual approval
gate is the write-gate slice's job (see below).

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
