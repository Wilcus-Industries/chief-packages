#!/usr/bin/env bash
# Deterministic install for duckduckgo-search: copy the skill verbatim. The
# Python-dependency self-edit (adding ddgs to pyproject) is NOT here — the
# agent does it from INSTALL.md, guarded by the done-check.
#
# Sources come from this script's own dir (wherever the package is
# cloned); destination paths are relative to the repo root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/duckduckgo-search"
dst="skills/duckduckgo-search"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply duckduckgo-search --source cloned
