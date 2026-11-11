#!/usr/bin/env python3
"""browser-login: chief's shared Chrome + the owner's remote login page.

One process, supervised by chief as a stdio MCP server:

- keeps a Chrome window running on the persistent chief-only profile with CDP
  on 127.0.0.1 (playwright-mcp attaches to the same browser);
- exposes one MCP tool, ``login_link(url)``: open ``url`` in a new tab and
  return a one-time link the owner opens on their phone;
- serves that link's viewer on 127.0.0.1 (``tailscale serve`` puts it on the
  tailnet under ``/browser``), bridging the phone to the tab.

Config comes from env (wired by ``playwright_tools.py wire``). stdout belongs
to the MCP protocol, so all logging goes to stderr and uvicorn's access log is
off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from cdp_client import CdpConnection, close_tab, create_tab, page_ws_url
from cdp_input import LOGIN_TAB_SETUP
from chrome_launch import ensure_chrome, keep_chrome_alive
from login_bridge import run_bridge
from login_sessions import TTL_SECONDS, SessionStore
from login_viewer import build_app
from mcp.server.fastmcp import FastMCP
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("browser-login")

PROFILE = Path(os.environ.get("BROWSER_PROFILE", "data/playwright/profile"))
CDP_PORT = int(os.environ.get("CDP_PORT", "9322"))
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8131"))
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", f"http://127.0.0.1:{VIEWER_PORT}")
PUBLIC_BASE = PUBLIC_BASE.rstrip("/")

store = SessionStore()
mcp = FastMCP("browser-login")
# token -> the login tab's CDP session, held open for the tab's whole life:
# LOGIN_TAB_SETUP (the passkey block) only lasts while a session is attached.
_tabs: dict[str, CdpConnection] = {}


async def _tab_connection(token: str, target_id: str) -> CdpConnection:
    conn = _tabs.get(token)
    if conn is None or conn.closed:
        conn = await CdpConnection.open(await page_ws_url(CDP_PORT, target_id))
        for method, params in LOGIN_TAB_SETUP:
            await conn.send(method, params)
        _tabs[token] = conn
    return conn


async def _finish(token: str, target_id: str) -> None:
    conn = _tabs.pop(token, None)
    if conn is not None:
        await conn.close()
    await close_tab(CDP_PORT, target_id)


@mcp.tool()
async def login_link(url: str) -> str:
    """Open ``url`` in chief's browser and return a one-time link for the owner.

    The owner opens the link on their phone, sees the live tab, logs in by
    hand (password, 2FA, captcha), and taps Done; the cookies stay in chief's
    profile. Text the owner the link. Never ask for their password.
    """
    if urlsplit(url).scheme not in ("http", "https"):
        return "error: url must be http(s)"
    await ensure_chrome(PROFILE, CDP_PORT)
    # Open blank, attach the passkey block, then load the site — so even a
    # passkey prompt fired on page load never reaches the Mac's native UI.
    session = store.mint(await create_tab(CDP_PORT, "about:blank"), url)
    conn = await _tab_connection(session.token, session.target_id)
    await conn.send("Page.navigate", {"url": url})
    minutes = TTL_SECONDS // 60
    return (
        f"{PUBLIC_BASE}/s/{session.token}\n"
        f"Single use, expires in {minutes} minutes. The owner taps Done when "
        "logged in; then the site is ready to browse."
    )


class _Phone:
    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def receive(self) -> dict[str, Any] | None:
        try:
            message = await self._ws.receive_json()
        except (WebSocketDisconnect, ValueError, RuntimeError):
            return None
        return message if isinstance(message, dict) else {}

    async def send(self, message: dict[str, Any]) -> None:
        await self._ws.send_json(message)


async def open_session(token: str, websocket: WebSocket) -> None:
    session = store.get(token)
    if session is None:
        await websocket.close(code=4410)
        return
    cdp = await _tab_connection(token, session.target_id)
    outcome = await run_bridge(cdp, _Phone(websocket))
    if outcome == "done":
        store.end(token)
        await _finish(token, session.target_id)
        with contextlib.suppress(Exception):
            await websocket.send_json({"t": "ended"})
            await websocket.close()


async def _sweep_expired(every: float = 30) -> None:
    while True:
        await asyncio.sleep(every)
        for session in store.sweep_expired():
            await _finish(session.token, session.target_id)


async def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    await ensure_chrome(PROFILE, CDP_PORT)
    app = build_app(store, open_session, secure_cookie=PUBLIC_BASE.startswith("https"))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=VIEWER_PORT,
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
    stdio = asyncio.create_task(mcp.run_stdio_async())
    background = [
        asyncio.create_task(server.serve()),
        asyncio.create_task(keep_chrome_alive(PROFILE, CDP_PORT)),
        asyncio.create_task(_sweep_expired()),
    ]
    # chief closing our stdin (stop/restart) ends the MCP session; then exit.
    # Chrome runs in its own session and survives for the next instance.
    await asyncio.wait([stdio, background[0]], return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    for task in [stdio, *background]:
        task.cancel()
    await asyncio.gather(stdio, *background, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
