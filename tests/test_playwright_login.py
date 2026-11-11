"""The playwright package's remote-login pieces: one-time links, the input
translation from phone gestures to CDP, the bridge between a phone and a tab,
and the viewer's HTTP surface. CDP is faked; no Chrome is launched here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from cdp_input import RESET_VIEWPORT, input_commands, viewport_commands
from chrome_launch import chrome_args
from login_bridge import run_bridge
from login_sessions import SessionStore
from login_viewer import build_app
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- one-time links --------------------------------------------------------


def test_link_claims_once_and_binds_the_claim() -> None:
    store = SessionStore(clock=Clock())
    session = store.mint("T1", "https://x.test/login")
    claim = store.claim(session.token)
    assert claim
    assert store.claim(session.token) is None  # second page load refused
    assert store.check(session.token, claim) is session
    assert store.check(session.token, "forged") is None
    assert store.check("unknown", claim) is None
    assert store.get(session.token) is session
    assert store.get("unknown") is None


def test_link_expires_and_end_invalidates() -> None:
    clock = Clock()
    store = SessionStore(clock=clock, ttl=60)
    a = store.mint("T1", "u")
    b = store.mint("T2", "u")
    claim = store.claim(b.token)
    assert claim
    clock.now += 61
    assert store.claim(a.token) is None
    assert store.check(b.token, claim) is None
    assert {s.target_id for s in store.sweep_expired()} == {"T1", "T2"}
    assert store.sweep_expired() == []  # each expiry reported once
    c = store.mint("T3", "u")
    assert store.end(c.token) is c
    assert store.claim(c.token) is None


def test_tokens_are_long_and_distinct() -> None:
    store = SessionStore(clock=Clock())
    tokens = {store.mint(f"T{i}", "u").token for i in range(50)}
    assert len(tokens) == 50
    assert min(len(t) for t in tokens) >= 32


# --- gesture -> CDP --------------------------------------------------------


def test_tap_is_a_left_click_at_the_point() -> None:
    cmds = input_commands({"t": "click", "x": 10.5, "y": 20})
    assert [c[0] for c in cmds] == ["Input.dispatchMouseEvent"] * 2
    assert [c[1]["type"] for c in cmds] == ["mousePressed", "mouseReleased"]
    assert all(c[1]["x"] == 10.5 and c[1]["y"] == 20 for c in cmds)


def test_text_scroll_and_keys() -> None:
    assert input_commands({"t": "text", "s": "hunter2"}) == [
        ("Input.insertText", {"text": "hunter2"})
    ]
    ((method, params),) = input_commands({"t": "scroll", "x": 1, "y": 2, "dy": 300})
    assert method == "Input.dispatchMouseEvent" and params["type"] == "mouseWheel"
    assert params["deltaY"] == 300
    enter = input_commands({"t": "key", "key": "Enter"})
    assert [p["type"] for _, p in enter] == ["keyDown", "keyUp"]
    assert enter[0][1]["text"] == "\r"  # keyDown with text submits forms
    assert input_commands({"t": "reload"}) == [("Page.reload", {})]


@pytest.mark.parametrize(
    "event",
    [
        {"t": "click", "x": "1", "y": 2},
        {"t": "click", "x": float("nan"), "y": 2},
        {"t": "key", "key": "F13"},
        {"t": "text", "s": "x" * 5000},
        {"t": "Runtime.evaluate", "expression": "1"},
        {"t": "click"},
        "not a mapping",
    ],
)
def test_malformed_or_unknown_events_send_nothing(event: Any) -> None:
    # The phone never gets a path to arbitrary CDP methods.
    assert input_commands(event) == []


def test_viewport_emulates_the_phone_and_clamps() -> None:
    cmds = dict(viewport_commands(390, 844, 3))
    metrics = cmds["Emulation.setDeviceMetricsOverride"]
    assert (metrics["width"], metrics["height"]) == (390, 844)
    assert metrics["deviceScaleFactor"] == 3 and metrics["mobile"] is True
    huge = dict(viewport_commands(99999, -5, 50))["Emulation.setDeviceMetricsOverride"]
    assert huge["width"] <= 2000 and huge["height"] >= 200
    assert huge["deviceScaleFactor"] <= 4
    assert ("Emulation.clearDeviceMetricsOverride", {}) in RESET_VIEWPORT


# --- Chrome launch ---------------------------------------------------------


def test_chrome_args_bind_localhost_and_avoid_keychain_prompts() -> None:
    args = chrome_args(Path("/p/profile"), 9322)
    assert "--remote-debugging-port=9322" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--user-data-dir=/p/profile" in args
    # Real keychain access from a daemon can raise a GUI prompt nobody answers.
    assert "--use-mock-keychain" in args
    # A real window, not headless: Google sign-in refuses HeadlessChrome. The
    # window sits unwatched on the Mac, so rendering must not be throttled
    # when it is hidden or covered, or the phone's stream freezes.
    assert not any(a.startswith("--headless") for a in args)
    for flag in (
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
    ):
        assert flag in args


# --- bridge: phone <-> tab -------------------------------------------------


class FakeCdp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.sent.append((method, params or {}))
        return {}

    async def next_event(self) -> dict[str, Any]:
        return await self.events.get()


class FakePhone:
    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self.inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        for message in inbound:
            self.inbound.put_nowait(message)
        self.outbound: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any] | None:
        return await self.inbound.get()

    async def send(self, message: dict[str, Any]) -> None:
        self.outbound.append(message)


def test_bridge_streams_frames_forwards_input_and_cleans_up() -> None:
    async def scenario() -> tuple[FakeCdp, FakePhone]:
        cdp = FakeCdp()
        phone = FakePhone([{"t": "hello", "w": 390, "h": 844, "dpr": 3}])
        task = asyncio.create_task(run_bridge(cdp, phone))
        await asyncio.sleep(0)
        frame = {
            "method": "Page.screencastFrame",
            "params": {
                "data": "QUJD",
                "sessionId": 7,
                "metadata": {"deviceWidth": 390},
            },
        }
        await cdp.events.put(frame)
        await asyncio.sleep(0.05)  # let the frame pump run before input ends it
        await phone.inbound.put({"t": "click", "x": 5, "y": 6})
        await phone.inbound.put({"t": "done"})
        await asyncio.wait_for(task, 2)
        return cdp, phone

    cdp, phone = asyncio.run(scenario())
    methods = [m for m, _ in cdp.sent]
    assert methods.index("Emulation.setDeviceMetricsOverride") < methods.index(
        "Page.startScreencast"
    )
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in cdp.sent
    assert {
        "t": "frame",
        "data": "QUJD",
        "meta": {"deviceWidth": 390},
    } in phone.outbound
    assert any(p.get("type") == "mousePressed" for m, p in cdp.sent)
    # Teardown: screencast stopped and the phone emulation undone.
    assert "Page.stopScreencast" in methods
    assert methods[-len(RESET_VIEWPORT) :] == [m for m, _ in RESET_VIEWPORT]


def test_bridge_ends_when_the_phone_disconnects() -> None:
    async def scenario() -> FakeCdp:
        cdp = FakeCdp()
        phone = FakePhone([{"t": "hello", "w": 400, "h": 800, "dpr": 2}])
        task = asyncio.create_task(run_bridge(cdp, phone))
        await phone.inbound.put(None)
        await asyncio.wait_for(task, 2)
        return cdp

    assert "Page.stopScreencast" in [m for m, _ in asyncio.run(scenario()).sent]


# --- viewer HTTP surface ---------------------------------------------------


def _client(store: SessionStore, opened: list[str]) -> TestClient:
    async def open_session(token: str, websocket: Any) -> None:
        opened.append(token)
        await websocket.send_json({"t": "ok"})

    return TestClient(build_app(store, open_session))


@pytest.mark.parametrize("prefix", ["", "/browser"])
def test_page_loads_once_then_link_is_spent(prefix: str) -> None:
    store = SessionStore(clock=Clock())
    session = store.mint("T1", "u")
    client = _client(store, [])
    first = client.get(f"{prefix}/s/{session.token}")
    assert first.status_code == 200
    assert "<html" in first.text.lower()
    assert first.headers["cache-control"] == "no-store"
    second = TestClient(client.app).get(f"{prefix}/s/{session.token}")
    assert second.status_code == 410
    assert client.get(f"{prefix}/s/nope").status_code == 410


def test_websocket_requires_the_claim_cookie() -> None:
    store = SessionStore(clock=Clock())
    session = store.mint("T1", "u")
    opened: list[str] = []
    client = _client(store, opened)
    assert client.get(f"/s/{session.token}").status_code == 200
    with client.websocket_connect(f"/s/{session.token}/ws") as ws:
        assert ws.receive_json() == {"t": "ok"}
    assert opened == [session.token]

    stranger = TestClient(client.app)
    with pytest.raises(WebSocketDisconnect) as refused:
        with stranger.websocket_connect(f"/s/{session.token}/ws") as ws:
            ws.receive_json()
    assert refused.value.code == 4403
    assert opened == [session.token]


# --- native-UI suppression -------------------------------------------------
#
# The phone sees only page pixels. Anything Chrome or macOS draws outside the
# page (passkey sheets, "Sign in to Chrome?", save-password bubbles, permission
# prompts) appears on the unwatched Mac and wedges the login.


def test_login_tabs_route_passkeys_to_an_empty_virtual_authenticator() -> None:
    from cdp_input import LOGIN_TAB_SETUP

    assert ("WebAuthn.enable", {"enableUI": False}) in LOGIN_TAB_SETUP


def test_profile_prefs_turn_off_native_prompts_without_clobbering(
    tmp_path: Path,
) -> None:
    import json

    from chrome_launch import apply_profile_prefs

    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir()
    prefs.write_text(json.dumps({"profile": {"name": "chief"}, "keep": 1}))
    apply_profile_prefs(tmp_path)
    data = json.loads(prefs.read_text())
    assert data["keep"] == 1 and data["profile"]["name"] == "chief"
    assert data["signin"]["allowed"] is False
    assert data["credentials_enable_service"] is False
    assert data["profile"]["password_manager_enabled"] is False
    blocked = data["profile"]["default_content_setting_values"]
    assert blocked["notifications"] == 2 and blocked["geolocation"] == 2


def test_profile_prefs_create_a_fresh_profile(tmp_path: Path) -> None:
    import json

    from chrome_launch import apply_profile_prefs

    apply_profile_prefs(tmp_path / "new")
    data = json.loads((tmp_path / "new" / "Default" / "Preferences").read_text())
    assert data["signin"]["allowed"] is False
