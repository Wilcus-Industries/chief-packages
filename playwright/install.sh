#!/usr/bin/env bash
# Deterministic install for playwright: npm-install the PINNED @playwright/mcp
# into data/playwright/server, create the profile/output/workspace dirs, land
# the skill + its scripts, wire config.yaml (server entry, gate lists,
# screening) through playwright_tools.py, and register the package.
#
# Run it from your repo root, then `restart`. It copies from its own dir, so it
# works wherever the package is cloned; destination paths are repo-root-relative.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

node_path="$(command -v node)" || { echo "install: node not found on PATH" >&2; exit 1; }
command -v npm >/dev/null || { echo "install: npm not found on PATH" >&2; exit 1; }
if [ "$(uname)" = "Darwin" ] && [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "install: Google Chrome not found in /Applications (the server drives" \
    "real Chrome: --browser chrome)" >&2
  exit 1
fi

pin="$(python3 "$here/scripts/playwright_tools.py" pin)"
base="data/playwright"
mkdir -p "$base/server" "$base/output" "$base/workspace"
# The profile holds live session cookies — treat it like a credential.
mkdir -p "$base/profile"
chmod 700 "$base/profile"
npm install --prefix "$base/server" --no-audit --no-fund --loglevel=error \
  "@playwright/mcp@${pin}"

dst="skills/playwright"
mkdir -p "$dst/scripts"
cp "$here/skills/playwright/SKILL.md" "$dst/SKILL.md"
cp "$here/scripts/playwright_tools.py" "$here/scripts/login.sh" \
  "$here/scripts/login.mjs" "$dst/scripts/"

uv run python "$here/scripts/playwright_tools.py" wire . "$(dirname "$node_path")"

uv run python -m chief.registry_apply playwright --source cloned
