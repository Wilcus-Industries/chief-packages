#!/usr/bin/env python3
"""playwright: chief's browser — one shared Chrome, one tab per session.

One process, supervised by chief as the stdio MCP server ``playwright``
(``session_context: true``, so each call names the calling chief session):

- keeps a Chrome window running on the persistent chief-only profile, CDP on
  127.0.0.1 — one set of logins for every session;
- re-exposes the pinned playwright-mcp tools, routing each session's calls to
  its own Playwright child and tab (``browser_pool``): idle tabs close after
  10 minutes, at most 4 at once, and switching tabs is refused. Every URL it
  forwards must be http(s) and not this machine (``url_policy``), and
  ``browser_run_code_unsafe`` is neither listed nor callable;
- adds ``login_link(url)`` and serves its phone viewer on 127.0.0.1
  (``tailscale serve`` mounts it on the tailnet at ``/browser``).

Config comes from env (wired by ``playwright_tools.py wire``). stdout belongs
to the MCP protocol, so logging goes to stderr and uvicorn's access log is off.
Importing this module starts nothing; ``main`` does, and ``build_server`` is
the MCP surface on its own (the tests drive it in memory).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from browser_pool import (
    BLOCKED,
    BLOCKED_MESSAGE,
    END,
    ENDED_MESSAGE,
    BrowserPool,
    TabLost,
    route,
)
from cdp_client import close_tab, page_ids, reconcile_tabs
from chrome_launch import ensure_chrome, keep_chrome_alive
from login_host import LOGIN_LINK_DESCRIPTION, LoginHost
from login_viewer import build_app
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from playwright_child import ChildSpec, PlaywrightChild
from playwright_tools import NEVER_TOOLS
from url_policy import effective_url, refusal

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
# Forwarded tools whose argument is a URL the browser will load. (The pinned
# browser_tabs takes one too, but only for "new", which is never forwarded.)
URL_ARGS: dict[str, str] = {"browser_navigate": "url"}
REFUSED_MESSAGE = "error: {name} is not available in chief's browser"

LOGIN_TOOL = types.Tool(
    name="login_link",
    description=LOGIN_LINK_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Login page URL"}},
        "required": ["url"],
    },
)


class Pool(Protocol):
    async def call(self, thread: str, name: str, args: dict[str, Any]) -> Any: ...

    async def end(self, thread: str) -> bool: ...


class Login(Protocol):
    async def login_link(self, url: str) -> str: ...


def _text(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=error
    )


def build_server(pool: Pool, login: Login, tools: list[types.Tool]) -> Server[Any, Any]:
    """The MCP surface. ``tools`` is the pinned server's list (filled in by
    ``main`` before serving); NEVER tools are dropped from it and refused."""
    server: Server[Any, Any] = Server("playwright")

    def offered() -> list[types.Tool]:
        return [t for t in tools if t.name not in NEVER_TOOLS]

    def calling_thread() -> str:
        meta = server.request_context.meta
        extra = (meta.model_extra if meta is not None else None) or {}
        thread = extra.get("chief/thread")
        if not thread:
            # Fails open to one shared tab: core lacks session_context, or the
            # config entry lost `session_context: true`.
            logger.warning(
                "browser call without a chief session (chief/thread); every "
                "such call shares one tab — check session_context is wired"
            )
            return UNSCOPED
        return str(thread)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [*offered(), LOGIN_TOOL]

    @server.call_tool(validate_input=False)
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        args = dict(arguments or {})
        if name == LOGIN_TOOL.name:
            text = await login.login_link(str(args.get("url", "")))
            return _text(text, error=text.startswith("error"))
        if name not in {t.name for t in offered()}:
            return _text(REFUSED_MESSAGE.format(name=name), error=True)
        thread = calling_thread()
        action = route(name, args)
        if action == BLOCKED:
            return _text(BLOCKED_MESSAGE, error=True)
        if action == END:
            await pool.end(thread)
            return _text(ENDED_MESSAGE)
        key = URL_ARGS.get(name)
        if key is not None:
            url = effective_url(str(args.get(key, "")))
            problem = refusal(url)
            if problem is not None:
                return _text(problem, error=True)
            args[key] = url
        try:
            result: types.CallToolResult = await pool.call(thread, name, args)
        except TabLost as lost:
            return _text(str(lost), error=True)
        return result

    return server


async def _reap_idle(pool: BrowserPool) -> None:
    while True:
        await asyncio.sleep(REAP_EVERY_SECONDS)
        try:
            for thread in await pool.reap_idle():
                logger.info("closed idle browser tab for session %s", thread)
        except Exception:
            logger.exception("idle reaping failed; retrying next round")


async def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    # keep_chrome_alive polls CDP every few seconds; one INFO line each is noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def list_tabs() -> set[str]:
        return await page_ids(CDP_PORT)

    async def close_target(target_id: str) -> None:
        await close_tab(CDP_PORT, target_id)

    async def start_child() -> PlaywrightChild:
        await ensure()
        return await PlaywrightChild.start(SPEC)

    pool = BrowserPool(start_child, list_tabs, close_target)

    async def ensure() -> bool:
        relaunched = await ensure_chrome(PROFILE, CDP_PORT)
        if relaunched:
            # Every tab died with the old Chrome; surviving children would
            # reattach to whatever page the new one shows first.
            logger.warning("Chrome (re)launched; dropping every session's tab")
            await pool.drop_all()
        return relaunched

    login = LoginHost(CDP_PORT, PUBLIC_BASE, ensure, tab_lock=pool.tab_lock)
    await ensure_chrome(PROFILE, CDP_PORT)
    # Tabs a previous host left (sessions and links lived in its memory).
    closed = await reconcile_tabs(CDP_PORT)
    if closed:
        logger.info("closed %d orphaned tab(s) from a previous run", closed)
    # The tool list comes from the pinned server itself; a probe child that
    # never opens a tab lends it, then exits.
    tools: list[types.Tool] = []
    probe = await PlaywrightChild.start(SPEC)
    try:
        tools.extend(await probe.list_tools())
    finally:
        await probe.close()
    server = build_server(pool, login, tools)
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

    async def serve_stdio() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    stdio = asyncio.create_task(serve_stdio())
    background = [
        asyncio.create_task(viewer.serve()),
        asyncio.create_task(keep_chrome_alive(ensure)),
        asyncio.create_task(login.sweep_expired()),
        asyncio.create_task(_reap_idle(pool)),
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
