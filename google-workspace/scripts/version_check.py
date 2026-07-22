#!/usr/bin/env python3
"""Deterministic upstream-release check for the pinned Google Workspace server.

No model turn, no LLM anywhere in the path: this reads the installed pin
(``patches/upstream.pin`` — the tag + commit slice #7 landed), asks the upstream
GitHub repo for its **latest release**, compares the two tags, and emits an owner
notification **only** when a newer release exists. The same inputs always produce
the same verdict — it is a pure comparison, not a judgement, so it can run as an
unattended chief schedule or an OS-level timer without ever waking the model.

Why deterministic matters here: the server this package launches holds a Gmail
*send* credential and is pinned to an exact commit on purpose (see
``patches/upstream.pin``). Bumping the pin is a deliberate, owner-driven edit.
This check never bumps anything — it only tells the owner "a newer release is
out, consider bumping the pin", the same informational stance core's
``chief.install.updatecheck`` takes toward ``chief update``.

The GitHub "latest release" endpoint (``/repos/<owner>/<repo>/releases/latest``)
excludes drafts and pre-releases by construction, so ``tag_name`` is always the
newest *stable* tag — exactly what a pin should track.

Wiring is a platform choice made at install time (INSTALL.md): an OS timer can
run this fully LLM-free (branch on the exit code, pipe stdout to the notifier),
or a chief prompt-schedule can wake the agent only to *relay* the script's
verdict (the agent does no version reasoning — the comparison already happened
here). Either way the detection is this module, and this module alone.

Exit codes let a timer act without parsing stdout:
    0  up to date (nothing on stdout)
    10 a newer release exists (the notification is on stdout)
    2  the check could not run (reason on stderr) — never a false "up to date"

Stdlib-only, no ``chief`` import: ``urllib`` does the HTTP, ``json`` parses the
release. It ships as a package script (run in the sandbox / by a timer, imported
bare in tests) and must load with neither core nor a provider present.
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
from pathlib import Path
from typing import Any

#: Where the cloned package's pin lives once installed (run from the repo root).
#: Overridable with ``--pin`` for a different layout or a dry run.
DEFAULT_PIN_FILE = "data/packages/google-workspace/patches/upstream.pin"

#: Bounded like every network call chief makes — a stalled endpoint degrades to a
#: loud "could not check", never a hang.
DEFAULT_TIMEOUT_SECONDS = 20.0

# CLI exit codes (see the module docstring).
EXIT_UP_TO_DATE = 0
EXIT_ERROR = 2
EXIT_NEWER = 10


class VersionCheckError(Exception):
    """The check could not be completed (bad pin, unreachable endpoint, or a
    release payload with no usable tag). Never swallowed into a false verdict."""


# --------------------------------------------------------------------------- #
# Pin file + endpoint                                                         #
# --------------------------------------------------------------------------- #


def parse_pin(pin_file: str | os.PathLike[str]) -> dict[str, str]:
    """Parse the ``KEY=value`` pin file (same shape ``setup_upstream.sh`` sources).

    Blank lines and ``#`` comments are ignored. Raises ``VersionCheckError`` when
    the file is missing or names neither the repo nor a tag — a check with no pin
    to compare against must fail loud, not silently report "up to date".
    """
    path = Path(pin_file)
    try:
        raw = path.read_text()
    except OSError as exc:
        raise VersionCheckError(
            f"cannot read the upstream pin {path}: {exc} — run the install (it "
            "lands patches/upstream.pin) or pass --pin"
        ) from exc
    pin: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        pin[key.strip()] = value.strip()
    if not pin.get("UPSTREAM_REPO") or not pin.get("UPSTREAM_TAG"):
        raise VersionCheckError(
            f"pin {path} is missing UPSTREAM_REPO and/or UPSTREAM_TAG — cannot "
            "resolve which upstream release to compare against"
        )
    return pin


def releases_api_url(repo_url: str) -> str:
    """Derive the GitHub ``releases/latest`` API URL for a ``github.com`` repo.

    ``https://github.com/owner/repo(.git)`` → ``https://api.github.com/repos/
    owner/repo/releases/latest``. Raises ``VersionCheckError`` for a non-GitHub
    or unparseable repo URL rather than guessing an endpoint.
    """
    parts = urllib.parse.urlsplit(repo_url.strip())
    if parts.hostname not in {"github.com", "www.github.com"}:
        raise VersionCheckError(
            f"upstream repo {repo_url!r} is not a github.com URL — the release "
            "check only knows the GitHub releases API"
        )
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) < 2:
        raise VersionCheckError(
            f"upstream repo {repo_url!r} has no owner/repo path"
        )
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"


def fetch_latest_release(
    endpoint: str,
    *,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """GET the GitHub latest-release JSON. Raises ``VersionCheckError`` on any
    network / HTTP / decode failure — never returns a partial or empty verdict.

    A ``token`` (a GitHub PAT, optional) is sent as a bearer to lift the low
    unauthenticated rate limit; without one the public endpoint still answers.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        # GitHub rejects a request with no User-Agent, so name ourselves.
        "User-Agent": "chief-google-workspace-version-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise VersionCheckError(
            f"GitHub returned HTTP {exc.code} for {endpoint}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise VersionCheckError(
            f"release endpoint {endpoint} unreachable: {exc.reason}"
        ) from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise VersionCheckError(
            f"release endpoint returned non-JSON: {payload[:200]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise VersionCheckError(
            f"release endpoint returned a {type(data).__name__}, not an object"
        )
    return data


def latest_tag(release: dict[str, Any]) -> str:
    """The ``tag_name`` of a release payload, or raise. GitHub's latest-release
    endpoint omits drafts/pre-releases, so this is the newest stable tag."""
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise VersionCheckError(
            "release payload has no usable 'tag_name' — cannot compare versions"
        )
    return tag.strip()


# --------------------------------------------------------------------------- #
# Version comparison (deterministic, semver-ish)                              #
# --------------------------------------------------------------------------- #


def _int_prefix(segment: str) -> int:
    """The leading integer of a version segment (``22`` from ``22rc``), else 0."""
    digits = ""
    for ch in segment:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 0


def _prerelease_key(pre: str) -> tuple[tuple[int, int | str], ...]:
    """A comparable key for a semver pre-release string (``rc.1`` → …).

    Numeric identifiers sort by value and below alphanumeric ones (SemVer §11);
    each identifier becomes ``(0, int)`` or ``(1, str)`` so a tuple compare is
    total. Only consulted when two *pre-releases* of the same release tuple are
    compared — upstream ships plain ``vX.Y.Z`` finals, so this is belt-and-braces.
    """
    key: list[tuple[int, int | str]] = []
    for ident in pre.split("."):
        if ident.isdigit():
            key.append((0, int(ident)))
        else:
            key.append((1, ident))
    return tuple(key)


@dataclass(frozen=True)
class _VersionKey:
    release: tuple[int, ...]
    is_final: int  # 1 for a final release, 0 for a pre-release
    prerelease: tuple[tuple[int, int | str], ...]


def parse_version(tag: str) -> _VersionKey:
    """Parse a release tag (``v1.22.0``, ``1.22.0-rc.1``) into a comparable key.

    The optional leading ``v``/``V`` and any ``+build`` metadata are dropped; a
    ``-prerelease`` suffix ranks the tag below the same release without it.
    """
    core = tag.strip()
    if core[:1] in {"v", "V"}:
        core = core[1:]
    core, _, _build = core.partition("+")
    release_part, _, pre = core.partition("-")
    release = tuple(_int_prefix(seg) for seg in release_part.split(".") if seg)
    return _VersionKey(
        release=release or (0,),
        is_final=0 if pre else 1,
        prerelease=_prerelease_key(pre) if pre else (),
    )


def is_newer(latest_tag_str: str, pinned_tag: str) -> bool:
    """True when ``latest_tag_str`` is a strictly newer release than ``pinned_tag``.

    Compares the numeric release tuples first (zero-padded to equal length so
    ``v1.22`` and ``v1.22.0`` compare equal), then ranks a final release above a
    pre-release of the same tuple, then the pre-release identifiers.
    """
    latest = parse_version(latest_tag_str)
    pinned = parse_version(pinned_tag)
    width = max(len(latest.release), len(pinned.release))
    lr = latest.release + (0,) * (width - len(latest.release))
    pr = pinned.release + (0,) * (width - len(pinned.release))
    if lr != pr:
        return lr > pr
    if latest.is_final != pinned.is_final:
        return latest.is_final > pinned.is_final
    return latest.prerelease > pinned.prerelease


# --------------------------------------------------------------------------- #
# The check + the owner notification                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VersionCheckResult:
    """The deterministic verdict: is the installed pin behind upstream's latest?"""

    repo: str
    pinned_tag: str
    latest_tag: str
    latest_url: str
    newer: bool


def check(
    pin_file: str | os.PathLike[str] = DEFAULT_PIN_FILE,
    *,
    endpoint: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> VersionCheckResult:
    """Read the pin, fetch upstream's latest release, and compare — no model turn.

    ``endpoint`` overrides the derived GitHub API URL (tests / an offline dry run
    point it at a fixture). Raises ``VersionCheckError`` on any failure so a
    caller never mistakes "could not check" for "up to date".
    """
    pin = parse_pin(pin_file)
    repo = pin["UPSTREAM_REPO"]
    pinned = pin["UPSTREAM_TAG"]
    api = endpoint or releases_api_url(repo)
    release = fetch_latest_release(api, token=token, timeout=timeout)
    tag = latest_tag(release)
    html_url = release.get("html_url")
    latest_url = html_url if isinstance(html_url, str) and html_url else repo
    return VersionCheckResult(
        repo=repo,
        pinned_tag=pinned,
        latest_tag=tag,
        latest_url=latest_url,
        newer=is_newer(tag, pinned),
    )


def notification(result: VersionCheckResult) -> str | None:
    """The owner-facing message when a newer release exists, else ``None``.

    Mirrors ``chief.install.updatecheck.notice``: informational only, and it
    names the deliberate, owner-driven follow-up — bumping the pin — never an
    automatic update (the server holds a send credential; the pin is sacred).
    """
    if not result.newer:
        return None
    return (
        f"A newer google-workspace upstream release is available: "
        f"{result.latest_tag} (pinned: {result.pinned_tag}). "
        f"Review {result.latest_url}, then — if you want it — bump "
        f"patches/upstream.pin (tag + commit) and re-generate the patch, a "
        f"deliberate edit. This server holds a Gmail send credential, so nothing "
        f"updates automatically."
    )


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Run one deterministic release check. Prints the notification to stdout and
    exits ``10`` when a newer release exists, ``0`` when up to date, ``2`` on any
    error (reason on stderr) — so a timer can branch on the exit code alone."""
    parser = argparse.ArgumentParser(
        prog="version_check",
        description=(
            "Deterministically check whether a newer upstream Google Workspace "
            "MCP release exists than the installed pin (no model turn)."
        ),
    )
    parser.add_argument(
        "--pin",
        default=DEFAULT_PIN_FILE,
        help="path to upstream.pin (default %(default)s)",
    )
    parser.add_argument(
        "--releases-endpoint",
        default=None,
        help="override the GitHub releases API URL (fixture / offline dry run)",
    )
    parser.add_argument(
        "--token-env",
        default=None,
        help="name of an env var holding a GitHub token (optional, lifts the "
        "unauthenticated rate limit)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="network timeout in seconds (default %(default)s)",
    )
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env) if args.token_env else None
    try:
        result = check(
            args.pin,
            endpoint=args.releases_endpoint,
            token=token,
            timeout=args.timeout,
        )
    except VersionCheckError as exc:
        print(f"version_check: {exc}", file=sys.stderr)
        return EXIT_ERROR
    message = notification(result)
    if message is None:
        print(
            f"version_check: up to date (pinned {result.pinned_tag}, latest "
            f"{result.latest_tag})",
            file=sys.stderr,
        )
        return EXIT_UP_TO_DATE
    print(message)
    return EXIT_NEWER


if __name__ == "__main__":
    raise SystemExit(main())
