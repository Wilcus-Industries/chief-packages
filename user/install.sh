#!/usr/bin/env bash
# Deterministic install for the user package: copy the skill verbatim and seed a
# starter User.md. No prompt wiring here or in INSTALL.md — the package's
# hooks.py registers a pre_turn hook that adds the file to the prompt on its
# own, so seeding the file is the whole install.
#
# Depends on the memory package: User.md lives in that store (data/memory/) and
# is versioned by the same git repo. Run it from your repo root, then `restart`.
# It copies from its own dir, so it works wherever the package is cloned; the
# destination skills/ + data/memory paths are repo-root-relative.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/user"
dst="skills/user"
mkdir -p "$dst"
cp "$src/SKILL.md" "$dst/SKILL.md"

# Seed a starter file once. Never overwrite an existing User.md — that is the
# owner's own data. The placeholders are the prompt for the first conversation.
store="data/memory"
mkdir -p "$store"
user="$store/User.md"
if [ ! -f "$user" ]; then
  cat >"$user" <<'EOF'
# User

Replace each placeholder as you learn it; delete any line that does not apply.
Keep this short — only facts that are true on every turn.

- Name:
- Goes by:
- Pronouns:
- Age / birthday:
- Location and timezone:
- Work:
- Household (people, pets):
EOF
fi
