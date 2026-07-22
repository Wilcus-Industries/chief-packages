#!/usr/bin/env bash
# Deterministic install for google-workspace: land the skill and its bundled
# gw_aliases.py, seed an empty alias file, and seed the inbound-Gmail screening
# classifier (#5). This wires the per-account alias helper the skill documents
# and the screening classifier def; the MCP server pin, OAuth grant, and
# write-gate are separate slices (see INSTALL.md's TODO(#7)/(#8)/(#9)), not here.
# The post-tool screening hook itself is registered by the boot loader from the
# manifest's `hooks:` block once the package is recorded in data/installed.yaml
# (INSTALL.md step 7) — no copy needed, the loader reads the cloned package dir.
#
# Parameters (env):
#   GATE_MODEL   model id for the inbound-Gmail screening classifier, chosen at
#                the install interview (optional; default openai/gpt-4.1-nano, a
#                fast/cheap OpenRouter guard model). Applied only on first
#                install, when the def is seeded — never overwrites owner edits.
#
# Run it from your repo root, then `restart`. It copies from its own dir, so it
# works wherever the package is cloned; the destination skills/ + data/ +
# classifiers/ paths are repo-root-relative.
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

# The inbound-Gmail screen is a core classifier, so its definition has to live
# in the classifiers dir, not the package. Never overwrite: the owner tunes this
# prompt in place, and a re-install must not silently revert their edits.
mkdir -p classifiers
if [ ! -f classifiers/gmail-inbound-screen.md ]; then
  cp "$here/classifiers/gmail-inbound-screen.md" \
    classifiers/gmail-inbound-screen.md
  # Pin the interview-chosen guard model into the freshly seeded def. The source
  # already carries the default; rewrite the one frontmatter line so a custom
  # choice sticks. sed -i.bak keeps this portable across GNU and BSD/macOS sed.
  gate_model="${GATE_MODEL:-openai/gpt-4.1-nano}"
  sed -i.bak -E "s|^model:.*|model: ${gate_model}|" \
    classifiers/gmail-inbound-screen.md
  rm -f classifiers/gmail-inbound-screen.md.bak
fi

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
