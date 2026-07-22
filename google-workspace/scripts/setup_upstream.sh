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
# Warming: `uv sync --locked` installs the server's PINNED deps into the shared
# uv cache now, so the daemon's first stdio connect does not race a cold
# dependency resolution. `--locked` refuses to re-resolve — an inconsistent
# tagged lockfile aborts loud instead of silently rewriting uv.lock with unvetted
# dep versions that the `--frozen` launch would then trust. Requires network +
# uv; both are install-time prerequisites.
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

# Guard the destructive `rm -rf "$dst"` below: $dst derives from the unvalidated
# GW_SERVER_DIR env var, so refuse anything that could wipe the wrong tree — an
# empty value, the filesystem root, an absolute path OUTSIDE the repo we run
# from, or a relative path that escapes the repo via `..`. The clone destination
# must live under the repo root (default data/google-workspace/server).
repo_root="$(pwd)"
case "$dst" in
  "")
    echo "setup_upstream: GW_SERVER_DIR is empty — refusing to rm -rf it." >&2
    exit 1 ;;
  "/")
    echo "setup_upstream: GW_SERVER_DIR is '/' — refusing to rm -rf it." >&2
    exit 1 ;;
  /*)
    case "$dst" in
      "$repo_root"/?*) : ;;  # absolute, strictly under the repo — OK
      *)
        echo "setup_upstream: GW_SERVER_DIR='$dst' is an absolute path outside" \
          "the repo root '$repo_root' — refusing to rm -rf it. Use a" \
          "repo-relative path (default data/google-workspace/server)." >&2
        exit 1 ;;
    esac ;;
  *..*)
    echo "setup_upstream: GW_SERVER_DIR='$dst' contains '..' and could escape" \
      "the repo — refusing to rm -rf it." >&2
    exit 1 ;;
esac

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
  echo "setup_upstream: warming server dependencies (uv sync --locked) ..."
  uv sync --locked --directory "$dst"
  echo "setup_upstream: dependencies warmed."
fi

echo "setup_upstream: done — patched, pinned server ready at $dst."
