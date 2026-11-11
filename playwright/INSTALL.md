# Installing playwright

Gives chief a real browser: the [Playwright MCP server](https://github.com/microsoft/playwright-mcp)
(`@playwright/mcp`, pinned in `scripts/playwright_tools.py`) attached to one
Google Chrome window (headed, so Google sign-in accepts it; it sits on the
Mac's screen) on a **persistent chief-only profile**. A second small
server, **browser-login** (`scripts/browser_host.py`, chief's own venv
python), owns that Chrome and serves a phone **login viewer**: chief texts the
owner a one-time link, the owner logs in by hand on their phone, and the
cookies stay in chief's profile. Depends on `screening`.

## Read before installing — tell the owner, get a yes

- The profile at `data/playwright/profile` holds live session cookies for
  every site the owner logs it into. It is a credential: gitignored `data/`,
  mode 700. Log it only into accounts chief should act in.
- Chrome's CDP port (127.0.0.1:9322) gives full control of that logged-in
  browser to any process running as this user on the machine.
- Login links are single-use, 15-minute, tailnet-only. They do not ask for the
  web password (owner-approved posture). Mounting them on the tailnet changes
  the machine's `tailscale serve` config (adds `/browser`).
- Browsing is free; acting (click, type, submit, upload, page JS) raises an
  approval card per call until the owner answers "always" for that tool.
  `browser_run_code_unsafe` goes on `gate.never`.
- Navigation is not side-effect free — a GET can carry data out in a query
  string. Screening on everything the browser returns is the guard.

## Prerequisites

`node` + `npm` on PATH, Google Chrome in `/Applications` (macOS), and — for
phone links — Tailscale with `tailscale serve` already fronting chief's web UI.

## Steps

1. Install `screening` first if it isn't already.
2. **Python dependency (guarded self-edit).** If `uv run python -c "import
   websockets"` fails, add `websockets` to `[project.dependencies]` in
   `pyproject.toml` and run `uv sync`.
3. Run `TAILSCALE_SERVE=1 bash data/packages/playwright/install.sh` from the
   repo root with the `shell` tool (drop `TAILSCALE_SERVE=1` if the owner
   declined the tailnet mount — links are then localhost-only). It:
   - npm-installs the pinned server into `data/playwright/server`;
   - creates `data/playwright/{profile,output,workspace}`;
   - copies the skill and its scripts to `skills/playwright/`;
   - runs `tailscale serve --bg --set-path /browser http://127.0.0.1:8131`;
   - wires `config.yaml` via `playwright_tools.py wire`: the `browser-login`
     server (env: profile, ports, public link base), the `playwright` server
     (`--cdp-endpoint`, `cwd` confined to `data/playwright/workspace`, node's
     `PATH`), the read tools + `mcp_browser-login_login_link` onto
     `gate.approved`, `browser_run_code_unsafe` onto `gate.never`, and
     `mcp_playwright_*` onto `typesafe.screen_tools` **if** that list is set.
     Lists are unioned, never replaced;
   - records the install (`chief.registry_apply`).
4. `restart`.
5. Verify: `uv run python -m chief.pkg.cli verify playwright` is clean;
   `browser_navigate` to `https://example.com` and `browser_snapshot` shows
   "Example Domain"; `mcp_browser-login_login_link` with
   `https://example.com` returns a link — text it to the owner and ask them to
   open it and tap Done.

## Bumping the pin

Change `PIN` in `scripts/playwright_tools.py`, list the new release's tools,
update `PINNED_TOOLS` and classify any new tool (the package tests fail until
you do), re-run `install.sh`, `restart`.
