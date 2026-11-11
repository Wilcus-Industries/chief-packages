#!/usr/bin/env bash
# Deterministic install for apple-findmy: install the peekaboo CLI and copy
# the skill verbatim. The interactive parts (Screen Recording + Accessibility
# grants) are NOT here — the agent walks them in INSTALL.md.
#
# Sources come from this script's own dir (wherever the package is
# cloned); destination paths are relative to the repo root. macOS-only (requires Homebrew).
set -euo pipefail

# peekaboo — the UI-automation CLI chief drives (via the shell tool) to read
# FindMy.app's window as text. Idempotent: skip if already on PATH.
if ! command -v peekaboo >/dev/null 2>&1; then
  brew install steipete/tap/peekaboo
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/apple-findmy"
dst="skills/apple-findmy"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply apple-findmy --source cloned
