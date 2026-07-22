"""The google-workspace sender trust catalog.

The catalog is the deterministic layer that answers "is this sender really who
it claims to be" — keyed on the *authenticated* sender parsed from a message's
``Authentication-Results`` headers (SPF/DKIM/DMARC), never the forgeable
``From``. These tests exercise the real module (no mock of the parse → lookup →
serialize mechanism); the module ships as a sandbox-run script under
``google-workspace/scripts/`` and is reached on ``sys.path`` via conftest.
"""

from __future__ import annotations

from textwrap import dedent

from sender_trust import (
    Action,
    SenderTrustCatalog,
    resolve_sender,
    verified_sender,
)


def _msg(headers: str, body: str = "the secret body — never logged\n") -> str:
    """Assemble a raw RFC-822 message from a dedented header block + body."""
    return dedent(headers).strip() + "\n\n" + body


# A verified message: SPF + DKIM + DMARC all pass, aligned to the From domain.
KNOWN_VERIFIED = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: Quarterly numbers
    Date: Mon, 21 Jul 2026 09:00:00 -0700
    Message-ID: <known-1@example.com>
    Authentication-Results: mx.google.com;
        spf=pass (google.com: domain of alice@example.com designates 1.2.3.4)
            smtp.mailfrom=alice@example.com;
        dkim=pass header.i=@example.com header.s=sel header.b=AbCdEf;
        dmarc=pass (p=REJECT sp=REJECT) header.from=example.com
    """
)

# A verified but un-catalogued sender: passes auth, just isn't known to us.
UNKNOWN_VERIFIED = _msg(
    """
    From: Bob Stranger <bob@stranger.test>
    To: owner@chief.test
    Subject: Have you heard of our lord and savior
    Date: Mon, 21 Jul 2026 09:05:00 -0700
    Message-ID: <unknown-1@stranger.test>
    Authentication-Results: mx.google.com;
        spf=pass smtp.mailfrom=bob@stranger.test;
        dkim=pass header.i=@stranger.test;
        dmarc=pass header.from=stranger.test
    """
)

# A spoof of the known Alice: From says alice@example.com, but every mechanism
# FAILS. Must not resolve to Alice's tier.
SPOOF_AUTH_FAILS = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:10:00 -0700
    Message-ID: <spoof-1@evil.test>
    Authentication-Results: mx.google.com;
        spf=fail smtp.mailfrom=alice@example.com;
        dkim=fail;
        dmarc=fail header.from=example.com
    """
)

# The subtle spoof: From claims the catalogued Alice, but the *authenticated*
# envelope (smtp.mailfrom) is mallory@evil.test. Keying on From would match
# Alice; keying on the verified sender must not.
SPOOF_FROM_ONLY = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:12:00 -0700
    Message-ID: <spoof-2@evil.test>
    Authentication-Results: mx.google.com;
        spf=pass smtp.mailfrom=mallory@evil.test;
        dkim=pass header.i=@evil.test
    """
)


def _catalog() -> SenderTrustCatalog:
    return SenderTrustCatalog({"alice@example.com": "trusted"})


# --- Acceptance criterion 1: round-trip parse -> lookup -> serialize ---------


def test_catalog_round_trips() -> None:
    original = SenderTrustCatalog(
        {"alice@example.com": "trusted", "carol@example.org": "known"}
    )
    text = original.serialize()

    reparsed = SenderTrustCatalog.parse(text)
    assert reparsed.as_dict() == original.as_dict()
    # lookup survives the round-trip
    assert reparsed.lookup("alice@example.com") == "trusted"
    assert reparsed.lookup("carol@example.org") == "known"
    # re-serializing the reparsed catalog is stable
    assert reparsed.serialize() == text


def test_catalog_lookup_is_case_insensitive() -> None:
    cat = SenderTrustCatalog({"Alice@Example.com": "trusted"})
    assert cat.lookup("alice@EXAMPLE.com") == "trusted"


# --- Acceptance criterion 4: lookup keys on the verified sender --------------


def test_verified_sender_extracted_from_auth_results() -> None:
    assert verified_sender(KNOWN_VERIFIED) == "alice@example.com"


def test_verified_sender_is_the_envelope_not_the_from_header() -> None:
    # From claims alice@example.com; the authenticated envelope is mallory.
    assert verified_sender(SPOOF_FROM_ONLY) == "mallory@evil.test"


def test_lookup_keys_on_verified_sender_not_from() -> None:
    cat = _catalog()  # knows alice@example.com
    decision = resolve_sender(SPOOF_FROM_ONLY, cat)
    # From matches a catalogued address, but the verified sender is mallory,
    # who is unknown — so this must NOT resolve to Alice's tier.
    assert decision.verified_sender == "mallory@evil.test"
    assert decision.tier is None
    assert not decision.known


# --- Acceptance criterion 2: failed auth is never treated as known ----------


def test_failed_auth_is_not_verified() -> None:
    assert verified_sender(SPOOF_AUTH_FAILS) is None


def test_failed_auth_not_matched_even_when_from_is_catalogued() -> None:
    cat = _catalog()  # knows alice@example.com
    decision = resolve_sender(SPOOF_AUTH_FAILS, cat)
    assert decision.verified_sender is None
    assert decision.tier is None
    assert not decision.known
    # falls through to the unknown/owner-confirm path
    assert decision.action is Action.OWNER_CONFIRM


def test_dmarc_fail_overrides_a_passing_mechanism() -> None:
    # spf passes for a different domain but DMARC (From alignment) fails ->
    # the From is not authenticated, so nothing is trusted.
    raw = _msg(
        """
        From: Alice Example <alice@example.com>
        Subject: nope
        Message-ID: <x@evil.test>
        Authentication-Results: mx.google.com;
            spf=pass smtp.mailfrom=drift@evil.test;
            dmarc=fail header.from=example.com
        """
    )
    assert verified_sender(raw) is None


# --- Acceptance criterion 3: known -> tier; unknown -> metadata log + confirm


def test_known_verified_sender_resolves_to_tier() -> None:
    decision = resolve_sender(KNOWN_VERIFIED, _catalog())
    assert decision.verified_sender == "alice@example.com"
    assert decision.tier == "trusted"
    assert decision.known
    assert decision.action is Action.ALLOW
    # a known sender is allowed through, not logged for confirmation
    assert decision.metadata is None


def test_unknown_verified_sender_is_metadata_only_and_owner_confirm() -> None:
    decision = resolve_sender(UNKNOWN_VERIFIED, _catalog())
    assert decision.verified_sender == "bob@stranger.test"
    assert decision.tier is None
    assert not decision.known
    assert decision.action is Action.OWNER_CONFIRM

    meta = decision.metadata
    assert meta is not None
    # metadata-only: envelope facts are captured...
    assert meta.verified_sender == "bob@stranger.test"
    assert "bob@stranger.test" in meta.from_header
    assert meta.subject == "Have you heard of our lord and savior"
    assert meta.message_id == "<unknown-1@stranger.test>"
    # ...but the body is NEVER part of the log.
    rendered = repr(meta) + str(meta)
    assert "secret body" not in rendered
    assert not hasattr(meta, "body")


def test_owner_confirm_adds_sender_at_a_tier() -> None:
    # The owner-confirm flow's terminal action: catalog the now-approved sender
    # so the next verified message from them resolves to a tier.
    cat = _catalog()
    assert resolve_sender(UNKNOWN_VERIFIED, cat).action is Action.OWNER_CONFIRM

    cat.set_tier("bob@stranger.test", "known")
    after = resolve_sender(UNKNOWN_VERIFIED, cat)
    assert after.tier == "known"
    assert after.action is Action.ALLOW
