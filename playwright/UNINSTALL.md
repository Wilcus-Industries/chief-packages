# Uninstalling playwright

1. From `config.yaml`, remove `mcp_servers.playwright` (and a leftover
   `mcp_servers.browser-login`), every `mcp_playwright_*` entry (and
   `mcp_browser-login_login_link`) in `gate.approved` and `gate.never`, and
   `mcp_playwright_*` from `typesafe.screen_tools` (list them with
   `uv run python skills/playwright/scripts/playwright_tools.py reads` /
   `never`). Keep every other entry.
2. Drop the owner's persisted "always allow" answers for these tools — core
   keeps them in `data/gate_approved.json` (a sorted JSON list of tool names),
   and a reinstall would otherwise come back with those cards pre-approved:
   `uv run python -c "import json,pathlib; p=pathlib.Path('data/gate_approved.json'); p.exists() and p.write_text(json.dumps(sorted(n for n in json.loads(p.read_text()) if not n.startswith(('mcp_playwright_','mcp_browser-login_')))))"`.
3. `restart`, then stop the shared Chrome (it outlives the server on purpose):
   `pkill -f -- '--remote-debugging-port=9322'`.
4. Remove the tailnet mount if install added it:
   `tailscale serve --set-path /browser off`.
5. Ask the owner whether to keep the logged-in profile. If not, delete
   `data/playwright/` (server, profile, output, workspace); if so, delete only
   `data/playwright/server`.
6. Delete the installed skill dir `skills/playwright/`.
7. Deregister: `uv run python -m chief.registry_apply playwright --remove`.
8. Delete this `UNINSTALL.md` (`data/packages/playwright/UNINSTALL.md`) — its
   absence signals the uninstall completed.
9. `restart` to bring the change live.
