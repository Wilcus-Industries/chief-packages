#!/usr/bin/env python3
"""Tool catalog + config wiring for the playwright MCP server.

**The catalog.** Every tool the PINNED ``@playwright/mcp`` release exposes is
classified once, here. chief core registers a discovered MCP tool as
``mcp_<server>_<tool>`` (``chief.mcpclient.manager``), so with server name
``playwright`` the gate sees e.g. ``mcp_playwright_browser_click``.

- **read** — looking: navigate, snapshot, screenshot, console/network logs,
  tab and viewport housekeeping. Registered on ``gate.approved`` so they run
  without a card.
- **write** — acting *as the owner* in a browser that may be logged in:
  clicking, typing, submitting, uploading local files, running page JS. Left
  OFF ``gate.approved`` so each one raises the approval card (the owner can
  answer "always" per tool).
- **never** — ``browser_run_code_unsafe`` runs arbitrary code inside the
  server's own node process: host code execution, not browsing. Put on
  ``gate.never``.

Denial stays allowlist-shaped: a tool a future release adds that nobody
classified is on neither list, so it cards. ``PINNED_TOOLS`` is the pinned
release's exact tool list; the tests fail if a ``PIN`` bump is not
reclassified.

Navigation is a read on purpose (the owner asked that browsing stay free), but
note it is not side-effect free: a GET can carry data out in its query string,
which is why everything the browser returns is screened.

**The wiring.** ``wire`` is the one writer install runs against
``config.yaml``. ``chief.config_apply`` replaces lists wholesale, which would
clobber the owner's ``gate.approved``; this unions instead.

Stdlib-only at import (``yaml`` is imported by the CLI path alone) and free of
any ``chief`` import: it runs from the installed skill dir and is imported bare
by the tests.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MCP_SERVER_NAME = "playwright"
PIN = "0.0.85"
SCREEN_PATTERN = f"mcp_{MCP_SERVER_NAME}_*"
CONNECT_TIMEOUT_SECONDS = 90
# The one server (scripts/browser_host.py) owns the shared Chrome, one
# Playwright child per chief session, and the phone login viewer.
LOGIN_LINK_TOOL = f"mcp_{MCP_SERVER_NAME}_login_link"
# The pre-per-session layout ran a second server; wire removes its traces.
OLD_LOGIN_SERVER = "browser-login"
OLD_LOGIN_LINK_TOOL = f"mcp_{OLD_LOGIN_SERVER}_login_link"
CDP_PORT = 9322
VIEWER_PORT = 8131


@dataclass(frozen=True)
class Host:
    """Machine facts install resolves once: node's dir (the server is a node
    script), chief's venv python (runs browser_host.py), and the public base
    URL login links are minted under (the tailnet ``/browser`` mount)."""

    node_bin: Path
    python: Path
    public_base: str


READ_TOOLS: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_find",
        "browser_wait_for",
        "browser_console_messages",
        "browser_network_requests",
        "browser_network_request",
        "browser_tabs",
        "browser_resize",
        "browser_emulate_media",
        "browser_hover",
        "browser_close",
    }
)

WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_press_key",
        "browser_select_option",
        "browser_drag",
        "browser_drop",
        "browser_handle_dialog",
        "browser_file_upload",
        "browser_evaluate",
    }
)

NEVER_TOOLS: frozenset[str] = frozenset({"browser_run_code_unsafe"})

ALL_TOOLS: frozenset[str] = READ_TOOLS | WRITE_TOOLS | NEVER_TOOLS

# `tools/list` of @playwright/mcp@PIN (default caps), recorded at pin time.
PINNED_TOOLS: frozenset[str] = frozenset(
    {
        "browser_click",
        "browser_close",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_emulate_media",
        "browser_evaluate",
        "browser_file_upload",
        "browser_fill_form",
        "browser_find",
        "browser_handle_dialog",
        "browser_hover",
        "browser_navigate",
        "browser_navigate_back",
        "browser_network_request",
        "browser_network_requests",
        "browser_press_key",
        "browser_resize",
        "browser_run_code_unsafe",
        "browser_select_option",
        "browser_snapshot",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_type",
        "browser_wait_for",
    }
)


def gated_name(tool: str) -> str:
    """The name core registers ``tool`` under — the one the gate decides on."""
    return f"mcp_{MCP_SERVER_NAME}_{tool}"


def _gated(tools: frozenset[str]) -> list[str]:
    return sorted(gated_name(t) for t in tools)


def data_dir(root: Path) -> Path:
    return root / "data" / MCP_SERVER_NAME


def server_entry(root: Path, host: Host) -> dict[str, Any]:
    """The ``mcp_servers.playwright`` entry, with absolute paths under ``root``.

    chief's venv python runs the installed ``browser_host.py``. It keeps one
    Chrome on the persistent chief-only profile (one set of logins, shared by
    every session), gives each chief session its own Playwright child and tab
    (``session_context`` tells it which session is calling), and serves the
    phone login viewer. Children run in ``WORKSPACE`` — they confine file
    access to their cwd, which must not be the chief repo — with node's dir
    leading ``PATH`` because the Playwright server is a node script.
    """
    base = data_dir(root)
    return {
        "command": [
            str(host.python),
            str(root / "skills" / "playwright" / "scripts" / "browser_host.py"),
        ],
        "session_context": True,
        "env": {
            "PLAYWRIGHT_MCP": str(
                base / "server" / "node_modules" / ".bin" / "playwright-mcp"
            ),
            "BROWSER_PROFILE": str(base / "profile"),
            "OUTPUT_DIR": str(base / "output"),
            "WORKSPACE": str(base / "workspace"),
            "CDP_PORT": str(CDP_PORT),
            "VIEWER_PORT": str(VIEWER_PORT),
            "PUBLIC_BASE": host.public_base,
            "PATH": f"{host.node_bin}:/usr/bin:/bin",
        },
        "timeout": CONNECT_TIMEOUT_SECONDS,
    }


def _union(current: object, additions: list[str]) -> list[str]:
    merged = list(current) if isinstance(current, list) else []
    merged += [item for item in additions if item not in merged]
    return merged


def wire(config: dict[str, Any], root: Path, host: Host) -> dict[str, Any]:
    """``config`` with the server, gate lists, and screening wired in (a copy)."""
    out = copy.deepcopy(config)
    servers = out.setdefault("mcp_servers", {})
    servers[MCP_SERVER_NAME] = server_entry(root, host)
    servers.pop(OLD_LOGIN_SERVER, None)
    gate = out.setdefault("gate", {})
    current = [t for t in gate.get("approved") or [] if t != OLD_LOGIN_LINK_TOOL]
    gate["approved"] = _union(current, _gated(READ_TOOLS) + [LOGIN_LINK_TOOL])
    gate["never"] = _union(gate.get("never"), _gated(NEVER_TOOLS))
    # Only extend an explicit screen list. Absent means core's default, which
    # already screens mcp_*; writing a list would replace (narrow) it.
    typesafe = out.get("typesafe")
    if isinstance(typesafe, dict) and "screen_tools" in typesafe:
        typesafe["screen_tools"] = _union(typesafe["screen_tools"], [SCREEN_PATTERN])
    return out


_USAGE = (
    "usage: playwright_tools.py\n"
    "  {reads|writes|never|pin|viewer-port|wire ROOT NODE_BIN BASE}\n"
    "  reads|writes|never  gated tool names in that class\n"
    "  pin                 the pinned @playwright/mcp version\n"
    "  wire ROOT NODE_BIN BASE  merge servers + gate + screening into\n"
    "                      ROOT/config.yaml (BASE: public login-link base URL;\n"
    "                      python is the interpreter running this)"
)


def main(argv: list[str]) -> int:
    if argv[:1] == ["wire"] and len(argv) == 4:
        import yaml

        path = Path(argv[1]) / "config.yaml"
        raw = yaml.safe_load(path.read_text()) if path.exists() else None
        host = Host(Path(argv[2]), Path(sys.executable), argv[3])
        wired = wire(raw or {}, Path(argv[1]).resolve(), host)
        path.write_text(yaml.safe_dump(wired, sort_keys=False))
        return 0
    listings = {
        "reads": _gated(READ_TOOLS),
        "writes": _gated(WRITE_TOOLS),
        "never": _gated(NEVER_TOOLS),
        "pin": [PIN],
        "viewer-port": [str(VIEWER_PORT)],
    }
    if len(argv) == 1 and argv[0] in listings:
        print("\n".join(listings[argv[0]]))
        return 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
