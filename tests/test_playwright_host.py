"""The browser host's glue: the MCP surface chief talks to, the login host
behind ``login_link``, orphan-tab reconciliation, and the Chrome launcher.

The MCP server runs in memory against a real ``mcp`` client session, which
sends the ``chief/thread`` request ``_meta`` exactly as core does for a
``session_context: true`` server. The pool, CDP, and Chrome are faked.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import browser_host
import cdp_client
import chrome_launch
import login_host
import pytest
from browser_pool import BLOCKED_MESSAGE, ENDED_MESSAGE, TAB_LOST_MESSAGE, TabLost
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

# --- the MCP surface --------------------------------------------------------


def _tool(name: str) -> types.Tool:
    return types.Tool(name=name, inputSchema={"type": "object"})


TOOLS = [
    _tool(n)
    for n in (
        "browser_navigate",
        "browser_snapshot",
        "browser_tabs",
        "browser_close",
        "browser_run_code_unsafe",
    )
]


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.ended: list[str] = []
        self.lose_tab = False

    async def call(self, thread: str, name: str, args: dict[str, Any]) -> Any:
        if self.lose_tab:
            raise TabLost(TAB_LOST_MESSAGE)
        self.calls.append((thread, name, args))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{thread}:{name}")]
        )

    async def end(self, thread: str) -> bool:
        self.ended.append(thread)
        return True


class FakeLogin:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def login_link(self, url: str) -> str:
        self.urls.append(url)
        return "https://mini.ts.net/browser/s/tok"


Session = Callable[..., Awaitable[types.CallToolResult]]


def _run(
    scenario: Callable[[Any, FakePool, FakeLogin], Awaitable[Any]],
) -> tuple[Any, FakePool, FakeLogin]:
    pool, login = FakePool(), FakeLogin()
    server = browser_host.build_server(pool, login, TOOLS)

    async def go() -> Any:
        async with create_connected_server_and_client_session(server) as client:
            return await scenario(client, pool, login)

    return asyncio.run(go()), pool, login


def _as(thread: str | None) -> dict[str, Any] | None:
    return {"chief/thread": thread} if thread else None


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def test_calls_route_to_the_calling_sessions_tab() -> None:
    async def scenario(client: Any, pool: FakePool, login: FakeLogin) -> list[str]:
        a = await client.call_tool("browser_snapshot", {}, meta=_as("thread-a"))
        b = await client.call_tool("browser_snapshot", {}, meta=_as("thread-b"))
        return [_text(a), _text(b)]

    texts, pool, _ = _run(scenario)
    assert texts == ["thread-a:browser_snapshot", "thread-b:browser_snapshot"]
    assert [c[0] for c in pool.calls] == ["thread-a", "thread-b"]


def test_a_call_without_a_session_is_unscoped_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(client: Any, pool: FakePool, login: FakeLogin) -> None:
        await client.call_tool("browser_snapshot", {})

    with caplog.at_level(logging.WARNING, logger="playwright-host"):
        _, pool, _ = _run(scenario)
    assert pool.calls[0][0] == browser_host.UNSCOPED
    assert "session_context" in caplog.text


def test_tab_switching_is_blocked_and_close_ends_only_this_session() -> None:
    async def scenario(
        client: Any, pool: FakePool, login: FakeLogin
    ) -> list[types.CallToolResult]:
        return [
            await client.call_tool(
                "browser_tabs", {"action": "select", "index": 0}, meta=_as("A")
            ),
            await client.call_tool("browser_tabs", {"action": "new"}, meta=_as("A")),
            await client.call_tool(
                "browser_tabs", {"action": "close", "index": 3}, meta=_as("A")
            ),
            await client.call_tool("browser_close", {}, meta=_as("B")),
            await client.call_tool("browser_tabs", {"action": "list"}, meta=_as("A")),
        ]

    (select, new, close, browser_close, listing), pool, _ = _run(scenario)
    assert select.isError and _text(select) == BLOCKED_MESSAGE
    assert new.isError and _text(new) == BLOCKED_MESSAGE
    assert _text(close) == ENDED_MESSAGE and _text(browser_close) == ENDED_MESSAGE
    assert pool.ended == ["A", "B"]
    assert _text(listing) == "A:browser_tabs"
    assert [c[1:] for c in pool.calls] == [("browser_tabs", {"action": "list"})]


def test_never_tools_are_hidden_and_refused() -> None:
    async def scenario(
        client: Any, pool: FakePool, login: FakeLogin
    ) -> tuple[list[str], types.CallToolResult, types.CallToolResult]:
        names = [t.name for t in (await client.list_tools()).tools]
        unsafe = await client.call_tool(
            "browser_run_code_unsafe", {"code": "async () => 1"}, meta=_as("A")
        )
        unknown = await client.call_tool("browser_install", {}, meta=_as("A"))
        return names, unsafe, unknown

    (names, unsafe, unknown), pool, _ = _run(scenario)
    assert "browser_run_code_unsafe" not in names
    assert {"browser_navigate", "login_link"} <= set(names)
    assert unsafe.isError and "not available" in _text(unsafe)
    assert unknown.isError
    assert pool.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "chrome://quit",
        "http://127.0.0.1:9322/json/close/T1",
        "localhost:9322/json",
        "data:text/html,hi",
    ],
)
def test_navigation_outside_the_web_is_refused(url: str) -> None:
    async def scenario(
        client: Any, pool: FakePool, login: FakeLogin
    ) -> types.CallToolResult:
        result: types.CallToolResult = await client.call_tool(
            "browser_navigate", {"url": url}, meta=_as("A")
        )
        return result

    result, pool, _ = _run(scenario)
    assert result.isError and _text(result).startswith("error: url")
    assert pool.calls == []


def test_navigation_forwards_the_url_the_server_would_load() -> None:
    async def scenario(client: Any, pool: FakePool, login: FakeLogin) -> None:
        await client.call_tool(
            "browser_navigate", {"url": "example.com/x"}, meta=_as("A")
        )

    _, pool, _ = _run(scenario)
    assert pool.calls == [("A", "browser_navigate", {"url": "https://example.com/x"})]


def test_login_link_and_a_lost_tab() -> None:
    async def scenario(
        client: Any, pool: FakePool, login: FakeLogin
    ) -> tuple[types.CallToolResult, types.CallToolResult]:
        link = await client.call_tool(
            "login_link", {"url": "https://x.test/login"}, meta=_as("A")
        )
        pool.lose_tab = True
        lost = await client.call_tool("browser_snapshot", {}, meta=_as("A"))
        return link, lost

    (link, lost), _, login = _run(scenario)
    assert not link.isError and login.urls == ["https://x.test/login"]
    assert lost.isError and _text(lost) == TAB_LOST_MESSAGE


# --- the login host ---------------------------------------------------------


class FakeTabCdp:
    """One login tab's CDP connection."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.sent.append((method, params or {}))
        return {}

    async def next_event(self) -> dict[str, Any]:
        return await self.events.get()

    async def close(self) -> None:
        self.closed = True


class FakeSocket:
    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        for message in inbound:
            self.inbound.put_nowait(message)
        self.outbound: list[dict[str, Any]] = []
        self.closed_with: int | None = None

    async def receive_json(self) -> Any:
        message = await self.inbound.get()
        if message is None:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect()
        return message

    async def send_json(self, message: dict[str, Any]) -> None:
        self.outbound.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


@pytest.fixture
def fake_cdp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"created": [], "closed": [], "conn": FakeTabCdp()}

    async def create_tab(port: int, url: str) -> str:
        state["created"].append(url)
        return "LOGIN1"

    async def close_tab(port: int, target_id: str) -> None:
        state["closed"].append(target_id)

    async def page_ws_url(port: int, target_id: str) -> str:
        return f"ws://127.0.0.1:{port}/devtools/page/{target_id}"

    async def open_conn(url: str) -> FakeTabCdp:
        conn: FakeTabCdp = state["conn"]
        return conn

    monkeypatch.setattr(login_host, "create_tab", create_tab)
    monkeypatch.setattr(login_host, "close_tab", close_tab)
    monkeypatch.setattr(login_host, "page_ws_url", page_ws_url)
    monkeypatch.setattr(login_host.CdpConnection, "open", staticmethod(open_conn))
    return state


def _login_host() -> tuple[login_host.LoginHost, list[int]]:
    ensured: list[int] = []

    async def ensure() -> bool:
        ensured.append(1)
        return False

    return login_host.LoginHost(9699, "https://mini.ts.net/browser/", ensure), ensured


def test_login_link_refuses_non_web_urls_without_opening_a_tab(
    fake_cdp: dict[str, Any],
) -> None:
    host, ensured = _login_host()
    for url in ("javascript:alert(1)", "http://127.0.0.1:9322/json", "file:///x"):
        assert asyncio.run(host.login_link(url)).startswith("error: url")
    assert fake_cdp["created"] == [] and ensured == []


def test_login_link_opens_blank_blocks_passkeys_then_navigates(
    fake_cdp: dict[str, Any],
) -> None:
    host, ensured = _login_host()
    text = asyncio.run(host.login_link("https://x.test/login"))
    assert text.startswith("https://mini.ts.net/browser/s/")
    assert ensured == [1] and fake_cdp["created"] == ["about:blank"]
    methods = [m for m, _ in fake_cdp["conn"].sent]
    assert methods.index("WebAuthn.enable") < methods.index("Page.navigate")
    assert ("Page.navigate", {"url": "https://x.test/login"}) in fake_cdp["conn"].sent


def test_a_new_socket_replaces_the_old_bridge_after_its_teardown(
    fake_cdp: dict[str, Any],
) -> None:
    """A zombie socket's bridge is stopped (and its teardown finished) before
    the new socket's bridge starts — so that teardown can never stop the live
    screencast or reset the live phone's viewport."""
    hello = {"t": "hello", "w": 390, "h": 844, "dpr": 3}

    async def scenario() -> tuple[FakeTabCdp, FakeSocket, FakeSocket, list[str]]:
        host, _ = _login_host()
        await host.login_link("https://x.test/login")
        (token,) = list(host._tabs)
        conn: FakeTabCdp = fake_cdp["conn"]
        old, new = FakeSocket([hello]), FakeSocket([hello])
        first = asyncio.create_task(host.open_session(token, old))
        await asyncio.sleep(0.05)
        mark = len(conn.sent)
        second = asyncio.create_task(host.open_session(token, new))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(first, 2)  # the old handler returns
        after = [m for m, _ in conn.sent[mark:]]
        await new.inbound.put({"t": "done"})
        await asyncio.wait_for(second, 2)
        return conn, old, new, after

    conn, old, new, after = asyncio.run(scenario())
    # The old bridge tore down first, then the new one started its screencast.
    assert after.index("Page.stopScreencast") < after.index("Page.startScreencast")
    last_stop = max(i for i, m in enumerate(after) if m == "Page.stopScreencast")
    last_start = max(i for i, m in enumerate(after) if m == "Page.startScreencast")
    assert last_stop < last_start  # nothing stopped the live stream afterwards
    assert {"t": "ended"} in new.outbound and {"t": "ended"} not in old.outbound
    assert fake_cdp["closed"] == ["LOGIN1"]


def test_sweep_keeps_running_when_a_round_fails(
    fake_cdp: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    host, _ = _login_host()
    rounds: list[int] = []

    def boom() -> list[Any]:
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("transient")
        return []

    host.store.sweep_expired = boom

    async def scenario() -> None:
        task = asyncio.create_task(host.sweep_expired(every=0.01))
        await asyncio.sleep(0.1)
        task.cancel()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert len(rounds) >= 2 and "login sweep failed" in caplog.text


# --- orphan tabs at startup ---------------------------------------------------


def _patch_targets(
    monkeypatch: pytest.MonkeyPatch, targets: list[dict[str, Any]]
) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {"closed": [], "created": []}

    async def fake_json(port: int, path: str) -> Any:
        assert path == "/json/list"
        return targets

    async def fake_close(port: int, target_id: str) -> None:
        seen["closed"].append(target_id)

    async def fake_create(port: int, url: str) -> str:
        seen["created"].append(url)
        return "NEWBLANK"

    monkeypatch.setattr(cdp_client, "_json", fake_json)
    monkeypatch.setattr(cdp_client, "close_tab", fake_close)
    monkeypatch.setattr(cdp_client, "create_tab", fake_create)
    return seen


def test_startup_closes_every_tab_but_one_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_targets(
        monkeypatch,
        [
            {"id": "S1", "type": "page", "url": "https://mail.test/inbox"},
            {"id": "B1", "type": "page", "url": "about:blank"},
            {"id": "W1", "type": "service_worker", "url": "https://x/sw.js"},
            {"id": "L1", "type": "page", "url": "https://bank.test/login"},
            {"id": "B2", "type": "page", "url": "about:blank"},
        ],
    )
    assert asyncio.run(cdp_client.reconcile_tabs(9699)) == 3
    assert seen["closed"] == ["S1", "L1", "B2"] and seen["created"] == []


def test_startup_keeps_a_fresh_blank_when_none_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_targets(
        monkeypatch, [{"id": "S1", "type": "page", "url": "https://x.test"}]
    )
    assert asyncio.run(cdp_client.reconcile_tabs(9699)) == 1
    assert seen["created"] == ["about:blank"] and seen["closed"] == ["S1"]


def test_page_ids_lists_only_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_targets(
        monkeypatch,
        [
            {"id": "P1", "type": "page", "url": "about:blank"},
            {"id": "W1", "type": "service_worker", "url": "x"},
        ],
    )
    assert asyncio.run(cdp_client.page_ids(9699)) == {"P1"}


# --- Chrome launch ------------------------------------------------------------


def test_concurrent_ensure_launches_chrome_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    up = {"value": False}
    launches: list[list[str]] = []

    async def cdp_up(port: int) -> bool:
        await asyncio.sleep(0)  # yield, as a real HTTP probe would
        return up["value"]

    def launch(args: list[str]) -> None:
        launches.append(args)
        up["value"] = True

    monkeypatch.setattr(chrome_launch, "cdp_up", cdp_up)
    monkeypatch.setattr(chrome_launch, "chrome_binary", lambda: "chrome")

    async def scenario() -> list[bool]:
        results: list[bool] = await asyncio.gather(
            *(chrome_launch.ensure_chrome(tmp_path, 9699, launch=launch) for _ in "abc")
        )
        return results

    results = asyncio.run(scenario())
    assert len(launches) == 1
    # Exactly one caller relaunched; the rest found it up.
    assert sorted(results) == [False, False, True]
    assert asyncio.run(chrome_launch.ensure_chrome(tmp_path, 9699, launch=launch)) is (
        False
    )


def test_keep_alive_survives_any_failure(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    async def ensure() -> bool:
        calls.append(1)
        raise OSError("disk full writing Preferences")

    async def scenario() -> None:
        task = asyncio.create_task(chrome_launch.keep_chrome_alive(ensure, every=0.01))
        await asyncio.sleep(0.1)
        task.cancel()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert len(calls) >= 2


def test_reaper_survives_any_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class Pool:
        async def reap_idle(self) -> list[str]:
            calls.append(1)
            raise RuntimeError("cdp down")

    monkeypatch.setattr(browser_host, "REAP_EVERY_SECONDS", 0.01)

    async def scenario() -> None:
        task = asyncio.create_task(browser_host._reap_idle(Pool()))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(scenario())
    assert len(calls) >= 2
