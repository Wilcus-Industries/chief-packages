"""A minimal Chrome DevTools Protocol client over a websocket.

Just enough for the login viewer and the host's tab bookkeeping:
request/response by id, an event queue, and the browser-level calls to list,
open and close tabs. CDP listens on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import websockets


class CdpError(RuntimeError):
    pass


class CdpConnection:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None

    @classmethod
    async def open(cls, url: str) -> CdpConnection:
        conn = cls(await websockets.connect(url, max_size=None))
        conn._reader = asyncio.create_task(conn._read())
        return conn

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                future = self._pending.pop(message.get("id", -1), None)
                if future is None:
                    await self._events.put(message)
                elif "error" in message:
                    future.set_exception(CdpError(str(message["error"])))
                else:
                    future.set_result(message.get("result", {}))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("CDP connection closed"))
            await self._events.put({"method": "chief.closed"})

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        await self._ws.send(
            json.dumps({"id": self._next_id, "method": method, "params": params or {}})
        )
        return await future

    @property
    def closed(self) -> bool:
        return self._reader is None or self._reader.done()

    async def next_event(self) -> dict[str, Any]:
        return await self._events.get()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()


async def _json(port: int, path: str) -> Any:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"http://127.0.0.1:{port}{path}")
        response.raise_for_status()
        return response.json()


async def _browser_call(port: int, method: str, params: dict[str, Any]) -> Any:
    version = await _json(port, "/json/version")
    conn = await CdpConnection.open(version["webSocketDebuggerUrl"])
    try:
        return await conn.send(method, params)
    finally:
        await conn.close()


async def create_tab(port: int, url: str) -> str:
    result = await _browser_call(port, "Target.createTarget", {"url": url})
    return str(result["targetId"])


async def close_tab(port: int, target_id: str) -> None:
    with contextlib.suppress(CdpError, httpx.HTTPError, OSError):
        await _browser_call(port, "Target.closeTarget", {"targetId": target_id})


async def page_ws_url(port: int, target_id: str) -> str:
    for target in await _json(port, "/json/list"):
        if target.get("id") == target_id:
            return str(target["webSocketDebuggerUrl"])
    raise CdpError(f"tab {target_id} is gone")


async def page_ids(port: int) -> set[str]:
    """The target ids of every open tab (page target) in the browser."""
    return {
        str(t["id"]) for t in await _json(port, "/json/list") if t.get("type") == "page"
    }


async def reconcile_tabs(port: int) -> int:
    """Close every tab but one blank one; return how many were closed.

    Run at browser-host startup: a previous host's session and login tabs are
    nobody's now (sessions and links live in memory), and would otherwise sit
    in the logged-in browser forever. One blank tab stays so the window — and
    Chrome with it — stays up.
    """
    pages = [t for t in await _json(port, "/json/list") if t.get("type") == "page"]
    keep = next((t for t in pages if t.get("url") == "about:blank"), None)
    keep_id = str(keep["id"]) if keep else await create_tab(port, "about:blank")
    stale = [str(t["id"]) for t in pages if str(t["id"]) != keep_id]
    for target_id in stale:
        await close_tab(port, target_id)
    return len(stale)
