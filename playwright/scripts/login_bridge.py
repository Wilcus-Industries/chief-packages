"""Bridge one phone connection to one Chrome tab.

Frames flow tab -> phone (CDP screencast); gestures flow phone -> tab, but only
through ``cdp_input``'s fixed vocabulary. The tab is emulated at the phone's
size for the session and restored afterwards. The phone also gets the main
frame's host on every top-level navigation, so the owner can see which site
they are typing a password into.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol
from urllib.parse import urlsplit

from cdp_input import RESET_VIEWPORT, input_commands, viewport_commands

JPEG_QUALITY = 60


class Cdp(Protocol):
    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any: ...

    async def next_event(self) -> dict[str, Any]: ...


class Phone(Protocol):
    async def receive(self) -> dict[str, Any] | None: ...

    async def send(self, message: dict[str, Any]) -> None: ...


def _origin(url: object) -> dict[str, Any]:
    host = urlsplit(url).hostname if isinstance(url, str) else None
    return {"t": "origin", "host": host or ""}


async def _pump_frames(cdp: Cdp, phone: Phone) -> None:
    while True:
        event = await cdp.next_event()
        if event.get("method") == "Page.frameNavigated":
            frame = event.get("params", {}).get("frame", {})
            if isinstance(frame, dict) and "parentId" not in frame:  # top only
                await phone.send(_origin(frame.get("url")))
            continue
        if event.get("method") != "Page.screencastFrame":
            continue
        params = event.get("params", {})
        await cdp.send(
            "Page.screencastFrameAck", {"sessionId": params.get("sessionId")}
        )
        await phone.send(
            {
                "t": "frame",
                "data": params.get("data"),
                "meta": params.get("metadata", {}),
            }
        )


async def _pump_input(cdp: Cdp, phone: Phone) -> str:
    while True:
        message = await phone.receive()
        if message is None:
            return "gone"
        if message.get("t") == "done":
            return "done"
        for method, params in input_commands(message):
            await cdp.send(method, params)


async def run_bridge(cdp: Cdp, phone: Phone) -> str:
    """Run until the owner taps Done (``"done"``) or the phone drops (``"gone"``)."""
    hello = await phone.receive()
    if not isinstance(hello, dict) or hello.get("t") != "hello":
        return "gone"
    width, height, dpr = hello.get("w"), hello.get("h"), hello.get("dpr")
    viewport = viewport_commands(width, height, dpr)
    metrics = viewport[0][1]
    try:
        for method, params in viewport:
            await cdp.send(method, params)
        await cdp.send("Page.enable")
        tree = await cdp.send("Page.getFrameTree")
        frame = (tree or {}).get("frameTree", {}).get("frame", {})
        await phone.send(_origin(frame.get("url")))
        scale = metrics["deviceScaleFactor"]
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": JPEG_QUALITY,
                "maxWidth": int(metrics["width"] * scale),
                "maxHeight": int(metrics["height"] * scale),
            },
        )
        frames = asyncio.create_task(_pump_frames(cdp, phone))
        try:
            return await _pump_input(cdp, phone)
        finally:
            frames.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await frames
    finally:
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")
        for method, params in RESET_VIEWPORT:
            with contextlib.suppress(Exception):
                await cdp.send(method, params)
