"""Broken-auth fails loud and stays down (slice #9).

Central mechanism under test: a Google Workspace tool result carrying a
broken-credential signal (revoked / expired / insufficient scope, or an exhausted
quota) is turned into a **visible** error while the tool stays **registered** —
never a silent disappearance. Default loud; configurable to quiet, but never
silent.

Two layers:

- The pure mapping (``classify_failure`` / ``credential_failure_note`` /
  ``parse_mode``), unit-tested against a revoked-credential fixture. No core
  needed.
- The real wiring through core's ``post_tool`` seam: registering the amplifier
  via the actual ``PackageHookRegistrar`` and running a fixture tool result
  through the real ``run_post_tool`` proves the model-facing text carries the
  loud re-auth notice AND keeps the original error (the tool did not vanish).
  Skips loudly without a core checkout, like the manifest / read-only-guard
  tests.

Mock-debt (#10): the broken-credential *signals* matched here are fixtures
modeled on google-auth / googleapiclient / upstream error shapes; confirming a
LIVE revoked Google token produces a matching string needs the running server +
a real account, deferred to #10.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import auth_failure as af
import pytest
from auth_failure import (
    DEFAULT_MODE,
    MCP_SERVER_NAME,
    AuthFailureMode,
    FailureKind,
    classify_failure,
    credential_failure_note,
    is_credential_failure,
    is_google_workspace_tool,
    parse_mode,
)

# A revoked-refresh-token error, shaped like google-auth's RefreshError.
REVOKED_CREDENTIAL_ERROR = (
    "google.auth.exceptions.RefreshError: ('invalid_grant: Token has been "
    "expired or revoked.', {'error': 'invalid_grant', 'error_description': "
    "'Token has been expired or revoked.'})"
)

INSUFFICIENT_SCOPE_ERROR = (
    "<HttpError 403 ... 'Request had insufficient authentication scopes.'>"
)

QUOTA_ERROR = (
    "<HttpError 403 ... 'reason': 'rateLimitExceeded', 'message': 'Rate Limit "
    "Exceeded'>"
)

CLEAN_RESULT = "Message sent. id=18f2c0a1b2c3d4e5, threadId=18f2c0a1b2c3d4e5"


# --------------------------------------------------------------------------- #
# The server-name pin must match tool_gate                                    #
# --------------------------------------------------------------------------- #


def test_mcp_server_name_matches_tool_gate() -> None:
    """The amplifier keys on the same ``mcp_<server>_`` prefix core registers
    Google tools under — a drift from tool_gate would make it miss every result."""
    from tool_gate import MCP_SERVER_NAME as GATE_NAME

    assert MCP_SERVER_NAME == GATE_NAME


def test_is_google_workspace_tool() -> None:
    assert is_google_workspace_tool("mcp_google-workspace_send_gmail_message")
    assert not is_google_workspace_tool("mcp_other_send")
    assert not is_google_workspace_tool("read_file")


# --------------------------------------------------------------------------- #
# Classification (deterministic)                                              #
# --------------------------------------------------------------------------- #


def test_revoked_credential_classifies_as_auth() -> None:
    assert classify_failure(REVOKED_CREDENTIAL_ERROR) is FailureKind.AUTH
    assert is_credential_failure(REVOKED_CREDENTIAL_ERROR)


def test_insufficient_scope_classifies_as_auth() -> None:
    assert classify_failure(INSUFFICIENT_SCOPE_ERROR) is FailureKind.AUTH


def test_quota_classifies_as_quota() -> None:
    assert classify_failure(QUOTA_ERROR) is FailureKind.QUOTA


def test_clean_result_is_not_a_failure() -> None:
    assert classify_failure(CLEAN_RESULT) is None
    assert not is_credential_failure(CLEAN_RESULT)
    assert classify_failure("") is None


def test_bare_403_is_not_a_credential_failure() -> None:
    """A 403 on one Drive file is a per-object permission, not a broken
    credential — it must not be amplified as a re-auth prompt."""
    per_file = "<HttpError 403 ... 'The user does not have permission for file X'>"
    assert classify_failure(per_file) is None


# --------------------------------------------------------------------------- #
# Mode parsing — default loud, never silently quiet                           #
# --------------------------------------------------------------------------- #


def test_default_mode_is_loud() -> None:
    assert DEFAULT_MODE is AuthFailureMode.LOUD


def test_parse_mode() -> None:
    assert parse_mode("loud") is AuthFailureMode.LOUD
    assert parse_mode("QUIET") is AuthFailureMode.QUIET
    assert parse_mode(" Quiet ") is AuthFailureMode.QUIET
    # Anything unrecognized falls back to loud, never silently to quiet.
    assert parse_mode("whisper") is AuthFailureMode.LOUD
    assert parse_mode("") is AuthFailureMode.LOUD
    assert parse_mode(None) is AuthFailureMode.LOUD
    assert parse_mode(123) is AuthFailureMode.LOUD


# --------------------------------------------------------------------------- #
# The visible notice — non-empty in every mode                                #
# --------------------------------------------------------------------------- #


def test_loud_auth_notice_names_account_and_reauth() -> None:
    note = credential_failure_note(
        REVOKED_CREDENTIAL_ERROR, AuthFailureMode.LOUD, account="me@company.com"
    )
    assert note is not None
    assert "me@company.com" in note
    assert "oauth_consent.py" in note
    assert "STILL REGISTERED" in note  # states the capability is not gone


def test_quiet_auth_notice_still_visible() -> None:
    """Quiet is terser but NEVER silent — it still surfaces a visible error and
    still points at re-auth."""
    note = credential_failure_note(
        REVOKED_CREDENTIAL_ERROR, AuthFailureMode.QUIET, account="me@company.com"
    )
    assert note is not None and note.strip()
    assert "me@company.com" in note
    assert "oauth_consent.py" in note


def test_quota_notice_does_not_say_reauth() -> None:
    loud = credential_failure_note(QUOTA_ERROR, AuthFailureMode.LOUD)
    assert loud is not None
    assert "QUOTA" in loud.upper()
    assert "oauth_consent.py" not in loud  # quota != re-consent


def test_note_is_none_for_a_clean_result() -> None:
    assert credential_failure_note(CLEAN_RESULT, AuthFailureMode.LOUD) is None


# --------------------------------------------------------------------------- #
# Real wiring through core's post_tool seam                                    #
# --------------------------------------------------------------------------- #

_SKIP = (
    "chief core checkout not found (set $CHIEF_CORE or clone next to "
    "chief-packages) — cannot run the REAL post_tool seam; on a dev machine with "
    "core checked out this must actually run, not skip"
)


def _run_amplifier(
    core_available: bool, tool: str, result: str, *, config: dict[str, Any]
) -> str:
    """Register the amplifier via the REAL registrar, run one result through the
    REAL ``run_post_tool`` seam, and return the model-facing text."""
    import asyncio

    from chief.hooks.posttool import run_post_tool
    from chief.hooks.registry import HookRegistry, PackageHookRegistrar
    from chief.provider.base import ToolCall

    registry = HookRegistry()
    context = SimpleNamespace(
        config=config, logger=logging.getLogger("test.auth_failure")
    )
    af.register(context, PackageHookRegistrar(registry, "google-workspace"))

    call = ToolCall(id="1", name=tool, arguments={"account": "me@company.com"})
    # ``run_post_tool`` is typed Any (core is under ignore_missing_imports); bind
    # to a str-annotated local so the helper's declared return type holds.
    text: str = asyncio.run(
        run_post_tool(
            registry.post_tool(),
            call,
            result,
            timeout=5.0,
            logger=logging.getLogger("test.posttool"),
        )
    )
    return text


def test_seam_default_loud_amplifies_revoked_credential(
    core_available: bool,
) -> None:
    """Acceptance: a revoked credential on a Google tool errors visibly (loud by
    default) and the original error is KEPT — the tool did not vanish."""
    if not core_available:
        pytest.skip(_SKIP)
    # No config -> default loud.
    text = _run_amplifier(
        core_available,
        "mcp_google-workspace_send_gmail_message",
        REVOKED_CREDENTIAL_ERROR,
        config={},
    )
    assert "AUTHORIZATION IS BROKEN" in text  # loud marker
    assert "me@company.com" in text
    assert "invalid_grant" in text  # original error kept below the notice


def test_seam_quiet_mode_still_surfaces_the_error(core_available: bool) -> None:
    if not core_available:
        pytest.skip(_SKIP)
    text = _run_amplifier(
        core_available,
        "mcp_google-workspace_send_gmail_message",
        REVOKED_CREDENTIAL_ERROR,
        config={"google_workspace": {"auth_failure_mode": "quiet"}},
    )
    assert "authorization broken" in text.lower()  # terse but present
    assert "invalid_grant" in text  # still keeps the original error


def test_seam_clean_result_passes_through_unchanged(core_available: bool) -> None:
    if not core_available:
        pytest.skip(_SKIP)
    text = _run_amplifier(
        core_available,
        "mcp_google-workspace_send_gmail_message",
        CLEAN_RESULT,
        config={},
    )
    assert text == CLEAN_RESULT  # a healthy result is not touched


def test_seam_ignores_non_google_tools(core_available: bool) -> None:
    if not core_available:
        pytest.skip(_SKIP)
    # An invalid_grant string from some OTHER tool must not be amplified.
    text = _run_amplifier(
        core_available,
        "mcp_other-server_do_thing",
        REVOKED_CREDENTIAL_ERROR,
        config={},
    )
    assert text == REVOKED_CREDENTIAL_ERROR
