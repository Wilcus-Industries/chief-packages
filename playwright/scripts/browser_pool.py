"""One Playwright child — one browser tab — per chief session.

Every session shares the Chrome profile (one set of logins) but never a tab:
Playwright children attached to one Chrome over CDP all grab its first page
unless each opens its own, so a child's first call is always a new tab. The
pool closes a session's tab and child when it goes idle, when it asks to close,
or when a newer session needs the slot; switching to another tab is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

IDLE_SECONDS = 10 * 60
MAX_SESSIONS = 4

FORWARD, BLOCKED, END = "forward", "blocked", "end"

BLOCKED_MESSAGE = (
    "error: each chief session has exactly one browser tab — switching or "
    "opening tabs is disabled. Navigate in your tab instead."
)
ENDED_MESSAGE = (
    "Closed this session's browser tab. The next browser call opens a new one."
)

_NEW_TAB: dict[str, Any] = {"action": "new"}
_CLOSE_TAB: dict[str, Any] = {"action": "close"}


class Child(Protocol):
    async def call(self, name: str, args: dict[str, Any]) -> Any: ...

    async def close(self) -> None: ...


ChildFactory = Callable[[], Awaitable[Child]]


def route(name: str, args: dict[str, Any]) -> str:
    """What to do with a session's call: forward it, refuse it, or end the
    session's browser (closing only its own tab, whatever index it named)."""
    if name == "browser_close":
        return END
    if name == "browser_tabs":
        action = args.get("action")
        if action == "close":
            return END
        if action in ("select", "new"):
            return BLOCKED
    return FORWARD


@dataclass
class _Entry:
    child: Child
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserPool:
    def __init__(
        self,
        factory: ChildFactory,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = IDLE_SECONDS,
        max_sessions: int = MAX_SESSIONS,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._idle = idle_seconds
        self._max = max_sessions
        self._entries: dict[str, _Entry] = {}
        self._creating = asyncio.Lock()

    @property
    def threads(self) -> list[str]:
        return list(self._entries)

    async def _entry(self, thread: str) -> _Entry:
        async with self._creating:
            entry = self._entries.get(thread)
            if entry is not None:
                return entry
            while len(self._entries) >= self._max:
                oldest = min(self._entries, key=lambda t: self._entries[t].last_used)
                logger.info("browser cap reached; closing session %s's tab", oldest)
                await self.end(oldest)
            child = await self._factory()
            await child.call("browser_tabs", _NEW_TAB)
            entry = _Entry(child, self._clock())
            self._entries[thread] = entry
            return entry

    async def call(self, thread: str, name: str, args: dict[str, Any]) -> Any:
        entry = await self._entry(thread)
        try:
            async with entry.lock:
                entry.last_used = self._clock()
                return await entry.child.call(name, args)
        except Exception:
            # A raised call (not a tool error result) means the child or its
            # transport died: drop it so the session's next call starts fresh.
            if self._entries.get(thread) is entry:
                await self.end(thread)
            raise

    async def end(self, thread: str) -> bool:
        entry = self._entries.pop(thread, None)
        if entry is None:
            return False
        async with entry.lock:
            with contextlib.suppress(Exception):
                await entry.child.call("browser_tabs", _CLOSE_TAB)
            with contextlib.suppress(Exception):
                await entry.child.close()
        return True

    async def reap_idle(self) -> list[str]:
        now = self._clock()
        idle = [t for t, e in self._entries.items() if now - e.last_used > self._idle]
        for thread in idle:
            await self.end(thread)
        return idle

    async def close_all(self) -> None:
        for thread in list(self._entries):
            await self.end(thread)
