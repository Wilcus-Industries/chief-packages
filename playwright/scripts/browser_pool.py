"""One Playwright child — one browser tab — per chief session.

Every session shares the Chrome profile (one set of logins) but never a tab:
Playwright children attached to one Chrome over CDP all grab its first page
unless each opens its own, so a child's first call is always a new tab. The
pool closes a session's tab and child when it goes idle, when it asks to close,
or when a newer session needs the slot; switching to another tab is refused.

A child's "current tab" is the pinned server's idea, and it re-points it
silently: when its tab closes under it (a manual close, a crash, a Chrome
relaunch) it falls through to some other page — another session's, or a login
tab. So the pool never trusts it. It records each session's tab by CDP target
id when the tab opens (the one new id in Chrome's tab list across the ``new``
call, diffed under ``tab_lock``), checks that id is still open before every
forwarded call, and closes tabs by id, never as "the current tab".
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
# How long ending a session waits for its in-flight call before closing the
# child out from under it (which fails that call).
END_LOCK_WAIT_SECONDS = 5.0

FORWARD, BLOCKED, END = "forward", "blocked", "end"

BLOCKED_MESSAGE = (
    "error: each chief session has exactly one browser tab — switching or "
    "opening tabs is disabled. Navigate in your tab instead."
)
ENDED_MESSAGE = (
    "Closed this session's browser tab. The next browser call opens a new one."
)
TAB_LOST_MESSAGE = (
    "error: this session's browser tab was closed outside chief (or Chrome "
    "restarted), so its page is gone. The next browser call opens a fresh "
    "tab — browser_navigate to where you were."
)

_NEW_TAB: dict[str, Any] = {"action": "new"}
# Calls that mean the same in a fresh blank tab: a lost tab is replaced and
# the call goes through instead of failing.
_FRESH_TAB_OK = frozenset({"browser_navigate"})


class TabLost(RuntimeError):
    """The session's tab is gone and the session was ended."""


class Child(Protocol):
    async def call(self, name: str, args: dict[str, Any]) -> Any: ...

    async def close(self) -> None: ...


ChildFactory = Callable[[], Awaitable[Child]]
ListTabs = Callable[[], Awaitable[set[str]]]
CloseTab = Callable[[str], Awaitable[None]]


def route(name: str, args: dict[str, Any]) -> str:
    """What to do with a session's call: forward it, refuse it, or end the
    session's browser (closing only its own tab, whatever index it named).
    ``browser_tabs`` is an allowlist: only ``list`` reaches the child."""
    if name == "browser_close":
        return END
    if name == "browser_tabs":
        action = args.get("action")
        if action == "list":
            return FORWARD
        if action == "close":
            return END
        return BLOCKED
    return FORWARD


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "isError", False))


@dataclass
class _Entry:
    child: Child
    target_id: str
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserPool:
    def __init__(
        self,
        factory: ChildFactory,
        list_tabs: ListTabs,
        close_tab: CloseTab,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = IDLE_SECONDS,
        max_sessions: int = MAX_SESSIONS,
        end_lock_wait: float = END_LOCK_WAIT_SECONDS,
        tab_lock: asyncio.Lock | None = None,
    ) -> None:
        self._factory = factory
        self._list_tabs = list_tabs
        self._close_tab = close_tab
        self._clock = clock
        self._idle = idle_seconds
        self._max = max_sessions
        self._end_wait = end_lock_wait
        self._entries: dict[str, _Entry] = {}
        self._creating = asyncio.Lock()
        # Held across the tab-list diff. Anyone else opening tabs (login
        # links) takes it too, so the one new id is this session's.
        self.tab_lock = tab_lock or asyncio.Lock()

    @property
    def threads(self) -> list[str]:
        return list(self._entries)

    def target_of(self, thread: str) -> str | None:
        entry = self._entries.get(thread)
        return entry.target_id if entry else None

    async def _open_tab(self, child: Child) -> str:
        async with self.tab_lock:
            before = await self._list_tabs()
            result = await child.call("browser_tabs", _NEW_TAB)
            if _is_error(result):
                raise RuntimeError(f"could not open a browser tab: {result}")
            opened = await self._list_tabs() - before
        if len(opened) != 1:
            for target_id in opened:
                await self._close_tab(target_id)
            raise RuntimeError(f"expected one new tab, Chrome shows {len(opened)}")
        return opened.pop()

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
            try:
                target_id = await self._open_tab(child)
            except BaseException:
                # No tab of its own: never keep (or leak) the child.
                with contextlib.suppress(Exception):
                    await child.close()
                raise
            entry = _Entry(child, target_id, self._clock())
            self._entries[thread] = entry
            return entry

    async def call(self, thread: str, name: str, args: dict[str, Any]) -> Any:
        """Forward one call to ``thread``'s own tab, opening it if need be.

        Raises TabLost if the session's tab vanished (the session is ended,
        but a ``browser_navigate`` goes through in a fresh tab instead)."""
        return await self._call(thread, name, args, retry=name in _FRESH_TAB_OK)

    async def _call(
        self, thread: str, name: str, args: dict[str, Any], retry: bool
    ) -> Any:
        entry = await self._entry(thread)
        error: Exception | None = None
        async with entry.lock:
            alive = (
                self._entries.get(thread) is entry
                and entry.target_id in await self._list_tabs()
            )
            if alive:
                entry.last_used = self._clock()
                try:
                    return await entry.child.call(name, args)
                except Exception as exc:
                    error = exc
        if error is not None:
            # A raised call (not a tool error result) means the child or its
            # transport died: drop it so the session's next call starts fresh.
            if self._entries.get(thread) is entry:
                await self.end(thread)
            raise error
        logger.warning("session %s's browser tab is gone; starting fresh", thread)
        if self._entries.get(thread) is entry:
            await self.end(thread)
        if retry:
            return await self._call(thread, name, args, retry=False)
        raise TabLost(TAB_LOST_MESSAGE)

    async def _close(self, entry: _Entry, close_tab: bool) -> None:
        """Close an entry's tab (by id) and child. Waits a bounded time for an
        in-flight call, then closes anyway — a hung call must never wedge
        reaping, the cap, or browser_close."""
        try:
            await asyncio.wait_for(entry.lock.acquire(), self._end_wait)
            held = True
        except TimeoutError:
            logger.warning("browser call still running; force-closing its child")
            held = False
        try:
            if close_tab:
                with contextlib.suppress(Exception):
                    await self._close_tab(entry.target_id)
            with contextlib.suppress(Exception):
                await entry.child.close()
        finally:
            if held:
                entry.lock.release()

    async def end(self, thread: str) -> bool:
        entry = self._entries.pop(thread, None)
        if entry is None:
            return False
        await self._close(entry, close_tab=True)
        return True

    async def drop_all(self) -> None:
        """Forget every session after Chrome relaunched: their tabs died with
        the old browser, and their children would reattach to whatever page
        the new one shows first. Children are closed; no tab is."""
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await self._close(entry, close_tab=False)

    async def reap_idle(self) -> list[str]:
        now = self._clock()
        idle = [t for t, e in self._entries.items() if now - e.last_used > self._idle]
        for thread in idle:
            await self.end(thread)
        return idle

    async def close_all(self) -> None:
        for thread in list(self._entries):
            await self.end(thread)
