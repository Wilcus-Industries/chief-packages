#!/usr/bin/env python3
"""playwright: chief's browser — one shared Chrome, one tab per session.

One process, supervised by chief as the stdio MCP server ``playwright``
(``session_context: true``, so each call names the calling chief session):

- keeps a Chrome window running on the persistent chief-only profile, CDP on
  127.0.0.1 — one set of logins for every session;
- re-exposes the pinned playwright-mcp tools, routing each session's calls to
  its own Playwright child and tab (``browser_pool``): idle tabs close after
  10 minutes, at most 4 at once, and switching tabs is refused;
- adds ``login_link(url)`` and serves its phone viewer on 127.0.0.1
  (``tailscale serve`` mounts it on the tailnet at ``/browser``).

Config comes from env (wired by ``playwright_tools.py wire``). stdout belongs
to the MCP protocol, so logging goes to stderr and uvicorn's access log is off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from browser_pool import (
    BLOCKED,
    BLOCKED_MESSAGE,
    END,
    ENDED_MESSAGE,
    BrowserPool,
    route,
)
from chrome_launch import ensure_chrome, keep_chrome_alive
from login_host import LOGIN_LINK_DESCRIPTION, LoginHost
from login_viewer import build_app
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from playwright_child import ChildSpec, PlaywrightChild

logger = logging.getLogger("playwright-host")

PROFILE = Path(os.environ.get("BROWSER_PROFILE", "data/playwright/profile"))
CDP_PORT = int(os.environ.get("CDP_PORT", "9322"))
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8131"))
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", f"http://127.0.0.1:{VIEWER_PORT}")
SPEC = ChildSpec(
    binary=os.environ.get("PLAYWRIGHT_MCP", "playwright-mcp"),
    cdp_port=CDP_PORT,
    output_dir=os.environ.get("OUTPUT_DIR", "data/playwright/output"),
    workspace=os.environ.get("WORKSPACE", "data/playwright/workspace"),
    path=os.environ.get("PATH", "/usr/bin:/bin"),
)
REAP_EVERY_SECONDS = 60
UNSCOPED = "_unscoped"  # a call with no chief session (not opted in / CLI)

LOGIN_TOOL = types.Tool(
    name="login_link",
    description=LOGIN_LINK_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Login page URL"}},
        "required": ["url"],
    },
)

server: Server[Any, Any] = Server("playwright")
login = LoginHost(PROFILE, CDP_PORT, PUBLIC_BASE)


async def _start_child() -> PlaywrightChild:
    await ensure_chrome(PROFILE, CDP_PORT)
    return await PlaywrightChild.start(SPEC)


pool = BrowserPool(_start_child)
playwright_tools: list[types.Tool] = []


def _text(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=error
    )


def _calling_thread() -> str:
    meta = server.request_context.meta
    extra = (meta.model_extra if meta is not None else None) or {}
    return str(extra.get("chief/thread") or UNSCOPED)


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [*playwright_tools, LOGIN_TOOL]


@server.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    if name == LOGIN_TOOL.name:
        text = await login.login_link(str(arguments.get("url", "")))
        return _text(text, error=text.startswith("error"))
    thread = _calling_thread()
    action = route(name, arguments)
    if action == BLOCKED:
        return _text(BLOCKED_MESSAGE, error=True)
    if action == END:
        await pool.end(thread)
        return _text(ENDED_MESSAGE)
    result: types.CallToolResult = await pool.call(thread, name, arguments)
    return result


async def _reap_idle() -> None:
    while True:
        await asyncio.sleep(REAP_EVERY_SECONDS)
        for thread in await pool.reap_idle():
            logger.info("closed idle browser tab for session %s", thread)


async def _serve_stdio() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    await ensure_chrome(PROFILE, CDP_PORT)
    # The tool list comes from the pinned server itself; a probe child that
    # never opens a tab lends it, then exits.
    probe = await PlaywrightChild.start(SPEC)
    try:
        playwright_tools.extend(await probe.list_tools())
    finally:
        await probe.close()
    app = build_app(
        login.store, login.open_session, secure_cookie=PUBLIC_BASE.startswith("https")
    )
    viewer = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=VIEWER_PORT,
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
    stdio = asyncio.create_task(_serve_stdio())
    background = [
        asyncio.create_task(viewer.serve()),
        asyncio.create_task(keep_chrome_alive(PROFILE, CDP_PORT)),
        asyncio.create_task(login.sweep_expired()),
        asyncio.create_task(_reap_idle()),
    ]
    # chief closing our stdin (stop/restart) ends the MCP session; then close
    # every session's tab and exit. Chrome runs in its own session and
    # survives (logins intact) for the next instance.
    await asyncio.wait([stdio, background[0]], return_when=asyncio.FIRST_COMPLETED)
    viewer.should_exit = True
    await pool.close_all()
    for task in [stdio, *background]:
        task.cancel()
    await asyncio.gather(stdio, *background, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
