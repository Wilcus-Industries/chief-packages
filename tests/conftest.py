"""Shared test fixtures.

The interesting checks run each package's ``mcp_servers`` block through chief
core's *real* manifest loader (``chief.mcp_manifest``). That module is
stdlib-only and imports standalone, so we reach it by putting a core checkout's
``src/`` on ``sys.path`` — no pip install, no core changes. This runs at import
time (pytest imports conftest before collecting), so test modules can just
``import chief.mcp_manifest``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def repo_root() -> Path:
    """Repo root = the directory holding the per-package dirs (and this repo's
    ``tests/``)."""
    return Path(__file__).resolve().parents[1]


def _has_loader(cand: Path) -> bool:
    return (cand / "src" / "chief" / "mcp_manifest.py").is_file()


def _core_candidates() -> list[Path]:
    """Where a chief core checkout might live, most-specific first.

    A set ``$CHIEF_CORE`` is authoritative: if it points at something without
    the loader module we do NOT fall through to the defaults (a typo'd pin
    would otherwise validate against a *different* core with no signal), so it
    is intentionally omitted here — ``_resolve_core`` handles it up front.
    """
    return [
        Path.home() / "code" / "chief",
        Path.home() / "chief-gf",
        # repo-relative sibling checkout (…/chief next to …/chief-packages)
        repo_root().parent / "chief",
    ]


def _truthy(val: str | None) -> bool:
    return val is not None and val.strip().lower() not in {"", "0", "false", "no"}


def _resolve_core() -> Path | None:
    """First candidate that actually contains the loader module wins.

    Fails loud in two cases the reviewer flagged:
    - ``$CHIEF_CORE`` set but invalid → the pin is authoritative, so a bad
      path is an error (never a silent fall-through to the defaults).
    - ``$CHIEF_CORE_REQUIRED`` truthy but nothing resolves → the real-loader
      tests would otherwise skip while the suite stays green; refuse instead.
    """
    env = os.environ.get("CHIEF_CORE")
    if env:
        pinned = Path(env).expanduser()
        if _has_loader(pinned):
            return pinned
        pytest.exit(
            f"$CHIEF_CORE={env!r} does not contain src/chief/mcp_manifest.py "
            "(expected a chief core checkout) — fix or unset the pin; "
            "not falling through to the default candidates",
            returncode=1,
        )

    for cand in _core_candidates():
        if _has_loader(cand):
            return cand

    if _truthy(os.environ.get("CHIEF_CORE_REQUIRED")):
        searched = ", ".join(str(c) for c in _core_candidates())
        pytest.exit(
            "CHIEF_CORE_REQUIRED is set but no chief core checkout was found "
            f"(searched: {searched}) — the real-loader tests would skip; set "
            "$CHIEF_CORE to a checkout or unset CHIEF_CORE_REQUIRED",
            returncode=1,
        )
    return None


def _ensure_core_on_path() -> bool:
    core = _resolve_core()
    if core is None:
        return False
    src = str(core / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return True


# Resolve + wire the core loader at import time so ``import chief.mcp_manifest``
# works in every test module that follows.
CORE_AVAILABLE: bool = _ensure_core_on_path()


def _package_scripts_on_path(package: str) -> None:
    """Put a package's ``scripts/`` dir on ``sys.path`` so its sandbox-run
    modules import bare in tests (e.g. ``import gw_aliases`` / ``sender_trust``).

    The package dirs are hyphenated (not importable as packages), so their
    scripts are reached the same way core is: by path insertion, at import
    time. Slices that ship testable package code (google-workspace #3–#6) reuse
    this instead of each re-deriving the path.
    """
    scripts = repo_root() / package / "scripts"
    if scripts.is_dir():
        src = str(scripts)
        if src not in sys.path:
            sys.path.insert(0, src)


_package_scripts_on_path("google-workspace")


@pytest.fixture
def core_available() -> bool:
    return CORE_AVAILABLE


@pytest.fixture
def packages_root() -> Path:
    return repo_root()
