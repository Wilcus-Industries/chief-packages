#!/usr/bin/env bash
# Apply the local patch(es) to a checked-out upstream Google Workspace MCP tree,
# FAILING LOUD if any patch does not apply.
#
# Why hard-fail and not warn: the patch adds the `Authentication-Results` header
# to the server's Gmail read output. Without it the sender-trust catalog (#4)
# has nothing to verify against, so it can never verify ANY sender — the catalog
# never falls back to the forgeable `From`; a missing AR header just fails closed
# and routes every message to the owner-confirm flow, so the feature is broken.
# So a patch that does not apply (e.g. the pin was bumped without regenerating
# the patch) is a HARD ERROR that aborts the install, never a warning.
#
# Usage:  apply_upstream_patch.sh <upstream-clone-dir>
# The patches live next to this script at ../patches/*.patch and are applied in
# sorted order. Zero patch files is the "offered upstream; patch deleted once
# merged" end state — but a lost/renamed patch file looks identical, so the
# zero-patch branch VERIFIES the mechanism is already present in the tree and
# HARD-FAILS otherwise (only a genuine upstream merge reaches exit 0 with no
# patch file).
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
  # No patch files: EITHER upstream merged the mechanism and the patch was
  # deleted (the intended end state), OR a patch file was lost/renamed and we
  # are one silent step from installing an UNPATCHED server. Those look
  # identical from the glob, so do NOT trust it — verify the mechanism the patch
  # installs (the Authentication-Results header in GMAIL_METADATA_HEADERS) is
  # actually present in the cloned tree, and HARD-FAIL if it is not. "Genuinely
  # merged upstream" is then the only way to reach exit 0 with no patch file.
  helpers="$clone/gmail/gmail_helpers.py"
  if grep -q '"Authentication-Results"' "$helpers" 2>/dev/null; then
    echo "apply_upstream_patch: no patch files in $patches_dir, but the" \
      "Authentication-Results mechanism is already present in $helpers —" \
      "treating as merged upstream. Nothing to apply."
    exit 0
  fi
  echo "apply_upstream_patch: FATAL — no patch files in $patches_dir AND the" >&2
  echo "  Authentication-Results mechanism is ABSENT from $helpers." >&2
  echo "  A patch file was lost or renamed; installing this unpatched server" >&2
  echo "  would omit the header the sender-trust catalog verifies, so the" >&2
  echo "  catalog could never verify any sender and every message would fail" >&2
  echo "  closed to owner-confirm — the feature is broken. Restore the patch" >&2
  echo "  under patches/*.patch, or, if upstream genuinely merged it, bump the" >&2
  echo "  pin to that commit. Aborting the install." >&2
  exit 1
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
    echo "  server omits the header the sender-trust catalog verifies, so the" >&2
    echo "  catalog can never verify any sender and every message fails closed" >&2
    echo "  to owner-confirm — the feature is broken. Aborting the install." >&2
    echo "  Likely cause: the upstream pin (patches/upstream.pin) was bumped" >&2
    echo "  without regenerating the patch against the new tree." >&2
    exit 1
  fi
  git -C "$clone" apply "$patch"
  echo "apply_upstream_patch: applied $name."
done
