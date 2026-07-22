#!/usr/bin/env bash
# Deterministic install for google-workspace: land the skill, its bundled
# gw_aliases.py, and the tool_gate.py write-gate catalog (the read/write split
# INSTALL step 5 registers on the gate); seed an empty alias file; and seed the
# inbound-Gmail screening classifier (#5). The post-tool screening hook itself is
# registered by the boot loader from the manifest's `hooks:` block once the
# package is recorded in data/installed.yaml (INSTALL.md step 7) — no copy needed,
# the loader reads the cloned package dir. This script ALSO clones the pinned
# upstream MCP server, applies the auth-result-header patch (fail-loud), and
# warms its deps (setup_upstream.sh, #7). The OAuth grant is still a separate
# slice (INSTALL.md TODO(#8)/(#9)), not here.
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
# tool_gate.py is the read/write tool catalog (#6) — land it at the path the
# skill/INSTALL document so `uv run python .../tool_gate.py reads` works to list
# the read tool names to register on the gate's approve set.
cp "$here/scripts/tool_gate.py" "$dst/scripts/tool_gate.py"

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
  # Validate before interpolating into the sed program: a value with |, &, \, or
  # a newline would break the s|| expression or corrupt the seeded def. Owner-
  # supplied (not attacker-reachable), but cheap to harden — reject anything
  # outside a model-id's safe alphabet with a clear message.
  # ``[[ =~ ]]`` tests the whole string: the class excludes newline, so an
  # embedded newline fails ``$`` and is rejected (unlike a per-line grep).
  if [[ ! "$gate_model" =~ ^[A-Za-z0-9._:/-]+$ ]]; then
    echo "install: GATE_MODEL=${gate_model} has characters outside" \
      "[A-Za-z0-9._:/-]; refusing to seed the screening classifier def" >&2
    exit 1
  fi
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

# Pin + patch + warm the upstream MCP server (#7). Clones the exact commit in
# patches/upstream.pin to data/google-workspace/server, applies the auth-result-
# header patch, and warms deps. FAILS LOUD (set -e aborts the install) if the
# patch does not apply — a server that omits Authentication-Results leaves the
# sender-trust catalog unable to verify any sender (it never falls back to the
# forgeable From), so every message fails closed to owner-confirm and the feature
# is broken. Set GW_SKIP_WARM=1 to skip the `uv sync` warm on an offline box
# (patch still runs).
bash "$here/scripts/setup_upstream.sh"
