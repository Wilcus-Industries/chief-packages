"""Consent-by-paste OAuth + per-account credential storage (slice #8).

The central mechanism under test: a pasted address-bar redirect string ->
authorization-code extraction -> token exchange (against a *fixture* token
endpoint here; the live Google exchange is #10) -> a per-account credential
written to chief's secret store keyed by email address, with 0600 perms.

Pure-Python + a local fixture HTTP server, so these run everywhere (no chief
core checkout needed). The module under test lives at
``google-workspace/scripts/oauth_consent.py``; ``conftest`` puts that dir on
``sys.path`` so it imports here by bare name.
"""

from __future__ import annotations

import http.server
import json
import stat
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import repo_root
from oauth_consent import (
    CORE_SCOPES,
    OAuthClient,
    RedirectParseError,
    TokenExchangeError,
    build_consent_url,
    credential_path,
    exchange_code,
    extract_auth_code,
    load_credential,
    store_credential,
)

SKILL_PATH = (
    repo_root() / "google-workspace" / "skills" / "google-workspace" / "SKILL.md"
)
INSTALL_PATH = repo_root() / "google-workspace" / "INSTALL.md"

CLIENT = OAuthClient(
    client_id="1234.apps.googleusercontent.com",
    client_secret="shh-secret",
    auth_uri="https://accounts.google.com/o/oauth2/auth",
    token_uri="https://oauth2.googleapis.com/token",
    redirect_uri="http://localhost",
)


# --- Consent URL --------------------------------------------------------------


def test_build_consent_url_carries_required_params() -> None:
    """The consent URL is a real Google auth URL: offline access, the account as
    login_hint, and the core Gmail/Calendar/Drive scopes."""
    url = build_consent_url(CLIENT, "me@company.com", state="xyz")
    assert url.startswith(CLIENT.auth_uri + "?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["client_id"] == [CLIENT.client_id]
    assert query["redirect_uri"] == [CLIENT.redirect_uri]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["login_hint"] == ["me@company.com"]
    assert query["state"] == ["xyz"]
    scope = query["scope"][0]
    assert "gmail" in scope and "calendar" in scope and "drive" in scope


def test_core_scopes_cover_the_core_tier_services() -> None:
    joined = " ".join(CORE_SCOPES)
    for service in ("gmail", "calendar", "drive"):
        assert service in joined, f"core scopes must cover {service}"


# --- Paste parsing ------------------------------------------------------------


def test_extract_auth_code_from_pasted_redirect() -> None:
    """The owner pastes the whole address-bar string; the code is pulled out."""
    pasted = "http://localhost/?code=4/0AY0e-abc123&scope=https://mail.google.com/"
    assert extract_auth_code(pasted) == "4/0AY0e-abc123"


def test_extract_auth_code_tolerates_surrounding_whitespace() -> None:
    pasted = "   http://localhost/?state=z&code=abc%2Fdef&scope=x   \n"
    assert extract_auth_code(pasted) == "abc/def"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not a url at all",
        "http://localhost/?scope=x",  # no code param
        "http://localhost/?error=access_denied",  # owner declined consent
        "http://localhost/?code=",  # empty code
    ],
)
def test_malformed_paste_rejected_with_clear_error(bad: str) -> None:
    with pytest.raises(RedirectParseError):
        extract_auth_code(bad)


def test_consent_denied_names_the_error() -> None:
    with pytest.raises(RedirectParseError, match="access_denied"):
        extract_auth_code("http://localhost/?error=access_denied")


# --- Token exchange against a fixture endpoint --------------------------------


class _FixtureToken(http.server.BaseHTTPRequestHandler):
    """Stand-in for Google's token endpoint. Records the last posted form and
    replies with a canned credential, or a 400 for a sentinel bad code."""

    last_form: dict[str, list[str]] = {}

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        type(self).last_form = urllib.parse.parse_qs(body)
        if type(self).last_form.get("code") == ["bad-code"]:
            payload = json.dumps(
                {"error": "invalid_grant", "error_description": "bad code"}
            ).encode()
            self.send_response(400)
        else:
            payload = json.dumps(
                {
                    "access_token": "ya29.access",
                    "refresh_token": "1//refresh",
                    "expires_in": 3599,
                    "scope": " ".join(CORE_SCOPES),
                    "token_type": "Bearer",
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence test noise
        return


@pytest.fixture
def token_endpoint() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _FixtureToken)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/token"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_exchange_code_against_fixture_returns_token(token_endpoint: str) -> None:
    token = exchange_code(CLIENT, "4/0Aauth", token_endpoint=token_endpoint)
    assert token["access_token"] == "ya29.access"
    assert token["refresh_token"] == "1//refresh"
    # The real OAuth2 grant was posted: the code, our client, and the grant type.
    form = _FixtureToken.last_form
    assert form["code"] == ["4/0Aauth"]
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == [CLIENT.client_id]
    assert form["client_secret"] == [CLIENT.client_secret]


def test_exchange_error_is_raised_loud(token_endpoint: str) -> None:
    with pytest.raises(TokenExchangeError):
        exchange_code(CLIENT, "bad-code", token_endpoint=token_endpoint)


# --- Per-account credential storage -------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_store_credential_keyed_by_email_with_0600(tmp_path: Path) -> None:
    token = {
        "access_token": "ya29.a",
        "refresh_token": "1//r",
        "expires_in": 3599,
        "scope": " ".join(CORE_SCOPES),
    }
    creds = tmp_path / "google_tokens"
    path = store_credential("me@company.com", token, CLIENT, credentials_dir=creds)

    # Keyed by the exact email address.
    assert path == creds / "me@company.com.json"
    assert path == credential_path("me@company.com", creds)

    # Restrictive perms: 0600 on the file, 0700 on the dir (these tokens send
    # mail as the owner).
    assert _mode(path) == 0o600, oct(_mode(path))
    assert _mode(creds) == 0o700, oct(_mode(creds))

    # Authorized-user shape the upstream server reads (from_authorized_user_*).
    data = json.loads(path.read_text())
    assert data["refresh_token"] == "1//r"
    assert data["client_id"] == CLIENT.client_id
    assert data["client_secret"] == CLIENT.client_secret
    assert data["token_uri"] == CLIENT.token_uri
    assert data["account"] == "me@company.com"
    assert "gmail" in " ".join(data["scopes"])


def test_second_account_authorizes_without_disturbing_the_first(
    tmp_path: Path,
) -> None:
    creds = tmp_path / "google_tokens"
    first = store_credential(
        "work@company.com",
        {"access_token": "a1", "refresh_token": "r1", "scope": "s"},
        CLIENT,
        credentials_dir=creds,
    )
    before = first.read_bytes()

    second = store_credential(
        "personal@gmail.com",
        {"access_token": "a2", "refresh_token": "r2", "scope": "s"},
        CLIENT,
        credentials_dir=creds,
    )

    # Both coexist, keyed independently; the first is byte-for-byte untouched.
    assert first != second
    assert first.exists() and second.exists()
    assert first.read_bytes() == before
    assert load_credential("work@company.com", credentials_dir=creds)["token"] == "a1"
    assert (
        load_credential("personal@gmail.com", credentials_dir=creds)["token"] == "a2"
    )


@pytest.mark.parametrize(
    "account",
    ["../evil", "a/b@x.com", "no-at-sign", "", "..", "me@nodot"],
)
def test_store_rejects_unsafe_or_non_email_account(
    account: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        store_credential(
            account, {"access_token": "x"}, CLIENT, credentials_dir=tmp_path
        )


def test_end_to_end_paste_to_disk(token_endpoint: str, tmp_path: Path) -> None:
    """The whole central mechanism, wired: pasted redirect -> code -> exchange
    (fixture) -> credential on disk, keyed by address, then read back."""
    pasted = "http://localhost/?code=4/0Aauth&scope=x"
    code = extract_auth_code(pasted)
    token = exchange_code(CLIENT, code, token_endpoint=token_endpoint)
    creds = tmp_path / "google_tokens"
    path = store_credential("owner@company.com", token, CLIENT, credentials_dir=creds)
    assert _mode(path) == 0o600
    loaded = load_credential("owner@company.com", credentials_dir=creds)
    assert loaded["refresh_token"] == "1//refresh"


# --- Docs ---------------------------------------------------------------------


def test_skill_documents_the_paste_back_flow() -> None:
    text = SKILL_PATH.read_text().lower()
    for token in ("paste", "consent", "redirect", "code", "oauth_consent"):
        assert token in text, f"SKILL.md must document {token!r}"
    assert "todo(#8)" not in text, "SKILL.md still carries a TODO(#8) marker"


def test_install_documents_the_oauth_flow() -> None:
    text = INSTALL_PATH.read_text().lower()
    assert "paste" in text and "consent" in text
    assert "todo(#8)" not in text, "INSTALL.md still carries a TODO(#8) marker"
