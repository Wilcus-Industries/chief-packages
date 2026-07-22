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

- **authserv-id pinning.** Only the AR header whose authserv-id matches *our*
  boundary MTA (``google_workspace.trusted_authserv_id``, default
  ``mx.google.com``) is read; an AR header bearing a *different* authserv-id —
  upstream, or attacker-forged on mail the attacker **sends** — is ignored, and
  a blank/absent trusted id fails closed to ``None``. This assumes the boundary
  MTA strips inbound AR headers bearing its own authserv-id before adding its
  genuine one, which Google does.
- **DMARC alignment.** SPF/DKIM authenticate a *domain*, not the ``From`` local
  part, so an identity is trusted only when an explicit ``dmarc=pass`` asserts
  ``From`` alignment. Absent DMARC → ``None`` (see ``verified_sender``).

**Inserted/imported mail — the provenance gate (#14).** The authserv-id pin
defends forged AR on *delivered* mail: a message an attacker **sends** carries
the wrong authserv-id (or none) and is rejected. It does **not**, by itself,
defend a message written directly into the mailbox — ``messages.insert`` (the
Gmail API insert scope), IMAP ``APPEND``, or "Import mail" — because there the
attacker controls the entire raw message, including a forged
``Authentication-Results: mx.google.com; …`` bearing *our* authserv-id, and
nothing at delivery stripped it. Header parsing alone cannot distinguish that
from a genuine boundary header. So trust is additionally gated on a
**retrieval-layer** signal the raw message cannot carry: ``resolve_sender``
grants ``ALLOW`` only when its ``provenance`` is ``SMTP_DELIVERED`` (default
``UNKNOWN`` → fail closed), and ``resolve_message`` / ``classify_delivery``
derive that from Gmail ``messages.get`` metadata (a receipt-time ``CATEGORY_*``
label that insert/import bypass). This is defense-in-depth, not proof (a
sophisticated inserter that also forges plausible API metadata is out of scope);
the load-bearing control for acting on the owner's behalf stays the write-gate
approval card (#6). Do not read this catalog as the last line of defense.

Scope note: this module is the trust catalog + verified-sender resolution only,
and it is a **noise-reducer, not the last line of defense**. Reads are open;
the load-bearing control for *actions* on the owner's behalf is the write-gate
approval card (#6), which still fires even for a catalogued sender. Binding the
write tools to that screening/approval gate lands in the later google-workspace
slices (#5 screening, #6 write-gate). ``resolve_sender`` emits the *decision*
(allow a known sender, or route an unknown one to the owner-confirm flow);
wiring that decision into core's gate is #6's job.

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
# and MUST ignore AR headers bearing a *different* authserv-id — an upstream or
# attacker-forged header on mail the attacker SENDS carries the wrong id and its
# verdicts are worthless. For these Gmail Workspace accounts the receiving MTA is
# Google, so the boundary authserv-id defaults to ``mx.google.com``; the owner
# can override it via the ``google_workspace.trusted_authserv_id`` config key
# when mail arrives behind a different boundary MTA. Note the pin does NOT catch
# a message written directly into the mailbox (``messages.insert`` / IMAP
# ``APPEND`` / "Import mail"), which can carry a forged AR bearing *our* own
# authserv-id — a residual limitation tracked in #14 (see the module docstring).
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
    """Drop RFC 5322 ``(...)`` comments, honouring nesting and quoted-strings.

    Comments carry free text (``spf=pass (google.com: domain of …)``) that would
    otherwise be tokenised as bogus properties. A ``(`` inside a quoted-string
    (``smtp.mailfrom="a(b)@example.com"``) is a literal, not a comment opener,
    so quoting suspends comment recognition; backslash quoted-pairs are honoured
    in both contexts (RFC 5322 §3.2.1–3.2.3)."""
    out: list[str] = []
    depth = 0
    in_quote = False
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:  # quoted-pair: next char is literal
                if depth == 0:
                    out.append(ch)
                    out.append(value[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
            if depth == 0:
                out.append(ch)
        elif ch == '"' and depth == 0:  # a comment may hold '"'; only open at depth 0
            in_quote = True
            out.append(ch)
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "\\" and depth != 0 and i + 1 < n:  # quoted-pair inside a comment
            i += 2
            continue
        elif depth == 0:
            out.append(ch)
        i += 1
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

    A blank / whitespace-only / ``None`` ``trusted_authserv_id`` (e.g. a missing
    or empty config value) fails closed to ``[]`` — it trusts nothing rather
    than exact-matching a forged empty ("``;``") authserv-id.
    """
    msg = message_from_string(raw_message)
    trusted = (trusted_authserv_id or "").strip().lower()
    if not trusted:
        return []  # no trusted authserv-id configured -> trust nothing
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

    1. **authserv-id pinning** (RFC 8601 §5/§7). Only an ``Authentication-
       Results`` header whose authserv-id matches our boundary MTA
       (``trusted_authserv_id``, default ``mx.google.com``) is read; an AR
       header bearing a *different* authserv-id — forged on mail the attacker
       sends, or upstream — is ignored, and a blank ``trusted_authserv_id``
       fails closed. No matching boundary header → ``None``. **Caveat:** this
       does *not* defend a message written straight into the mailbox
       (``messages.insert`` / IMAP ``APPEND`` / "Import mail") with a forged AR
       bearing our *own* authserv-id — a residual limitation tracked in #14;
       the load-bearing control for actions is the write-gate approval card
       (#6), not this catalog.
    2. **DMARC alignment.** The identity comes from a passing SPF
       (``smtp.mailfrom``) or DKIM (``header.i`` / ``header.d``) mechanism —
       never the forgeable ``From`` — but SPF/DKIM authenticate the envelope /
       signing *domain*, not the ``From`` local part. Only an explicit
       ``dmarc=pass`` asserts that ``From`` alignment, so verification requires
       it: a missing or non-pass DMARC → ``None`` (a bare SPF pass on a shared /
       multi-tenant domain would otherwise authenticate *any* local part there).

    SPF's ``smtp.mailfrom`` (a full envelope address) is preferred over DKIM
    (often domain-only).

    **DKIM identity granularity.** When only DKIM authenticates, the identity
    comes from ``header.i`` / ``header.d``. ``header.i`` is commonly domain-level
    (``@example.com``), which normalises to ``@example.com`` — a *domain*
    identity. The catalog keys on whatever string is returned, so a domain
    identity matches a domain-level catalog entry (a key of ``@example.com``) but
    **never** a per-address entry (``alice@example.com``): a DKIM-domain-only
    sender therefore falls through to ``OWNER_CONFIRM`` unless the owner
    catalogs the domain itself. That is the safe direction — DKIM alone does not
    authenticate a specific mailbox, so it must not silently match one. A full
    ``header.i`` (``alice@example.com``) matches a per-address entry as usual.
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


class DeliveryProvenance(Enum):
    """How a message arrived in the mailbox — the retrieval-layer signal that
    gates whether its ``Authentication-Results`` header may be trusted at all.

    ``Authentication-Results`` is only meaningful on mail that actually transited
    Google's inbound MTA — Google strips a sender's forged AR and stamps its own.
    A message written **directly into the mailbox** (``gmail.insert``, IMAP
    ``APPEND``, or "Import mail") carries whatever raw bytes the writer chose,
    *including* a forged ``Authentication-Results: mx.google.com; …dmarc=pass…``
    bearing our own pinned authserv-id — nothing stripped it, so header parsing
    (``verified_sender``) cannot tell it from a genuine boundary header. The only
    defense is out-of-band: a delivery-provenance signal from the retrieval path.
    """

    SMTP_DELIVERED = "smtp_delivered"  # transited Google's inbound MTA
    NOT_DELIVERED = "not_delivered"  # inserted / imported / APPENDed
    UNKNOWN = "unknown"  # no provenance signal — treated as untrusted


# Gmail applies a ``CATEGORY_*`` label during receipt-time classification, which
# ``messages.insert`` / IMAP ``APPEND`` / import explicitly BYPASS (the Gmail API
# docs: insert "bypass[es] most scanning and classification"). Their presence is
# positive evidence the message was genuinely delivered; a mailbox-writer with
# only insert scope controls the raw message bytes, not Gmail's own classifier.
_DELIVERY_CLASSIFIED_LABELS = frozenset(
    {
        "CATEGORY_PERSONAL",
        "CATEGORY_SOCIAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)


def classify_delivery(metadata: Mapping[str, object]) -> DeliveryProvenance:
    """Best-effort delivery provenance from a Gmail ``messages.get`` metadata
    resource (``labelIds`` etc.) — the signal ``resolve_sender`` gates trust on.

    Conservative and fail-closed. A Gmail receipt-time ``CATEGORY_*`` label is
    positive evidence of SMTP delivery (insert/import bypass classification).
    Its *absence* is not proof of insertion — Gmail may leave a message
    uncategorized — so absence yields ``UNKNOWN``, which is treated as untrusted:
    trust is granted only on positive delivery evidence, never by default.

    **Limits.** This reads Gmail *metadata*, not the forgeable raw message, which
    is the whole point (an inserter controls bytes, not the classifier). But it
    is defense-in-depth, not proof: a sophisticated inserter that also sets a
    plausible ``CATEGORY_*`` label via the API would read as delivered here. The
    load-bearing control for acting on the owner's behalf remains the write-gate
    approval card (#6); this only hardens the noise-reducing catalog.
    """
    labels = metadata.get("labelIds")
    label_set = {str(item) for item in labels} if isinstance(labels, list) else set()
    if label_set & _DELIVERY_CLASSIFIED_LABELS:
        return DeliveryProvenance.SMTP_DELIVERED
    return DeliveryProvenance.UNKNOWN


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
    *,
    provenance: DeliveryProvenance = DeliveryProvenance.UNKNOWN,
) -> TrustDecision:
    """Resolve a raw message to a trust decision.

    Verified known sender **on genuinely delivered mail** → its tier, ``ALLOW``.
    Everything else (verification failed, the verified sender isn't catalogued,
    or the message did not transit Google's inbound MTA) → a metadata-only log
    and the ``OWNER_CONFIRM`` flow. The lookup keys on ``verified_sender``, so a
    ``From``-only spoof of a catalogued address never resolves to its tier — and
    ``verified_sender`` itself only trusts an AR header added at our
    ``trusted_authserv_id`` boundary and backed by an explicit ``dmarc=pass``.

    **Provenance gate (#14).** ``provenance`` must be
    ``DeliveryProvenance.SMTP_DELIVERED`` for a catalogued sender to be allowed.
    An inserted/imported/APPENDed message (or one with no provenance signal —
    the fail-closed ``UNKNOWN`` default) is routed to ``OWNER_CONFIRM`` *even if*
    it carries a valid-looking ``mx.google.com`` AR for a catalogued address:
    that AR is forgeable on mail that never transited Google's MTA, so header
    parsing alone cannot trust it. Use ``resolve_message`` to derive
    ``provenance`` from Gmail metadata, or ``classify_delivery`` directly.
    """
    verified = verified_sender(raw_message, trusted_authserv_id)
    tier = catalog.lookup(verified)
    if tier is not None and provenance is DeliveryProvenance.SMTP_DELIVERED:
        return TrustDecision(verified, tier, Action.ALLOW, metadata=None)
    msg = message_from_string(raw_message)
    return TrustDecision(
        verified, None, Action.OWNER_CONFIRM, metadata=_metadata_of(msg, verified)
    )


def resolve_message(
    raw_message: str,
    metadata: Mapping[str, object],
    catalog: SenderTrustCatalog,
    trusted_authserv_id: str = DEFAULT_TRUSTED_AUTHSERV_ID,
) -> TrustDecision:
    """Resolve a fetched Gmail message (raw bytes + its ``messages.get``
    metadata) to a trust decision — the entry point the retrieval integration
    (#5/#7) uses. Derives delivery provenance from ``metadata`` via
    ``classify_delivery`` and applies the ``resolve_sender`` provenance gate, so
    an inserted/imported message never earns catalogued trust from its own
    embedded AR headers.
    """
    return resolve_sender(
        raw_message,
        catalog,
        trusted_authserv_id,
        provenance=classify_delivery(metadata),
    )
