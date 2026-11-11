"""Launch and keep alive the one Chrome chief and the owner's phone share.

A real (headed) Chrome window on the persistent chief-only profile, CDP on
localhost only. Not headless: Google sign-in refuses HeadlessChrome, and the
owner explicitly chose a visible window on the Mac to make Google logins work.
It runs in its own session so a browser host restart (every chief
restart) reattaches to it instead of killing the owner's logged-in browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chrome_binary() -> str:
    if sys.platform == "darwin":
        return MAC_CHROME
    found = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if found is None:
        raise RuntimeError("Google Chrome not found")
    return found


# Profile prefs that switch off UI Chrome draws outside the page — invisible
# to the phone, so each one would wedge a login on the unwatched Mac. 2=block.
PROFILE_PREFS: dict[str, Any] = {
    "signin": {"allowed": False},  # no "Sign in to Chrome?" intercept
    "credentials_enable_service": False,  # no "Save password?" bubble
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
    "profile": {
        "password_manager_enabled": False,
        "default_content_setting_values": {
            "notifications": 2,
            "geolocation": 2,
            "media_stream_camera": 2,
            "media_stream_mic": 2,
        },
    },
}


def _merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def apply_profile_prefs(profile: Path) -> None:
    """Merge PROFILE_PREFS into the profile's Preferences. Only while Chrome is
    not running — Chrome rewrites the file from memory on exit."""
    path = profile / "Default" / "Preferences"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    path.write_text(json.dumps(_merge(current, PROFILE_PREFS)))


def chrome_args(profile: Path, port: int) -> list[str]:
    return [
        # The window sits unwatched on the Mac: keep rendering when it is
        # hidden or covered, or the phone's screencast freezes.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        # Real keychain access from a daemon can raise a GUI prompt nobody is
        # there to answer; Playwright-launched Chrome used the mock one too.
        "--use-mock-keychain",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]


async def cdp_up(port: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"http://127.0.0.1:{port}/json/version")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _popen(args: list[str]) -> None:
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# Every caller (the keep-alive loop, a session's first call, login_link) goes
# through here; one at a time, so only one Chrome launch and one Preferences
# write ever happen together. (Per event loop: an asyncio.Lock binds to one.)
_launch_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _launch_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _launch_locks.get(loop)
    if lock is None:
        lock = _launch_locks[loop] = asyncio.Lock()
    return lock


async def ensure_chrome(
    profile: Path,
    port: int,
    wait: float = 20,
    launch: Callable[[list[str]], None] = _popen,
) -> bool:
    """Start Chrome unless one already answers on ``port``. True if this call
    (re)launched it — every tab that existed before is gone."""
    async with _launch_lock():
        if await cdp_up(port):
            return False
        apply_profile_prefs(profile)
        launch([chrome_binary(), *chrome_args(profile, port)])
        deadline = asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < deadline:
            if await cdp_up(port):
                return True
            await asyncio.sleep(0.25)
        raise RuntimeError(f"Chrome did not open CDP on 127.0.0.1:{port}")


async def keep_chrome_alive(
    ensure: Callable[[], Awaitable[object]], every: float = 5
) -> None:
    """Call ``ensure`` every ``every`` seconds forever; a failed round is
    logged and retried, never fatal."""
    while True:
        try:
            await ensure()
        except Exception:
            logger.exception("chrome relaunch failed")
        await asyncio.sleep(every)
