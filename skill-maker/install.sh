#!/usr/bin/env bash
# Deterministic install for skill-maker: copy the skill verbatim. Prompt
# policy only — nothing else to do.
#
# Sources come from this script's own dir (wherever the package is
# cloned); destination paths are relative to the repo root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/skill-maker"
dst="skills/skill-maker"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply skill-maker --source cloned
