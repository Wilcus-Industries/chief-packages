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
    parse_authentication_results,
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

# From claims the catalogued Alice, but the *authenticated* envelope
# (smtp.mailfrom) is a different mailbox at the same DMARC-aligned domain —
# mallory@example.com — under a genuine mx.google.com boundary header. Keying on
# From would match Alice; keying on the verified sender must not.
ENVELOPE_NOT_FROM = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:12:00 -0700
    Message-ID: <envelope-not-from@example.com>
    Authentication-Results: mx.google.com;
        spf=pass smtp.mailfrom=mallory@example.com;
        dkim=pass header.i=@example.com header.s=sel;
        dmarc=pass header.from=example.com
    """
)


# --- Fix 1: forged Authentication-Results NOT added at our trust boundary ----

# No genuine mx.google.com header. Mail imported via messages.insert / IMAP
# APPEND / "Import mail" carries only an attacker-supplied AR header whose
# authserv-id (attacker.test) is not our boundary. Its pass verdicts, and the
# catalogued From they'd authenticate, must be ignored entirely.
FORGED_AR_WRONG_AUTHSERV = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:20:00 -0700
    Message-ID: <forged-1@evil.test>
    Authentication-Results: attacker.test;
        spf=pass smtp.mailfrom=alice@example.com;
        dkim=pass header.i=@example.com;
        dmarc=pass header.from=example.com
    """
)

# A message with no Authentication-Results header at our boundary at all.
NO_BOUNDARY_AR = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: hello
    Date: Mon, 21 Jul 2026 09:22:00 -0700
    Message-ID: <no-ar-1@example.com>
    """
)

# A forged AR (wrong authserv-id) prepended ABOVE the genuine mx.google.com one.
# The forged top header claims the catalogued Alice; the genuine boundary header
# authenticates the un-catalogued mallory. Only the boundary header may win.
FORGED_ABOVE_GENUINE = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:24:00 -0700
    Message-ID: <forged-2@evil.test>
    Authentication-Results: attacker.test;
        spf=pass smtp.mailfrom=alice@example.com;
        dmarc=pass header.from=example.com;
    Authentication-Results: mx.google.com;
        spf=pass smtp.mailfrom=mallory@example.com;
        dmarc=pass header.from=example.com
    """
)

# A genuine boundary header whose authserv-id differs from the trusted id only
# by case, a version token, and CFWS — must still match.
GENUINE_AR_CASE_AND_VERSION = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: Quarterly numbers
    Date: Mon, 21 Jul 2026 09:26:00 -0700
    Message-ID: <case-1@example.com>
    Authentication-Results: MX.GOOGLE.COM 1 (added by boundary);
        spf=pass smtp.mailfrom=alice@example.com;
        dmarc=pass header.from=example.com
    """
)

# A forged AR whose authserv-id is EMPTY (just a leading ';'). If a future
# config wiring yields a blank/None trusted_authserv_id, an exact-match on ""
# would trust this forgery — the empty-id pin must fail closed instead.
FORGED_EMPTY_AUTHSERV = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:30:00 -0700
    Message-ID: <forged-empty@evil.test>
    Authentication-Results: ;
        spf=pass smtp.mailfrom=alice@example.com;
        dmarc=pass header.from=example.com
    """
)

# Two authserv-ids that resemble the trusted mx.google.com but differ in format:
# a hyphen for the second dot, and a trailing suffix. An exact-match pin (not a
# substring / prefix match) must reject both.
FORGED_LOOKALIKE_AUTHSERV = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:32:00 -0700
    Message-ID: <lookalike-1@evil.test>
    Authentication-Results: mx-google.com;
        spf=pass smtp.mailfrom=alice@example.com;
        dmarc=pass header.from=example.com
    """
)

FORGED_SUFFIX_AUTHSERV = _msg(
    """
    From: Alice Example <alice@example.com>
    To: owner@chief.test
    Subject: wire the money now
    Date: Mon, 21 Jul 2026 09:34:00 -0700
    Message-ID: <suffix-1@evil.test>
    Authentication-Results: mx.google.com.evil;
        spf=pass smtp.mailfrom=alice@example.com;
        dmarc=pass header.from=example.com
    """
)

# --- Fix 2: a bare SPF pass on a shared/multi-tenant domain, DMARC absent ----

# shared.test hosts many principals. SPF authenticates only the envelope
# *domain*, so a principal can put a catalogued victim's local part in
# smtp.mailfrom and still pass SPF. With no DMARC verdict to assert From
# alignment, this must NOT authenticate victim@shared.test.
SHARED_DOMAIN_SPF_NO_DMARC = _msg(
    """
    From: Victim <victim@shared.test>
    To: owner@chief.test
    Subject: please approve
    Date: Mon, 21 Jul 2026 09:28:00 -0700
    Message-ID: <shared-1@shared.test>
    Authentication-Results: mx.google.com;
        spf=pass smtp.mailfrom=victim@shared.test
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
    assert verified_sender(ENVELOPE_NOT_FROM) == "mallory@example.com"


def test_lookup_keys_on_verified_sender_not_from() -> None:
    cat = _catalog()  # knows alice@example.com
    decision = resolve_sender(ENVELOPE_NOT_FROM, cat)
    # From matches a catalogued address, but the verified sender is mallory,
    # who is unknown — so this must NOT resolve to Alice's tier.
    assert decision.verified_sender == "mallory@example.com"
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


# --- Security fix 1: authserv-id pinning (RFC 8601 trust boundary) -----------


def test_forged_ar_with_wrong_authserv_id_is_ignored() -> None:
    # The only AR header was inserted by an attacker (authserv-id attacker.test),
    # not our boundary MTA — its pass verdicts must not authenticate anyone.
    assert verified_sender(FORGED_AR_WRONG_AUTHSERV) is None


def test_forged_ar_does_not_resolve_a_catalogued_from() -> None:
    cat = _catalog()  # knows alice@example.com
    decision = resolve_sender(FORGED_AR_WRONG_AUTHSERV, cat)
    # From names the catalogued Alice and the forged AR says pass, but the
    # header wasn't added at our boundary -> unverified -> owner-confirm.
    assert decision.verified_sender is None
    assert decision.tier is None
    assert not decision.known
    assert decision.action is Action.OWNER_CONFIRM


def test_no_boundary_ar_fails_closed() -> None:
    assert verified_sender(NO_BOUNDARY_AR) is None
    assert resolve_sender(NO_BOUNDARY_AR, _catalog()).action is Action.OWNER_CONFIRM


def test_genuine_boundary_ar_is_trusted() -> None:
    # A real mx.google.com boundary header authenticates the catalogued sender.
    assert verified_sender(KNOWN_VERIFIED) == "alice@example.com"
    assert resolve_sender(KNOWN_VERIFIED, _catalog()).action is Action.ALLOW


def test_authserv_id_match_ignores_case_and_version() -> None:
    # 'MX.GOOGLE.COM 1 (comment)' still pins to the trusted mx.google.com id.
    assert verified_sender(GENUINE_AR_CASE_AND_VERSION) == "alice@example.com"


def test_forged_ar_above_genuine_does_not_win() -> None:
    # A forged attacker.test header prepended above the genuine mx.google.com
    # one claims Alice; only the boundary header (mallory) may be read.
    assert verified_sender(FORGED_ABOVE_GENUINE) == "mallory@example.com"
    decision = resolve_sender(FORGED_ABOVE_GENUINE, _catalog())
    assert decision.verified_sender == "mallory@example.com"
    assert decision.tier is None
    assert decision.action is Action.OWNER_CONFIRM


def test_trusted_authserv_id_is_configurable() -> None:
    # The boundary id is overridable (google_workspace.trusted_authserv_id):
    # pinning to attacker.test would read that header — proving the default
    # mx.google.com is what rejects the forgery, not an accident of parsing.
    assert (
        verified_sender(FORGED_AR_WRONG_AUTHSERV, trusted_authserv_id="attacker.test")
        == "alice@example.com"
    )


def test_blank_trusted_authserv_id_fails_closed() -> None:
    # A blank/whitespace/None trusted_authserv_id (e.g. a future config wiring
    # yields "") must trust NOTHING — not match a forged empty-authserv-id AR.
    # Without the guard, "" == "" trusts the forgery and authenticates Alice.
    blanks: tuple[str | None, ...] = ("", "   ", None)
    for blank in blanks:
        assert parse_authentication_results(FORGED_EMPTY_AUTHSERV, blank) == []
        assert verified_sender(FORGED_EMPTY_AUTHSERV, trusted_authserv_id=blank) is None
        decision = resolve_sender(
            FORGED_EMPTY_AUTHSERV,
            _catalog(),  # knows alice@example.com
            trusted_authserv_id=blank,
        )
        # From names the catalogued Alice, but nothing is authenticated ->
        # owner-confirm, never her tier.
        assert decision.verified_sender is None
        assert decision.tier is None
        assert decision.action is Action.OWNER_CONFIRM


def test_lookalike_authserv_id_is_rejected() -> None:
    # The pin is an exact match, not a substring/prefix/suffix one: an id that
    # only resembles mx.google.com (a hyphen, or a trailing suffix) is a
    # different, attacker-chosen authserv-id and must fail closed.
    assert verified_sender(FORGED_LOOKALIKE_AUTHSERV) is None
    assert verified_sender(FORGED_SUFFIX_AUTHSERV) is None
    for raw in (FORGED_LOOKALIKE_AUTHSERV, FORGED_SUFFIX_AUTHSERV):
        decision = resolve_sender(raw, _catalog())
        assert decision.verified_sender is None
        assert decision.tier is None
        assert decision.action is Action.OWNER_CONFIRM


# --- Security fix 2: an unaligned SPF/DKIM pass without DMARC is not trusted -


def test_unaligned_spf_without_dmarc_is_not_trusted() -> None:
    # Shared-domain SPF pass, no DMARC verdict -> the local part is
    # unauthenticated (SPF proves only the envelope domain).
    assert verified_sender(SHARED_DOMAIN_SPF_NO_DMARC) is None


def test_shared_domain_spf_no_dmarc_routes_to_owner_confirm() -> None:
    cat = SenderTrustCatalog({"victim@shared.test": "trusted"})
    decision = resolve_sender(SHARED_DOMAIN_SPF_NO_DMARC, cat)
    # Even though victim@shared.test is catalogued and SPF passed, the absent
    # DMARC alignment means we cannot trust the local part -> owner-confirm.
    assert decision.verified_sender is None
    assert decision.tier is None
    assert decision.action is Action.OWNER_CONFIRM
