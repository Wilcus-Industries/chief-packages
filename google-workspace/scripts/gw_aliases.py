#!/usr/bin/env python3
"""Deterministic alias -> address resolution for the google-workspace package.

Every Google Workspace tool call carries an explicit ``account`` argument: a
full email address. Personal and work accounts run in one process with
credentials keyed per address, so the agent must always say *which* account a
call is for. The owner keeps a small alias file mapping friendly names
("work") to full addresses; this module parses that file and resolves a name —
or an already-full address — to the address a tool call should carry.

Resolution *raises* rather than guessing when the account is omitted, unknown,
or ambiguous. The upstream MCP server has a default-account fallback; this
package leaves it unset on purpose, so a missing account surfaces here as an
error the agent resolves by asking the owner, never a silent pick.

Stdlib-only (the package declares no ``python_deps``). The alias file is a
simple ``name: address`` line format with ``#`` comments and blank lines::

    # google-workspace account aliases
    work: me@company.com
    personal: me@gmail.com
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

_SEP = ":"
_COMMENT = "#"
# Where the owner keeps the alias file, relative to chief's run root. A later
# slice may make this configurable via the ``google_workspace`` config key.
_DEFAULT_ALIAS_PATH = "data/google-workspace/aliases.txt"


class AliasError(ValueError):
    """Base class for every alias-resolution failure."""


class AliasFileError(AliasError):
    """The alias file is malformed — a bad line or a duplicate name."""


class MissingAccountError(AliasError):
    """No account was given. The default-account fallback is deliberately
    left unset, so an omitted account is an error, never a silent default."""


class UnknownAliasError(AliasError):
    """The name is neither a known alias nor a known account address."""


class AmbiguousAliasError(AliasError):
    """The name matches more than one account with differing addresses, so
    resolving it would mean guessing which the owner meant."""


def _looks_like_email(value: str) -> bool:
    """Minimal address sanity check — one ``@`` with a dotted domain."""
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


def _validate_alias_name(name: str) -> None:
    """Reject names that break the ``parse(x.serialize())`` round-trip.

    ``parse`` can never emit such names (a ``#`` line is skipped as a comment,
    ``:`` is the separator, newlines split lines), so this only bites the public
    ``AliasFile.__init__`` constructor — but a name that serializes to a comment
    or re-parses as a different entry would silently corrupt the mapping.
    """
    if name.startswith(_COMMENT):
        raise AliasFileError(
            f"alias name {name!r} may not start with {_COMMENT!r} "
            "(it would serialize as a comment and vanish on re-parse)"
        )
    if _SEP in name or "\n" in name or "\r" in name:
        raise AliasFileError(
            f"alias name {name!r} may not contain {_SEP!r} or a newline "
            "(it would mis-parse on re-read)"
        )


class AliasFile:
    """An immutable friendly-name -> address map with strict resolution."""

    def __init__(self, entries: Mapping[str, str]) -> None:
        validated: dict[str, str] = {}
        for name, address in entries.items():
            key = name.strip()
            addr = address.strip()
            if not key:
                raise AliasFileError("alias name must be non-empty")
            _validate_alias_name(key)
            if key in validated:
                raise AliasFileError(
                    f"duplicate alias name {key!r} (names collide after trimming)"
                )
            if not _looks_like_email(addr):
                raise AliasFileError(f"{addr!r} is not an email address")
            validated[key] = addr
        # An entry whose *name* equals another entry's *address* is unreachable
        # as an alias: ``resolve`` checks exact-address pass-through first, so
        # the name silently resolves to itself instead of its alias target.
        # Refuse the overlap rather than shadow. Case-folded to match resolve's
        # address-membership check.
        addr_folds = {a.casefold() for a in validated.values()}
        for key in validated:
            if key.casefold() in addr_folds:
                raise AliasFileError(
                    f"alias name {key!r} is also an account address in the file "
                    "— it would be shadowed by exact-address pass-through; "
                    "rename the alias"
                )
        self._entries = validated

    @property
    def entries(self) -> dict[str, str]:
        """A copy of the name -> address map (mutating it is a no-op)."""
        return dict(self._entries)

    @classmethod
    def parse(cls, text: str) -> AliasFile:
        """Parse the ``name: address`` line format. Blank and ``#`` lines are
        skipped; a malformed or duplicate line raises ``AliasFileError``."""
        entries: dict[str, str] = {}
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith(_COMMENT):
                continue
            if _SEP not in line:
                raise AliasFileError(
                    f"line {lineno}: expected 'name: address', got {raw!r}"
                )
            name, _, address = line.partition(_SEP)
            name = name.strip()
            addr = address.strip()
            if not name:
                raise AliasFileError(f"line {lineno}: empty alias name")
            if name in entries:
                raise AliasFileError(f"line {lineno}: duplicate alias {name!r}")
            # Validate the address in the parse loop so a bad address reports its
            # line number, matching every other parse diagnostic (the
            # constructor re-checks, but without a line number to attach).
            if not _looks_like_email(addr):
                raise AliasFileError(
                    f"line {lineno}: {addr!r} is not an email address"
                )
            entries[name] = addr
        return cls(entries)

    def serialize(self) -> str:
        """Canonical text form: names sorted, one ``name: address`` per line.
        ``AliasFile.parse(x.serialize())`` round-trips the mapping."""
        return "".join(
            f"{name}{_SEP} {self._entries[name]}\n" for name in sorted(self._entries)
        )

    def resolve(self, account: str | None) -> str:
        """Resolve ``account`` (an alias name or a known address) to an address.

        Raises ``MissingAccountError`` for an omitted account,
        ``AmbiguousAliasError`` when the name matches several accounts, and
        ``UnknownAliasError`` when it matches none. It never falls back to a
        default account.
        """
        if account is None or not account.strip():
            raise MissingAccountError(
                "no account given: the google-workspace default-account "
                "fallback is unset — pass an explicit account (alias or address)"
            )
        key = account.strip()
        folded = key.casefold()
        # Address pass-through is case-folded to match the alias-name lookup
        # below (addresses are effectively case-insensitive), so a known address
        # in any case resolves to its canonical stored form rather than raising.
        addr_by_fold = {addr.casefold(): addr for addr in self._entries.values()}
        if folded in addr_by_fold:
            return addr_by_fold[folded]
        matches = {
            addr for name, addr in self._entries.items() if name.casefold() == folded
        }
        if len(matches) > 1:
            raise AmbiguousAliasError(
                f"{key!r} matches multiple accounts "
                f"({', '.join(sorted(matches))}) — ask the owner which one"
            )
        if matches:
            return next(iter(matches))
        raise UnknownAliasError(f"{key!r} is not a known alias or account address")


def main(argv: list[str] | None = None) -> int:
    """CLI: resolve an alias/address, or print the alias file canonically.

    ``resolve`` exits 1 on any alias error (unknown/ambiguous/omitted) with the
    reason on stderr, so a caller can distinguish "no such account" from a
    genuinely resolved address on stdout.
    """
    parser = argparse.ArgumentParser(
        prog="gw_aliases",
        description="Resolve a google-workspace account alias to an address.",
    )
    parser.add_argument(
        "--file",
        default=_DEFAULT_ALIAS_PATH,
        help="alias file (name: address per line); default %(default)s",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    resolve_cmd = sub.add_parser("resolve", help="resolve an alias or address")
    resolve_cmd.add_argument("account", help="alias name or full email address")
    sub.add_parser("list", help="print the alias file in canonical form")

    args = parser.parse_args(argv)
    path = Path(args.file)
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"gw_aliases: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        aliases = AliasFile.parse(text)
        if args.command == "list":
            sys.stdout.write(aliases.serialize())
        else:
            print(aliases.resolve(args.account))
    except AliasError as exc:
        print(f"gw_aliases: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
