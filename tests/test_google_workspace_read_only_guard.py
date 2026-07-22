"""Regression guard: MCP-registered tools must keep ``ToolSpec.read_only=False``.

Security-load-bearing (#6, review fix). The write-gate posture in ``tool_gate``
relies on the gate carding every write. But core's gate has a *second*
auto-approve path besides the by-name allowlist: ``chief.gate.GatedTools``
auto-approves any tool whose ``ToolSpec.read_only`` is True, even when it is
absent from ``gate.approved``. Today that path is safe for MCP tools only
because ``chief.mcpclient.manager._register_tools`` hard-codes
``read_only=False`` and never reads the tool's ``readOnlyHint`` annotation.

That is fragile: upstream (taylorwilsdon/google_workspace_mcp) annotates the
disk-writing ``get_drive_file_download_url`` with ``readOnlyHint=True``. If core
ever propagated that hint into ``ToolSpec.read_only``, that write would
auto-approve with **no card** — silently defeating the read/write split. This
test pins the dependency against the REAL core registration code: it feeds
``_register_tools`` a listing whose tools carry ``readOnlyHint=True`` (the exact
mis-annotation that would be dangerous) and asserts every resulting spec is
still ``read_only=False``. If core starts honouring the hint, this breaks — the
signal the split depends on it not doing.

Skips (loudly) only when no core checkout is reachable — same contract as the
manifest / gate / core-seam tests. On a dev machine with chief core checked out
it must actually run, not skip.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import ListToolsResult, Tool, ToolAnnotations

# The package's write catalog — the tools that MUST stay carded. Reached via
# sys.path (conftest puts google-workspace/scripts/ on it), same as the other
# google-workspace script tests.
from tool_gate import MCP_SERVER_NAME, WRITE_TOOLS, gated_name

_SKIP = (
    "chief core checkout not found (set $CHIEF_CORE or clone next to chief-packages) "
    "— cannot exercise the REAL chief.mcpclient.manager registration; on a dev "
    "machine with core checked out this must actually run, not skip"
)


def _listing_with_read_only_hints() -> ListToolsResult:
    """A tool listing shaped like upstream's: every tool annotated
    ``readOnlyHint=True``, including the disk-writing download tool. If core
    honoured the hint, these would register as ``read_only=True``."""
    tools = [
        Tool(
            name=tool,
            description=f"{tool} (annotated readOnlyHint=True by upstream)",
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        # get_drive_file_download_url is the load-bearing case: a write upstream
        # marks readOnlyHint=True. Cover the whole write catalog for good measure.
        for tool in sorted(WRITE_TOOLS)
    ]
    return ListToolsResult(tools=tools)


def test_mcp_registration_forces_read_only_false(core_available: bool) -> None:
    """The real ``_register_tools`` must not lift ``read_only`` from MCP
    annotations — every registered spec stays ``read_only=False`` even when the
    source tool is annotated ``readOnlyHint=True``."""
    if not core_available:
        pytest.skip(_SKIP)
    from chief.agent.tools import ToolRegistry
    from chief.mcpclient.manager import McpManager

    registry = ToolRegistry()
    manager = McpManager(registry)

    count = manager._register_tools(MCP_SERVER_NAME, _listing_with_read_only_hints())
    assert count == len(WRITE_TOOLS)

    specs = registry.specs()
    assert specs, "registration produced no specs"
    for spec in specs:
        assert spec.read_only is False, (
            f"{spec.name} registered read_only=True — core now propagates the MCP "
            "readOnlyHint into ToolSpec.read_only, which would auto-approve this "
            "write with no card. The write-gate posture (#6) depends on this "
            "staying False; see tool_gate.py's docstring."
        )


def test_download_write_would_not_hit_the_read_only_auto_approve(
    core_available: bool,
) -> None:
    """Tie the guard to the exact gate expression: ``GatedTools`` auto-approves
    the set ``{spec.name for spec in specs if spec.read_only}``. The disk-writing
    download tool's gated name must NOT land in that set, so its only path to run
    is the card."""
    if not core_available:
        pytest.skip(_SKIP)
    from chief.agent.tools import ToolRegistry
    from chief.mcpclient.manager import McpManager

    registry = ToolRegistry()
    McpManager(registry)._register_tools(
        MCP_SERVER_NAME, _listing_with_read_only_hints()
    )

    # Mirrors chief.gate.GatedTools._read_only exactly.
    read_only_names: set[Any] = {
        spec.name for spec in registry.specs() if spec.read_only
    }
    assert read_only_names == set(), (
        "no MCP-registered spec may be read_only; the gate's read_only path would "
        "otherwise auto-approve it"
    )
    download = gated_name("get_drive_file_download_url")
    assert download not in read_only_names
