"""Per-session browsers: one Playwright child (one tab) per chief session.

All sessions share the Chrome profile (one set of logins) but never a tab. The
pool opens a fresh tab per session, closes it when the session goes idle or
asks to close, caps concurrent sessions, and refuses tab switching. Children
are faked; no Chrome here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from browser_pool import BLOCKED, END, FORWARD, BrowserPool, route


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeChild:
    def __init__(self, n: int) -> None:
        self.n = n
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def call(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return f"child{self.n}:{name}"

    async def close(self) -> None:
        self.closed = True


def make_pool(clock: Clock, **kwargs: Any) -> tuple[BrowserPool, list[FakeChild]]:
    children: list[FakeChild] = []

    async def factory() -> FakeChild:
        child = FakeChild(len(children))
        children.append(child)
        return child

    return BrowserPool(factory, clock=clock, **kwargs), children


NEW_TAB = ("browser_tabs", {"action": "new"})
CLOSE_TAB = ("browser_tabs", {"action": "close"})


def test_each_session_gets_its_own_child_and_fresh_tab() -> None:
    async def scenario() -> list[FakeChild]:
        pool, children = make_pool(Clock())
        assert await pool.call("A", "browser_navigate", {"url": "a"}) == (
            "child0:browser_navigate"
        )
        assert await pool.call("B", "browser_navigate", {"url": "b"}) == (
            "child1:browser_navigate"
        )
        assert await pool.call("A", "browser_snapshot", {}) == "child0:browser_snapshot"
        return children

    a, b = asyncio.run(scenario())
    # The first thing every child does is open its own tab, so it never
    # inherits (and fights over) another session's page.
    assert a.calls[0] == NEW_TAB and b.calls[0] == NEW_TAB
    assert [n for n, _ in a.calls] == [
        "browser_tabs",
        "browser_navigate",
        "browser_snapshot",
    ]


def test_concurrent_first_calls_for_one_session_make_one_child() -> None:
    async def scenario() -> list[FakeChild]:
        pool, children = make_pool(Clock())
        await asyncio.gather(
            pool.call("A", "browser_snapshot", {}),
            pool.call("A", "browser_snapshot", {}),
        )
        return children

    assert len(asyncio.run(scenario())) == 1


def test_idle_sessions_close_their_tab_and_child() -> None:
    async def scenario() -> tuple[list[str], list[FakeChild], BrowserPool]:
        clock = Clock()
        pool, children = make_pool(clock, idle_seconds=600)
        await pool.call("A", "browser_snapshot", {})
        clock.now = 300
        await pool.call("B", "browser_snapshot", {})
        clock.now = 601
        return await pool.reap_idle(), children, pool

    reaped, (a, b), pool = asyncio.run(scenario())
    assert reaped == ["A"]
    assert a.calls[-1] == CLOSE_TAB and a.closed
    assert not b.closed
    assert pool.threads == ["B"]


def test_cap_ends_the_least_recently_used_session() -> None:
    async def scenario() -> tuple[list[FakeChild], BrowserPool]:
        clock = Clock()
        pool, children = make_pool(clock, max_sessions=2)
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
    async def scenario() -> tuple[list[FakeChild], BrowserPool, bool]:
        pool, children = make_pool(Clock())
        await pool.call("A", "browser_snapshot", {})
        await pool.call("B", "browser_snapshot", {})
        ended = await pool.end("A")
        await pool.close_all()
        return children, pool, ended

    (a, b), pool, ended = asyncio.run(scenario())
    assert ended and a.closed and b.closed
    assert a.calls[-1] == CLOSE_TAB and b.calls[-1] == CLOSE_TAB
    assert pool.threads == []


def test_end_survives_a_child_that_fails_to_close_its_tab() -> None:
    class Broken(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> str:
            if args == {"action": "close"}:
                raise RuntimeError("tab already gone")
            return await super().call(name, args)

    async def scenario() -> Broken:
        child = Broken(0)

        async def factory() -> Broken:
            return child

        pool = BrowserPool(factory, clock=Clock())
        await pool.call("A", "browser_snapshot", {})
        await pool.end("A")
        return child

    assert asyncio.run(scenario()).closed


def test_a_crashed_child_is_replaced_on_the_next_call() -> None:
    class Dies(FakeChild):
        async def call(self, name: str, args: dict[str, Any]) -> str:
            if name == "browser_navigate" and self.n == 0:
                raise ConnectionError("child died")
            return await super().call(name, args)

    async def scenario() -> tuple[list[Dies], str]:
        children: list[Dies] = []

        async def factory() -> Dies:
            children.append(Dies(len(children)))
            return children[-1]

        pool = BrowserPool(factory, clock=Clock())
        with pytest.raises(ConnectionError):
            await pool.call("A", "browser_navigate", {"url": "x"})
        return children, await pool.call("A", "browser_navigate", {"url": "x"})

    children, result = asyncio.run(scenario())
    assert children[0].closed
    assert result == "child1:browser_navigate"


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("browser_navigate", {"url": "x"}, FORWARD),
        ("browser_tabs", {"action": "list"}, FORWARD),
        ("browser_tabs", {"action": "select", "index": 0}, BLOCKED),
        ("browser_tabs", {"action": "new"}, BLOCKED),
        ("browser_tabs", {"action": "close"}, END),
        ("browser_tabs", {"action": "close", "index": 0}, END),
        ("browser_close", {}, END),
    ],
)
def test_route(name: str, args: dict[str, Any], expected: str) -> None:
    assert route(name, args) == expected
