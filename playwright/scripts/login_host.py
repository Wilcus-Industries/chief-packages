"""The phone login flow: mint a link for a tab, bridge the phone to it.

A login tab is chief's own (not any session's Playwright tab). Its CDP session
is held open for the tab's whole life, because LOGIN_TAB_SETUP — the passkey
block that keeps macOS's native passkey UI off the unwatched Mac — only lasts
while a session is attached.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cdp_client import CdpConnection, close_tab, create_tab, page_ws_url
from cdp_input import LOGIN_TAB_SETUP
from chrome_launch import ensure_chrome
from login_bridge import run_bridge
from login_sessions import TTL_SECONDS, SessionStore
from starlette.websockets import WebSocket, WebSocketDisconnect

LOGIN_LINK_DESCRIPTION = (
    "Open `url` in chief's browser and return a one-time link for the owner. "
    "The owner opens it on their phone, sees the live tab, logs in by hand "
    "(password, 2FA, captcha), and taps Done; the cookies stay in chief's "
    "profile for every session. Text the owner the link. Never ask for their "
    "password."
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


class LoginHost:
    def __init__(self, profile: Path, cdp_port: int, public_base: str) -> None:
        self.store = SessionStore()
        self._profile = profile
        self._port = cdp_port
        self._base = public_base.rstrip("/")
        self._tabs: dict[str, CdpConnection] = {}

    async def _tab_connection(self, token: str, target_id: str) -> CdpConnection:
        conn = self._tabs.get(token)
        if conn is None or conn.closed:
            conn = await CdpConnection.open(await page_ws_url(self._port, target_id))
            for method, params in LOGIN_TAB_SETUP:
                await conn.send(method, params)
            self._tabs[token] = conn
        return conn

    async def _finish(self, token: str, target_id: str) -> None:
        conn = self._tabs.pop(token, None)
        if conn is not None:
            await conn.close()
        await close_tab(self._port, target_id)

    async def login_link(self, url: str) -> str:
        if urlsplit(url).scheme not in ("http", "https"):
            return "error: url must be http(s)"
        await ensure_chrome(self._profile, self._port)
        # Open blank, attach the passkey block, then load the site — so even a
        # passkey prompt fired on page load never reaches the Mac's native UI.
        session = self.store.mint(await create_tab(self._port, "about:blank"), url)
        conn = await self._tab_connection(session.token, session.target_id)
        await conn.send("Page.navigate", {"url": url})
        return (
            f"{self._base}/s/{session.token}\n"
            f"Single use, expires in {TTL_SECONDS // 60} minutes. The owner taps "
            "Done when logged in; then the site is ready to browse."
        )

    async def open_session(self, token: str, websocket: WebSocket) -> None:
        session = self.store.get(token)
        if session is None:
            await websocket.close(code=4410)
            return
        cdp = await self._tab_connection(token, session.target_id)
        if await run_bridge(cdp, _Phone(websocket)) == "done":
            self.store.end(token)
            await self._finish(token, session.target_id)
            with contextlib.suppress(Exception):
                await websocket.send_json({"t": "ended"})
                await websocket.close()

    async def sweep_expired(self, every: float = 30) -> None:
        while True:
            await asyncio.sleep(every)
            for session in self.store.sweep_expired():
                await self._finish(session.token, session.target_id)
