#!/usr/bin/env python3
"""Consent-by-paste OAuth + per-account credential storage for google-workspace.

There is no browser and no callback listener on chief's daemon host, so the
usual "spin up a localhost server and wait for the redirect" desktop-OAuth flow
does not apply. Instead chief builds the Google consent URL, the owner opens it
wherever they have a browser, consents, and pastes the resulting **address-bar
redirect string** back into chat. This module carries that flow end to end:

    build_consent_url  -> the URL the owner opens (offline access, login_hint),
                          binding the session with a random ``state`` + a PKCE
                          verifier persisted 0600 under the credentials dir
    extract_auth_code  -> pull the ?code= out of the pasted redirect string
    extract_state      -> pull the ?state= out of the pasted redirect string
    exchange_code      -> POST the standard OAuth2 grant (with the PKCE verifier)
    store_credential   -> write the credential to chief's secret store, keyed by
                          the account's email address, with 0600 perms — but only
                          after the exchanged ``id_token``'s email claim is
                          verified to match the requested account (anti-fixation)
    exchange_and_store -> the full pipeline the CLI ``exchange`` runs: verify
                          state, exchange with the verifier, verify identity, store

These tokens can send mail as the owner, so this is the lethal-trifecta
credential seam: the consent binding (state + PKCE + granted-email check) is what
keeps a phished paste from binding an *attacker's* mailbox under the owner's key.

The token exchange speaks the real OAuth2 protocol (form-encoded
``grant_type=authorization_code`` POST), so pointed at Google's ``token_uri`` it
works live — the live Google exchange is exercised in slice #10. Tests here
point ``token_endpoint`` at a local fixture server: the *mechanism* is real, the
*endpoint* is a fixture (this slice's declared mock-debt).

id_token email claim vs. full JWKS verification (#10)
-----------------------------------------------------
``store_credential`` decodes the ``id_token``'s ``email`` claim and refuses to
store unless it equals the requested account. The id_token comes from the
*trusted token endpoint* (fixed from the owner's client file, never the pasted
string), so reading its email claim defeats the fixation attack even without a
signature check. Full JWKS **signature** verification against Google's public
keys is the live path and lands with #10; until then a compromised/MITM'd token
endpoint is out of scope (that endpoint is Google over TLS in the live flow).

Credential layout — coupling to the upstream server
---------------------------------------------------
Credentials are written one JSON file per account, named ``<email>.json``, in a
credentials directory (default ``secrets/google_tokens``). The file is the
Google **authorized-user** shape produced by ``Credentials.to_json()`` and read
back by ``google.oauth2.credentials.Credentials.from_authorized_user_file`` —
which is exactly how the pinned upstream server
(``taylorwilsdon/google_workspace_mcp`` / PyPI ``workspace-mcp``) loads a
per-account credential. So the server can read what consent writes, provided its
credentials directory points at ours. The directory is set per invocation with
the ``--credentials-dir`` flag; #7 pins the server version and wires the server's
own credentials directory at the same path so the two agree.

Stdlib-only — the package declares no ``python_deps``; ``urllib`` does the HTTP,
``hashlib``/``base64``/``secrets`` do PKCE + the (unverified) id_token decode.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ``_looks_like_email`` already encodes the address sanity check the alias file
# uses; reuse it here rather than re-deriving one, so "what counts as an email"
# stays defined in one place. Both modules live in this scripts/ dir, which
# conftest / the sandbox put on sys.path.
from gw_aliases import _looks_like_email

# Where chief keeps the OAuth *client* config (the desktop-app client id/secret
# the owner downloads from Google Cloud Console) and the derived per-account
# tokens. Both live under the secret store, which is gitignored and excluded
# from backups — these tokens can send mail as the owner.
DEFAULT_CLIENT_SECRET = "secrets/google_workspace_credentials"
DEFAULT_CREDENTIALS_DIR = "secrets/google_tokens"

# Core-tier Gmail / Calendar / Drive scopes — the grant the core-tier tools
# need. openid + userinfo.email identify the account we key the credential by
# (and return the id_token whose email claim the storage guard checks).
# Refinable per install; #10/live confirms the exact set the server requires.
CORE_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
)

_UNIVERSE_DOMAIN = "googleapis.com"

# Entropy for the per-session CSRF ``state`` and the PKCE ``code_verifier``.
# token_urlsafe(n) yields ~1.3*n chars; a 64-byte verifier stays inside RFC 7636's
# 43–128 char bound.
_STATE_BYTES = 32
_VERIFIER_BYTES = 64

# Loopback / out-of-band redirects a Desktop-app OAuth client legitimately uses.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"


class OAuthError(Exception):
    """Base class for every consent-by-paste failure."""


class ClientConfigError(OAuthError):
    """The OAuth client-secrets JSON is missing or malformed."""


class RedirectParseError(OAuthError):
    """The pasted redirect string has no usable authorization code."""


class StateMismatchError(OAuthError):
    """The pasted redirect's ``state`` does not match the state issued for this
    account — a forged, injected, or cross-session consent."""


class TokenExchangeError(OAuthError):
    """The token endpoint rejected the exchange or returned no access token."""


class IdentityMismatchError(OAuthError):
    """The exchanged ``id_token`` proves a different Google account than the one
    the credential would be keyed by — a consent-fixation attempt."""


class CredentialStoreError(OAuthError):
    """The credential could not be written to the secret store."""


def _is_loopback_redirect(redirect_uri: str) -> bool:
    """A Desktop-app OAuth client's redirect is loopback ``http://localhost`` (or
    127.0.0.1 / ::1), or the OOB urn. A hosted ``https://…`` redirect means a
    *web* client, which this consent-by-paste flow does not support."""
    if redirect_uri == _OOB_REDIRECT:
        return True
    try:
        parts = urllib.parse.urlsplit(redirect_uri)
    except ValueError:
        return False
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class OAuthClient:
    """A Google OAuth *client* (desktop app): the id/secret the owner installs
    once, plus the auth/token endpoints and the redirect the consent URL uses."""

    client_id: str
    client_secret: str
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    redirect_uri: str = "http://localhost"

    @classmethod
    def from_client_config(cls, data: dict[str, Any]) -> OAuthClient:
        """Parse the client-secrets JSON downloaded from Google Cloud Console.

        Requires the ``installed`` (Desktop app) top-level key — a ``web`` client
        is **rejected**: consent-by-paste needs a loopback redirect, and a web
        client's hosted redirect + secret handling is a different (and more
        dangerous, if misused here) flow. The redirect defaults to loopback: the
        owner consents on another device and copies the (unreachable)
        ``http://localhost/?code=...`` the browser lands on — no listener runs.
        """
        block = data.get("installed")
        if not isinstance(block, dict):
            if isinstance(data.get("web"), dict):
                raise ClientConfigError(
                    "this is a 'web' OAuth client; consent-by-paste needs a "
                    "**Desktop app** client (loopback redirect). Create a Desktop "
                    "OAuth client in Google Cloud Console and download that JSON"
                )
            raise ClientConfigError(
                "client config must have an 'installed' (Desktop app) object — the "
                "JSON downloaded from Google Cloud Console"
            )
        try:
            client_id = str(block["client_id"])
            client_secret = str(block["client_secret"])
        except KeyError as exc:
            raise ClientConfigError(f"client config missing {exc}") from exc
        # Guard against a malformed ``redirect_uris`` (e.g. a bare string, which
        # ``redirects[0]`` would silently index char-by-char): only a non-empty
        # list of strings is honoured, else fall back to the loopback default.
        redirects = block.get("redirect_uris")
        if isinstance(redirects, list) and redirects and isinstance(redirects[0], str):
            redirect_uri = redirects[0]
        else:
            redirect_uri = "http://localhost"
        if not _is_loopback_redirect(redirect_uri):
            raise ClientConfigError(
                f"redirect_uri {redirect_uri!r} is not a loopback/localhost "
                "address; consent-by-paste requires a Desktop app client whose "
                "redirect is http://localhost"
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            auth_uri=str(block.get("auth_uri", cls.auth_uri)),
            token_uri=str(block.get("token_uri", cls.token_uri)),
            redirect_uri=redirect_uri,
        )

    @classmethod
    def from_secret_file(cls, path: str | os.PathLike[str]) -> OAuthClient:
        p = Path(path)
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError as exc:
            raise ClientConfigError(
                f"OAuth client secret not found at {p} — download the desktop "
                "client JSON from Google Cloud Console and place it there"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ClientConfigError(
                f"cannot read OAuth client config {p}: {exc}"
            ) from exc
        # The client id/secret live here; a group/other-readable file leaks them.
        _warn_and_tighten_loose_perms(p)
        if not isinstance(data, dict):
            raise ClientConfigError(f"{p}: client config must be a JSON object")
        return cls.from_client_config(data)


def _warn_and_tighten_loose_perms(path: Path) -> None:
    """If ``path`` is readable by group/other, warn loudly and tighten to 0600.
    The client-secret file holds ``client_id`` + ``client_secret``; the secret
    store dir is often 0755, so a default-umask save can leave it world-readable.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        print(
            f"oauth_consent: WARNING {path} is mode {oct(mode)} — the OAuth client "
            "id/secret is readable by group/other; tightening to 0600",
            file=sys.stderr,
        )
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            print(
                f"oauth_consent: WARNING could not tighten {path}: {exc}",
                file=sys.stderr,
            )


def build_consent_url(
    client: OAuthClient,
    account: str,
    *,
    scopes: tuple[str, ...] = CORE_SCOPES,
    credentials_dir: str | os.PathLike[str] = DEFAULT_CREDENTIALS_DIR,
    state: str | None = None,
    code_verifier: str | None = None,
) -> str:
    """Build the Google consent URL the owner opens to grant ``account``.

    ``access_type=offline`` + ``prompt=consent`` force a refresh token back on
    consent (the credential must keep working unattended); ``login_hint`` steers
    the account chooser to the address we intend to key the credential by.

    A random ``state`` and a PKCE ``code_verifier`` are issued and persisted 0600
    under ``credentials_dir`` (``.<account>.pending.json``); the URL carries the
    ``state`` and the S256 ``code_challenge``. The later paste must echo the same
    ``state`` (checked in ``consume_pending_state``), and the exchange sends the
    stored verifier — so a redirect from a *different* consent session (a phished
    paste) is refused. ``state`` / ``code_verifier`` are injectable for tests.
    """
    acct = _normalize_account(account)
    state = state or secrets.token_urlsafe(_STATE_BYTES)
    code_verifier = code_verifier or secrets.token_urlsafe(_VERIFIER_BYTES)
    challenge = _code_challenge(code_verifier)
    _ensure_credentials_dir(credentials_dir)
    _write_secret_json(
        pending_state_path(acct, credentials_dir),
        {"account": acct, "state": state, "code_verifier": code_verifier},
    )
    params = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "login_hint": acct,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return client.auth_uri + "?" + urllib.parse.urlencode(params)


def _code_challenge(verifier: str) -> str:
    """RFC 7636 S256 challenge: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def extract_auth_code(pasted: str) -> str:
    """Pull the authorization code out of a pasted address-bar redirect string.

    The owner pastes the whole thing (``http://localhost/?code=...&scope=...``);
    we parse the query and return the ``code``. A malformed paste — no URL, no
    ``code``, an empty ``code``, or an ``error=access_denied`` from a declined
    consent — is rejected with a clear ``RedirectParseError`` rather than a
    silent empty exchange.
    """
    text = (pasted or "").strip()
    if not text:
        raise RedirectParseError(
            "nothing pasted — copy the whole address bar after consenting "
            "(it looks like http://localhost/?code=...) and paste it back"
        )
    query = _parse_redirect_query(text)
    if "error" in query:
        raise RedirectParseError(
            f"consent was not granted: the redirect carries "
            f"error={query['error'][0]!r} — re-open the consent URL and approve"
        )
    codes = query.get("code")
    if not codes or not codes[0].strip():
        raise RedirectParseError(
            "no authorization code in the pasted string — it must be the full "
            "redirect URL containing '?code=...' (got: "
            f"{text[:60]!r}{'…' if len(text) > 60 else ''})"
        )
    return codes[0].strip()


def extract_state(pasted: str) -> str | None:
    """Pull the ``state`` out of a pasted redirect string, or ``None`` if absent
    or blank. The value is checked against the issued state in
    ``consume_pending_state``."""
    query = _parse_redirect_query((pasted or "").strip())
    states = query.get("state")
    if not states or not states[0].strip():
        return None
    return states[0].strip()


def _parse_redirect_query(text: str) -> dict[str, list[str]]:
    parts = urllib.parse.urlsplit(text)
    return urllib.parse.parse_qs(parts.query, keep_blank_values=True)


def exchange_code(
    client: OAuthClient,
    code: str,
    *,
    token_endpoint: str | None = None,
    code_verifier: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Exchange an authorization code for a token at the token endpoint.

    Speaks the real OAuth2 code grant — a form-encoded POST with the code, the
    client credentials, the redirect, ``grant_type=authorization_code``, and the
    PKCE ``code_verifier`` when one is supplied. At Google's ``token_uri`` this is
    the live exchange (slice #10); tests point ``token_endpoint`` at a fixture. A
    non-2xx response or a body with no ``access_token`` raises
    ``TokenExchangeError`` — never a silent empty token, and the no-token error
    names the fields present rather than echoing the raw body (a misbehaving
    endpoint could otherwise leak a refresh_token).
    """
    endpoint = token_endpoint or client.token_uri
    form = {
        "code": code,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "redirect_uri": client.redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    body = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise TokenExchangeError(
            f"token endpoint returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TokenExchangeError(
            f"token endpoint {endpoint} unreachable: {exc.reason}"
        ) from exc
    try:
        token = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TokenExchangeError(
            f"token endpoint returned non-JSON: {payload[:200]!r}"
        ) from exc
    if not isinstance(token, dict) or not token.get("access_token"):
        present = sorted(token) if isinstance(token, dict) else type(token).__name__
        raise TokenExchangeError(
            f"token response has no access_token (fields present: {present}) — the "
            "endpoint did not return a usable token"
        )
    return token


def _safe_account(account: str) -> bool:
    """An account is safe to key a credential file by when it is a plain email
    address AND a bare filename component — no path separators, no traversal.
    The email check keeps us from writing garbage; the basename check keeps a
    crafted address from escaping the credentials dir."""
    if not account or not _looks_like_email(account):
        return False
    if account in {".", ".."} or "/" in account or "\\" in account or "\x00" in account:
        return False
    return os.path.basename(account) == account


def _normalize_account(account: str) -> str:
    """Lowercase + strip the account, then validate it is a plain, path-safe
    email. Case-normalizing means ``Me@X.com`` and ``me@x.com`` key one file (and
    compare equal against the id_token's email claim), never two."""
    acct = (account or "").strip().lower()
    if not _safe_account(acct):
        raise ValueError(
            f"{account!r} is not a storable account — it must be a plain email "
            "address (no path separators)"
        )
    return acct


def credential_path(account: str, credentials_dir: str | os.PathLike[str]) -> Path:
    """The on-disk path a given account's credential is stored at.

    Raises ``ValueError`` for an account that is not a plain, path-safe email —
    the single choke point that both storage and lookup route through, so a
    traversal attempt can never resolve to a path outside the dir.
    """
    return Path(credentials_dir) / f"{_normalize_account(account)}.json"


def pending_state_path(
    account: str, credentials_dir: str | os.PathLike[str]
) -> Path:
    """Where the per-account pending ``state`` + PKCE verifier live between
    ``build_consent_url`` and the paste-back exchange. Hidden dotfile, same
    path-safety choke point as the credential file."""
    return Path(credentials_dir) / f".{_normalize_account(account)}.pending.json"


def consume_pending_state(
    account: str,
    provided_state: str | None,
    *,
    credentials_dir: str | os.PathLike[str] = DEFAULT_CREDENTIALS_DIR,
) -> str:
    """Verify the pasted ``provided_state`` matches the state issued for this
    account, and return the persisted PKCE ``code_verifier``.

    A missing pending file, a blank/absent pasted state, or any mismatch raises
    ``StateMismatchError`` — this is the guard that stops a redirect from a
    *different* consent session (a phished paste) from being exchanged and bound
    under the owner's key. The comparison is constant-time.
    """
    acct = _normalize_account(account)
    path = pending_state_path(acct, credentials_dir)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise StateMismatchError(
            f"no pending consent for {acct!r} — run `url {acct}` first, then paste "
            "back the redirect from THAT browser session"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateMismatchError(
            f"cannot read pending consent for {acct!r}: {exc}"
        ) from exc
    expected = raw.get("state") if isinstance(raw, dict) else None
    if (
        not provided_state
        or not isinstance(expected, str)
        or not expected
        or not hmac.compare_digest(provided_state, expected)
    ):
        raise StateMismatchError(
            "the redirect's state does not match the state issued for this "
            "account — refusing (possible forged or cross-session consent); "
            f"re-run `url {acct}` and paste back that session's redirect"
        )
    verifier = raw.get("code_verifier")
    if not isinstance(verifier, str) or not verifier:
        raise StateMismatchError(
            f"pending consent for {acct!r} is missing its PKCE verifier — "
            f"re-run `url {acct}`"
        )
    return verifier


def _clear_pending_state(
    account: str, credentials_dir: str | os.PathLike[str]
) -> None:
    """Remove a consumed pending-state file. Best-effort — a leftover file only
    means the next `url` reissues it."""
    try:
        path = pending_state_path(account, credentials_dir)
    except ValueError:
        return
    _unlink_quiet(path)


def store_credential(
    account: str,
    token: dict[str, Any],
    client: OAuthClient,
    *,
    credentials_dir: str | os.PathLike[str] = DEFAULT_CREDENTIALS_DIR,
    scopes: tuple[str, ...] = CORE_SCOPES,
) -> Path:
    """Write ``token`` as a per-account credential, keyed by email address.

    **Anti-fixation choke point.** Before anything is written, the exchanged
    ``id_token``'s email claim is decoded and must equal ``account`` — so a
    consent that authorized a *different* Google account (an attacker's, phished
    into the owner's paste) is refused, never stored under the owner's key. See
    ``_require_granted_email``.

    The file is the Google authorized-user shape the upstream server reads
    (``from_authorized_user_file``); it is written 0600 in a 0700 directory,
    atomically (O_EXCL|O_NOFOLLOW temp + fsync + replace) so a crash mid-write
    can't leave a truncated credential and a pre-planted symlink can't redirect
    the write. Multi-account: each address gets its own ``<email>.json``, so a
    second grant never disturbs the first. Returns the path written.
    """
    acct = _normalize_account(account)
    _require_granted_email(acct, token)
    path = credential_path(acct, credentials_dir)
    _ensure_credentials_dir(credentials_dir)

    scope_value = token.get("scope")
    granted = scope_value.split() if isinstance(scope_value, str) and scope_value else (
        list(scopes)
    )
    record: dict[str, Any] = {
        "account": acct,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "token_uri": client.token_uri,
        "scopes": granted,
        "universe_domain": _UNIVERSE_DOMAIN,
        "expiry": _expiry_iso(token.get("expires_in")),
    }
    _write_secret_json(path, record)
    return path


def _require_granted_email(account: str, token: dict[str, Any]) -> None:
    """Refuse to store unless the token's ``id_token`` proves the granted account
    is ``account`` (already normalized/lowercased).

    The id_token comes from the trusted token endpoint (never the pasted string),
    so its email claim is the anti-fixation control: an attacker who phishes the
    owner into pasting THEIR ``?code=`` cannot bind the attacker mailbox under the
    owner's key. JWKS **signature** verification is #10; the claim check holds now
    because the token *endpoint*, not the paste, is the claim's source.
    """
    granted = _decode_jwt_email(str(token.get("id_token") or ""))
    if granted is None:
        raise IdentityMismatchError(
            "token response carried no verifiable id_token email claim — refusing "
            "to store (the grant must include the 'openid' and 'email' scopes)"
        )
    if granted != account:
        raise IdentityMismatchError(
            f"granted account {granted!r} does not match the requested account "
            f"{account!r} — refusing to store (possible consent fixation): the "
            "pasted consent authorized a DIFFERENT Google account than intended"
        )


def _decode_jwt_email(id_token: str) -> str | None:
    """Return the lowercased ``email`` claim from a JWT ``id_token``, or ``None``.

    Decodes the base64url payload segment WITHOUT verifying the signature — safe
    here only because the id_token is delivered by the trusted token endpoint
    (see ``_require_granted_email``); JWKS signature verification is #10.
    """
    parts = id_token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment + padding)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    return email.strip().lower()


def exchange_and_store(
    client: OAuthClient,
    account: str,
    pasted: str,
    *,
    credentials_dir: str | os.PathLike[str] = DEFAULT_CREDENTIALS_DIR,
    token_endpoint: str | None = None,
) -> Path:
    """The full paste-back pipeline the CLI ``exchange`` runs.

    Parse the code + state out of the paste, verify the state against the issued
    session (returning the PKCE verifier), exchange with that verifier, verify the
    granted identity in ``store_credential``, write the credential, and clear the
    now-consumed pending state. Any failure raises before a credential is written.
    """
    acct = _normalize_account(account)
    code = extract_auth_code(pasted)
    state = extract_state(pasted)
    verifier = consume_pending_state(acct, state, credentials_dir=credentials_dir)
    token = exchange_code(
        client, code, token_endpoint=token_endpoint, code_verifier=verifier
    )
    path = store_credential(acct, token, client, credentials_dir=credentials_dir)
    _clear_pending_state(acct, credentials_dir)
    return path


def load_credential(
    account: str,
    *,
    credentials_dir: str | os.PathLike[str] = DEFAULT_CREDENTIALS_DIR,
) -> dict[str, Any]:
    """Read back a stored per-account credential (the authorized-user JSON)."""
    path = credential_path(account, credentials_dir)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CredentialStoreError(
            f"no stored credential for {account!r} at {path} — authorize it first"
        ) from exc
    if not isinstance(data, dict):
        raise CredentialStoreError(f"{path}: credential must be a JSON object")
    return data


def _expiry_iso(expires_in: Any) -> str | None:
    """Turn a token's ``expires_in`` seconds into an absolute ISO timestamp
    (what ``Credentials`` stores as ``expiry``). None when absent/unparseable —
    the credential is still valid, the server just re-checks sooner.

    Formatted explicitly as ``%Y-%m-%dT%H:%M:%S.%f`` + ``Z``. ``isoformat()`` is
    wrong here: when ``microsecond == 0`` it drops the fractional part and keeps a
    ``+00:00`` offset, which google-auth's ``from_authorized_user_info`` loader
    (``expiry.rstrip("Z").split(".")[0]`` then ``strptime("%Y-%m-%dT%H:%M:%S")``)
    cannot parse — leaving the credential file unloadable roughly 1 second in
    every million. The fractional ``.%f`` + trailing ``Z`` always parse.
    """
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    expiry = datetime.now(UTC) + timedelta(seconds=seconds)
    return expiry.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _ensure_credentials_dir(credentials_dir: str | os.PathLike[str]) -> Path:
    """Create (if needed) and lock down the credentials dir to 0700 — sibling
    account files live here, all owner-only."""
    directory = Path(credentials_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise CredentialStoreError(
            f"cannot prepare credentials dir {directory}: {exc}"
        ) from exc
    return directory


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_secret_json(path: Path, record: dict[str, Any]) -> None:
    """Atomically write ``record`` as JSON with 0600 perms. Owner-only from the
    moment it exists: the temp file is created 0600 via ``os.open`` with
    ``O_EXCL|O_NOFOLLOW`` (no wider window, no following a pre-planted symlink),
    the write is checked for a short count, ``fsync``'d, then renamed into place.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError as exc:
        raise CredentialStoreError(
            f"refusing to write {path}: temp file {tmp} already exists (a prior "
            "write may have crashed, or the path was pre-created) — remove it and "
            "retry"
        ) from exc
    except OSError as exc:
        raise CredentialStoreError(f"cannot open temp file {tmp}: {exc}") from exc
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise CredentialStoreError(
                f"short write to {tmp}: {written} of {len(payload)} bytes"
            )
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _unlink_quiet(tmp)
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        _unlink_quiet(tmp)
        raise CredentialStoreError(f"cannot finalize credential {path}: {exc}") from exc


def _cmd_url(args: argparse.Namespace) -> int:
    client = OAuthClient.from_secret_file(args.client)
    print(
        build_consent_url(
            client, args.account, credentials_dir=args.credentials_dir
        )
    )
    return 0


def _cmd_exchange(args: argparse.Namespace) -> int:
    """Parse a pasted redirect, verify state, exchange the code, store the
    credential.

    Default token endpoint is the client's real ``token_uri`` (Google) — that
    live path is slice #10; ``--token-endpoint`` points it at a fixture for a
    dry run. Prints the path written.
    """
    client = OAuthClient.from_secret_file(args.client)
    path = exchange_and_store(
        client,
        args.account,
        args.pasted,
        credentials_dir=args.credentials_dir,
        token_endpoint=args.token_endpoint,
    )
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI for the consent-by-paste flow, run through chief's ``shell`` tool.

    ``url`` prints the consent URL to hand the owner (and persists the session's
    state + PKCE verifier under ``--credentials-dir``); ``exchange`` takes the
    pasted redirect back, verifies the state, exchanges it, and stores the
    per-account credential. Both must be pointed at the **same**
    ``--credentials-dir``. Any OAuth failure exits non-zero with the reason on
    stderr, so a caller can tell "bad paste / dead endpoint / forged state /
    wrong account" apart from a stored credential on stdout.
    """
    parser = argparse.ArgumentParser(
        prog="oauth_consent",
        description="Consent-by-paste OAuth for google-workspace accounts.",
    )
    parser.add_argument(
        "--client",
        default=DEFAULT_CLIENT_SECRET,
        help="OAuth client secret JSON (default %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    url_cmd = sub.add_parser("url", help="print the consent URL for an account")
    url_cmd.add_argument("account", help="the full email address the grant is for")
    url_cmd.add_argument(
        "--credentials-dir",
        default=DEFAULT_CREDENTIALS_DIR,
        help="per-account credential + pending-state dir (default %(default)s)",
    )
    url_cmd.set_defaults(func=_cmd_url)

    ex_cmd = sub.add_parser(
        "exchange", help="exchange a pasted redirect and store the credential"
    )
    ex_cmd.add_argument("account", help="the full email address the grant is for")
    ex_cmd.add_argument("pasted", help="the pasted address-bar redirect string")
    ex_cmd.add_argument(
        "--credentials-dir",
        default=DEFAULT_CREDENTIALS_DIR,
        help="per-account credential dir (default %(default)s)",
    )
    ex_cmd.add_argument(
        "--token-endpoint",
        default=None,
        help="override the token endpoint (default: the client's Google token_uri)",
    )
    ex_cmd.set_defaults(func=_cmd_exchange)

    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        assert isinstance(result, int)
        return result
    except (OAuthError, ValueError) as exc:
        print(f"oauth_consent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
