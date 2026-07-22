#!/usr/bin/env bash
# Deterministic install for google-workspace: land the skill, its bundled
# gw_aliases.py, and the tool_gate.py write-gate catalog (the read/write split
# INSTALL step 5 registers on the gate), then seed an empty alias file. The MCP
# server version pin and the OAuth grant are still separate slices (see
# INSTALL.md's TODO(#7)/(#8)/(#9) steps), not here.
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
# tool_gate.py is the read/write tool catalog (#6) — land it at the path the
# skill/INSTALL document so `uv run python .../tool_gate.py reads` works to list
# the read tool names to register on the gate's approve set.
cp "$here/scripts/tool_gate.py" "$dst/scripts/tool_gate.py"

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
