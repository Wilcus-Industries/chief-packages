"""Per-session browsers: one Playwright child (one tab) per chief session.

All sessions share the Chrome profile (one set of logins) but never a tab. The
pool opens a fresh tab per session, records it by CDP target id, closes it
(by id) when the session goes idle or asks to close, caps concurrent sessions,
refuses tab switching, and never lets a session drift onto a tab that is not
its own. Children and Chrome's tab list are faked; no Chrome here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from browser_pool import BLOCKED, END, FORWARD, BrowserPool, TabLost, route


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeChrome:
    """Chrome's tab list, as CDP's /json/list reports it."""

    def __init__(self) -> None:
        self.tabs: set[str] = {"blank"}
        self.closed: list[str] = []
        self._n = 0

    def open(self) -> str:
        self._n += 1
        target = f"T{self._n}"
        self.tabs.add(target)
        return target

    async def list_tabs(self) -> set[str]:
        return set(self.tabs)

    async def close_tab(self, target_id: str) -> None:
        self.closed.append(target_id)
        self.tabs.discard(target_id)


class FakeChild:
    def __init__(self, n: int, chrome: FakeChrome | None = None) -> None:
        self.n = n
        self.chrome = chrome
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.tab: str | None = None

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if (name, args) == ("browser_tabs", {"action": "new"}) and self.chrome:
            self.tab = self.chrome.open()
        return f"child{self.n}:{name}"

    async def close(self) -> None:
        self.closed = True


def make_pool(
    clock: Clock, child: type[FakeChild] = FakeChild, **kwargs: Any
) -> tuple[BrowserPool, list[Any], FakeChrome]:
    chrome = FakeChrome()
    children: list[Any] = []

    async def factory() -> FakeChild:
        made = child(len(children), chrome)
        children.append(made)
        return made

    pool = BrowserPool(
        factory, chrome.list_tabs, chrome.close_tab, clock=clock, **kwargs
    )
    return pool, children, chrome


NEW_TAB = ("browser_tabs", {"action": "new"})


def test_each_session_gets_its_own_child_and_fresh_tab() -> None:
    async def scenario() -> tuple[list[FakeChild], BrowserPool]:
        pool, children, _ = make_pool(Clock())
        assert await pool.call("A", "browser_navigate", {"url": "a"}) == (
            "child0:browser_navigate"
        )
        assert await pool.call("B", "browser_navigate", {"url": "b"}) == (
            "child1:browser_navigate"
        )
        assert await pool.call("A", "browser_snapshot", {}) == "child0:browser_snapshot"
        return children, pool

    (a, b), pool = asyncio.run(scenario())
    # The first thing every child does is open its own tab, so it never
    # inherits (and fights over) another session's page.
    assert a.calls[0] == NEW_TAB and b.calls[0] == NEW_TAB
    assert [n for n, _ in a.calls] == [
        "browser_tabs",
        "browser_navigate",
        "browser_snapshot",
    ]
    # Each session's tab is recorded by its CDP target id.
    assert pool.target_of("A") == a.tab and pool.target_of("B") == b.tab
    assert a.tab != b.tab


def test_concurrent_first_calls_for_one_session_make_one_child() -> None:
    async def scenario() -> list[FakeChild]:
        pool, children, _ = make_pool(Clock())
        await asyncio.gather(
            pool.call("A", "browser_snapshot", {}),
            pool.call("A", "browser_snapshot", {}),
        )
        return children

    assert len(asyncio.run(scenario())) == 1


def test_idle_sessions_close_their_tab_by_id_and_child() -> None:
    async def scenario() -> tuple[list[str], list[FakeChild], BrowserPool, FakeChrome]:
        clock = Clock()
        pool, children, chrome = make_pool(clock, idle_seconds=600)
        await pool.call("A", "browser_snapshot", {})
        clock.now = 300
        await pool.call("B", "browser_snapshot", {})
        clock.now = 601
        return await pool.reap_idle(), children, pool, chrome

    reaped, (a, b), pool, chrome = asyncio.run(scenario())
    assert reaped == ["A"]
    # Closed by target id through CDP — never "the child's current tab".
    assert chrome.closed == [a.tab] and a.closed
    assert ("browser_tabs", {"action": "close"}) not in a.calls
    assert not b.closed
    assert pool.threads == ["B"]


def test_cap_ends_the_least_recently_used_session() -> None:
    async def scenario() -> tuple[list[FakeChild], BrowserPool]:
        clock = Clock()
        pool, children, _ = make_pool(clock, max_sessions=2)
        await pool.call("A", "browser_snapshot", {})
        clock.now = 1
        await pool.call("B", "browser_snapshot", {})
        clock.now = 2
        await pool.call("A", "browser_snapshot", {})  # A is now the fresher one
        clock.now = 3
        await pool.call("C", "browser_snapshot", {})
        return children, pool

    (a, b, c), pool = asyncio.run(scenario())
    assert b.closed and not a.closed and not c.closed
    assert sorted(pool.threads) == ["A", "C"]


def test_end_and_close_all() -> None:
    async def scenario() -> tuple[list[FakeChild], BrowserPool, bool, FakeChrome]:
        pool, children, chrome = make_pool(Clock())
        await pool.call("A", "browser_snapshot", {})
        await pool.call("B", "browser_snapshot", {})
        ended = await pool.end("A")
        await pool.close_all()
        return children, pool, ended, chrome

    (a, b), pool, ended, chrome = asyncio.run(scenario())
    assert ended and a.closed and b.closed
    assert chrome.closed == [a.tab, b.tab]
    assert chrome.tabs == {"blank"}
    assert pool.threads == []


def test_end_survives_a_tab_that_will_not_close() -> None:
    async def scenario() -> FakeChild:
        pool, children, chrome = make_pool(Clock())

        async def broken(target_id: str) -> None:
            raise RuntimeError("tab already gone")

        pool._close_tab = broken
        await pool.call("A", "browser_snapshot", {})
        await pool.end("A")
        child: FakeChild = children[0]
        return child

    assert asyncio.run(scenario()).closed


def test_a_crashed_child_is_replaced_on_the_next_call() -> None:
    class Dies(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> Any:
            if name == "browser_navigate" and self.n == 0:
                raise ConnectionError("child died")
            return await super().call(name, args)

    async def scenario() -> tuple[list[Dies], str]:
        pool, children, _ = make_pool(Clock(), child=Dies)
        with pytest.raises(ConnectionError):
            await pool.call("A", "browser_navigate", {"url": "x"})
        return children, await pool.call("A", "browser_navigate", {"url": "x"})

    children, result = asyncio.run(scenario())
    assert children[0].closed
    assert result == "child1:browser_navigate"


# --- the session's tab is the one it opened, or none -----------------------


class _Result:
    def __init__(self, error: bool) -> None:
        self.isError = error


def test_a_failed_new_tab_fails_the_call_and_closes_the_child() -> None:
    class NoTab(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> Any:
            self.calls.append((name, args))
            return _Result(error=True)

    async def scenario() -> tuple[list[NoTab], BrowserPool]:
        pool, children, _ = make_pool(Clock(), child=NoTab)
        with pytest.raises(RuntimeError, match="could not open a browser tab"):
            await pool.call("A", "browser_snapshot", {})
        return children, pool

    (child,), pool = asyncio.run(scenario())
    # The child never got its own tab: it is not kept (it would drive some
    # other page) and not leaked.
    assert child.closed and pool.threads == []
    assert [n for n, _ in child.calls] == ["browser_tabs"]


def test_a_raising_new_tab_does_not_leak_the_child() -> None:
    class Raises(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> Any:
            raise ConnectionError("child died on start")

    async def scenario() -> tuple[list[Raises], BrowserPool]:
        pool, children, _ = make_pool(Clock(), child=Raises)
        with pytest.raises(ConnectionError):
            await pool.call("A", "browser_snapshot", {})
        return children, pool

    (child,), pool = asyncio.run(scenario())
    assert child.closed and pool.threads == []


def test_a_new_tab_that_chrome_does_not_show_fails() -> None:
    class Phantom(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> Any:
            self.calls.append((name, args))
            return "ok"  # claims success, opens nothing

    async def scenario() -> tuple[list[Phantom], BrowserPool]:
        pool, children, _ = make_pool(Clock(), child=Phantom)
        with pytest.raises(RuntimeError, match="expected one new tab"):
            await pool.call("A", "browser_snapshot", {})
        return children, pool

    (child,), pool = asyncio.run(scenario())
    assert child.closed and pool.threads == []


def test_a_tab_closed_outside_the_pool_ends_the_session() -> None:
    async def scenario() -> tuple[list[FakeChild], BrowserPool, FakeChrome]:
        pool, children, chrome = make_pool(Clock())
        await pool.call("A", "browser_snapshot", {})
        await pool.call("B", "browser_snapshot", {})
        # The owner closes A's tab by hand. A's child would now fall through
        # to B's tab (or a login tab) — the call must not reach it.
        chrome.tabs.discard(children[0].tab)
        with pytest.raises(TabLost):
            await pool.call("A", "browser_click", {"target": "e1"})
        return children, pool, chrome

    (a, b), pool, chrome = asyncio.run(scenario())
    assert ("browser_click", {"target": "e1"}) not in a.calls
    assert a.closed and not b.closed
    assert pool.threads == ["B"]
    # Closing A never touched B's tab.
    assert b.tab in chrome.tabs


def test_a_lost_tab_is_replaced_for_a_navigation() -> None:
    async def scenario() -> tuple[list[FakeChild], str, BrowserPool]:
        pool, children, chrome = make_pool(Clock())
        await pool.call("A", "browser_snapshot", {})
        chrome.tabs.discard(children[0].tab)
        result = await pool.call("A", "browser_navigate", {"url": "https://x.test"})
        return children, result, pool

    (old, new), result, pool = asyncio.run(scenario())
    assert result == "child1:browser_navigate"
    assert old.closed and pool.target_of("A") == new.tab


def test_drop_all_forgets_every_session_after_a_relaunch() -> None:
    async def scenario() -> tuple[list[FakeChild], BrowserPool, FakeChrome]:
        pool, children, chrome = make_pool(Clock())
        await pool.call("A", "browser_snapshot", {})
        await pool.call("B", "browser_snapshot", {})
        chrome.tabs = {"fresh-blank"}  # Chrome relaunched: every tab is new
        await pool.drop_all()
        await pool.call("A", "browser_snapshot", {})
        return children, pool, chrome

    (a, b, a2), pool, chrome = asyncio.run(scenario())
    assert a.closed and b.closed
    assert chrome.closed == []  # their tabs died with the old browser
    assert pool.threads == ["A"] and pool.target_of("A") == a2.tab


def test_a_hung_call_does_not_wedge_reaping() -> None:
    class Hangs(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> Any:
            if name == "browser_wait_for":
                await asyncio.Event().wait()  # never answers
            return await super().call(name, args)

    async def scenario() -> tuple[list[str], list[Hangs], BrowserPool]:
        clock = Clock()
        pool, children, _ = make_pool(
            clock, child=Hangs, idle_seconds=10, end_lock_wait=0.05
        )
        await pool.call("A", "browser_snapshot", {})
        hung = asyncio.create_task(pool.call("A", "browser_wait_for", {}))
        await asyncio.sleep(0)
        clock.now = 11
        reaped = await asyncio.wait_for(pool.reap_idle(), 2)
        hung.cancel()
        return reaped, children, pool

    reaped, (child,), pool = asyncio.run(scenario())
    assert reaped == ["A"] and child.closed and pool.threads == []


def test_end_does_not_wait_forever_on_a_busy_session() -> None:
    class Hangs(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> Any:
            if name == "browser_wait_for":
                await asyncio.Event().wait()
            return await super().call(name, args)

    async def scenario() -> bool:
        pool, _, _ = make_pool(Clock(), child=Hangs, end_lock_wait=0.05)
        await pool.call("A", "browser_snapshot", {})
        hung = asyncio.create_task(pool.call("A", "browser_wait_for", {}))
        await asyncio.sleep(0)
        ended: bool = await asyncio.wait_for(pool.end("A"), 2)
        hung.cancel()
        return ended

    assert asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("browser_navigate", {"url": "x"}, FORWARD),
        ("browser_tabs", {"action": "list"}, FORWARD),
        ("browser_tabs", {"action": "select", "index": 0}, BLOCKED),
        ("browser_tabs", {"action": "new"}, BLOCKED),
        ("browser_tabs", {"action": "new", "url": "https://x.test"}, BLOCKED),
        ("browser_tabs", {"action": "rename"}, BLOCKED),
        ("browser_tabs", {}, BLOCKED),
        ("browser_tabs", {"action": "close"}, END),
        ("browser_tabs", {"action": "close", "index": 0}, END),
        ("browser_close", {}, END),
    ],
)
def test_route(name: str, args: dict[str, Any], expected: str) -> None:
    # browser_tabs is an allowlist: only "list" reaches a child.
    assert route(name, args) == expected
