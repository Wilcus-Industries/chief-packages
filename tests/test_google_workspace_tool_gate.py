"""Gate posture for the google-workspace MCP tools, asserted against the REAL gate.

Security-load-bearing (#6). Per the PRD the gate approval card is the one control that
survives a clever injection: the classifier and the sender-trust catalog reduce noise,
the gate is what actually *stops an action*. So this posture must be enforced by core's
real ``chief.gate.GatePolicy``, not a reimplementation of it.

The mechanism: the package's ``tool_gate`` module classifies each upstream tool read vs
write. INSTALL registers the **read** tool names on the gate's by-name approve set
(``config.yaml``'s ``gate.approved`` → ``GatePolicy.approved``) so they auto-approve.
**Write** tools are deliberately left OFF it, so every write falls to ``Decision.ASK`` —
the approval card — under chief's default-ask, empty-list posture. These tests build the
policy exactly as core's ``build_gate`` does (``approved`` = the registered read
allowlist, ``never`` empty) and exercise the real ``decide`` path.

Skips (loudly) only when no core checkout is reachable — same contract as the
manifest and core-seam tests. On a dev machine with chief core checked out they
must actually run, not skip.
"""

from __future__ import annotations

from typing import Any

import pytest

# Reached via sys.path (conftest puts google-workspace/scripts/ on it), same as the
# gw_aliases / sender_trust package scripts.
from tool_gate import (
    ALL_TOOLS,
    MCP_SERVER_NAME,
    READ_TOOLS,
    WRITE_TOOLS,
    gated_name,
    read_allowlist,
    write_tool_names,
)

_SKIP = (
    "chief core checkout not found (set $CHIEF_CORE or clone next to chief-packages) "
    "— cannot exercise the REAL chief.gate; on a dev machine with core checked out "
    "this must actually run, not skip"
)


def _real_policy() -> Any:
    """The REAL GatePolicy, built the way core's ``build_gate`` builds it at boot:

    ``approved`` = the config-registered read allowlist (``config.gate_approved``);
    ``never`` = empty — the owner's default-ask, empty-list posture. No mock: a
    regression in either the classification or the gate's decision path fails here.
    """
    from chief.gate import GatePolicy

    return GatePolicy(approved=set(read_allowlist()))


def test_split_is_disjoint_nonempty_and_covers_all() -> None:
    assert READ_TOOLS and WRITE_TOOLS
    assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
    assert ALL_TOOLS == READ_TOOLS | WRITE_TOOLS
    # The canonical actions an injection would attempt MUST be on the write side.
    assert {"send_gmail_message", "manage_event"} <= WRITE_TOOLS


def test_gated_name_matches_core_registration_convention() -> None:
    # core registers discovered MCP tools as ``mcp_<server>_<tool>``
    # (chief.mcpclient.manager) — the exact name the gate then decides on.
    assert MCP_SERVER_NAME == "google-workspace"
    assert gated_name("send_gmail_message") == (
        "mcp_google-workspace_send_gmail_message"
    )


def test_read_allowlist_is_reads_only() -> None:
    """AC1/AC2 (install side): the allowlist install registers is exactly the read
    tools' gated names — no write tool's gated name may leak onto it."""
    reads = set(read_allowlist())
    assert reads == {gated_name(t) for t in READ_TOOLS}
    assert reads.isdisjoint(write_tool_names())


@pytest.mark.parametrize("tool", sorted(READ_TOOLS))
def test_every_read_tool_auto_approves(tool: str, core_available: bool) -> None:
    """AC1: every read tool is on the by-name allowlist → the real gate approves it."""
    if not core_available:
        pytest.skip(_SKIP)
    from chief.gate import Decision

    assert _real_policy().decide(gated_name(tool)) is Decision.APPROVED, (
        f"{tool} should auto-approve (read tool on the allowlist)"
    )


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_every_write_tool_raises_a_card(tool: str, core_available: bool) -> None:
    """AC2: every write tool is NOT allowlisted → the real gate raises the card
    (Decision.ASK), never auto-approves and never silently denies."""
    if not core_available:
        pytest.skip(_SKIP)
    from chief.gate import Decision

    decision = _real_policy().decide(gated_name(tool))
    assert decision is Decision.ASK, (
        f"{tool} must raise the approval card, not {decision}"
    )


def test_omitted_tool_still_raises_a_card(core_available: bool) -> None:
    """AC4 regression: guards the PRD's default-allow hazard. Denial must stay
    explicit — a tool merely absent from the read allowlist must still ASK, and a
    fresh empty-list policy must ASK even for a would-be read."""
    if not core_available:
        pytest.skip(_SKIP)
    from chief.gate import Decision, GatePolicy

    # A plausible future tool that is on neither list must fall to the card.
    unlisted = gated_name("obliterate_all_the_things")
    assert _real_policy().decide(unlisted) is Decision.ASK

    # The empty-list, default-ask posture itself: with nothing registered, even a
    # read-shaped name asks — omission never becomes an implicit allow.
    empty = GatePolicy()
    assert empty.decide(gated_name("search_gmail_messages")) is Decision.ASK
    assert empty.decide(gated_name("send_gmail_message")) is Decision.ASK
