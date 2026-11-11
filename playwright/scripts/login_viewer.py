"""The phone-facing viewer: one page per login link plus its websocket.

Served on 127.0.0.1 and reached through ``tailscale serve --set-path
/browser``, so routes answer with and without that prefix. The page is a live
screencast of one tab; taps, scrolls, and typing go back over the websocket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from login_sessions import SessionStore
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

OpenSession = Callable[[str, WebSocket], Awaitable[None]]

PAGE = (Path(__file__).parent / "login_viewer.html").read_text()
SPENT = (
    "<!doctype html><meta name=viewport content='width=device-width'>"
    "<body style='font:16px system-ui;padding:24px'>"
    "<p>This login link was already used or has expired.</p>"
    "<p>Ask chief for a new one.</p>"
)
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def cookie_name(token: str) -> str:
    return f"bl_{token[:16]}"


def build_app(
    store: SessionStore, open_session: OpenSession, secure_cookie: bool = False
) -> Starlette:
    async def page(request: Request) -> Response:
        token = request.path_params["token"]
        name = cookie_name(token)
        if store.check(token, request.cookies.get(name)) is not None:
            return HTMLResponse(PAGE, headers=NO_STORE)  # same phone, reload
        claim = store.claim(token)
        if claim is None:
            return HTMLResponse(SPENT, status_code=410, headers=NO_STORE)
        response = HTMLResponse(PAGE, headers=NO_STORE)
        response.set_cookie(
            name, claim, httponly=True, samesite="strict", secure=secure_cookie
        )
        return response

    async def socket(websocket: WebSocket) -> None:
        token = websocket.path_params["token"]
        if store.check(token, websocket.cookies.get(cookie_name(token))) is None:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        await open_session(token, websocket)

    routes: list[Route | WebSocketRoute] = []
    for prefix in ("", "/browser"):
        routes.append(Route(f"{prefix}/s/{{token}}", page))
        routes.append(WebSocketRoute(f"{prefix}/s/{{token}}/ws", socket))
    return Starlette(routes=routes)
