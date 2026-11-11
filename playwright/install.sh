#!/usr/bin/env bash
# Deterministic install for playwright: npm-install the PINNED @playwright/mcp
# into data/playwright/server, create the profile/output/workspace dirs, land
# the skill + its scripts (the browser host: shared Chrome, one Playwright
# child per chief session, phone login viewer), optionally mount the login
# viewer on the tailnet, wire config.yaml (the server, gate lists, screening)
# through playwright_tools.py, and register the package.
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
# The host tells sessions apart by the chief/thread _meta that core sends only
# to a `session_context: true` server. An older core silently drops the key and
# every session would share one tab (logged in as the owner) — refuse instead.
uv run python -c "import dataclasses; from chief.mcpclient.config import ServerConfig; \
assert 'session_context' in {f.name for f in dataclasses.fields(ServerConfig)}" \
  2>/dev/null || {
  echo "install: this chief core has no MCP session_context support — update" \
    "chief (chief update) first; without it every session shares one tab" >&2
  exit 1
}
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
for f in playwright_tools.py browser_host.py browser_pool.py playwright_child.py \
  login_host.py cdp_client.py cdp_input.py chrome_launch.py login_bridge.py \
  login_sessions.py login_viewer.py login_viewer.html url_policy.py; do
  cp "$here/scripts/$f" "$dst/scripts/$f"
done

public="http://127.0.0.1:${port}/browser"
if [ "${TAILSCALE_SERVE:-0}" = "1" ]; then
  command -v tailscale >/dev/null || { echo "install: tailscale not found" >&2; exit 1; }
  # Login links carry no password: tailnet-only reach is half their auth. With
  # Funnel on, `serve` paths are on the public internet — refuse.
  tailscale serve status --json | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
def funnel(node):
    if isinstance(node, dict):
        return any(
            (k == "AllowFunnel" and isinstance(v, dict) and any(v.values()))
            or funnel(v)
            for k, v in node.items()
        )
    if isinstance(node, list):
        return any(funnel(v) for v in node)
    return False
sys.exit(1 if raw and funnel(json.loads(raw)) else 0)
' || {
    echo "install: Tailscale Funnel is enabled on this machine — /browser would" \
      "be public. Turn Funnel off (tailscale funnel reset) or install without" \
      "TAILSCALE_SERVE=1" >&2
    exit 1
  }
  tailscale serve --bg --set-path /browser "http://127.0.0.1:${port}" >/dev/null
  dns="$(tailscale status --json | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  public="https://${dns}/browser"
fi

uv run python "$tools" wire . "$(dirname "$node_path")" "$public"

uv run python -m chief.registry_apply playwright --source cloned
echo "login links: ${public}/s/<token>"
