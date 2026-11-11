"""Which URLs chief's browser may be sent to — http(s) on a non-loopback host.

The pinned playwright-mcp accepts ``about:``, ``data:``, ``blob:`` and
``javascript:`` on ``browser_navigate`` (an auto-approved read), and a
``javascript:`` URL runs script in whatever logged-in page the tab shows —
``browser_evaluate`` without its approval card. Loopback hosts reach the CDP
port itself (``/json/close/<id>``) and the login viewer; ``chrome://`` can
quit or crash the shared browser. So the host checks every URL it forwards,
and ``login_link``, with this one helper.

Chrome parses URLs by the WHATWG rules, which differ from ``urlsplit``: it
treats ``\\`` as ``/``, drops tabs and newlines, percent-decodes and IDNA-maps
the host, and reads ``127.1`` / ``2130706433`` / ``0x7f.1`` as IPv4. Anything
whose host could read differently between the two is refused rather than
re-parsed. A DNS name that resolves to loopback is not caught here; Chrome's
DevTools endpoint itself rejects a Host header that is not an IP or localhost.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import unquote, urlsplit

ALLOWED_SCHEMES = ("http", "https")
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_BARE_SCHEME = re.compile(r"^(about|data|blob|javascript):", re.IGNORECASE)
_LOOPBACK_PREFIXES = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
_THIS_NETWORK = ipaddress.IPv4Network("0.0.0.0/8")


def effective_url(url: str) -> str:
    """The URL the pinned server would actually load for ``url``: it prefixes
    a scheme-less one (``example.com``) with ``https://`` (``http://`` for
    loopback). Mirrors its ``checkUrlAndNavigate``; check this, forward this."""
    if _HAS_SCHEME.match(url) or _BARE_SCHEME.match(url):
        return url
    if url.startswith(_LOOPBACK_PREFIXES):
        return "http://" + url
    return "https://" + url


def _ipv4(host: str) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(host)
    except ValueError:
        pass
    try:  # the legacy forms Chrome accepts too: 127.1, 2130706433, 0x7f.1
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def _local_v4(ip: ipaddress.IPv4Address) -> bool:
    return ip.is_loopback or ip in _THIS_NETWORK


def _host_refusal(host: str) -> str | None:
    if ":" in host:
        try:
            ip6 = ipaddress.IPv6Address(host)
        except ValueError:
            return "error: url has an invalid host"
        mapped = ip6.ipv4_mapped
        if ip6.is_loopback or ip6.is_unspecified or (mapped and _local_v4(mapped)):
            return "error: url must not point at this machine (loopback)"
        return None
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return "error: url has an invalid host"
    host = host.rstrip(".")
    if not host:
        return "error: url has no host"
    if host == "localhost" or host.endswith(".localhost"):
        return "error: url must not point at this machine (loopback)"
    ip4 = _ipv4(host)
    if ip4 is not None and _local_v4(ip4):
        return "error: url must not point at this machine (loopback)"
    return None


def refusal(url: str) -> str | None:
    """Why ``url`` may not be opened (an ``error: ...`` line), or None if it
    is an http(s) URL on a host that is not this machine."""
    if any(ord(c) <= 0x20 or ord(c) == 0x7F or c == "\\" for c in url):
        return "error: url must not contain spaces, control characters, or '\\'"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError:
        return "error: url is malformed"
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return "error: url must be http(s)"
    if "@" in parts.netloc:
        return "error: url must not carry a username or password"
    host = unquote(parts.hostname or "")
    if not host:
        return "error: url has no host"
    if any(ord(c) <= 0x20 or ord(c) == 0x7F or c in "\\/@%" for c in host):
        return "error: url has an invalid host"
    return _host_refusal(host)
