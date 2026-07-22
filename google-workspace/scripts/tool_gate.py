#!/usr/bin/env python3
"""Tool catalog + read/write split for the google-workspace MCP server.

This is the security-load-bearing manifest for slice #6. The upstream server
(taylorwilsdon/google_workspace_mcp) is launched at the ``core`` tool tier
restricted to Gmail / Calendar / Drive — sixteen tools. Each is classified
**read** (safe to auto-approve) or **write** (must ask). chief core registers a
discovered MCP tool under the qualified name ``mcp_<server>_<tool>`` (see
``chief.mcpclient.manager``), so with this package's server name
``google-workspace`` a tool ``send_gmail_message`` reaches the gate as
``mcp_google-workspace_send_gmail_message`` — the name ``GatePolicy.decide``
acts on.

**The posture (what install wires):** the read tools' gated names are registered
on the gate's by-name approve set — ``config.yaml``'s ``gate.approved`` list,
which core folds into ``GatePolicy.approved`` at boot (``chief.wiring.build_gate``).
Those auto-approve. The write tools are deliberately **left off** that set, so
each falls to ``GatePolicy.decide`` → ``Decision.ASK`` — the approval card — under
chief's default-ask, empty-list posture. Per the PRD this card is the one control
that survives a clever injection: the classifier and the sender-trust catalog
only reduce noise; the gate is what stops an action.

**Denial stays explicit (do not regress).** The posture must never flip to
default-allow. A write tool is gated *because it is absent from an allowlist*, not
because it is on a denylist — so a newly-added or mis-catalogued tool that nobody
allowlisted still raises the card by default. ``tool_gate`` therefore only ever
grows the **read** allowlist; it never enumerates writes into ``gate.approved``.

Classification rule: a tool is a **read** only if it neither mutates remote
Google state, nor sends/shares anything outward, nor writes bytes to local disk.
Anything with a side effect is a **write** (card). Notable calls:

- ``get_drive_shareable_link`` — READ. Returns the *existing* webViewLink and
  current sharing status via ``files().get``; it does not change sharing (that is
  ``manage_drive_access`` / ``set_drive_file_permissions``, extended tier, not in
  the core set).
- ``get_drive_file_download_url`` — WRITE. In stdio mode (this package's launch
  mode) it *downloads the file and saves it to local disk*, returning the path.
  That local-filesystem side effect — attacker-controllable bytes landing on
  disk — is why it does not auto-approve, despite the read-shaped name. Fail-safe:
  when a tool's effect is ambiguous, it cards.

Stdlib-only and free of any ``chief`` import on purpose: this module ships as a
package script (run in the sandbox, imported bare in tests) and must load without
core installed. Building the actual ``GatePolicy`` from this data is core's job at
boot; the tests exercise that real gate directly.
"""

from __future__ import annotations

import sys

#: The manifest's ``mcp_servers`` key — the ``<server>`` in ``mcp_<server>_<tool>``.
MCP_SERVER_NAME = "google-workspace"

# --------------------------------------------------------------------------- #
# The core-tier Gmail / Calendar / Drive tools, split read vs write.          #
# Bare upstream tool names (from the server's core tool tier); the gated       #
# ``mcp_google-workspace_*`` names are derived below.                          #
# --------------------------------------------------------------------------- #

#: Reads — no remote mutation, no send/share, no local write. Auto-approved.
READ_TOOLS: frozenset[str] = frozenset(
    {
        # Gmail
        "search_gmail_messages",
        "get_gmail_message_content",
        "get_gmail_messages_content_batch",
        # Calendar
        "list_calendars",
        "get_events",
        # Drive
        "search_drive_files",
        "get_drive_file_content",
        "get_drive_shareable_link",
    }
)

#: Writes — send, create/modify, or a local-disk side effect. Always carded;
#: deliberately NOT registered on the gate's approve set.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # Gmail
        "send_gmail_message",
        # Calendar (create / update / delete events)
        "manage_event",
        # Drive
        "create_drive_file",
        "create_drive_folder",
        "import_to_google_doc",
        "import_to_google_slides",
        "import_to_google_sheets",
        "get_drive_file_download_url",  # downloads to local disk in stdio mode
    }
)

#: Every core-tier google-workspace tool this package governs.
ALL_TOOLS: frozenset[str] = READ_TOOLS | WRITE_TOOLS


def gated_name(tool: str) -> str:
    """The qualified name core registers this tool under: ``mcp_<server>_<tool>``.

    Matches ``chief.mcpclient.manager`` exactly, so the returned string is the one
    ``GatePolicy.decide`` receives at runtime.
    """
    return f"mcp_{MCP_SERVER_NAME}_{tool}"


def read_allowlist() -> list[str]:
    """The gated read tool names install registers on ``gate.approved`` (sorted).

    This is the by-name read allowlist: exactly these auto-approve. Writes are
    absent by construction, so they card.
    """
    return sorted(gated_name(t) for t in READ_TOOLS)


def write_tool_names() -> list[str]:
    """The gated write tool names — the ones that MUST stay off the approve set."""
    return sorted(gated_name(t) for t in WRITE_TOOLS)


def _approved_yaml() -> str:
    """A ready-to-merge ``config.yaml`` block registering the read allowlist.

    Paste under the top-level ``gate:`` key (merge with any existing
    ``approved:`` entries). Writes are intentionally omitted — that omission is
    the posture.
    """
    lines = ["gate:", "  approved:"]
    lines += [f"    - {name}" for name in read_allowlist()]
    return "\n".join(lines)


_USAGE = (
    "usage: tool_gate.py {reads|writes|all|approved-yaml}\n"
    "  reads         gated read tool names to register on gate.approved\n"
    "  writes        gated write tool names (left off gate.approved — they card)\n"
    "  all           every governed tool name\n"
    "  approved-yaml a config.yaml gate.approved block for the reads"
)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(_USAGE, file=sys.stderr)
        return 2
    command = argv[0]
    if command == "reads":
        print("\n".join(read_allowlist()))
    elif command == "writes":
        print("\n".join(write_tool_names()))
    elif command == "all":
        print("\n".join(sorted(gated_name(t) for t in ALL_TOOLS)))
    elif command == "approved-yaml":
        print(_approved_yaml())
    else:
        print(_USAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
