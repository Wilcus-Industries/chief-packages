"""Translate the phone viewer's gestures into CDP commands — and nothing else.

The phone is an untrusted-ish client (anyone holding a live link), so it never
names a CDP method: it sends a small vocabulary of events, each validated and
mapped here to a fixed command list. Anything malformed or unknown maps to [].
"""

from __future__ import annotations

import math
from typing import Any

Command = tuple[str, dict[str, Any]]

MAX_TEXT = 1000

# key name -> (code, windowsVirtualKeyCode, text sent on keyDown)
_KEYS: dict[str, tuple[str, int, str | None]] = {
    "Enter": ("Enter", 13, "\r"),
    "Backspace": ("Backspace", 8, None),
    "Tab": ("Tab", 9, None),
    "Escape": ("Escape", 27, None),
    "ArrowLeft": ("ArrowLeft", 37, None),
    "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None),
    "ArrowDown": ("ArrowDown", 40, None),
}

# Run on a login tab's CDP session for its whole life. Passkey requests go to
# an empty virtual authenticator instead of the macOS passkey sheet / iCloud
# Keychain / Bluetooth prompts the phone can't see; the request fails and the
# site offers its in-page fallback ("Try another way").
LOGIN_TAB_SETUP: list[Command] = [("WebAuthn.enable", {"enableUI": False})]

RESET_VIEWPORT: list[Command] = [
    ("Emulation.clearDeviceMetricsOverride", {}),
    ("Emulation.setTouchEmulationEnabled", {"enabled": False}),
]


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _point(event: dict[str, Any]) -> tuple[float, float] | None:
    x, y = _num(event.get("x")), _num(event.get("y"))
    return None if x is None or y is None else (x, y)


def _mouse(kind: str, x: float, y: float, **extra: Any) -> Command:
    return ("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **extra})


def _key(name: str) -> list[Command]:
    code, vk, text = _KEYS[name]
    down: dict[str, Any] = {
        "type": "keyDown",
        "key": name,
        "code": code,
        "windowsVirtualKeyCode": vk,
    }
    if text is not None:
        down["text"] = text
    up = {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": vk}
    return [("Input.dispatchKeyEvent", down), ("Input.dispatchKeyEvent", up)]


def input_commands(event: object) -> list[Command]:
    if not isinstance(event, dict):
        return []
    kind = event.get("t")
    if kind == "click":
        point = _point(event)
        if point is None:
            return []
        btn = {"button": "left", "clickCount": 1}
        return [
            _mouse("mousePressed", *point, **btn),
            _mouse("mouseReleased", *point, **btn),
        ]
    if kind == "scroll":
        point, dy = _point(event), _num(event.get("dy"))
        if point is None or dy is None:
            return []
        return [_mouse("mouseWheel", *point, deltaX=0, deltaY=dy)]
    if kind == "text":
        text = event.get("s")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            return []
        return [("Input.insertText", {"text": text})]
    if kind == "key":
        name = event.get("key")
        return _key(name) if isinstance(name, str) and name in _KEYS else []
    if kind == "reload":
        return [("Page.reload", {})]
    if kind == "back":
        return [("Runtime.evaluate", {"expression": "history.back()"})]
    return []


def _clamp(value: object, low: float, high: float, default: float) -> float:
    number = _num(value)
    return default if number is None else min(max(number, low), high)


def viewport_commands(width: object, height: object, dpr: object) -> list[Command]:
    """Render the tab at the phone's size so the page lays out for it."""
    metrics = {
        "width": int(_clamp(width, 200, 2000, 390)),
        "height": int(_clamp(height, 200, 2000, 844)),
        "deviceScaleFactor": _clamp(dpr, 1, 4, 2),
        "mobile": True,
    }
    return [
        ("Emulation.setDeviceMetricsOverride", metrics),
        ("Emulation.setTouchEmulationEnabled", {"enabled": True}),
    ]
