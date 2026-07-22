#!/usr/bin/env python3
"""Consent-by-paste OAuth + per-account credential storage for google-workspace.

There is no browser and no callback listener on chief's daemon host, so the
usual "spin up a localhost server and wait for the redirect" desktop-OAuth flow
does not apply. Instead chief builds the Google consent URL, the owner opens it
wherever they have a browser, consents, and pastes the resulting **address-bar
redirect string** back into chat. This module carries that flow end to end:

    build_consent_url  -> the URL the owner opens (offline access, login_hint)
    extract_auth_code  -> pull the ?code= out of the pasted redirect string
    exchange_code      -> POST the standard OAuth2 grant to the token endpoint
    store_credential   -> write the credential to chief's secret store, keyed by
                          the account's email address, with 0600 perms

The token exchange speaks the real OAuth2 protocol (form-encoded
``grant_type=authorization_code`` POST), so pointed at Google's ``token_uri`` it
works live — the live Google exchange is exercised in slice #10. Tests here
point ``token_endpoint`` at a local fixture server: the *mechanism* is real, the
*endpoint* is a fixture (this slice's declared mock-debt).

Credential layout — coupling to the upstream server
---------------------------------------------------
Credentials are written one JSON file per account, named ``<email>.json``, in a
credentials directory (default ``secrets/google_tokens``). The file is the
Google **authorized-user** shape produced by ``Credentials.to_json()`` and read
back by ``google.oauth2.credentials.Credentials.from_authorized_user_file`` —
which is exactly how the pinned upstream server
(``taylorwilsdon/google_workspace_mcp`` / PyPI ``workspace-mcp``) loads a
per-account credential. So the server can read what consent writes, provided its
credentials directory points at ours (wire that env when the version is pinned
in #7). The directory is overridable via the ``google_workspace`` config key so
the pin can align the two without a code change.

Stdlib-only — the package declares no ``python_deps``; ``urllib`` does the HTTP.
"""

from __future__ import annotations

import argparse
import json
import os
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
# need. openid + userinfo.email identify the account we key the credential by.
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


class OAuthError(Exception):
    """Base class for every consent-by-paste failure."""


class ClientConfigError(OAuthError):
    """The OAuth client-secrets JSON is missing or malformed."""


class RedirectParseError(OAuthError):
    """The pasted redirect string has no usable authorization code."""


class TokenExchangeError(OAuthError):
    """The token endpoint rejected the exchange or returned no access token."""


class CredentialStoreError(OAuthError):
    """The credential could not be written to the secret store."""


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

        Accepts either the ``installed`` (desktop app) or ``web`` top-level key,
        as Google exports them. The redirect defaults to loopback: the owner
        consents on another device and copies the (unreachable) ``http://
        localhost/?code=...`` the browser lands on — no listener runs here.
        """
        block = data.get("installed") or data.get("web")
        if not isinstance(block, dict):
            raise ClientConfigError(
                "client config must have an 'installed' or 'web' object "
                "(the JSON downloaded from Google Cloud Console)"
            )
        try:
            client_id = str(block["client_id"])
            client_secret = str(block["client_secret"])
        except KeyError as exc:
            raise ClientConfigError(f"client config missing {exc}") from exc
        redirects = block.get("redirect_uris") or ["http://localhost"]
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            auth_uri=str(block.get("auth_uri", cls.auth_uri)),
            token_uri=str(block.get("token_uri", cls.token_uri)),
            redirect_uri=str(redirects[0]),
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
        if not isinstance(data, dict):
            raise ClientConfigError(f"{p}: client config must be a JSON object")
        return cls.from_client_config(data)


def build_consent_url(
    client: OAuthClient,
    account: str,
    *,
    scopes: tuple[str, ...] = CORE_SCOPES,
    state: str | None = None,
) -> str:
    """Build the Google consent URL the owner opens to grant ``account``.

    ``access_type=offline`` + ``prompt=consent`` force a refresh token back on
    consent (the credential must keep working unattended); ``login_hint`` steers
    the account chooser to the address we intend to key the credential by.
    """
    if not _safe_account(account):
        raise ValueError(
            f"{account!r} is not a plain email address — pass the full account "
            "the grant is for (e.g. me@company.com)"
        )
    params = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "login_hint": account,
    }
    if state is not None:
        params["state"] = state
    return client.auth_uri + "?" + urllib.parse.urlencode(params)


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
    parts = urllib.parse.urlsplit(text)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
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


def exchange_code(
    client: OAuthClient,
    code: str,
    *,
    token_endpoint: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Exchange an authorization code for a token at the token endpoint.

    Speaks the real OAuth2 code grant — a form-encoded POST with the code, the
    client credentials, the redirect, and ``grant_type=authorization_code``. At
    Google's ``token_uri`` this is the live exchange (slice #10); tests point
    ``token_endpoint`` at a fixture. A non-2xx response or a body with no
    ``access_token`` raises ``TokenExchangeError`` — never a silent empty token.
    """
    endpoint = token_endpoint or client.token_uri
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": client.redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
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
        raise TokenExchangeError(
            f"token response has no access_token: {payload[:200]!r}"
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


def credential_path(account: str, credentials_dir: str | os.PathLike[str]) -> Path:
    """The on-disk path a given account's credential is stored at.

    Raises ``ValueError`` for an account that is not a plain, path-safe email —
    the single choke point that both storage and lookup route through, so a
    traversal attempt can never resolve to a path outside the dir.
    """
    if not _safe_account(account):
        raise ValueError(
            f"{account!r} is not a storable account — it must be a plain email "
            "address (no path separators)"
        )
    return Path(credentials_dir) / f"{account}.json"


def store_credential(
    account: str,
    token: dict[str, Any],
    client: OAuthClient,
    *,
    credentials_dir: str | os.PathLike[str] = DEFAULT_CREDENTIALS_DIR,
    scopes: tuple[str, ...] = CORE_SCOPES,
) -> Path:
    """Write ``token`` as a per-account credential, keyed by email address.

    The file is the Google authorized-user shape the upstream server reads
    (``from_authorized_user_file``); it is written 0600 in a 0700 directory,
    atomically (temp + replace) so a crash mid-write can't leave a truncated
    credential. Multi-account: each address gets its own ``<email>.json``, so a
    second grant never disturbs the first. Returns the path written.
    """
    path = credential_path(account, credentials_dir)
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Tighten the dir even if it pre-existed loose — sibling account files
        # live here, all owner-only.
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise CredentialStoreError(
            f"cannot prepare credentials dir {directory}: {exc}"
        ) from exc

    scope_value = token.get("scope")
    granted = scope_value.split() if isinstance(scope_value, str) and scope_value else (
        list(scopes)
    )
    record: dict[str, Any] = {
        "account": account,
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
    the credential is still valid, the server just re-checks sooner."""
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _write_secret_json(path: Path, record: dict[str, Any]) -> None:
    """Atomically write ``record`` as JSON with 0600 perms. Owner-only from the
    moment it exists: the temp file is created 0600 via ``os.open`` (no wider
    window), then renamed into place."""
    tmp = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode())
        finally:
            os.close(fd)
        # Enforce 0600 even if the temp pre-existed with looser perms.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CredentialStoreError(f"cannot write credential {path}: {exc}") from exc


def _cmd_url(args: argparse.Namespace) -> int:
    client = OAuthClient.from_secret_file(args.client)
    print(build_consent_url(client, args.account, state=args.state))
    return 0


def _cmd_exchange(args: argparse.Namespace) -> int:
    """Parse a pasted redirect, exchange the code, store the credential.

    Default token endpoint is the client's real ``token_uri`` (Google) — that
    live path is slice #10; ``--token-endpoint`` points it at a fixture for a
    dry run. Prints the path written.
    """
    client = OAuthClient.from_secret_file(args.client)
    code = extract_auth_code(args.pasted)
    token = exchange_code(client, code, token_endpoint=args.token_endpoint)
    path = store_credential(
        args.account, token, client, credentials_dir=args.credentials_dir
    )
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI for the consent-by-paste flow, run through chief's ``shell`` tool.

    ``url`` prints the consent URL to hand the owner; ``exchange`` takes the
    pasted redirect back, exchanges it, and stores the per-account credential.
    Any OAuth failure exits non-zero with the reason on stderr, so a caller can
    tell "bad paste / dead endpoint" apart from a stored credential on stdout.
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
    url_cmd.add_argument("--state", default=None, help="opaque state to echo back")
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
