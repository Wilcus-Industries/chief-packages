"""Proof that the heavier core seams the later google-workspace slices need are
importable in this repo's test env.

Unlike the stdlib-only ``chief.mcp_manifest`` loader (covered by the manifest
tests), the seams #5 (screening) and #6 (write gate) build on — ``chief.gate``,
``chief.hooks.posttool``, ``chief.classifiers`` — transitively import core's
third-party runtime deps (httpx, sqlalchemy, …). Those deps are declared in this
repo's dev group precisely so these imports resolve against the sys.path'd local
core checkout. This test is the canary: if a dep goes missing or a seam moves,
it fails loudly here instead of surfacing as a confusing collection error in a
downstream slice.

Skips (loudly) only when no core checkout is reachable — same contract as the
manifest loader tests.
"""

from __future__ import annotations

import importlib

import pytest

# The seams the google-workspace write path (#5/#6) will import for real.
_CORE_SEAMS = ("chief.gate", "chief.hooks.posttool", "chief.classifiers")


@pytest.mark.parametrize("module_name", _CORE_SEAMS)
def test_core_seam_imports(module_name: str, core_available: bool) -> None:
    if not core_available:
        pytest.skip(
            "chief core checkout not found (set $CHIEF_CORE or clone next to "
            "chief-packages) — cannot import the real core seams; on a dev "
            "machine with core checked out this must actually run, not skip"
        )
    module = importlib.import_module(module_name)
    assert module is not None
