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


def _core_candidates() -> list[Path]:
    """Where a chief core checkout might live, most-specific first."""
    candidates: list[Path] = []
    env = os.environ.get("CHIEF_CORE")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "code" / "chief")
    candidates.append(Path.home() / "chief-gf")
    # repo-relative sibling checkout (…/chief next to …/chief-packages)
    candidates.append(repo_root().parent / "chief")
    return candidates


def _resolve_core() -> Path | None:
    """First candidate that actually contains the loader module wins."""
    for cand in _core_candidates():
        if (cand / "src" / "chief" / "mcp_manifest.py").is_file():
            return cand
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


@pytest.fixture
def core_available() -> bool:
    return CORE_AVAILABLE


@pytest.fixture
def packages_root() -> Path:
    return repo_root()
