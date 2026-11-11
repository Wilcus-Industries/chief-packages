# Uninstalling playwright

1. From `config.yaml`, remove `mcp_servers.playwright` and
   `mcp_servers.browser-login`, every `mcp_playwright_*` entry and
   `mcp_browser-login_login_link` in `gate.approved` and `gate.never`, and
   `mcp_playwright_*` from `typesafe.screen_tools` (list them with
   `uv run python skills/playwright/scripts/playwright_tools.py reads` /
   `never`). Keep every other entry.
2. `restart`, then stop the shared Chrome (it outlives the server on purpose):
   `pkill -f -- '--remote-debugging-port=9322'`.
3. Remove the tailnet mount if install added it:
   `tailscale serve --set-path /browser off`.
4. Ask the owner whether to keep the logged-in profile. If not, delete
   `data/playwright/` (server, profile, output, workspace); if so, delete only
   `data/playwright/server`.
5. Delete the installed skill dir `skills/playwright/`.
6. Deregister: `uv run python -m chief.registry_apply playwright --remove`.
7. Delete this `UNINSTALL.md` (`data/packages/playwright/UNINSTALL.md`) — its
   absence signals the uninstall completed.
8. `restart` to bring the change live.
