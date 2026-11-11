"""The phone login flow: mint a link for a tab, bridge the phone to it.

A login tab is chief's own (not any session's Playwright tab). Its CDP session
is held open for the tab's whole life, because LOGIN_TAB_SETUP — the passkey
block that keeps macOS's native passkey UI off the unwatched Mac — only lasts
while a session is attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from cdp_client import CdpConnection, close_tab, create_tab, page_ws_url
from cdp_input import LOGIN_TAB_SETUP
from login_bridge import run_bridge
from login_sessions import TTL_SECONDS, SessionStore
from starlette.websockets import WebSocket, WebSocketDisconnect
from url_policy import refusal

logger = logging.getLogger(__name__)

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
    """``ensure`` starts Chrome if it is down. ``tab_lock`` is the lock the
    browser pool holds while it opens a session's tab: it tells tabs apart by
    diffing Chrome's tab list, so a login tab must not appear mid-diff."""

    def __init__(
        self,
        cdp_port: int,
        public_base: str,
        ensure: Callable[[], Awaitable[object]],
        tab_lock: asyncio.Lock | None = None,
    ) -> None:
        self.store = SessionStore()
        self._port = cdp_port
        self._base = public_base.rstrip("/")
        self._ensure = ensure
        self._tab_lock = tab_lock or asyncio.Lock()
        self._tabs: dict[str, CdpConnection] = {}
        self._bridges: dict[str, asyncio.Task[str]] = {}

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
        problem = refusal(url)
        if problem is not None:
            return problem
        await self._ensure()
        # Open blank, attach the passkey block, then load the site — so even a
        # passkey prompt fired on page load never reaches the Mac's native UI.
        async with self._tab_lock:
            target_id = await create_tab(self._port, "about:blank")
        session = self.store.mint(target_id, url)
        conn = await self._tab_connection(session.token, session.target_id)
        await conn.send("Page.navigate", {"url": url})
        return (
            f"{self._base}/s/{session.token}\n"
            f"Single use, expires in {TTL_SECONDS // 60} minutes. The owner taps "
            "Done when logged in; then the site is ready to browse."
        )

    async def _run_one_bridge(
        self, token: str, cdp: CdpConnection, phone: _Phone
    ) -> str:
        """Run the bridge for ``token``, first stopping (and waiting out the
        teardown of) any earlier socket's bridge for it — a stale socket's
        teardown must never stop the live one's screencast or viewport."""
        # A loop, not an if: another socket may have started a bridge while
        # this one waited. No await between the last check and the insert.
        while (previous := self._bridges.get(token)) is not None:
            previous.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await previous
            if self._bridges.get(token) is previous:
                del self._bridges[token]
        task = asyncio.create_task(run_bridge(cdp, phone))
        self._bridges[token] = task
        try:
            await asyncio.wait({task})
        finally:
            if not task.done():  # this handler itself was cancelled
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if self._bridges.get(token) is task:
                del self._bridges[token]
        if task.cancelled():
            return "replaced"
        return task.result()

    async def open_session(self, token: str, websocket: WebSocket) -> None:
        session = self.store.get(token)
        if session is None:
            await websocket.close(code=4410)
            return
        cdp = await self._tab_connection(token, session.target_id)
        if await self._run_one_bridge(token, cdp, _Phone(websocket)) == "done":
            self.store.end(token)
            await self._finish(token, session.target_id)
            with contextlib.suppress(Exception):
                await websocket.send_json({"t": "ended"})
                await websocket.close()

    async def sweep_expired(self, every: float = 30) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                for session in self.store.sweep_expired():
                    await self._finish(session.token, session.target_id)
            except Exception:
                logger.exception("login sweep failed; retrying next round")
