#!/usr/bin/env python3
"""Broken Google-Workspace authorization fails LOUD and stays down (default).

The failure this guards against is a *silent* one: when a refresh token is
revoked, consent expires, or a quota is exhausted, a naive design might drop the
Google tools from the registry (a circuit-breaker) — and then the owner keeps
assuming they still have Gmail/Calendar/Drive that has quietly stopped working.
This package's stance, matching the ``anthropic-oauth`` never-silently-fall-back
rule, is the opposite: the tools **stay registered** and every call **errors
visibly** until the owner re-authorizes.

Mechanism (the offline-buildable central part of #9): a ``post_tool`` hook stands
between a Google Workspace tool returning and the model reading the result. When
the result carries a broken-credential signal (an ``invalid_grant`` /
"expired or revoked" refresh error, an insufficient-scope 401/403, or an
exhausted quota), the hook **annotates** the result with a prominent, actionable
re-auth notice — keeping every byte of the original error below it (an
:class:`Annotate`, never a :class:`Veto` that would withhold it, and never a
deregistration). The tool is still there; the failure is just impossible to miss.

Loud by default, configurable:

    google_workspace.auth_failure_mode: loud | quiet   # default loud

``loud`` prepends the full re-auth playbook; ``quiet`` prepends a terse one-liner.
Neither is silent — even ``quiet`` still surfaces a visible error, because the one
thing this module refuses to do is let a broken capability disappear without a
word. An unrecognized/blank mode falls back to ``loud`` (never accidentally quiet).

Mock-debt (#10): the broken-credential *signals* matched below are modeled on the
error shapes google-auth / googleapiclient / the upstream server emit; confirming
that a **live** revoked Google token actually produces a matching string needs the
running server + a real Google account, which lands with the live integration in
#10. The mapping and the wiring here are real and tested offline against those
fixtures; only the live trigger is deferred.

Import-light on purpose (only stdlib/typing at module load), so the boot hook
loader pays nothing to import it; the ``chief`` seam (:class:`Annotate`) is pulled
in lazily inside the hook, the moment it fires.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chief.hooks.posttool import PostToolVerdict
    from chief.provider.base import ToolCall

# The manifest ``mcp_servers`` key. MUST equal ``tool_gate.MCP_SERVER_NAME`` — the
# ``<server>`` in the ``mcp_<server>_<tool>`` names core registers Google tools
# under; a mismatch would make the amplifier miss every Google result. Duplicated
# (not imported) to keep this hook module free of a sibling-script import at load;
# ``tests/test_google_workspace_auth_failure.py`` pins the two equal.
MCP_SERVER_NAME = "google-workspace"

#: The gated-name prefix a Google Workspace tool result arrives under.
_TOOL_PREFIX = f"mcp_{MCP_SERVER_NAME}_"

#: The config key selecting how loudly a broken-credential result is surfaced.
CONFIG_KEY_AUTH_FAILURE_MODE = "google_workspace.auth_failure_mode"


class AuthFailureMode(Enum):
    """How loudly a broken-credential tool result is surfaced. ``LOUD`` is the
    default and the safe fallback for any unrecognized value."""

    LOUD = "loud"
    QUIET = "quiet"


#: Loud is the default: a capability the owner keeps assuming they have must not
#: fail quietly.
DEFAULT_MODE = AuthFailureMode.LOUD


class FailureKind(Enum):
    """The kind of broken-credential failure, which tailors the notice.

    ``AUTH`` → a revoked/expired/insufficient credential; the fix is re-consent.
    ``QUOTA`` → an exhausted rate limit / quota; the fix is wait or raise the
    quota — *not* re-auth. Both keep the tools registered and erroring visibly.
    """

    AUTH = "auth"
    QUOTA = "quota"


# Broken-credential signals (revoked / expired / insufficient scope / bad token).
# Matched case-insensitively as whole phrases; a bare "401"/"403" is deliberately
# NOT here — a 403 on one Drive file is a per-object permission, not a broken
# credential — so a status code only counts when it co-occurs with one of these.
_AUTH_SIGNALS: tuple[str, ...] = (
    "invalid_grant",
    "token has been expired or revoked",
    "expired or revoked",
    "invalid credentials",
    "invalid authentication credentials",
    "request had insufficient authentication scopes",
    "insufficient authentication scopes",
    "access_token_scope_insufficient",
    "invalid_token",
    "invalid_rapt",
    "rapt_required",
    "reauth related error",
    "unauthorized_client",
    "refresherror",
    "the credentials do not contain the necessary fields",
    "unauthenticated",
)

# Quota / rate-limit exhaustion signals.
_QUOTA_SIGNALS: tuple[str, ...] = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quotaexceeded",
    "dailylimitexceeded",
    "rate limit exceeded",
    "quota exceeded",
    "resource_exhausted",
    "usagelimits",
)

# Compiled once; each alternative is escaped so a signal is a literal phrase.
_AUTH_RE = re.compile("|".join(re.escape(s) for s in _AUTH_SIGNALS), re.IGNORECASE)
_QUOTA_RE = re.compile("|".join(re.escape(s) for s in _QUOTA_SIGNALS), re.IGNORECASE)


def is_google_workspace_tool(tool_name: str) -> bool:
    """True for a tool core registered under this package's MCP server."""
    return tool_name.startswith(_TOOL_PREFIX)


def classify_failure(result: str) -> FailureKind | None:
    """The kind of broken-credential failure a tool result carries, or ``None``.

    Auth (revoked/expired/scope) is checked before quota so a message carrying
    both reads as the auth failure that needs re-consent. Purely deterministic —
    a literal-phrase scan, no model call.
    """
    if not result:
        return None
    if _AUTH_RE.search(result):
        return FailureKind.AUTH
    if _QUOTA_RE.search(result):
        return FailureKind.QUOTA
    return None


def is_credential_failure(result: str) -> bool:
    """True when a tool result carries any broken-credential / quota signal."""
    return classify_failure(result) is not None


def parse_mode(value: Any) -> AuthFailureMode:
    """Parse a config value into a mode. Anything unrecognized — a blank, a typo,
    a non-string — falls back to ``LOUD``, never silently to ``QUIET``: the safe
    default when the setting is unclear is to be loud."""
    if isinstance(value, str):
        try:
            return AuthFailureMode(value.strip().lower())
        except ValueError:
            return DEFAULT_MODE
    return DEFAULT_MODE


def _account_phrase(account: str | None) -> str:
    return f" for {account}" if account else ""


def failure_notice(
    kind: FailureKind, mode: AuthFailureMode, *, account: str | None = None
) -> str:
    """The visible notice prepended above a broken-credential tool result.

    Always non-empty (both modes) and always states the load-bearing invariant:
    the tools are still registered — the capability is not gone, it is broken and
    needs the owner. ``loud`` gives the full re-auth playbook; ``quiet`` a terse
    one-liner. Never silent.
    """
    acct = _account_phrase(account)
    if kind is FailureKind.QUOTA:
        if mode is AuthFailureMode.QUIET:
            return (
                f"⚠ Google Workspace quota/rate limit exhausted{acct} — calls "
                "fail until it resets (tools still registered; not an auth issue)."
            )
        return (
            f"⚠ GOOGLE WORKSPACE QUOTA EXHAUSTED{acct}. The Gmail/Calendar/Drive "
            "tools are still registered, but every call will fail until the rate "
            "limit resets or the quota is raised in Google Cloud Console. This is "
            "NOT an auth failure — do not re-run consent. Tell the owner the "
            "capability is temporarily rate-limited, and do not report an action "
            "as done when the call actually failed."
        )
    # FailureKind.AUTH
    if mode is AuthFailureMode.QUIET:
        return (
            f"⚠ Google Workspace authorization broken{acct} (revoked/expired) — "
            "re-authorize with oauth_consent.py (tools still registered)."
        )
    account_arg = account or "<account>"
    return (
        f"⚠ GOOGLE WORKSPACE AUTHORIZATION IS BROKEN{acct} — the credential is "
        "revoked, expired, or missing a required scope. The Gmail/Calendar/Drive "
        "tools are STILL REGISTERED: the capability is not gone, but every call "
        "will keep failing until the owner re-authorizes. Do NOT tell the owner "
        "an action succeeded when the call failed; surface this and ask them to "
        "re-consent by paste:\n"
        f"  uv run python skills/google-workspace/scripts/oauth_consent.py url "
        f"{account_arg}\n"
        f"  uv run python skills/google-workspace/scripts/oauth_consent.py "
        f'exchange {account_arg} "<pasted redirect>"'
    )


def credential_failure_note(
    result: str,
    mode: AuthFailureMode = DEFAULT_MODE,
    *,
    account: str | None = None,
) -> str | None:
    """The notice to prepend for a broken-credential result, or ``None`` when the
    result carries no such signal. The pure core the ``post_tool`` hook wraps."""
    kind = classify_failure(result)
    if kind is None:
        return None
    return failure_notice(kind, mode, account=account)


def _mode_from_context(context: Any) -> AuthFailureMode:
    """Read ``google_workspace.auth_failure_mode`` off the hook context, defaulting
    to loud. The context's ``config`` is already sliced to this package's
    ``config_keys`` (``{"google_workspace": {...}}``); a missing key ⇒ loud."""
    config = getattr(context, "config", None) or {}
    section = config.get("google_workspace") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return DEFAULT_MODE
    return parse_mode(section.get("auth_failure_mode"))


def register(context: Any, hooks: Any) -> None:
    """Wire the broken-auth ``post_tool`` amplifier. Called by the package's hook
    registrar (the manifest's single ``hooks:`` entry point, shared with the
    inbound-Gmail screen — see ``gmail_screen.register``).

    Reads the loud/quiet mode once at boot; a config change takes effect on the
    next ``restart`` (the same lifecycle every package config key has).
    """
    mode = _mode_from_context(context)

    @hooks.post_tool
    async def amplify(call: ToolCall, result: str) -> PostToolVerdict:
        from chief.hooks.posttool import Annotate

        if not is_google_workspace_tool(call.name):
            return None
        account = None
        args = getattr(call, "arguments", None)
        if isinstance(args, dict):
            acct = args.get("account")
            if isinstance(acct, str) and acct.strip():
                account = acct.strip()
        note = credential_failure_note(result, mode, account=account)
        if note is None:
            return None
        return Annotate(note=note)
