#!/usr/bin/env bash
# Deterministic install for maps: copy the skill + its bundled client script
# verbatim. Nothing interactive — no config, no secrets, stdlib-only.
#
# Sources come from this script's own dir (wherever the package is
# cloned); destination paths are relative to the repo root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/maps"
dst="skills/maps"
mkdir -p "$dst/scripts"
cp "$src/SKILL.md" "$dst/SKILL.md"
cp "$here/scripts/maps_client.py" "$dst/scripts/maps_client.py"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply maps --source cloned
