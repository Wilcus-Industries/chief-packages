#!/usr/bin/env bash
# Deterministic install for the google-workspace alias helper: land the skill
# and its bundled gw_aliases.py, then seed an empty alias file. This wires ONLY
# the per-account alias helper the skill documents — the MCP server pin, OAuth
# grant, and write-gate are separate slices (see INSTALL.md's TODO(#7)/(#8)/(#9)
# steps), not here.
#
# Run it from your repo root, then `restart`. It copies from its own dir, so it
# works wherever the package is cloned; the destination skills/ + data/ paths
# are repo-root-relative.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/google-workspace"
dst="skills/google-workspace"
mkdir -p "$dst/scripts"
cp "$src/SKILL.md" "$dst/SKILL.md"
# gw_aliases.py lives at the package root but the skill documents it at
# skills/google-workspace/scripts/gw_aliases.py — copy it there so that path
# exists post-install.
cp "$here/scripts/gw_aliases.py" "$dst/scripts/gw_aliases.py"

# Seed an empty alias file once. Never clobber an existing one — the owner's
# name: address aliases are their own data.
store="data/google-workspace"
mkdir -p "$store"
aliases="$store/aliases.txt"
if [ ! -f "$aliases" ]; then
  cat >"$aliases" <<'EOF'
# google-workspace account aliases — one `name: address` per line.
# Blank lines and `#` comments are ignored. Add your accounts, e.g.:
#   work: me@company.com
#   personal: me@gmail.com
EOF
fi
