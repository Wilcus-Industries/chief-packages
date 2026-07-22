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


def _parse_one_header(value: str) -> list[AuthResult]:
    results: list[AuthResult] = []
    # Each ';'-delimited field after the authserv-id is one method's resinfo.
    for field in _strip_comments(value).split(";"):
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
    return results


def parse_authentication_results(raw_message: str) -> list[AuthResult]:
    """All method verdicts across every ``Authentication-Results`` header."""
    msg = message_from_string(raw_message)
    results: list[AuthResult] = []
    for header in msg.get_all("Authentication-Results", []):
        results.extend(_parse_one_header(str(header)))
    return results


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


def verified_sender(raw_message: str) -> str | None:
    """The authenticated sender address, or ``None`` if verification fails.

    Derived from the ``Authentication-Results`` *parameters* of a passing
    mechanism — SPF ``smtp.mailfrom`` or DKIM ``header.i`` / ``header.d`` — never
    from the ``From`` header. Gated by DMARC alignment:

    - No SPF or DKIM pass at all → ``None`` (nothing was authenticated).
    - An explicit ``dmarc=fail`` → ``None``, even if SPF/DKIM passed for some
      *other* domain (the ``From`` alignment failed, so ``From`` can't be
      trusted).
    - Otherwise the passing mechanism's identity is returned, SPF preferred
      (a full envelope address) over DKIM (often domain-only).
    """
    results = parse_authentication_results(raw_message)
    spf = _verdict(results, _SPF)
    dkim = _verdict(results, _DKIM)
    dmarc = _verdict(results, _DMARC)

    spf_ok = spf is not None and spf.result == _PASS
    dkim_ok = dkim is not None and dkim.result == _PASS
    if not (spf_ok or dkim_ok):
        return None
    if dmarc is not None and dmarc.result != _PASS:
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
    raw_message: str, catalog: SenderTrustCatalog
) -> TrustDecision:
    """Resolve a raw message to a trust decision.

    Verified known sender → its tier, ``ALLOW``. Everything else (verification
    failed, or the verified sender isn't catalogued) → a metadata-only log and
    the ``OWNER_CONFIRM`` flow. Crucially, the lookup keys on ``verified_sender``,
    so a ``From``-only spoof of a catalogued address never resolves to its tier.
    """
    verified = verified_sender(raw_message)
    tier = catalog.lookup(verified)
    if tier is not None:
        return TrustDecision(verified, tier, Action.ALLOW, metadata=None)
    msg = message_from_string(raw_message)
    return TrustDecision(
        verified, None, Action.OWNER_CONFIRM, metadata=_metadata_of(msg, verified)
    )
