"""Alias -> address resolution for the google-workspace package (slice #3).

Pure logic — no chief core import needed, so these run everywhere. The module
under test lives at ``google-workspace/scripts/gw_aliases.py``; ``conftest``
puts that dir on ``sys.path`` so it imports here by bare name.
"""

from __future__ import annotations

import pytest
from conftest import repo_root
from gw_aliases import (
    AliasFile,
    AliasFileError,
    AmbiguousAliasError,
    MissingAccountError,
    UnknownAliasError,
)

SAMPLE = "work: me@company.com\npersonal: me@gmail.com\n"

SKILL_PATH = (
    repo_root() / "google-workspace" / "skills" / "google-workspace" / "SKILL.md"
)


def test_alias_file_round_trips() -> None:
    """parse -> resolve -> serialize: the mapping survives a round-trip and
    serialize is canonical (names sorted), so re-parsing is stable."""
    af = AliasFile.parse(SAMPLE)
    assert af.resolve("work") == "me@company.com"
    assert af.serialize() == "personal: me@gmail.com\nwork: me@company.com\n"
    assert AliasFile.parse(af.serialize()).entries == af.entries


def test_known_alias_resolves_unknown_raises() -> None:
    af = AliasFile.parse(SAMPLE)
    assert af.resolve("personal") == "me@gmail.com"
    with pytest.raises(UnknownAliasError):
        af.resolve("school")


def test_full_address_passes_through() -> None:
    """A tool always receives an address, so an already-resolved known address
    resolves to itself; an unknown address is not silently accepted."""
    af = AliasFile.parse(SAMPLE)
    assert af.resolve("me@company.com") == "me@company.com"
    with pytest.raises(UnknownAliasError):
        af.resolve("stranger@nowhere.com")


def test_omitted_account_never_defaults() -> None:
    """An omitted account raises even when exactly one account exists — the
    server's default-account fallback is deliberately left unset."""
    single = AliasFile.parse("work: solo@company.com\n")
    for missing in (None, "", "   "):
        with pytest.raises(MissingAccountError):
            single.resolve(missing)


def test_ambiguous_alias_raises() -> None:
    """Two entries whose names collide case-insensitively but point to distinct
    addresses make that name ambiguous — resolution refuses to guess."""
    af = AliasFile.parse("team: a@x.com\nTeam: b@y.com\n")
    with pytest.raises(AmbiguousAliasError):
        af.resolve("team")


def test_case_insensitive_single_match_resolves() -> None:
    af = AliasFile.parse("Work: me@company.com\n")
    assert af.resolve("work") == "me@company.com"


def test_malformed_and_duplicate_lines_raise() -> None:
    with pytest.raises(AliasFileError):
        AliasFile.parse("no-separator-here\n")
    with pytest.raises(AliasFileError):
        AliasFile.parse("work: not-an-email\n")
    with pytest.raises(AliasFileError):
        AliasFile.parse("work: a@x.com\nwork: b@y.com\n")


def test_comments_and_blanks_ignored() -> None:
    text = "# accounts\n\nwork: me@company.com\n\n# trailing note\n"
    af = AliasFile.parse(text)
    assert af.entries == {"work": "me@company.com"}


def test_skill_documents_explicit_account_and_aliases() -> None:
    """The SKILL must tell chief to pass an explicit account resolved via the
    alias file, and to ask when a name is ambiguous rather than guessing."""
    text = SKILL_PATH.read_text().lower()
    for token in ("account", "alias", "ambiguous", "ask", "gw_aliases"):
        assert token in text, f"SKILL.md must mention {token!r}"
