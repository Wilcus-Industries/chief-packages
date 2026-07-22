"""Consent-by-paste OAuth + per-account credential storage (slice #8).

The central mechanism under test: a pasted address-bar redirect string ->
authorization-code extraction -> token exchange (against a *fixture* token
endpoint here; the live Google exchange is #10) -> a per-account credential
written to chief's secret store keyed by email address, with 0600 perms.

The security fix-list (#8 hardening) is exercised here too: consent is bound to
a per-account ``state`` + PKCE verifier issued at URL time, the stored account is
verified against the exchanged ``id_token``'s email claim (anti-fixation), the
expiry timestamp round-trips through google-auth's loader, and the client secret
file is checked for loose perms.

Pure-Python + a local fixture HTTP server, so these run everywhere (no chief
core checkout needed). The module under test lives at
``google-workspace/scripts/oauth_consent.py``; ``conftest`` puts that dir on
``sys.path`` so it imports here by bare name.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import stat
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import oauth_consent as oc
import pytest
from conftest import repo_root
from oauth_consent import (
    CORE_SCOPES,
    ClientConfigError,
    CredentialStoreError,
    IdentityMismatchError,
    OAuthClient,
    RedirectParseError,
    StateMismatchError,
    TokenExchangeError,
    build_consent_url,
    consume_pending_state,
    credential_path,
    exchange_and_store,
    exchange_code,
    extract_auth_code,
    extract_state,
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

# The fixture token endpoint returns an id_token for this address; the identity
# check keys the accept/reject cases off it.
GRANTED_EMAIL = "owner@company.com"


def _b64url(obj: dict[str, object]) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_id_token(email: str) -> str:
    """An unsigned JWT carrying an ``email`` claim. #8 decodes the claim without
    verifying the signature (JWKS verification is #10), so a bare header.payload
    .sig with alg=none is enough to drive the identity check under test."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url({"email": email, "sub": "123"})
    return f"{header}.{payload}.sig"


def _state_from_url(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]


# --- Consent URL --------------------------------------------------------------


def test_build_consent_url_carries_required_params(tmp_path: Path) -> None:
    """The consent URL is a real Google auth URL: offline access, the account as
    login_hint, the core scopes, plus the bound state and PKCE challenge."""
    url = build_consent_url(
        CLIENT, "me@company.com", credentials_dir=tmp_path, state="xyz"
    )
    assert url.startswith(CLIENT.auth_uri + "?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["client_id"] == [CLIENT.client_id]
    assert query["redirect_uri"] == [CLIENT.redirect_uri]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["login_hint"] == ["me@company.com"]
    assert query["state"] == ["xyz"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    scope = query["scope"][0]
    assert "gmail" in scope and "calendar" in scope and "drive" in scope


def test_core_scopes_cover_the_core_tier_services() -> None:
    joined = " ".join(CORE_SCOPES)
    for service in ("gmail", "calendar", "drive"):
        assert service in joined, f"core scopes must cover {service}"


def test_build_consent_url_persists_state_and_verifier_0600(tmp_path: Path) -> None:
    """The state + PKCE verifier issued at URL time are persisted 0600 so the
    later paste can be checked against the session that actually started."""
    creds = tmp_path / "google_tokens"
    build_consent_url(
        CLIENT,
        "me@company.com",
        credentials_dir=creds,
        state="the-state",
        code_verifier="the-verifier",
    )
    pending = creds / ".me@company.com.pending.json"
    assert pending.exists()
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600, oct(
        stat.S_IMODE(pending.stat().st_mode)
    )
    data = json.loads(pending.read_text())
    assert data["state"] == "the-state"
    assert data["code_verifier"] == "the-verifier"


def test_build_consent_url_generates_random_state_and_challenge(
    tmp_path: Path,
) -> None:
    """With no explicit state, each call issues a fresh random state + a PKCE
    challenge derived (S256) from a fresh verifier."""
    import hashlib

    u1 = build_consent_url(CLIENT, "a@company.com", credentials_dir=tmp_path)
    u2 = build_consent_url(CLIENT, "b@company.com", credentials_dir=tmp_path)
    s1, s2 = _state_from_url(u1), _state_from_url(u2)
    assert s1 and s2 and s1 != s2

    # The challenge equals base64url(sha256(verifier)) of the persisted verifier.
    pending = json.loads((tmp_path / ".a@company.com.pending.json").read_text())
    verifier = pending["code_verifier"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(u1).query)[
        "code_challenge"
    ] == [expected]


# --- Client config parsing (Fix 5) --------------------------------------------


def test_from_client_config_rejects_web_client() -> None:
    """A 'web' OAuth client is refused — consent-by-paste needs a Desktop app
    (loopback redirect) client."""
    with pytest.raises(ClientConfigError, match="Desktop"):
        OAuthClient.from_client_config(
            {
                "web": {
                    "client_id": "i",
                    "client_secret": "s",
                    "redirect_uris": ["https://app.example.com/cb"],
                }
            }
        )


def test_from_client_config_handles_string_redirect_uris(tmp_path: Path) -> None:
    """A malformed string ``redirect_uris`` must not be indexed char-by-char
    (the old ``redirects[0]`` bug); it falls back to the loopback default."""
    client = OAuthClient.from_client_config(
        {
            "installed": {
                "client_id": "i",
                "client_secret": "s",
                "redirect_uris": "http://localhost",
            }
        }
    )
    assert client.redirect_uri == "http://localhost"


def test_from_client_config_rejects_non_loopback_redirect() -> None:
    with pytest.raises(ClientConfigError):
        OAuthClient.from_client_config(
            {
                "installed": {
                    "client_id": "i",
                    "client_secret": "s",
                    "redirect_uris": ["https://evil.example.com/cb"],
                }
            }
        )


def test_from_secret_file_warns_and_tightens_loose_perms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The client-secret file holds client_id + client_secret; a world-readable
    file is flagged loudly and tightened to 0600 (Fix 4)."""
    p = tmp_path / "client.json"
    p.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "id",
                    "client_secret": "sec",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    os.chmod(p, 0o644)
    OAuthClient.from_secret_file(p)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert stat.S_IMODE(p.stat().st_mode) == 0o600, oct(
        stat.S_IMODE(p.stat().st_mode)
    )


# --- Paste parsing ------------------------------------------------------------


def test_extract_auth_code_from_pasted_redirect() -> None:
    """The owner pastes the whole address-bar string; the code is pulled out."""
    pasted = "http://localhost/?code=4/0AY0e-abc123&scope=https://mail.google.com/"
    assert extract_auth_code(pasted) == "4/0AY0e-abc123"


def test_extract_auth_code_tolerates_surrounding_whitespace() -> None:
    pasted = "   http://localhost/?state=z&code=abc%2Fdef&scope=x   \n"
    assert extract_auth_code(pasted) == "abc/def"


def test_extract_state_pulls_state_or_none() -> None:
    assert extract_state("http://localhost/?code=c&state=xyz") == "xyz"
    assert extract_state("http://localhost/?code=c") is None
    assert extract_state("http://localhost/?code=c&state=") is None


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
    replies with a canned credential (including an id_token), a 400 for the
    ``bad-code`` sentinel, or an access_token-less body for ``no-token``."""

    last_form: dict[str, list[str]] = {}

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        type(self).last_form = urllib.parse.parse_qs(body)
        code = type(self).last_form.get("code")
        if code == ["bad-code"]:
            payload = json.dumps(
                {"error": "invalid_grant", "error_description": "bad code"}
            ).encode()
            self.send_response(400)
        elif code == ["no-token"]:
            # A misbehaving endpoint returns a refresh_token but no access_token;
            # the error must name fields, never echo this secret value.
            payload = json.dumps(
                {"refresh_token": "sup3r-secret-refresh", "token_type": "Bearer"}
            ).encode()
            self.send_response(200)
        else:
            payload = json.dumps(
                {
                    "access_token": "ya29.access",
                    "refresh_token": "1//refresh",
                    "expires_in": 3599,
                    "scope": " ".join(CORE_SCOPES),
                    "token_type": "Bearer",
                    "id_token": _make_id_token(GRANTED_EMAIL),
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


def test_exchange_sends_pkce_verifier(token_endpoint: str) -> None:
    exchange_code(
        CLIENT, "4/0Aauth", token_endpoint=token_endpoint, code_verifier="my-verifier"
    )
    assert _FixtureToken.last_form["code_verifier"] == ["my-verifier"]


def test_exchange_error_is_raised_loud(token_endpoint: str) -> None:
    with pytest.raises(TokenExchangeError):
        exchange_code(CLIENT, "bad-code", token_endpoint=token_endpoint)


def test_exchange_no_access_token_names_fields_not_values(
    token_endpoint: str,
) -> None:
    """A body with no access_token must report the field NAMES present, never the
    raw body — else a stray refresh_token would leak (Fix 5)."""
    with pytest.raises(TokenExchangeError) as excinfo:
        exchange_code(CLIENT, "no-token", token_endpoint=token_endpoint)
    msg = str(excinfo.value)
    assert "refresh_token" in msg  # the field name is named
    assert "sup3r-secret-refresh" not in msg  # the value is never echoed


# --- State + PKCE binding (Fix 1) ---------------------------------------------


def test_consume_pending_state_returns_verifier_on_match(tmp_path: Path) -> None:
    creds = tmp_path / "google_tokens"
    build_consent_url(
        CLIENT,
        "me@company.com",
        credentials_dir=creds,
        state="right",
        code_verifier="the-verifier",
    )
    assert (
        consume_pending_state("me@company.com", "right", credentials_dir=creds)
        == "the-verifier"
    )


def test_consume_pending_state_rejects_mismatch(tmp_path: Path) -> None:
    """A pasted state that does not match the issued state is refused — the CSRF
    / injected-consent guard."""
    creds = tmp_path / "google_tokens"
    build_consent_url(
        CLIENT, "me@company.com", credentials_dir=creds, state="right"
    )
    with pytest.raises(StateMismatchError):
        consume_pending_state("me@company.com", "wrong", credentials_dir=creds)


def test_consume_pending_state_rejects_absent_state(tmp_path: Path) -> None:
    creds = tmp_path / "google_tokens"
    build_consent_url(
        CLIENT, "me@company.com", credentials_dir=creds, state="right"
    )
    with pytest.raises(StateMismatchError):
        consume_pending_state("me@company.com", None, credentials_dir=creds)


def test_consume_pending_state_rejects_when_no_pending(tmp_path: Path) -> None:
    with pytest.raises(StateMismatchError):
        consume_pending_state(
            "me@company.com", "anything", credentials_dir=tmp_path / "google_tokens"
        )


# --- Per-account credential storage -------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _token(email: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "access_token": "ya29.a",
        "refresh_token": "1//r",
        "expires_in": 3599,
        "scope": " ".join(CORE_SCOPES),
        "id_token": _make_id_token(email),
    }
    base.update(extra)
    return base


def test_store_credential_keyed_by_email_with_0600(tmp_path: Path) -> None:
    creds = tmp_path / "google_tokens"
    path = store_credential(
        "me@company.com", _token("me@company.com"), CLIENT, credentials_dir=creds
    )

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


def test_store_refuses_when_granted_email_mismatches_account(
    tmp_path: Path,
) -> None:
    """Anti-fixation: the id_token proves a DIFFERENT account than the owner's
    key, so the credential is refused and no file is written."""
    creds = tmp_path / "google_tokens"
    token = _token("attacker@evil.com")  # the phished consent's real identity
    with pytest.raises(IdentityMismatchError):
        store_credential("owner@company.com", token, CLIENT, credentials_dir=creds)
    assert not (creds / "owner@company.com.json").exists()


def test_store_refuses_when_no_id_token(tmp_path: Path) -> None:
    creds = tmp_path / "google_tokens"
    with pytest.raises(IdentityMismatchError):
        store_credential(
            "owner@company.com", {"access_token": "x"}, CLIENT, credentials_dir=creds
        )


def test_store_accepts_when_granted_email_matches_case_insensitively(
    tmp_path: Path,
) -> None:
    creds = tmp_path / "google_tokens"
    token = _token("Owner@Company.com")  # id_token case differs from arg
    path = store_credential("owner@company.com", token, CLIENT, credentials_dir=creds)
    assert path.exists()


def test_account_email_case_normalized(tmp_path: Path) -> None:
    """``Me@X.com`` and ``me@x.com`` resolve to one lowercase-keyed file."""
    creds = tmp_path / "google_tokens"
    path = store_credential(
        "Me@X.com", _token("me@x.com"), CLIENT, credentials_dir=creds
    )
    assert path == creds / "me@x.com.json"
    assert load_credential("me@x.com", credentials_dir=creds)["token"] == "ya29.a"
    assert load_credential("ME@X.COM", credentials_dir=creds)["account"] == "me@x.com"


def test_second_account_authorizes_without_disturbing_the_first(
    tmp_path: Path,
) -> None:
    creds = tmp_path / "google_tokens"
    first = store_credential(
        "work@company.com",
        _token("work@company.com", access_token="a1", refresh_token="r1"),
        CLIENT,
        credentials_dir=creds,
    )
    before = first.read_bytes()

    second = store_credential(
        "personal@gmail.com",
        _token("personal@gmail.com", access_token="a2", refresh_token="r2"),
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


def test_write_secret_refuses_pre_planted_symlink(tmp_path: Path) -> None:
    """O_EXCL|O_NOFOLLOW: a symlink pre-planted at the temp path is refused, so a
    write can never be redirected to clobber a file outside the tokens dir."""
    creds = tmp_path / "google_tokens"
    creds.mkdir()
    os.chmod(creds, 0o700)
    victim = tmp_path / "victim"
    victim.write_text("do not clobber")
    os.symlink(victim, creds / ".owner@company.com.json.tmp")

    with pytest.raises(CredentialStoreError):
        store_credential(
            "owner@company.com",
            _token("owner@company.com"),
            CLIENT,
            credentials_dir=creds,
        )
    assert victim.read_text() == "do not clobber"


# --- Expiry round-trips through google-auth's loader (Fix 3) ------------------


def test_expiry_iso_loads_in_google_auth_at_whole_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``microsecond == 0``, ``isoformat()`` emitted ``...+00:00`` with no
    fractional part, which google-auth's ``from_authorized_user_info`` could not
    strptime. The explicit ``...%f + 'Z'`` format must load cleanly."""
    from google.oauth2.credentials import Credentials

    frozen = datetime(2026, 1, 1, 12, 34, 56, 0, tzinfo=UTC)  # microsecond == 0
    monkeypatch.setattr(
        oc, "datetime", SimpleNamespace(now=lambda tz=None: frozen)
    )
    expiry = oc._expiry_iso(3600)
    assert expiry is not None and expiry.endswith(".000000Z"), expiry

    info = {
        "client_id": CLIENT.client_id,
        "client_secret": CLIENT.client_secret,
        "refresh_token": "1//r",
        "token": "ya29",
        "token_uri": CLIENT.token_uri,
        "scopes": list(CORE_SCOPES),
        "expiry": expiry,
    }
    creds = Credentials.from_authorized_user_info(info)  # must not raise
    assert creds.expiry == datetime(2026, 1, 1, 13, 34, 56)


# --- End-to-end: URL -> paste -> exchange -> disk -----------------------------


def test_end_to_end_paste_to_disk(token_endpoint: str, tmp_path: Path) -> None:
    """The whole central mechanism, wired: build URL (issues state+PKCE) ->
    pasted redirect carrying the matching state -> code + verifier exchange
    (fixture) -> id_token-verified credential on disk, keyed by address, read
    back; the pending-state file is cleared afterwards."""
    creds = tmp_path / "google_tokens"
    url = build_consent_url(CLIENT, GRANTED_EMAIL, credentials_dir=creds)
    state = _state_from_url(url)
    pasted = f"http://localhost/?code=4/0Aauth&state={state}&scope=x"

    path = exchange_and_store(
        CLIENT,
        GRANTED_EMAIL,
        pasted,
        credentials_dir=creds,
        token_endpoint=token_endpoint,
    )
    assert _mode(path) == 0o600
    loaded = load_credential(GRANTED_EMAIL, credentials_dir=creds)
    assert loaded["refresh_token"] == "1//refresh"
    # PKCE verifier was carried into the exchange, and pending state is consumed.
    assert _FixtureToken.last_form["code_verifier"][0]
    assert not (creds / f".{GRANTED_EMAIL}.pending.json").exists()


def test_end_to_end_rejects_forged_state(
    token_endpoint: str, tmp_path: Path
) -> None:
    """A paste whose state does not match the issued session is refused before
    any exchange happens."""
    creds = tmp_path / "google_tokens"
    build_consent_url(CLIENT, GRANTED_EMAIL, credentials_dir=creds)
    pasted = "http://localhost/?code=4/0Aauth&state=forged&scope=x"
    with pytest.raises(StateMismatchError):
        exchange_and_store(
            CLIENT,
            GRANTED_EMAIL,
            pasted,
            credentials_dir=creds,
            token_endpoint=token_endpoint,
        )
    assert not (creds / f"{GRANTED_EMAIL}.json").exists()


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
