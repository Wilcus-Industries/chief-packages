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

Read tools — search/get mail, list calendars, get events, search/get Drive files,
and get an existing shareable link — have no side effect and are **open**: the
install registers their `mcp_google-workspace_*` names on chief's gate by-name
approve set, so they auto-approve without a card. The exact read/write split is
catalogued in `scripts/tool_gate.py` (`uv run python scripts/tool_gate.py reads`).

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

Two fail-closed boundaries keep the auth verdicts themselves honest (RFC 8601
§5/§7):

- **authserv-id pinning.** Only an `Authentication-Results` header whose
  authserv-id matches *our* boundary MTA is trusted — it must match
  `google_workspace.trusted_authserv_id` (default `mx.google.com`, overridable
  per install; a blank/misconfigured id fails closed). An AR header bearing a
  *different* authserv-id — upstream, or forged on mail an attacker **sends** —
  is ignored. No matching boundary header ⇒ unverified.
- **DMARC alignment.** SPF/DKIM authenticate a *domain*, not the `From` local
  part, so an identity is trusted only when an explicit `dmarc=pass` asserts
  `From` alignment. A bare SPF/DKIM pass with DMARC absent ⇒ unverified (else a
  shared/multi-tenant domain could authenticate any address at that domain).

**Residual limitation (tracked in #14).** The authserv-id pin defends forged AR
on *delivered* mail — a message an attacker **sends** carries the wrong
authserv-id and is rejected. It does **not** defend a message written directly
into the mailbox (`messages.insert` / IMAP `APPEND` / "Import mail"): there the
attacker controls the whole raw message, including a forged
`Authentication-Results: mx.google.com; …` bearing our own authserv-id, and
nothing at delivery stripped it. The real fix is a retrieval-layer
delivery-provenance signal, filed as **#14** — not header parsing. Treat this
catalog as a **noise-reducer, not the last line of defense**: the load-bearing
control for actions on the owner's behalf is the write-gate approval card (#6),
which fires even for a catalogued sender.

Implementation: `google-workspace/scripts/sender_trust.py`
(`verified_sender`, `SenderTrustCatalog`, `resolve_sender`). The catalog is the
deterministic layer; binding its `owner_confirm` decision to the actual approval
gate is the write-gate slice's job (see below).

## Writing is gated

Every write — sending mail (`send_gmail_message`), creating/editing/deleting
calendar events (`manage_event`), and writing to Drive (`create_drive_file`,
`create_drive_folder`, the `import_to_google_*` tools, and
`get_drive_file_download_url`, which writes file bytes to local disk) — is
deliberately **left off** the gate's approve set, so **every write raises chief's
gate approval card and waits for an explicit owner "yes"**. This holds until the
owner decides otherwise; there is no silent send or mutate. Writes stay carded
even for a catalogued/verified sender — per the PRD this card is the one control
that survives a clever injection, so the sender-trust catalog and any classifier
only reduce noise around it, they never bypass it.

The posture is an **allowlist**, not a denylist: a write tool cards because it is
absent from the approved reads, so a newly-added or unrecognized tool cards by
default too — denial stays explicit. The write tool names are catalogued in
`scripts/tool_gate.py` (`uv run python scripts/tool_gate.py writes`).
TODO(#5): the heavier `screening` classification wiring layers on top of this card.

## Setup, at a glance

- Owner consent + OAuth credentials live in the `google_workspace_credentials`
  secret. TODO(#8).
- The server launches via `uvx workspace-mcp --tool-tier core --tools gmail
  drive calendar`, so only the `core`-tier Gmail / Calendar / Drive tools are
  registered (without the flags upstream exposes every service at every tier).
  TODO(#7): pin the exact `workspace-mcp` version; keep the tier/service flags.
- Version + uninstall bookkeeping. TODO(#9).
