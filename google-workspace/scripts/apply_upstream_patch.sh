#!/usr/bin/env bash
# Apply the local patch(es) to a checked-out upstream Google Workspace MCP tree,
# FAILING LOUD if any patch does not apply.
#
# Why hard-fail and not warn: the patch adds the `Authentication-Results` header
# to the server's Gmail read output. Without it the sender-trust catalog (#4)
# has nothing to verify against and would silently degrade to matching the
# forgeable `From` — a degradation worse than having no catalog at all. So a
# patch that does not apply (e.g. the pin was bumped without regenerating the
# patch) is a HARD ERROR that aborts the install, never a warning.
#
# Usage:  apply_upstream_patch.sh <upstream-clone-dir>
# The patches live next to this script at ../patches/*.patch and are applied in
# sorted order. Zero patch files is allowed — the "offered upstream; deletes
# itself once merged" end state — and simply applies nothing.
#
# Idempotent: a patch already present in the tree (reverse-applies cleanly) is
# skipped, so re-running setup over an already-patched tree is not an error.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
patches_dir="$here/../patches"

clone="${1:-}"
if [ -z "$clone" ]; then
  echo "apply_upstream_patch: usage: $0 <upstream-clone-dir>" >&2
  exit 2
fi
if [ ! -d "$clone/.git" ]; then
  echo "apply_upstream_patch: '$clone' is not a git checkout — cannot apply the" \
    "auth-result-header patch. Clone the pinned upstream first" \
    "(scripts/setup_upstream.sh)." >&2
  exit 1
fi

shopt -s nullglob
patches=("$patches_dir"/*.patch)
shopt -u nullglob

if [ "${#patches[@]}" -eq 0 ]; then
  echo "apply_upstream_patch: no patches in $patches_dir — nothing to apply" \
    "(patch presumably merged upstream and removed)."
  exit 0
fi

for patch in "${patches[@]}"; do
  name="$(basename "$patch")"
  if git -C "$clone" apply --reverse --check "$patch" >/dev/null 2>&1; then
    echo "apply_upstream_patch: $name already applied — skipping."
    continue
  fi
  if ! git -C "$clone" apply --check "$patch" >/dev/null 2>&1; then
    echo "apply_upstream_patch: FATAL — $name does NOT apply to the upstream" \
      "checkout at '$clone'." >&2
    echo "  The Authentication-Results header patch is REQUIRED: without it the" >&2
    echo "  server omits the header the sender-trust catalog verifies, silently" >&2
    echo "  degrading to unverified From matching. Aborting the install." >&2
    echo "  Likely cause: the upstream pin (patches/upstream.pin) was bumped" >&2
    echo "  without regenerating the patch against the new tree." >&2
    exit 1
  fi
  git -C "$clone" apply "$patch"
  echo "apply_upstream_patch: applied $name."
done
