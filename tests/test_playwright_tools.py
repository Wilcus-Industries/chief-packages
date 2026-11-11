"""The playwright package's tool catalog and config wiring.

The catalog splits every tool of the PINNED ``@playwright/mcp`` release into
read (auto-approve), write (approval card), and never (refused outright). The
gate posture is asserted against core's REAL ``chief.gate.GatePolicy``, built
the way ``build_gate`` builds it from ``config.yaml``. ``wire`` is the one
config writer install runs; it must union into the owner's existing lists,
never clobber them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# Reached via sys.path (conftest puts playwright/scripts/ on it).
from playwright_tools import (
    ALL_TOOLS,
    MCP_SERVER_NAME,
    NEVER_TOOLS,
    PIN,
    PINNED_TOOLS,
    READ_TOOLS,
    SCREEN_PATTERN,
    WRITE_TOOLS,
    gated_name,
    server_entry,
    wire,
)

_SKIP = (
    "chief core checkout not found (set $CHIEF_CORE or clone next to "
    "chief-packages) — cannot exercise the REAL chief.gate"
)


def test_split_is_disjoint_and_covers_exactly_the_pinned_release() -> None:
    assert READ_TOOLS.isdisjoint(WRITE_TOOLS)
    assert READ_TOOLS.isdisjoint(NEVER_TOOLS)
    assert WRITE_TOOLS.isdisjoint(NEVER_TOOLS)
    # Bumping PIN without reclassifying the new release's tools fails here.
    assert ALL_TOOLS == PINNED_TOOLS


def test_actions_card_and_host_code_execution_is_refused() -> None:
    assert {"browser_click", "browser_type", "browser_fill_form"} <= WRITE_TOOLS
    # file_upload sends local bytes to a site; evaluate runs page JS.
    assert {"browser_file_upload", "browser_evaluate"} <= WRITE_TOOLS
    # run_code_unsafe runs arbitrary code in the server's node process.
    assert NEVER_TOOLS == {"browser_run_code_unsafe"}


def test_gated_name_matches_core_registration_convention() -> None:
    assert MCP_SERVER_NAME == "playwright"
    assert gated_name("browser_click") == "mcp_playwright_browser_click"
    assert SCREEN_PATTERN == "mcp_playwright_*"


def test_real_gate_posture(core_available: bool) -> None:
    if not core_available:
        pytest.skip(_SKIP)
    from chief.gate import Decision, GatePolicy

    root = Path("/r")
    config = wire({}, root, node_bin=Path("/n/bin"))
    gate = config["gate"]
    policy = GatePolicy(never=frozenset(gate["never"]), approved=set(gate["approved"]))
    for tool in READ_TOOLS:
        assert policy.decide(gated_name(tool)) is Decision.APPROVED
    for tool in WRITE_TOOLS:
        assert policy.decide(gated_name(tool)) is Decision.ASK
    for tool in NEVER_TOOLS:
        assert policy.decide(gated_name(tool)) is Decision.NEVER


def test_server_entry_is_absolute_pinned_and_confined() -> None:
    root = Path("/home/o/chief")
    entry = server_entry(root, node_bin=Path("/opt/homebrew/bin"))
    cmd = entry["command"]
    base = "/home/o/chief/data/playwright"
    assert cmd[0] == f"{base}/server/node_modules/.bin/playwright-mcp"
    assert cmd[cmd.index("--user-data-dir") + 1] == f"{base}/profile"
    assert cmd[cmd.index("--output-dir") + 1] == f"{base}/output"
    assert cmd[cmd.index("--browser") + 1] == "chrome"
    assert "--headless" in cmd
    assert "--allow-unrestricted-file-access" not in cmd
    # File access is confined to the server's cwd, never the chief repo root.
    assert entry["cwd"] == f"{base}/workspace"
    assert entry["env"]["PATH"].split(":")[0] == "/opt/homebrew/bin"
    assert entry["timeout"] > 0
    assert PIN.count(".") == 2


def test_wire_unions_into_existing_lists_and_is_idempotent() -> None:
    existing: dict[str, Any] = {
        "gate": {"never": ["x_never"], "approved": ["notify_owner"], "announce": True},
        "typesafe": {"screen_tools": ["mcp_google-workspace_*"]},
        "mcp_servers": {"google-workspace": {"command": ["uv"]}},
    }
    once = wire(existing, Path("/r"), node_bin=Path("/n"))
    twice = wire(once, Path("/r"), node_bin=Path("/n"))
    assert once == twice
    assert "notify_owner" in once["gate"]["approved"]
    assert "x_never" in once["gate"]["never"]
    assert once["gate"]["announce"] is True
    assert once["typesafe"]["screen_tools"] == [
        "mcp_google-workspace_*",
        SCREEN_PATTERN,
    ]
    assert set(once["mcp_servers"]) == {"google-workspace", "playwright"}
    assert len(once["gate"]["approved"]) == len(set(once["gate"]["approved"]))
    # The input is not mutated.
    assert existing["gate"]["approved"] == ["notify_owner"]


def test_wire_leaves_default_screen_tools_alone() -> None:
    # No typesafe.screen_tools key = core's default, which already screens mcp_*.
    # Writing a list there would REPLACE that default and narrow screening.
    config = wire({}, Path("/r"), node_bin=Path("/n"))
    assert "typesafe" not in config


def test_wire_cli_rewrites_config_yaml(tmp_path: Path) -> None:
    from playwright_tools import main

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"budget": {"cap_usd": 20}}))
    assert main(["wire", str(tmp_path), "/n/bin"]) == 0
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["budget"] == {"cap_usd": 20}
    assert "playwright" in written["mcp_servers"]
