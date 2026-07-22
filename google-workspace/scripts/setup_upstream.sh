#!/usr/bin/env bash
# Clone the PINNED upstream Google Workspace MCP server, apply the local
# auth-result-header patch (fail-loud), and warm its dependencies. Run from the
# chief repo root; the checkout lands at data/google-workspace/server, which the
# manifest launch command points at.
#
# Pin, not branch: this server holds a Gmail send credential, so it is cloned at
# the exact commit recorded in patches/upstream.pin and the checked-out HEAD is
# verified to match. A moving branch is never used.
#
# Warming: `uv sync` resolves + downloads the server's deps into the shared uv
# cache now, so the daemon's first stdio connect does not race a cold dependency
# resolution. Requires network + uv; both are install-time prerequisites.
#
# Env:
#   GW_SERVER_DIR   checkout destination (default data/google-workspace/server)
#   GW_SKIP_WARM    set non-empty to skip `uv sync` (offline/CI); the clone +
#                   patch still run so the patched tree is present.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pin_file="$here/../patches/upstream.pin"

if [ ! -f "$pin_file" ]; then
  echo "setup_upstream: missing pin file $pin_file — cannot clone a pinned" \
    "upstream. Aborting." >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$pin_file"

: "${UPSTREAM_REPO:?setup_upstream: UPSTREAM_REPO unset in $pin_file}"
: "${UPSTREAM_TAG:?setup_upstream: UPSTREAM_TAG unset in $pin_file}"
: "${UPSTREAM_COMMIT:?setup_upstream: UPSTREAM_COMMIT unset in $pin_file}"

dst="${GW_SERVER_DIR:-data/google-workspace/server}"

# Fresh clone every time so the patch applies to a pristine tree (never a
# double-apply). Clone the tag shallow, then hard-verify HEAD is the pinned SHA.
rm -rf "$dst"
mkdir -p "$(dirname "$dst")"
echo "setup_upstream: cloning $UPSTREAM_REPO @ $UPSTREAM_TAG into $dst ..."
git clone --quiet --depth 1 --branch "$UPSTREAM_TAG" "$UPSTREAM_REPO" "$dst"

head="$(git -C "$dst" rev-parse HEAD)"
if [ "$head" != "$UPSTREAM_COMMIT" ]; then
  echo "setup_upstream: FATAL — $UPSTREAM_TAG resolved to $head, not the pinned" \
    "$UPSTREAM_COMMIT. The tag moved or the mirror differs; refusing to run an" \
    "unpinned server that holds a send credential." >&2
  exit 1
fi
echo "setup_upstream: HEAD verified at pinned $UPSTREAM_COMMIT."

# Apply the auth-result-header patch — HARD ERROR if it does not apply.
bash "$here/apply_upstream_patch.sh" "$dst"

# Warm dependencies into the shared uv cache so the first daemon connect is fast.
if [ -n "${GW_SKIP_WARM:-}" ]; then
  echo "setup_upstream: GW_SKIP_WARM set — skipping dependency warm."
else
  echo "setup_upstream: warming server dependencies (uv sync) ..."
  uv sync --directory "$dst"
  echo "setup_upstream: dependencies warmed."
fi

echo "setup_upstream: done — patched, pinned server ready at $dst."
