# Uninstalling playwright

1. From `config.yaml`, remove `mcp_servers.playwright`, every
   `mcp_playwright_*` entry in `gate.approved` and `gate.never`, and
   `mcp_playwright_*` from `typesafe.screen_tools` (list them with
   `uv run python skills/playwright/scripts/playwright_tools.py reads` /
   `never`). Keep every other entry.
2. Ask the owner whether to keep the logged-in profile. If not, delete
   `data/playwright/` (server, profile, output, workspace); if so, delete only
   `data/playwright/server`.
3. Delete the installed skill dir `skills/playwright/`.
4. Deregister: `uv run python -m chief.registry_apply playwright --remove`.
5. Delete this `UNINSTALL.md` (`data/packages/playwright/UNINSTALL.md`) — its
   absence signals the uninstall completed.
6. `restart` to bring the change live.
