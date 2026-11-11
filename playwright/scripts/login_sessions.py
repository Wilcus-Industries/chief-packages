"""One-time login links for the remote browser viewer.

A link is a random token naming one Chrome tab. The first page load *claims*
it: the server hands that browser a second random value (a cookie), and only a
websocket presenting it can drive the tab. A second page load, an expired
token, or an ended session are all refused. Tailnet-only reachability plus a
short-lived, single-claim token is the whole auth story (owner-approved).
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

TTL_SECONDS = 15 * 60


@dataclass
class LoginSession:
    token: str
    target_id: str
    url: str
    expires_at: float
    claim: str | None = None
    ended: bool = False


class SessionStore:
    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl: float = TTL_SECONDS
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._sessions: dict[str, LoginSession] = {}

    def mint(self, target_id: str, url: str) -> LoginSession:
        token = secrets.token_urlsafe(32)
        session = LoginSession(token, target_id, url, self._clock() + self._ttl)
        self._sessions[token] = session
        return session

    def _live(self, token: str) -> LoginSession | None:
        session = self._sessions.get(token)
        if session is None or session.ended or self._clock() >= session.expires_at:
            return None
        return session

    def get(self, token: str) -> LoginSession | None:
        """The live session for ``token`` (no claim check — callers past the
        websocket's claim check use it to find the tab)."""
        return self._live(token)

    def claim(self, token: str) -> str | None:
        """Bind the link to the first browser that opens it; None if spent."""
        session = self._live(token)
        if session is None or session.claim is not None:
            return None
        session.claim = secrets.token_urlsafe(32)
        return session.claim

    def check(self, token: str, claim: str | None) -> LoginSession | None:
        session = self._live(token)
        if session is None or session.claim is None or claim is None:
            return None
        if not hmac.compare_digest(session.claim, claim):
            return None
        return session

    def end(self, token: str) -> LoginSession | None:
        session = self._sessions.get(token)
        if session is None or session.ended:
            return None
        session.ended = True
        return session

    def sweep_expired(self) -> list[LoginSession]:
        """End and return sessions past expiry (each reported once), so the
        host can close their tabs."""
        now = self._clock()
        expired = [
            s for s in self._sessions.values() if not s.ended and now >= s.expires_at
        ]
        for session in expired:
            session.ended = True
        return expired
