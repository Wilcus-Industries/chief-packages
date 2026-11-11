# Installing playwright

Gives chief a real browser. One server, `playwright` (`scripts/browser_host.py`,
chief's own venv python), keeps one Google Chrome window (headed, so Google
sign-in accepts it; it sits on the Mac's screen) on a **persistent chief-only
profile** — one set of logins shared by every chief session — and gives each
**session its own tab**: one pinned [Playwright MCP](https://github.com/microsoft/playwright-mcp)
child (`@playwright/mcp`, pinned in `scripts/playwright_tools.py`) per session,
idle tabs closed after 10 minutes, at most 4 at once, tab switching refused.
It needs core's `session_context` MCP option (chief ≥ the release carrying
it) to tell sessions apart; `install.sh` refuses an older core. It also serves a phone **login viewer**: chief
texts the owner a one-time link, the owner logs in by hand on their phone,
and the cookies stay in chief's profile. Depends on `screening`.

## Read before installing — tell the owner, get a yes

- The profile at `data/playwright/profile` holds live session cookies for
  every site the owner logs it into. It is a credential: gitignored `data/`,
  mode 700. Log it only into accounts chief should act in.
- Chrome's CDP port (127.0.0.1:9322) gives full control of that logged-in
  browser to any process running as this user on the machine.
- Login links are single-use, 15-minute, tailnet-only. They do not ask for the
  web password (owner-approved posture). Mounting them on the tailnet changes
  the machine's `tailscale serve` config (adds `/browser`).
- Browsing is free; acting (click, type, submit, upload, page JS) and reading
  one network request in full (`browser_network_request` — its headers carry
  credentials) raise an approval card per call until the owner answers
  "always" for that tool. `browser_run_code_unsafe` goes on `gate.never`, and
  the host neither lists nor runs it.
- Navigation goes only to http(s) sites that are not this machine: the host
  refuses `javascript:`, `data:`, `chrome://`, `file:` and loopback URLs
  (which would run page script without a card, or reach the CDP port).
- Navigation is not side-effect free — a GET can carry data out in a query
  string. Screening on everything the browser returns is the guard.

## Prerequisites

`node` + `npm` on PATH, Google Chrome in `/Applications` (macOS), and — for
phone links — Tailscale with `tailscale serve` already fronting chief's web UI
and **Funnel off** (install refuses to mount `/browser` while Funnel is on:
it would put the login viewer on the public internet).

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
   - refuses if this chief core lacks `session_context`;
   - refuses if Tailscale Funnel is on, else runs
     `tailscale serve --bg --set-path /browser http://127.0.0.1:8131`;
   - wires `config.yaml` via `playwright_tools.py wire`: the `playwright`
     server (`session_context: true`; env: the pinned binary, profile, output
     and workspace dirs — children's file access is confined to the
     workspace — ports, public link base, node's `PATH`), removing the older
     two-server layout's `browser-login` entry if present; the read tools +
     `mcp_playwright_login_link` onto `gate.approved`, `browser_run_code_unsafe` onto `gate.never`, and
     `mcp_playwright_*` onto `typesafe.screen_tools` **if** that list is set.
     Lists are unioned, never replaced;
   - records the install (`chief.registry_apply`).
4. `restart`.
5. Verify: `uv run chief-pkg verify playwright` is clean;
   `browser_navigate` to `https://example.com` and `browser_snapshot` shows
   "Example Domain"; `mcp_playwright_login_link` with
   `https://example.com` returns a link — text it to the owner and ask them to
   open it and tap Done.

## Bumping the pin

Change `PIN` in `scripts/playwright_tools.py`. The package tests then fail
until you re-record `PINNED_TOOLS` from the new release's `tools/list`, set
`PINNED_TOOLS_VERSION` to the new pin, and classify every tool the list adds
(read / write / never). Check any new URL-taking argument against
`URL_ARGS` in `browser_host.py`. Re-run `install.sh`, `restart`.
