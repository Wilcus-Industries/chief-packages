#!/usr/bin/env bash
# Deterministic install for playwright: npm-install the PINNED @playwright/mcp
# into data/playwright/server, create the profile/output/workspace dirs, land
# the skill + its scripts (incl. the browser-login server), optionally mount
# the login viewer on the tailnet, wire config.yaml (both servers, gate lists,
# screening) through playwright_tools.py, and register the package.
#
# Parameters (env):
#   TAILSCALE_SERVE=1  run `tailscale serve --bg --set-path /browser` so login
#                      links work from the owner's phone over the tailnet.
#                      Changes the machine's tailscale config — owner-approved
#                      at the install interview. Without it links are
#                      localhost-only.
#
# Run it from your repo root, then `restart`. It copies from its own dir, so it
# works wherever the package is cloned; destination paths are repo-root-relative.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tools="$here/scripts/playwright_tools.py"

node_path="$(command -v node)" || { echo "install: node not found on PATH" >&2; exit 1; }
command -v npm >/dev/null || { echo "install: npm not found on PATH" >&2; exit 1; }
if [ "$(uname)" = "Darwin" ] && [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "install: Google Chrome not found in /Applications" >&2
  exit 1
fi
uv run python -c "import websockets" 2>/dev/null || {
  echo "install: websockets not importable — add it to pyproject.toml and" \
    "uv sync first (INSTALL.md step 2)" >&2
  exit 1
}

pin="$(python3 "$tools" pin)"
port="$(python3 "$tools" viewer-port)"
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
rm -f "$dst/scripts/login.sh" "$dst/scripts/login.mjs"  # pre-viewer leftovers
for f in playwright_tools.py browser_host.py cdp_client.py cdp_input.py \
  chrome_launch.py login_bridge.py login_sessions.py login_viewer.py \
  login_viewer.html; do
  cp "$here/scripts/$f" "$dst/scripts/$f"
done

public="http://127.0.0.1:${port}/browser"
if [ "${TAILSCALE_SERVE:-0}" = "1" ]; then
  command -v tailscale >/dev/null || { echo "install: tailscale not found" >&2; exit 1; }
  tailscale serve --bg --set-path /browser "http://127.0.0.1:${port}" >/dev/null
  dns="$(tailscale status --json | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  public="https://${dns}/browser"
fi

uv run python "$tools" wire . "$(dirname "$node_path")" "$public"

uv run python -m chief.registry_apply playwright --source cloned
echo "login links: ${public}/s/<token>"
