#!/usr/bin/env python3
"""Sender trust catalog for inbound Google Workspace mail.

Ports the shape of chief core's iMessage sender whitelist — a per-sender mode
table, an unknown-sender metadata-only log, and an owner-confirm flow — to email
(see ``chief/adapters/imessage.py`` in core for the original). The catalog is
the *deterministic* layer that answers "is this sender really who it claims to
be," a question prompting cannot answer safely.

The one rule that makes it trustworthy: the catalog keys on the **authenticated**
sender parsed from a message's ``Authentication-Results`` headers (SPF / DKIM /
DMARC), **never** the forgeable ``From`` header. A message whose authentication
fails is never matched as a known sender — even when its ``From`` names a
catalogued address — and a ``From``-only spoof of a known address does not
resolve to that sender's tier.

That rule only holds if the AR verdicts are themselves trustworthy, so two
fail-closed boundaries protect the parse (both RFC 8601 §5/§7):

- **authserv-id pinning.** Only the AR header added by *our* boundary MTA
  (``google_workspace.trusted_authserv_id``, default ``mx.google.com``) is
  read. Any other AR header — upstream, or attacker-inserted on mail with no
  genuine boundary AR (``messages.insert`` / IMAP ``APPEND`` / "Import mail") —
  is forgeable and ignored. This assumes the boundary MTA strips inbound AR
  headers bearing its own authserv-id before adding its genuine one, which
  Google does; without a boundary AR the message fails closed to ``None``.
- **DMARC alignment.** SPF/DKIM authenticate a *domain*, not the ``From`` local
  part, so an identity is trusted only when an explicit ``dmarc=pass`` asserts
  ``From`` alignment. Absent DMARC → ``None`` (see ``verified_sender``).

Scope note: this module is the trust catalog + verified-sender resolution only.
Reads are open; binding the write tools to a screening/approval gate lands in the
later google-workspace slices (#5 screening, #6 write-gate). ``resolve_sender``
emits the *decision* (allow a known sender, or route an unknown one to the
owner-confirm flow); wiring that decision into core's gate is #6's job.

Stdlib for parsing (``email``); PyYAML for the human-editable catalog file, which
chief maintains under its config the way core keeps ``imessage.owner_handles``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email import message_from_string
from email.message import Message
from email.utils import parseaddr
from enum import Enum

import yaml

# Authentication-Results methods we understand. Others (e.g. ``arc``,
# ``iprev``) are parsed but ignored for the verification verdict.
_SPF = "spf"
_DKIM = "dkim"
_DMARC = "dmarc"
_METHODS = frozenset({_SPF, _DKIM, _DMARC})

_PASS = "pass"
_FAIL = "fail"

# The ``Authentication-Results`` header is only trustworthy when it was added at
# *our* trust boundary. RFC 8601 §5/§7: a consumer MUST pin its own authserv-id
# and MUST ignore AR headers not added by its boundary MTA — any other AR header
# (upstream, or attacker-inserted via ``messages.insert`` / IMAP ``APPEND`` /
# "Import mail") is forgeable and its verdicts are worthless. For these Gmail
# Workspace accounts the receiving MTA is Google, so the boundary authserv-id
# defaults to ``mx.google.com``; the owner can override it via the
# ``google_workspace.trusted_authserv_id`` config key when mail arrives behind a
# different boundary MTA.
CONFIG_KEY_TRUSTED_AUTHSERV_ID = "google_workspace.trusted_authserv_id"
DEFAULT_TRUSTED_AUTHSERV_ID = "mx.google.com"


# --------------------------------------------------------------------------- #
# Authentication-Results parsing                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthResult:
    """One method verdict from an ``Authentication-Results`` header.

    ``props`` holds the ``ptype.property=value`` pairs (lower-cased keys), e.g.
    ``{"smtp.mailfrom": "alice@example.com"}`` for SPF or ``{"header.i":
    "@example.com"}`` for DKIM.
    """

    method: str
    result: str
    props: Mapping[str, str]


def _strip_comments(value: str) -> str:
    """Drop RFC-822 ``(...)`` comments, honouring nesting.

    Comments carry free text (``spf=pass (google.com: domain of …)``) that would
    otherwise be tokenised as bogus properties."""
    out: list[str] = []
    depth = 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _header_authserv_id(fields: list[str]) -> str:
    """The authserv-id token (RFC 8601 §2.2), lower-cased for a case-insensitive
    boundary match.

    It is the first token of the header — before the first ``;`` and before any
    optional version number or CFWS. Comments are already stripped by the
    caller, so a trailing version digit-string is simply a second token we drop.
    """
    if not fields:
        return ""
    head_tokens = fields[0].split()
    return head_tokens[0].strip().lower() if head_tokens else ""


def _parse_one_header(value: str) -> tuple[str, list[AuthResult]]:
    """Parse one header into its authserv-id and the method verdicts it carries."""
    fields = _strip_comments(value).split(";")
    authserv_id = _header_authserv_id(fields)
    results: list[AuthResult] = []
    # Each ';'-delimited field after the authserv-id is one method's resinfo.
    for field in fields:
        tokens = field.split()
        if not tokens:
            continue
        head = tokens[0]
        if "=" not in head:
            continue  # authserv-id (and its optional version) — no '='
        method, _, result = head.partition("=")
        method = method.strip().lower()
        if method not in _METHODS:
            continue
        props: dict[str, str] = {}
        for tok in tokens[1:]:
            if "=" in tok:
                key, _, val = tok.partition("=")
                props[key.strip().lower()] = val.strip()
        results.append(AuthResult(method, result.strip().lower(), props))
    return authserv_id, results


def parse_authentication_results(
    raw_message: str, trusted_authserv_id: str
) -> list[AuthResult]:
    """Method verdicts from the ``Authentication-Results`` header added at *our*
    trust boundary — never any other AR header on the message.

    Only a header whose authserv-id matches ``trusted_authserv_id`` (case-
    insensitive exact match, version/CFWS ignored) is trusted; every other
    ``Authentication-Results`` header is forgeable and ignored (RFC 8601 §5/§7).
    The **topmost** matching header wins. When *no* header matches, returns
    ``[]`` — fail closed: nothing was authenticated at our boundary.
    """
    msg = message_from_string(raw_message)
    trusted = trusted_authserv_id.strip().lower()
    for header in msg.get_all("Authentication-Results", []):
        authserv_id, results = _parse_one_header(str(header))
        if authserv_id == trusted:
            return results  # topmost matching (boundary) header wins
    return []


def _verdict(results: list[AuthResult], method: str) -> AuthResult | None:
    for res in results:
        if res.method == method:
            return res
    return None


# --------------------------------------------------------------------------- #
# Verified-sender extraction                                                  #
# --------------------------------------------------------------------------- #


def _normalize_address(addr: str) -> str:
    """Lower-case the bare address, tolerating ``Name <addr>`` and ``<addr>``."""
    _, parsed = parseaddr(addr)
    return (parsed or addr).strip().strip("<>").lower()


def verified_sender(
    raw_message: str,
    trusted_authserv_id: str = DEFAULT_TRUSTED_AUTHSERV_ID,
) -> str | None:
    """The authenticated sender address, or ``None`` if verification fails.

    Two fail-closed trust boundaries gate the answer:

    1. **authserv-id pinning** (RFC 8601 §5/§7). Only the ``Authentication-
       Results`` header added by our own boundary MTA (``trusted_authserv_id``,
       default ``mx.google.com``) is read; forged / upstream AR headers are
       ignored. No boundary header on the message → ``None``.
    2. **DMARC alignment.** The identity comes from a passing SPF
       (``smtp.mailfrom``) or DKIM (``header.i`` / ``header.d``) mechanism —
       never the forgeable ``From`` — but SPF/DKIM authenticate the envelope /
       signing *domain*, not the ``From`` local part. Only an explicit
       ``dmarc=pass`` asserts that ``From`` alignment, so verification requires
       it: a missing or non-pass DMARC → ``None`` (a bare SPF pass on a shared /
       multi-tenant domain would otherwise authenticate *any* local part there).

    SPF's ``smtp.mailfrom`` (a full envelope address) is preferred over DKIM
    (often domain-only).
    """
    results = parse_authentication_results(raw_message, trusted_authserv_id)
    spf = _verdict(results, _SPF)
    dkim = _verdict(results, _DKIM)
    dmarc = _verdict(results, _DMARC)

    spf_ok = spf is not None and spf.result == _PASS
    dkim_ok = dkim is not None and dkim.result == _PASS
    if not (spf_ok or dkim_ok):
        return None
    # DMARC is the only verdict that asserts From-alignment; require it present
    # and passing. Fail closed when it is absent — a bare SPF/DKIM pass does not
    # authenticate the local part on a shared domain.
    if dmarc is None or dmarc.result != _PASS:
        return None

    if spf_ok and spf is not None:
        mailfrom = spf.props.get("smtp.mailfrom")
        if mailfrom:
            return _normalize_address(mailfrom)
    if dkim_ok and dkim is not None:
        identity = dkim.props.get("header.i") or dkim.props.get("header.d")
        if identity:
            return _normalize_address(identity)
    return None


# --------------------------------------------------------------------------- #
# The catalog                                                                 #
# --------------------------------------------------------------------------- #


class SenderTrustCatalog:
    """A per-sender tier table chief maintains, keyed on verified addresses.

    Addresses are normalised (lower-cased) on both storage and lookup so
    ``Alice@Example.com`` and ``alice@example.com`` are one key — email addresses
    are case-insensitive in practice, and the auth-result parser lower-cases too.
    """

    def __init__(self, tiers: Mapping[str, str] | None = None) -> None:
        self._tiers: dict[str, str] = {
            _normalize_address(addr): tier for addr, tier in (tiers or {}).items()
        }

    def lookup(self, address: str | None) -> str | None:
        """The sender's tier, or ``None`` when unknown (or ``address`` is
        ``None`` — an unverified sender)."""
        if address is None:
            return None
        return self._tiers.get(_normalize_address(address))

    def set_tier(self, address: str, tier: str) -> None:
        """Catalog a sender at a tier — the owner-confirm flow's terminal step."""
        self._tiers[_normalize_address(address)] = tier

    def remove(self, address: str) -> None:
        self._tiers.pop(_normalize_address(address), None)

    def as_dict(self) -> dict[str, str]:
        """A copy of the address → tier mapping, address-sorted for stability."""
        return dict(sorted(self._tiers.items()))

    def serialize(self) -> str:
        """Render to a stable YAML mapping (the on-disk catalog format)."""
        return yaml.safe_dump(self.as_dict(), sort_keys=True)

    @classmethod
    def parse(cls, text: str) -> SenderTrustCatalog:
        """Load a catalog from its serialized YAML form."""
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("sender trust catalog must be a mapping of addr -> tier")
        return cls({str(k): str(v) for k, v in data.items()})


# --------------------------------------------------------------------------- #
# Resolution — verified sender -> trust decision                             #
# --------------------------------------------------------------------------- #


class Action(Enum):
    """What to do with a message, given its sender's trust."""

    ALLOW = "allow"  # a known, verified sender
    OWNER_CONFIRM = "owner_confirm"  # unknown or unverified -> owner-confirm flow


@dataclass(frozen=True)
class SenderMetadata:
    """The unknown-sender **metadata-only** log — envelope facts, never the body.

    Ports core's "unknown-sender metadata-only log": chief records who a message
    claims to be from (and who it was actually verified as) without exposing or
    acting on the untrusted content.
    """

    verified_sender: str | None
    from_header: str
    subject: str
    date: str
    message_id: str


def _metadata_of(msg: Message, verified: str | None) -> SenderMetadata:
    return SenderMetadata(
        verified_sender=verified,
        from_header=str(msg.get("From", "")),
        subject=str(msg.get("Subject", "")),
        date=str(msg.get("Date", "")),
        message_id=str(msg.get("Message-ID", "")),
    )


@dataclass(frozen=True)
class TrustDecision:
    """The outcome of resolving a message against the catalog.

    ``metadata`` is populated only on the owner-confirm path (the metadata-only
    log for an unknown/unverified sender); a known sender is allowed straight
    through with no content logged.
    """

    verified_sender: str | None
    tier: str | None
    action: Action
    metadata: SenderMetadata | None

    @property
    def known(self) -> bool:
        return self.tier is not None


def resolve_sender(
    raw_message: str,
    catalog: SenderTrustCatalog,
    trusted_authserv_id: str = DEFAULT_TRUSTED_AUTHSERV_ID,
) -> TrustDecision:
    """Resolve a raw message to a trust decision.

    Verified known sender → its tier, ``ALLOW``. Everything else (verification
    failed, or the verified sender isn't catalogued) → a metadata-only log and
    the ``OWNER_CONFIRM`` flow. Crucially, the lookup keys on ``verified_sender``,
    so a ``From``-only spoof of a catalogued address never resolves to its tier —
    and ``verified_sender`` itself only trusts an AR header added at our
    ``trusted_authserv_id`` boundary and backed by an explicit ``dmarc=pass``.
    """
    verified = verified_sender(raw_message, trusted_authserv_id)
    tier = catalog.lookup(verified)
    if tier is not None:
        return TrustDecision(verified, tier, Action.ALLOW, metadata=None)
    msg = message_from_string(raw_message)
    return TrustDecision(
        verified, None, Action.OWNER_CONFIRM, metadata=_metadata_of(msg, verified)
    )
